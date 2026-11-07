"""The refresh report broken down by site, and taken away as CSV.

By model answers "what is going end of life"; by site answers "what does
each site have to replace", which is the list a site's refresh is planned
from. Each site's units are priced at that site's own rate, so the by-site
numbers are the same money the region and grand totals are made of, just
cut differently.
"""

import csv
import io
from datetime import date
from decimal import Decimal

from dcim.models import (
    Device, DeviceRole, DeviceType, Manufacturer, Region, Site,
)
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from netbox_refresh.models import ModelLifecycle, ReplacementPrice


class SiteBreakdownTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.emea = Region.objects.create(name='EMEA', slug='emea')
        cls.amer = Region.objects.create(name='AMER', slug='amer')
        cls.london = Site.objects.create(name='London', slug='london', region=cls.emea)
        cls.dallas = Site.objects.create(name='Dallas', slug='dallas', region=cls.amer)
        cls.remote = Site.objects.create(name='Remote', slug='remote')

        cls.mfr = Manufacturer.objects.create(name='Cisco', slug='cisco')
        cls.old = DeviceType.objects.create(manufacturer=cls.mfr, model='C3560-48',
                                            slug='c3560-48', part_number='WS-C3560-48')
        cls.other = DeviceType.objects.create(manufacturer=cls.mfr, model='C2960-24',
                                              slug='c2960-24')
        cls.new = DeviceType.objects.create(manufacturer=cls.mfr, model='C9350-48',
                                            slug='c9350-48')
        cls.role = DeviceRole.objects.create(name='Access', slug='access')
        # London: two C3560s and a C2960. Dallas: one C3560. Remote: one C2960.
        for name, dt, site in (('lon1', cls.old, cls.london), ('lon2', cls.old, cls.london),
                               ('lon3', cls.other, cls.london), ('dal1', cls.old, cls.dallas),
                               ('rem1', cls.other, cls.remote)):
            Device.objects.create(name=name, device_type=dt, role=cls.role, site=site)

        ModelLifecycle(assigned_object=cls.old, end_of_support=date(2027, 1, 1),
                       replacement_device_type=cls.new,
                       replacement_cost=Decimal('1000.00'), currency='USD').save()
        # Unpriced on purpose: the gap must show per site, not vanish.
        ModelLifecycle(assigned_object=cls.other, end_of_support=date(2026, 6, 1)).save()
        # London pays in pounds.
        ReplacementPrice.objects.create(device_type=cls.new, site=cls.london,
                                        cost=Decimal('900.00'), currency='GBP')

        cls.user = get_user_model().objects.create_superuser('report', password='x')
        cls.url = reverse('plugins:netbox_refresh:refresh_report')

    def setUp(self):
        self.client.force_login(self.user)

    def site_rows(self, **params):
        response = self.client.get(self.url, dict(params, breakdown='site'))
        self.assertEqual(response.status_code, 200)
        return response, list(response.context['site_table'].data)

    def test_every_site_lists_what_it_has_to_replace(self):
        _response, rows = self.site_rows()
        by_site = {}
        for row in rows:
            by_site.setdefault(row['site'], []).append((row['model'], row['installed']))
        self.assertEqual(by_site, {
            'London': [('C2960-24', 1), ('C3560-48', 2)],   # soonest milestone first
            'Dallas': [('C3560-48', 1)],
            'Remote': [('C2960-24', 1)],
        })
        # Site by site, the siteless bucket last, would come after Remote if any.
        self.assertEqual([r['site'] for r in rows][:3], ['Dallas', 'London', 'London'])

    def test_each_site_is_priced_at_its_own_rate(self):
        _response, rows = self.site_rows()
        london = next(r for r in rows if r['site'] == 'London' and r['model'] == 'C3560-48')
        dallas = next(r for r in rows if r['site'] == 'Dallas')
        self.assertEqual((london['unit_cost'], london['extended_cost']),
                         ('900.00 GBP', '1,800.00 GBP'))
        self.assertEqual((dallas['unit_cost'], dallas['extended_cost']),
                         ('1,000.00 USD', '1,000.00 USD'))
        unpriced = next(r for r in rows if r['site'] == 'Remote')
        self.assertEqual((unpriced['unit_cost'], unpriced['extended_cost']), ('—', '—'))

    def test_the_site_totals_add_up_to_the_report_totals(self):
        response, _rows = self.site_rows()
        cost = {r['site']: r for r in response.context['site_cost_table'].data}
        self.assertEqual(cost['London']['total'], '1,800.00 GBP')
        self.assertEqual(cost['London']['units'], 3)
        self.assertEqual(cost['London']['models'], 2)
        self.assertEqual(cost['London']['unpriced'], 1)
        self.assertEqual(cost['Dallas']['total'], '1,000.00 USD')
        self.assertEqual(cost['Remote']['unpriced'], 1)
        self.assertEqual(response.context['totals'],
                         {'GBP': Decimal('1800.00'), 'USD': Decimal('1000.00')})
        self.assertEqual(response.context['site_count'], 3)
        self.assertContains(response, 'Refresh needed by site')
        self.assertContains(response, 'Cost by site')

    def test_the_scope_filters_apply_to_the_site_view_too(self):
        _response, rows = self.site_rows(region=self.emea.pk)
        self.assertEqual({r['site'] for r in rows}, {'London'})

    def test_by_model_is_still_the_default(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['breakdown'], 'model')
        self.assertNotContains(response, 'Refresh needed by site')
        self.assertEqual(len(response.context['table'].data), 2)

    # --- CSV

    def csv_rows(self, **params):
        response = self.client.get(self.url, dict(params, export='csv'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response['Content-Type'].startswith('text/csv'))
        self.assertIn('attachment; filename="refresh-report-by-', response['Content-Disposition'])
        return response, list(csv.reader(io.StringIO(response.content.decode('utf-8'))))

    def test_the_by_site_export_is_the_by_site_table_with_numbers_a_spreadsheet_can_sum(self):
        response, rows = self.csv_rows(breakdown='site')
        self.assertIn('by-site-', response['Content-Disposition'])
        self.assertEqual(rows[0], [
            'Milestone', 'Site', 'Region', 'Hardware model', 'Manufacturer', 'Part number',
            'Milestone date', 'Units', 'Replacement', 'Unit cost', 'Currency', 'Extended cost',
        ])
        london = next(r for r in rows[1:] if r[1] == 'London' and r[3] == 'C3560-48')
        self.assertEqual(london, [
            'End of life (soonest of support / security)', 'London', 'EMEA', 'C3560-48',
            'Cisco', 'WS-C3560-48', '2027-01-01', '2', 'C9350-48', '900.00', 'GBP', '1800.00',
        ])
        remote = next(r for r in rows[1:] if r[1] == 'Remote')
        # Unpriced and regionless: empty cells, never a dash or a zero.
        self.assertEqual(remote[2], '')
        self.assertEqual(remote[9:], ['', '', ''])
        self.assertEqual(len(rows) - 1, 4)

    def test_the_by_model_export_is_the_by_model_table(self):
        response, rows = self.csv_rows()
        self.assertIn('by-model-', response['Content-Disposition'])
        self.assertEqual(rows[0][1:], [
            'Hardware model', 'Manufacturer', 'Part number', 'Milestone date', 'Installed',
            'Replacement', 'Unit cost', 'Extended cost',
        ])
        c3560 = next(r for r in rows[1:] if r[1] == 'C3560-48')
        self.assertEqual(c3560[5], '3')
        # Two currencies on one model stay side by side, as on the page.
        self.assertEqual(c3560[8], '1,800.00 GBP + 1,000.00 USD')

    def test_the_export_honours_the_filters(self):
        _response, rows = self.csv_rows(breakdown='site', region=self.amer.pk)
        self.assertEqual([r[1] for r in rows[1:]], ['Dallas'])

    def test_the_page_offers_the_export(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'name="export" value="csv"')
