"""Per-place purchase prices and the report's regional cost breakdown.

The same box does not cost the same in every country, and the price that
varies is the price of the model being BOUGHT — prices key to the
replacement, not to the hardware on its way out, so one entry covers every
model that funnels into that purchase. What matters here is precedence — the
replacement's site price beats its nearest region's, which beats a farther
ancestor's, which beats the outgoing lifecycle's baseline — and that the
report sums money at each unit's own resolved price, per currency, never
converting.
"""

from datetime import date
from decimal import Decimal

from dcim.models import (
    Device, DeviceRole, DeviceType, Manufacturer, Region, Site,
)
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from netbox_refresh.models import ModelLifecycle, ReplacementPrice
from netbox_refresh.pricing import PriceResolver


class RegionalPricingTestData(TestCase):
    @classmethod
    def setUpTestData(cls):
        # emea -> uk -> (london); emea -> (paris); amer -> (dallas);
        # (remote) has no region at all.
        cls.emea = Region.objects.create(name='EMEA', slug='emea')
        cls.uk = Region.objects.create(name='UK', slug='uk', parent=cls.emea)
        cls.amer = Region.objects.create(name='AMER', slug='amer')
        cls.london = Site.objects.create(name='London', slug='london', region=cls.uk)
        cls.paris = Site.objects.create(name='Paris', slug='paris', region=cls.emea)
        cls.dallas = Site.objects.create(name='Dallas', slug='dallas', region=cls.amer)
        cls.remote = Site.objects.create(name='Remote', slug='remote')

        cls.mfr = Manufacturer.objects.create(name='Cisco', slug='cisco')
        # The outgoing model and the one being bought to replace it.
        cls.old = DeviceType.objects.create(manufacturer=cls.mfr, model='C3560-48',
                                            slug='c3560-48')
        cls.new = DeviceType.objects.create(manufacturer=cls.mfr, model='C9350-48',
                                            slug='c9350-48')
        cls.role = DeviceRole.objects.create(name='Access', slug='access')
        for name, site in (('lon1', cls.london), ('par1', cls.paris),
                           ('dal1', cls.dallas), ('rem1', cls.remote)):
            Device.objects.create(name=name, device_type=cls.old,
                                  role=cls.role, site=site)

        cls.lifecycle = ModelLifecycle(
            assigned_object=cls.old, end_of_support=date(2027, 1, 1),
            replacement_device_type=cls.new,
            replacement_cost=Decimal('1000.00'), currency='USD',
        )
        cls.lifecycle.save()

    @classmethod
    def price(cls, **kwargs):
        kwargs.setdefault('device_type', cls.new)
        return ReplacementPrice.objects.create(**kwargs)


class PriceResolverTest(RegionalPricingTestData):
    def resolve(self, site):
        return PriceResolver([self.lifecycle.pk]).resolve(self.lifecycle, site)

    def test_no_prices_means_the_baseline(self):
        hit = self.resolve(self.dallas)
        self.assertEqual((hit.cost, hit.currency, hit.source),
                         (Decimal('1000.00'), 'USD', 'base'))

    def test_a_region_price_covers_sites_nested_below_it(self):
        """A price on EMEA covers London even though London's own region is UK."""
        self.price(region=self.emea, cost=Decimal('1200.00'), currency='EUR')
        hit = self.resolve(self.london)
        self.assertEqual((hit.cost, hit.currency, hit.source),
                         (Decimal('1200.00'), 'EUR', 'region'))

    def test_the_nearest_region_wins_over_an_ancestor(self):
        self.price(region=self.emea, cost=Decimal('1200.00'), currency='EUR')
        self.price(region=self.uk, cost=Decimal('1100.00'), currency='GBP')
        self.assertEqual(self.resolve(self.london).cost, Decimal('1100.00'))
        # Paris hangs off EMEA directly; the UK price must not leak onto it.
        self.assertEqual(self.resolve(self.paris).cost, Decimal('1200.00'))

    def test_a_site_price_beats_every_region(self):
        self.price(region=self.uk, cost=Decimal('1100.00'), currency='GBP')
        self.price(site=self.london, cost=Decimal('1500.00'), currency='GBP')
        hit = self.resolve(self.london)
        self.assertEqual((hit.cost, hit.source), (Decimal('1500.00'), 'site'))

    def test_a_region_price_does_not_reach_other_trees(self):
        self.price(region=self.emea, cost=Decimal('1200.00'), currency='EUR')
        self.assertEqual(self.resolve(self.dallas).source, 'base')

    def test_a_regionless_site_gets_the_baseline(self):
        self.price(region=self.emea, cost=Decimal('1200.00'), currency='EUR')
        self.assertEqual(self.resolve(self.remote).source, 'base')

    def test_two_outgoing_models_share_one_replacements_prices(self):
        """The reason prices key to the model being bought: enter once."""
        other_old = DeviceType.objects.create(manufacturer=self.mfr,
                                              model='C3750-48', slug='c3750-48')
        other = ModelLifecycle(assigned_object=other_old,
                               end_of_support=date(2027, 1, 1),
                               replacement_device_type=self.new)
        other.save()
        self.price(region=self.emea, cost=Decimal('1200.00'), currency='EUR')
        resolver = PriceResolver([self.lifecycle.pk, other.pk])
        self.assertEqual(resolver.resolve(other, self.paris).cost, Decimal('1200.00'))
        self.assertEqual(resolver.resolve(self.lifecycle, self.paris).cost,
                         Decimal('1200.00'))

    def test_a_lifecycle_without_a_replacement_only_gets_its_baseline(self):
        """You cannot price "the new one" before naming the new one."""
        self.price(region=self.amer, cost=Decimal('900.00'))
        self.lifecycle.replacement_device_type = None
        self.lifecycle.save()
        hit = PriceResolver([self.lifecycle.pk]).resolve(self.lifecycle, self.dallas)
        self.assertEqual(hit.source, 'base')

    def test_no_price_anywhere_is_none_not_zero(self):
        """A missing price must be visible, not silently priced at nothing."""
        self.lifecycle.replacement_cost = None
        self.lifecycle.save()
        self.assertIsNone(self.resolve(self.dallas))

    def test_exactly_one_model_and_one_scope_are_enforced(self):
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            ReplacementPrice(device_type=self.new, cost=Decimal('1')).full_clean()
        with self.assertRaises(ValidationError):
            ReplacementPrice(region=self.emea, cost=Decimal('1')).full_clean()
        with self.assertRaises(ValidationError):
            ReplacementPrice(device_type=self.new, region=self.emea,
                             site=self.london, cost=Decimal('1')).full_clean()


class RegionBreakdownReportTest(RegionalPricingTestData):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.price(region=cls.emea, cost=Decimal('1200.00'), currency='EUR')
        cls.price(site=cls.london, cost=Decimal('1500.00'), currency='GBP')
        User = get_user_model()
        cls.user = User.objects.create_user('pricing', password='x')
        cls.user.is_superuser = True
        cls.user.save()

    def setUp(self):
        self.client.force_login(self.user)

    def report(self, **params):
        return self.client.get(reverse('plugins:netbox_refresh:refresh_report'), params)

    def test_totals_sum_each_unit_at_its_own_sites_price(self):
        """London 1500 GBP, Paris 1200 EUR, Dallas + regionless 1000 USD each."""
        totals = self.report().context['totals']
        self.assertEqual(totals, {
            'GBP': Decimal('1500.00'),
            'EUR': Decimal('1200.00'),
            'USD': Decimal('2000.00'),
        })

    def test_the_breakdown_has_one_row_per_region(self):
        rows = {r['region']: r for r in self.report().context['region_table'].data}
        self.assertEqual(rows['EMEA / UK']['total'], '1,500.00 GBP')
        self.assertEqual(rows['EMEA']['total'], '1,200.00 EUR')
        self.assertEqual(rows['AMER']['total'], '1,000.00 USD')
        self.assertEqual(rows['(no region)']['total'], '1,000.00 USD')

    def test_the_regionless_bucket_sorts_last(self):
        labels = [r['region'] for r in self.report().context['region_table'].data]
        self.assertEqual(labels[-1], '(no region)')

    def test_a_units_missing_price_is_reported_not_zeroed(self):
        self.lifecycle.replacement_cost = None
        self.lifecycle.save()
        context = self.report().context
        # Dallas and the regionless site now have no price at all.
        self.assertEqual(context['unpriced_units'], 2)
        self.assertEqual(context['missing_cost'], 1)
        self.assertNotIn('USD', context['totals'])

    def test_the_model_row_says_the_price_varies(self):
        rows = self.report().context['table'].data
        self.assertEqual(len(rows), 1)
        self.assertIn('(+2 regional)', rows[0]['unit_cost'])
        self.assertIn('GBP', rows[0]['extended_cost'])
        self.assertIn('EUR', rows[0]['extended_cost'])
        self.assertIn('USD', rows[0]['extended_cost'])

    def test_site_filter_narrows_the_breakdown_too(self):
        context = self.report(site=self.london.pk).context
        self.assertEqual(context['totals'], {'GBP': Decimal('1500.00')})
        labels = [r['region'] for r in context['region_table'].data]
        self.assertEqual(labels, ['EMEA / UK'])


class PriceWorksheetTest(RegionalPricingTestData):
    """The one-page entry flow: every site that will need this model, priced."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        User = get_user_model()
        cls.user = User.objects.create_user('worksheet', password='x')
        cls.user.is_superuser = True
        cls.user.save()

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse('plugins:netbox_refresh:replacementprice_worksheet')

    def test_lists_every_site_holding_hardware_this_model_replaces(self):
        response = self.client.get(self.url, {'device_type': self.new.pk})
        self.assertEqual(response.status_code, 200)
        sites = [row['site'].name for group in response.context['site_groups']
                 for row in group['sites']]
        self.assertEqual(sorted(sites), ['Dallas', 'London', 'Paris', 'Remote'])
        self.assertEqual(response.context['total_units'], 4)

    def test_region_rows_include_ancestors(self):
        """EMEA appears even though every site lives deeper, so it can be set once."""
        response = self.client.get(self.url, {'device_type': self.new.pk})
        labels = [r['label'] for r in response.context['region_rows']]
        self.assertIn('EMEA', labels)
        self.assertIn('EMEA / UK', labels)

    def test_saving_creates_region_and_site_prices(self):
        self.client.post(self.url, {
            'device_type': self.new.pk,
            'region_cost_%d' % self.emea.pk: '1200',
            'region_currency_%d' % self.emea.pk: 'eur',
            'site_cost_%d' % self.london.pk: '1,500.00',
            'site_currency_%d' % self.london.pk: 'GBP',
        })
        emea = ReplacementPrice.objects.get(device_type=self.new, region=self.emea)
        self.assertEqual((emea.cost, emea.currency), (Decimal('1200'), 'EUR'))
        london = ReplacementPrice.objects.get(device_type=self.new, site=self.london)
        self.assertEqual((london.cost, london.currency), (Decimal('1500.00'), 'GBP'))

    def test_blanking_a_filled_site_returns_it_to_inheriting(self):
        self.price(site=self.london, cost=Decimal('1500.00'), currency='GBP')
        self.client.post(self.url, {
            'device_type': self.new.pk,
            'site_cost_%d' % self.london.pk: '',
        })
        self.assertFalse(ReplacementPrice.objects.filter(
            device_type=self.new, site=self.london).exists())

    def test_resaving_the_same_values_changes_nothing(self):
        """Idempotence: a resubmitted worksheet must not churn the changelog."""
        self.price(region=self.emea, cost=Decimal('1200.00'), currency='EUR')
        before = ReplacementPrice.objects.get(region=self.emea).last_updated
        self.client.post(self.url, {
            'device_type': self.new.pk,
            'region_cost_%d' % self.emea.pk: '1200.00',
            'region_currency_%d' % self.emea.pk: 'EUR',
        })
        self.assertEqual(
            ReplacementPrice.objects.get(region=self.emea).last_updated, before)
