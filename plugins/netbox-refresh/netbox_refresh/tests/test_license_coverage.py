from datetime import timedelta

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Platform, Site
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from netbox_refresh.models import LicenseAssignment, LicenseEntitlement, SoftwareVersion, SupportContract


class LicenseCoverageTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name='F5', slug='f5')
        platform = Platform.objects.create(name='F5 TMOS', slug='f5-tmos')
        model = DeviceType.objects.create(manufacturer=manufacturer, model='BIG-IP VE', slug='big-ip-ve')
        role = DeviceRole.objects.create(name='Load Balancer', slug='load-balancer')
        site = Site.objects.create(name='Lab', slug='lab')
        cls.devices = [
            Device.objects.create(name=name, device_type=model, role=role, site=site, platform=platform)
            for name in ('bigip-a', 'bigip-b', 'bigip-c')
        ]
        cls.entitlement = LicenseEntitlement.objects.create(
            manufacturer=manufacturer, product='BIG-IP VE lab pool', reference='POOL-1', seats=2,
        )
        cls.user = get_user_model().objects.create_superuser('lifecycle-admin', password='test')
        cls.version = SoftwareVersion.objects.create(platform=platform, version='17.5.1')

    def setUp(self):
        self.client.force_login(self.user)

    def test_api_assigns_multiple_devices_to_one_contract_and_report_shows_coverage_gaps(self):
        today = timezone.localdate()
        url = reverse('plugins-api:netbox_refresh-api:supportcontract-list')
        payload = {
            'provider': 'F5', 'reference': 'CASE-1',
            'starts_on': today.isoformat(), 'ends_on': (today + timedelta(days=2)).isoformat(),
            'devices': [device.pk for device in self.devices[:2]],
        }
        response = self.client.post(url, payload, content_type='application/json')
        self.assertEqual(response.status_code, 201, response.content)
        contract = SupportContract.objects.get(reference='CASE-1')
        self.assertCountEqual(contract.devices.all(), self.devices[:2])

        report_url = reverse('plugins:netbox_refresh:coverage_report')
        report = self.client.get(report_url)
        self.assertEqual(report.status_code, 200)
        self.assertEqual(list(report.context['page']), [self.devices[2]])
        self.assertContains(self.client.get(self.devices[0].get_absolute_url()), 'CASE-1')
        self.assertContains(self.client.get(contract.get_absolute_url()), 'bigip-b')

        contract.starts_on = None
        contract.save()
        self.assertEqual(contract.coverage_status, 'Dates unverified')
        self.assertCountEqual(list(self.client.get(report_url).context['page']), self.devices)

    def test_license_entitlement_can_exist_without_assignments_and_appear_on_devices(self):
        entitlement = self.entitlement
        self.assertContains(self.client.get(entitlement.get_absolute_url()), 'No visible assignments')
        url = reverse('plugins-api:netbox_refresh-api:licenseassignment-list')
        payload = {'entitlement': entitlement.pk, 'device': self.devices[0].pk, 'seats': 1}
        response = self.client.post(url, payload, content_type='application/json')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.client.get(entitlement.get_absolute_url()).context['visible_seats'], 1)
        self.assertContains(self.client.get(self.devices[0].get_absolute_url()), 'POOL-1')
        self.assertContains(self.client.get(LicenseAssignment.objects.get().get_absolute_url()), 'Assigned seats')

        duplicate = self.client.post(url, payload, content_type='application/json')
        self.assertEqual(duplicate.status_code, 400, duplicate.content)
        self.assertEqual(LicenseAssignment.objects.count(), 1)

    def test_invalid_contract_term_is_rejected(self):
        today = timezone.localdate()
        contract = SupportContract(
            provider='F5', reference='CASE-2', starts_on=today, ends_on=today - timedelta(days=1),
        )
        with self.assertRaisesMessage(ValidationError, 'The end date must be on or after the start date.'):
            contract.full_clean()

    def test_release_support_dates_can_be_edited_and_filtered_in_the_api(self):
        today = timezone.localdate()
        url = reverse('plugins-api:netbox_refresh-api:softwareversion-detail', args=[self.version.pk])
        response = self.client.patch(
            url, {'end_of_support': today.isoformat(), 'lifecycle_source_url': 'https://example.test/notice'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        list_url = reverse('plugins-api:netbox_refresh-api:softwareversion-list')
        self.assertEqual(self.client.get(list_url, {'end_of_support__lte': today.isoformat()}).json()['count'], 1)
        self.assertEqual(self.client.get(list_url, {
            'end_of_support__lte': (today - timedelta(days=1)).isoformat(),
        }).json()['count'], 0)
        self.assertContains(self.client.get(self.version.get_absolute_url()), 'https://example.test/notice')
