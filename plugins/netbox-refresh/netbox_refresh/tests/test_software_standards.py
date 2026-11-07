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

    def test_device_tag_overrides_general_download_and_conflicting_tags_withhold_it(self):
        from extras.models import Tag

        from netbox_refresh.compliance import StandardResolver, device_compliance_rows
        from netbox_refresh.models import SoftwareStandard

        general = SoftwareVersion.objects.create(
            platform=self.platform, version='general', image_url='https://images.example.test/general.img',
        )
        mode = SoftwareVersion.objects.create(
            platform=self.platform, version='mode', image_url='https://images.example.test/mode.img',
        )
        reading = DeviceSoftware.objects.create(device=self.device, software_version=general)
        platform_standard = SoftwareStandard.objects.create(preferred_version=general)
        platform_standard.platforms.add(self.platform)
        platform_standard.approved_versions.add(general)
        type_standard = SoftwareStandard.objects.create(preferred_version=general)
        type_standard.device_types.add(self.device.device_type)
        type_standard.approved_versions.add(general)

        mode_tag = Tag.objects.create(name='Special mode', slug='special-mode')
        alternate_tag = Tag.objects.create(name='Alternate mode', slug='alternate-mode')
        alias_tag = Tag.objects.create(name='Mode alias', slug='mode-alias')
        url = reverse('plugins-api:netbox_refresh-api:softwarestandard-list')

        def tagged_standard(*tags):
            response = self.client.post(url, {
                'device_tags': [tag.pk for tag in tags],
                'approved_versions': [mode.pk], 'preferred_version': mode.pk,
            }, content_type='application/json')
            self.assertEqual(response.status_code, 201, response.content)
            return SoftwareStandard.objects.get(pk=response.json()['id'])

        tagged = tagged_standard(mode_tag, alias_tag)
        tagged_standard(alternate_tag)
        self.assertEqual(reading.standard, type_standard)
        self.device.tags.add(mode_tag, alias_tag)
        self.assertEqual(StandardResolver().for_device(self.device), tagged)
        self.assertEqual(reading.standard, tagged)
        self.assertEqual(reading.compliance_status, 'non-compliant')
        card = self.client.get(self.device.get_absolute_url())
        self.assertContains(card, mode.image_url)
        self.assertNotContains(card, general.image_url)

        self.device.tags.add(alternate_tag)
        self.assertIsNone(StandardResolver().for_device(self.device))
        self.assertEqual(device_compliance_rows([self.device])[0]['status'], 'standard-conflict')
        card = self.client.get(self.device.get_absolute_url())
        self.assertContains(card, 'Conflicting tag standards')
        self.assertNotContains(card, mode.image_url)
        self.assertNotContains(card, general.image_url)

        self.device.tags.clear()
        self.assertEqual(reading.standard, type_standard)
        self.assertEqual(reading.compliance_status, 'compliant')

    def test_tag_scope_form_and_api_reject_mixed_and_overlapping_scopes(self):
        from extras.models import Tag

        tag = Tag.objects.create(name='Special mode', slug='special-mode')
        version = SoftwareVersion.objects.create(platform=self.platform, version='mode')
        add_url = reverse('plugins:netbox_refresh:softwarestandard_add')
        form = self.client.get(add_url)
        self.assertContains(form, 'Device tags')
        url = reverse('plugins-api:netbox_refresh-api:softwarestandard-list')
        payload = {'device_tags': [tag.pk], 'approved_versions': [version.pk]}
        mixed = self.client.post(url, {**payload, 'platforms': [self.platform.pk]}, content_type='application/json')
        self.assertEqual(mixed.status_code, 400, mixed.content)
        created = self.client.post(add_url, {**payload, 'valid_from': timezone.localdate().isoformat()})
        self.assertEqual(created.status_code, 302, getattr(created.context, 'form', created.context))
        duplicate = self.client.post(url, payload, content_type='application/json')
        self.assertContains(duplicate, 'overlaps', status_code=400)
