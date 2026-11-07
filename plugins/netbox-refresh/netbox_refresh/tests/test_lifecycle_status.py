"""Telling "nobody has looked" apart from "we looked and there was nothing".

On the dates alone the two are identical — every date column empty. The
difference is last_checked: a model somebody checked (or the Cisco sync
checked) with nothing to show is EoL-not-announced, which is an answer and a
date to re-check from; a model nobody has checked is unknown, which is a
to-do. Without the distinction the to-do list never shrinks.
"""

from datetime import date, timedelta
from unittest import mock

from dcim.models import DeviceType, Manufacturer
from django.test import TestCase
from django.utils import timezone

from netbox_refresh.choices import LifecycleSourceChoices, LifecycleStatusChoices
from netbox_refresh.filtersets import ModelLifecycleFilterSet
from netbox_refresh.models import ModelLifecycle
from netbox_refresh.sync import _record_checked


class LifecycleStatusTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mfr = Manufacturer.objects.create(name='Juniper', slug='juniper')

    def record(self, slug, **fields):
        dt = DeviceType.objects.create(manufacturer=self.mfr, model=slug.upper(),
                                       slug=slug)
        record = ModelLifecycle(assigned_object=dt, **fields)
        record.save()
        return record

    def test_never_checked_and_no_dates_is_unknown(self):
        self.assertEqual(self.record('a').status, LifecycleStatusChoices.STATUS_UNKNOWN)

    def test_checked_and_no_dates_is_not_announced(self):
        r = self.record('b', last_checked=date.today())
        self.assertEqual(r.status, LifecycleStatusChoices.STATUS_NOT_ANNOUNCED)

    def test_a_cisco_sync_counts_as_a_check(self):
        """Records synced before last_checked existed must not fall back to
        unknown — the sync looked, even if it only stamped last_synced."""
        r = self.record('c', last_synced=timezone.now(),
                        source=LifecycleSourceChoices.SOURCE_CISCO)
        self.assertEqual(r.status, LifecycleStatusChoices.STATUS_NOT_ANNOUNCED)
        self.assertEqual(r.checked_on, timezone.localdate())

    def test_checked_on_is_the_later_of_the_two(self):
        earlier = date.today() - timedelta(days=30)
        r = self.record('d', last_checked=earlier, last_synced=timezone.now())
        self.assertEqual(r.checked_on, timezone.localdate())

    def test_any_published_date_means_announced_even_when_checked(self):
        """A check that found something is not 'not announced'."""
        r = self.record('e', last_checked=date.today(),
                        end_of_sw_maintenance=date.today() + timedelta(days=400))
        self.assertEqual(r.status, LifecycleStatusChoices.STATUS_EOS_ANNOUNCED)

    def test_an_obscure_date_alone_still_counts_as_announced(self):
        """Previously only three of the eight dates were consulted."""
        r = self.record('f', end_of_routine_failure_analysis=date(2030, 1, 1))
        self.assertEqual(r.status, LifecycleStatusChoices.STATUS_EOS_ANNOUNCED)

    def test_past_dates_still_outrank_everything(self):
        past = date.today() - timedelta(days=1)
        r = self.record('g', last_checked=date.today(), end_of_sale=past)
        self.assertEqual(r.status, LifecycleStatusChoices.STATUS_END_OF_SALE)
        r = self.record('h', last_checked=date.today(), end_of_support=past)
        self.assertEqual(r.status, LifecycleStatusChoices.STATUS_END_OF_SUPPORT)

    def test_status_choices_no_longer_offer_a_state_nothing_returns(self):
        values = {value for value, _label, _color in LifecycleStatusChoices.CHOICES}
        self.assertNotIn('current', values)
        self.assertIn(LifecycleStatusChoices.STATUS_NOT_ANNOUNCED, values)


class StatusFilterAgreesWithTheProperty(TestCase):
    """The list page filters in SQL; the badge renders from the property. They
    must sort every record into the same bucket or the filter lies."""

    @classmethod
    def setUpTestData(cls):
        cls.mfr = Manufacturer.objects.create(name='Cisco', slug='cisco')
        today = date.today()
        past, future = today - timedelta(days=1), today + timedelta(days=400)
        specs = {
            'unknown': {},
            'checked': {'last_checked': today},
            'synced': {'last_synced': timezone.now()},
            'announced': {'end_of_sale': future},
            'obscure': {'end_of_service_attach': future},
            'past-sale': {'end_of_sale': past, 'end_of_support': future},
            'past-eol': {'end_of_support': past},
            'past-security': {'end_of_security_support': past, 'end_of_support': future},
        }
        cls.records = {}
        for slug, fields in specs.items():
            dt = DeviceType.objects.create(manufacturer=cls.mfr, model=slug, slug=slug)
            record = ModelLifecycle(assigned_object=dt, **fields)
            record.save()
            cls.records[slug] = record

    def filtered(self, *statuses):
        qs = ModelLifecycleFilterSet({'status': list(statuses)},
                                     ModelLifecycle.objects.all()).qs
        return {r.assigned_object.model for r in qs}

    def test_every_status_filters_exactly_what_the_property_says(self):
        for value, _label, _color in LifecycleStatusChoices.CHOICES:
            expected = {slug for slug, r in self.records.items() if r.status == value}
            self.assertEqual(self.filtered(value), expected, value)

    def test_several_statuses_union(self):
        got = self.filtered(LifecycleStatusChoices.STATUS_UNKNOWN,
                            LifecycleStatusChoices.STATUS_NOT_ANNOUNCED)
        self.assertEqual(got, {'unknown', 'checked', 'synced'})


class CiscoNoDataIsRecordedAsAChecked(TestCase):
    """Cisco answering "no EoL data" for a current PID used to leave no trace.
    Now it stamps the model as checked, so it reads as EoL-not-announced."""

    @classmethod
    def setUpTestData(cls):
        cls.mfr = Manufacturer.objects.create(name='Cisco', slug='cisco')
        cls.dt = DeviceType.objects.create(manufacturer=cls.mfr, model='C9300-48P',
                                           slug='c9300-48p', part_number='C9300-48P')

    def test_creates_a_checked_record_where_none_existed(self):
        outcome = _record_checked(self.dt)
        self.assertEqual(outcome, 'created')
        record = ModelLifecycle.objects.get(assigned_object_id=self.dt.pk)
        self.assertEqual(record.last_checked, timezone.localdate())
        self.assertEqual(record.source, LifecycleSourceChoices.SOURCE_CISCO)
        self.assertEqual(record.status, LifecycleStatusChoices.STATUS_NOT_ANNOUNCED)

    def test_does_not_erase_dates_a_record_already_has(self):
        existing = ModelLifecycle(assigned_object=self.dt, end_of_sale=date(2030, 1, 1),
                                  source=LifecycleSourceChoices.SOURCE_CISCO)
        existing.save()
        self.assertEqual(_record_checked(self.dt), 'updated')
        existing.refresh_from_db()
        self.assertEqual(existing.end_of_sale, date(2030, 1, 1))
        self.assertEqual(existing.last_checked, timezone.localdate())

    def test_respects_manually_maintained_records(self):
        manual = ModelLifecycle(assigned_object=self.dt,
                                source=LifecycleSourceChoices.SOURCE_MANUAL)
        manual.save()
        self.assertEqual(_record_checked(self.dt), 'skipped_manual')
        manual.refresh_from_db()
        self.assertIsNone(manual.last_checked)

    def test_dry_run_writes_nothing(self):
        _record_checked(self.dt, dry_run=True)
        self.assertFalse(ModelLifecycle.objects.filter(assigned_object_id=self.dt.pk).exists())
