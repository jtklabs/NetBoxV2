from datetime import timedelta

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Platform, Site
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from netbox_refresh.models import (
    DeviceCollectionState,
    DeviceInventoryChange,
    DeviceObservation,
    DeviceRelationship,
    DeviceSoftware,
    LifecycleAction,
    LicenseAssignment,
    LicenseEntitlement,
    SoftwareStandard,
    SoftwareVersion,
)
from netbox_refresh.operations import (
    ingest_observation,
    readiness_for_device,
    refresh_actions,
)


class OperationsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        vendor = Manufacturer.objects.create(name="Test Vendor", slug="test-vendor")
        platform = Platform.objects.create(name="Test OS", slug="test-os")
        model = DeviceType.objects.create(
            manufacturer=vendor, model="Test Model", slug="test-model"
        )
        site = Site.objects.create(name="Operations Lab", slug="operations-lab")
        role = DeviceRole.objects.create(name="Switch", slug="switch")
        cls.device = Device.objects.create(
            name="switch-a", site=site, role=role, device_type=model, platform=platform
        )
        cls.peer = Device.objects.create(
            name="switch-b", site=site, role=role, device_type=model, platform=platform
        )
        cls.user = get_user_model().objects.create_superuser(
            "operations-admin", password="test"
        )
        cls.version = SoftwareVersion.objects.create(
            platform=platform,
            version="2.0",
            image_filename="os.bin",
            image_url="https://images.example.test/os.bin",
            image_size=1000,
        )
        cls.standard = SoftwareStandard.objects.create(
            description="Approved code", valid_from=timezone.localdate()
        )
        cls.standard.platforms.add(platform)
        cls.standard.approved_versions.add(cls.version)
        cls.standard.preferred_version = cls.version
        cls.standard.save()

    def report(self, job="job-1", **kwargs):
        return {
            "job_id": job,
            "poller": "checkmk-us",
            "executor_version": "0.26.0",
            "observed_at": timezone.now(),
            "components": {"identity": {"status": "success"}},
            "facts": {"serial": "SERIAL-A", "software_version": "1.0"},
            **kwargs,
        }

    def test_retry_and_old_reports_do_not_change_current_state(self):
        first, created = ingest_observation(self.device, self.report())
        self.assertTrue(created)
        repeated, created = ingest_observation(
            self.device, self.report(facts={"serial": "WRONG"})
        )
        self.assertFalse(created)
        self.assertEqual(first.pk, repeated.pk)
        ingest_observation(
            self.device, self.report("job-2", facts={"software_version": "2.0"})
        )
        self.assertEqual(DeviceInventoryChange.objects.get().field, "software_version")
        ingest_observation(
            self.device,
            self.report(
                "job-old",
                observed_at=timezone.now() - timedelta(days=2),
                facts={"serial": "OLD"},
            ),
        )
        current = DeviceCollectionState.objects.get()
        self.assertEqual(current.facts["serial"], "SERIAL-A")
        self.assertEqual(current.facts["software_version"], "2.0")
        self.assertEqual(DeviceObservation.objects.count(), 3)

    def test_partial_collection_preserves_per_component_success(self):
        ingest_observation(self.device, self.report())
        success = DeviceCollectionState.objects.get().components["identity"][
            "last_success"
        ]
        ingest_observation(
            self.device,
            self.report(
                "job-2",
                components={
                    "identity": {"status": "failed", "reason": "Timeout"},
                    "licenses": {"status": "success"},
                },
                facts={},
            ),
        )
        current = DeviceCollectionState.objects.get()
        self.assertEqual(current.status, "partial")
        self.assertEqual(current.components["identity"]["last_success"], success)
        self.assertEqual(current.facts["serial"], "SERIAL-A")
        self.assertFalse(DeviceInventoryChange.objects.exists())

    def test_relationships_retire_only_on_complete_success_and_confirm_reciprocal(self):
        rel = {
            "kind": "lldp",
            "local_interface": "Ethernet1",
            "peer_name": self.peer.name,
            "peer_interface": "Ethernet2",
        }
        ingest_observation(
            self.device,
            self.report(
                components={"neighbors": {"status": "success"}}, relationships=[rel]
            ),
        )
        ingest_observation(
            self.peer,
            self.report(
                components={"neighbors": {"status": "success"}},
                relationships=[
                    {
                        **rel,
                        "local_interface": "Ethernet2",
                        "peer_name": self.device.name,
                        "peer_interface": "Ethernet1",
                    }
                ],
            ),
        )
        self.assertEqual(DeviceRelationship.objects.filter(confirmed=True).count(), 2)
        ingest_observation(
            self.device,
            self.report(
                "failed",
                components={"neighbors": {"status": "failed"}},
                relationships=[],
            ),
        )
        self.assertTrue(
            DeviceRelationship.objects.filter(device=self.device).get().present
        )
        ingest_observation(
            self.device,
            self.report(
                "empty",
                components={"neighbors": {"status": "success"}},
                relationships=[],
            ),
        )
        self.assertFalse(
            DeviceRelationship.objects.filter(device=self.device).get().present
        )
        self.assertFalse(DeviceRelationship.objects.filter(confirmed=True).exists())

    def test_airgapped_target_is_known_but_readiness_unknown(self):
        row = readiness_for_device(self.device)
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["preferred_version"]["version"], "2.0")
        self.assertEqual(row["compliance"], "unknown")
        self.assertIn("Readiness evidence missing or stale", row["reasons"])

    def test_readiness_requires_all_checks_and_fresh_evidence(self):
        checks = {
            key: {"status": "passed"}
            for key in ("ssh", "credentials", "trust", "backup", "storage", "ha")
        }
        DeviceSoftware.objects.create(
            device=self.device,
            software_version=self.version,
            collected_at=timezone.now(),
        )
        ingest_observation(self.device, self.report(readiness=checks))
        self.assertEqual(
            readiness_for_device(self.device)["status"], "already-compliant"
        )
        checks["ha"] = {"status": "failed", "reason": "Not synchronized"}
        ingest_observation(self.device, self.report("job-2", readiness=checks))
        self.assertEqual(readiness_for_device(self.device)["status"], "blocked")

    def test_actions_preserve_planning_and_resolve_conditions(self):
        expiry = timezone.localdate() + timedelta(days=6)
        entitlement = LicenseEntitlement.objects.create(
            manufacturer=self.device.device_type.manufacturer,
            product="Routing",
            reference="test-ref",
            expires_on=expiry,
        )
        assignment = LicenseAssignment.objects.create(
            device=self.device, entitlement=entitlement
        )
        refresh_actions(Device.objects.filter(pk=self.device.pk))
        action = LifecycleAction.objects.get(kind="license-renewal")
        action.status = "planned"
        action.due_override = expiry - timedelta(days=1)
        action.save()
        refresh_actions(Device.objects.filter(pk=self.device.pk))
        action.refresh_from_db()
        self.assertEqual(action.status, "planned")
        self.assertEqual(action.effective_due_date, expiry - timedelta(days=1))
        self.assertEqual(action.reminder_stage, "7-days")
        assignment.delete()
        refresh_actions(Device.objects.filter(pk=self.device.pk))
        action.refresh_from_db()
        self.assertFalse(action.condition_active)

    def test_report_api_validation_and_pages(self):
        self.client.force_login(self.user)
        url = reverse("plugins-api:netbox_refresh-api:deviceobservation-report")
        data = self.report(device=self.device.pk)
        data["observed_at"] = data["observed_at"].isoformat()
        bad = self.client.post(
            url,
            {**data, "facts": {"license_key": "SHOULD-NOT-BE-STORED"}},
            content_type="application/json",
        )
        self.assertEqual(bad.status_code, 400, bad.content)
        response = self.client.post(url, data, content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(
            self.client.post(url, data, content_type="application/json").status_code,
            200,
        )
        for name in (
            "devicecollectionstate",
            "deviceobservation",
            "deviceinventorychange",
            "devicerelationship",
            "lifecycleaction",
        ):
            response = self.client.get(
                reverse("plugins:netbox_refresh:" + name + "_list")
            )
            self.assertEqual(response.status_code, 200, response.content[:1000])
        for obj in (
            DeviceCollectionState.objects.get(),
            DeviceObservation.objects.get(),
        ):
            response = self.client.get(obj.get_absolute_url())
            self.assertEqual(response.status_code, 200, response.content[:1000])
        self.assertEqual(
            self.client.get(
                reverse("plugins:netbox_refresh:readiness_report")
            ).status_code,
            200,
        )
        response = self.client.get(
            reverse("plugins-api:netbox_refresh-api:readiness"),
            {"device": self.device.pk},
        )
        self.assertEqual(response.status_code, 200, response.content)
        response = self.client.post(
            reverse("plugins-api:netbox_refresh-api:action-refresh"),
            {"device": self.device.pk},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertContains(
            self.client.get(self.device.get_absolute_url()), "executor 0.26.0"
        )
        refresh_actions(Device.objects.filter(pk=self.device.pk))
        action = LifecycleAction.objects.first()
        self.assertEqual(self.client.get(action.get_absolute_url()).status_code, 200)
        edit = reverse(
            "plugins-api:netbox_refresh-api:lifecycleaction-detail", args=[action.pk]
        )
        response = self.client.patch(
            edit,
            {"status": "planned", "kind": "forged"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        action.refresh_from_db()
        self.assertEqual(action.status, "planned")
        self.assertNotEqual(action.kind, "forged")

    def test_report_api_rejects_unprivileged_user(self):
        user = get_user_model().objects.create_user(
            "operations-reader", password="test"
        )
        self.client.force_login(user)
        response = self.client.post(
            reverse("plugins-api:netbox_refresh-api:deviceobservation-report"),
            {},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_scheduled_refresh_emits_native_events_without_duplicate_reminders(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from extras.events import serialize_for_event
        from netbox_refresh.jobs import LifecycleActionRefreshJob

        job = SimpleNamespace(user=None, data=None, save=lambda **kwargs: None)
        with patch("extras.events.flush_events") as flush:
            LifecycleActionRefreshJob(job).run()
            self.assertTrue(flush.called)
            events = flush.call_args.args[0]
            self.assertTrue(events)
            payload = serialize_for_event(events[0]["object"])
            self.assertIn("reminder_stage", payload)
            self.assertIn("effective_due_date", payload)
        with patch("extras.events.flush_events") as flush:
            LifecycleActionRefreshJob(job).run()
            flush.assert_not_called()

    def test_manual_provenance_can_be_reported_without_tags(self):
        self.client.force_login(self.user)
        ingest_observation(self.device, self.report(field_sources={"model": "manual"}))
        ingest_observation(self.peer, self.report(field_sources={"model": "observed"}))
        url = reverse("plugins-api:netbox_refresh-api:devicecollectionstate-list")
        response = self.client.get(url, {"manual": "true"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["device"], self.device.pk)

    def test_readiness_does_not_recommend_a_visible_general_rule_over_a_hidden_specific_one(
        self,
    ):
        from django.contrib.contenttypes.models import ContentType
        from users.models import ObjectPermission

        hidden = SoftwareStandard.objects.create(
            preferred_version=self.version, valid_from=timezone.localdate()
        )
        hidden.device_types.add(self.device.device_type)
        hidden.approved_versions.add(self.version)
        user = get_user_model().objects.create_user("scoped-operations-reader")
        for model, constraint in (
            (Device, {"pk": self.device.pk}),
            (DeviceCollectionState, None),
            (DeviceSoftware, None),
            (SoftwareStandard, {"pk": self.standard.pk}),
            (SoftwareVersion, None),
        ):
            permission = ObjectPermission.objects.create(
                name=f"read-ops-{model.__name__}",
                actions=["view"],
                constraints=constraint,
            )
            permission.object_types.add(ContentType.objects.get_for_model(model))
            permission.users.add(user)
        self.client.force_login(user)
        response = self.client.get(
            reverse("plugins-api:netbox_refresh-api:readiness"),
            {"device": self.device.pk},
        )
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()["results"][0]
        self.assertEqual(row["status"], "unknown")
        self.assertIsNone(row["preferred_version"])
        self.assertIsNone(row["standard"])
        self.assertEqual(row["reasons"], ["Applicable standard is not visible"])
