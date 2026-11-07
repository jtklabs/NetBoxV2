"""Every hardware model on the lifecycle tab, with or without a lifecycle.

A list of what is configured cannot show what is missing, and the missing
models are where the unplanned refreshes come from. So the tab also lists
every device and module type in NetBox that has no lifecycle yet, reading
"None configured", with an Add button that arrives on the form with the
model already chosen.
"""

from dcim.models import (
    Device, DeviceRole, DeviceType, Manufacturer, ModuleType, Site,
)
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from netbox_refresh.models import ModelLifecycle


class LifecycleCoverageTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cisco = Manufacturer.objects.create(name='Cisco', slug='cisco')
        cls.arista = Manufacturer.objects.create(name='Arista', slug='arista')
        cls.covered = DeviceType.objects.create(manufacturer=cls.cisco, model='C9300-24P',
                                                slug='c9300-24p')
        cls.gap = DeviceType.objects.create(manufacturer=cls.cisco, model='C2960-24',
                                            slug='c2960-24', part_number='WS-C2960-24')
        cls.other = DeviceType.objects.create(manufacturer=cls.arista, model='DCS-7050SX',
                                              slug='dcs-7050sx')
        cls.module_gap = ModuleType.objects.create(manufacturer=cls.cisco, model='C9300-NM-8X')
        ModelLifecycle(assigned_object=cls.covered).save()

        site = Site.objects.create(name='Site', slug='site')
        role = DeviceRole.objects.create(name='Access', slug='access')
        for i in range(3):
            Device.objects.create(name='gap%d' % i, device_type=cls.gap, role=role, site=site)
        Device.objects.create(name='ok1', device_type=cls.covered, role=role, site=site)

        cls.user = get_user_model().objects.create_superuser('cover', password='x')
        cls.url = reverse('plugins:netbox_refresh:modellifecycle_list')

    def setUp(self):
        self.client.force_login(self.user)

    def rows(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return response, list(response.context['unconfigured_table'].data)

    def test_models_without_a_lifecycle_are_listed_as_none_configured(self):
        response, rows = self.rows()
        # Installed units first, then by manufacturer and model.
        self.assertEqual([r['model'] for r in rows], ['C2960-24', 'DCS-7050SX', 'C9300-NM-8X'])
        self.assertContains(response, 'None configured')
        self.assertNotIn('C9300-24P', [r['model'] for r in rows])

    def test_installed_units_come_first_and_are_counted(self):
        _response, rows = self.rows()
        self.assertEqual(rows[0]['model'], 'C2960-24')
        self.assertEqual(rows[0]['installed'], 3)
        self.assertEqual(rows[0]['kind'], 'Device type')
        module = next(r for r in rows if r['model'] == 'C9300-NM-8X')
        self.assertEqual((module['kind'], module['installed']), ('Module type', 0))

    def test_the_add_button_arrives_with_the_model_chosen(self):
        response, rows = self.rows()
        gap = next(r for r in rows if r['model'] == 'C2960-24')
        self.assertEqual(gap['add_url'], '%s?device_type=%d' % (
            reverse('plugins:netbox_refresh:modellifecycle_add'), self.gap.pk))
        self.assertContains(response, gap['add_url'])
        module = next(r for r in rows if r['model'] == 'C9300-NM-8X')
        self.assertIn('?module_type=%d' % self.module_gap.pk, module['add_url'])
        # And the form honours it.
        form_page = self.client.get(gap['add_url'])
        self.assertEqual(form_page.status_code, 200)
        self.assertEqual(form_page.context['form'].initial.get('device_type'), str(self.gap.pk))

    def test_the_lists_filters_narrow_the_gaps_too(self):
        _response, rows = self.rows(manufacturer_id=self.arista.pk)
        self.assertEqual([r['model'] for r in rows], ['DCS-7050SX'])
        _response, rows = self.rows(q='2960')
        self.assertEqual([r['model'] for r in rows], ['C2960-24'])
        _response, rows = self.rows(q='WS-C2960')
        self.assertEqual([r['model'] for r in rows], ['C2960-24'])

    def test_adding_the_lifecycle_moves_the_model_across(self):
        ModelLifecycle(assigned_object=self.gap).save()
        _response, rows = self.rows()
        self.assertNotIn('C2960-24', [r['model'] for r in rows])

    def test_the_configured_list_is_untouched(self):
        response, _rows = self.rows()
        self.assertEqual([lc.assigned_object for lc in response.context['table'].data],
                         [self.covered])
