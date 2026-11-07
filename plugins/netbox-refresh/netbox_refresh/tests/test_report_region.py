"""Scoping the refresh report to a region.

Regions nest, so a report scoped to EMEA has to mean every site beneath it.
Matching only sites parented directly to the chosen region would report on a
fraction of the estate and read as though the rest owns no hardware — the kind
of wrong that looks like an answer.
"""

from datetime import date

from dcim.models import (
    Device, DeviceRole, DeviceType, Manufacturer, Region, Site,
)
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from netbox_refresh.models import ModelLifecycle
from netbox_refresh.views import _sites_in_scope


class RegionScopeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        # emea -> emea-north -> (london); emea -> (paris);  amer -> (dallas)
        cls.emea = Region.objects.create(name='EMEA', slug='emea')
        cls.north = Region.objects.create(name='EMEA North', slug='emea-north',
                                          parent=cls.emea)
        cls.amer = Region.objects.create(name='AMER', slug='amer')
        cls.london = Site.objects.create(name='London', slug='london', region=cls.north)
        cls.paris = Site.objects.create(name='Paris', slug='paris', region=cls.emea)
        cls.dallas = Site.objects.create(name='Dallas', slug='dallas', region=cls.amer)

        cls.mfr = Manufacturer.objects.create(name='Cisco', slug='cisco')
        cls.dt = DeviceType.objects.create(manufacturer=cls.mfr, model='C9300-24P',
                                           slug='c9300-24p')
        cls.role = DeviceRole.objects.create(name='Access', slug='access')
        for i, site in enumerate((cls.london, cls.paris, cls.dallas)):
            Device.objects.create(name=f'sw{i}', device_type=cls.dt,
                                  role=cls.role, site=site)
        ModelLifecycle(assigned_object=cls.dt, end_of_support=date(2027, 1, 1),
                       replacement_cost=1000).save()

        User = get_user_model()
        cls.user = User.objects.create_user('report', password='x')
        cls.user.is_superuser = True
        cls.user.save()

    def setUp(self):
        self.client.force_login(self.user)

    def names(self, scope):
        return sorted(s.name for s in scope) if scope is not None else None

    def test_a_region_includes_the_sites_nested_below_it(self):
        """London sits under EMEA North, which sits under EMEA."""
        self.assertEqual(self.names(_sites_in_scope([self.emea], None)),
                         ['London', 'Paris'])

    def test_a_leaf_region_is_just_its_own_sites(self):
        self.assertEqual(self.names(_sites_in_scope([self.north], None)), ['London'])

    def test_another_region_is_excluded(self):
        self.assertNotIn('Dallas', self.names(_sites_in_scope([self.emea], None)))

    def test_sites_alone_still_work(self):
        self.assertEqual(self.names(_sites_in_scope(None, [self.dallas])), ['Dallas'])

    def test_both_filters_narrow_rather_than_widen(self):
        """Two filters shown together read as narrowing; a site outside the
        chosen region is a contradiction, not an addition."""
        self.assertEqual(
            self.names(_sites_in_scope([self.emea], [self.london, self.dallas])),
            ['London'])

    def test_no_filters_means_no_scope_not_no_sites(self):
        """An empty list would count nothing at all."""
        self.assertIsNone(_sites_in_scope(None, None))

    def test_a_region_holding_nothing_counts_nothing(self):
        empty = Region.objects.create(name='APAC', slug='apac')
        self.assertEqual(self.names(_sites_in_scope([empty], None)), [])

    def test_the_report_counts_only_that_region(self):
        url = reverse('plugins:netbox_refresh:refresh_report')
        everywhere = self.client.get(url)
        emea = self.client.get(url, {'region': self.emea.pk})
        self.assertEqual(everywhere.status_code, 200)
        self.assertEqual(emea.status_code, 200)

        # total_units is what the report puts at the top: how many of the
        # matching models are actually installed in scope.
        self.assertEqual(everywhere.context['total_units'], 3)
        self.assertEqual(emea.context['total_units'], 2,
                         'Dallas was counted in an EMEA report')

    def test_the_filter_is_offered_on_the_form(self):
        response = self.client.get(reverse('plugins:netbox_refresh:refresh_report'))
        self.assertIn('region', response.context['form'].fields)
