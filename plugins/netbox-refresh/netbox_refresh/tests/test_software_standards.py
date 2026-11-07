from datetime import timedelta

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Platform, Site
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from netbox_refresh.models import DeviceSoftware, SoftwareVersion


class SoftwareStandardsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name='F5', slug='f5')
        cls.platform = Platform.objects.create(name='F5 TMOS', slug='f5-tmos')
        model = DeviceType.objects.create(manufacturer=manufacturer, model='BIG-IP VE', slug='big-ip-ve')
        role = DeviceRole.objects.create(name='Load Balancer', slug='load-balancer')
        site = Site.objects.create(name='Lab', slug='lab')
        cls.device = Device.objects.create(
            name='bigip', device_type=model, role=role, site=site, platform=cls.platform,
        )
        cls.user = get_user_model().objects.create_superuser('software-admin', password='test')

    def setUp(self):
        self.client.force_login(self.user)

    def test_reported_versions_are_reusable_and_evaluate_against_multiple_approvals(self):
        url = reverse('plugins-api:netbox_refresh-api:devicesoftware-report')
        now = timezone.now()

        def report(version, collected_at):
            response = self.client.post(url, {
                'device': self.device.pk, 'source': 'snmp', 'version': version,
                'collected_at': collected_at.isoformat(),
            }, content_type='application/json')
            self.assertEqual(response.status_code, 200, response.content)
            return response.json()['results'][0]

        first = report('17.5.1.10-0.0.35', now)
        self.assertEqual(first['result'], 'created')
        current = DeviceSoftware.objects.get(device=self.device)
        self.assertEqual(current.software_version.platform, self.platform)
        alternate = SoftwareVersion.objects.create(platform=self.platform, version='17.5.1.11')
        standard_url = reverse('plugins-api:netbox_refresh-api:softwarestandard-list')
        standard = self.client.post(standard_url, {
            'platforms': [self.platform.pk], 'valid_from': timezone.localdate().isoformat(),
            'approved_versions': [current.software_version_id, alternate.pk],
        }, content_type='application/json')
        self.assertEqual(standard.status_code, 201, standard.content)
        self.assertEqual(current.compliance_status, 'compliant')

        changed = report(alternate.version, now + timedelta(seconds=1))
        self.assertEqual((changed['result'], changed['id']), ('changed', first['id']))
        current.refresh_from_db()
        self.assertEqual(current.software_version, alternate)
        self.assertEqual(current.compliance_status, 'compliant')

        report('17.5.1.12', now + timedelta(seconds=2))
        stale = report(alternate.version, now)
        self.assertEqual(stale['result'], 'stale')
        current.refresh_from_db()
        self.assertEqual(current.software_version.version, '17.5.1.12')
        self.assertEqual(current.compliance_status, 'non-compliant')
        self.assertContains(self.client.get(self.device.get_absolute_url()), 'Non-compliant')
        self.assertEqual(SoftwareVersion.objects.count(), 3)
