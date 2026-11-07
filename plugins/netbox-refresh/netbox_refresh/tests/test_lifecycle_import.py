"""Importing a spreadsheet of end-of-life dates.

Two things this covers, both of which made a vendor's EoX bulletin unusable:
a lifecycle record is unique per hardware model, so any row for a model
already loaded failed the whole upload; and dates only parsed as ISO, which is
not what comes out of a spreadsheet.
"""

from datetime import date

from dcim.models import DeviceType, Manufacturer, ModuleType
from django.test import TestCase

from netbox_refresh.forms import ModelLifecycleImportForm
from netbox_refresh.models import ModelLifecycle


class LifecycleImportTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mfr = Manufacturer.objects.create(name='Cisco', slug='cisco')
        cls.dt = DeviceType.objects.create(
            manufacturer=cls.mfr, model='C9300-24P', slug='c9300-24p')
        cls.other = DeviceType.objects.create(
            manufacturer=cls.mfr, model='C9300-48P', slug='c9300-48p')
        cls.mt = ModuleType.objects.create(manufacturer=cls.mfr, model='C9300-NM-8X')

    def load(self, headers, **data):
        """Bind the form the way a CSV upload does: only supplied columns."""
        form = ModelLifecycleImportForm(data=data, headers={h: None for h in headers})
        return form


class TestARowForAModelAlreadyLoaded(LifecycleImportTest):
    def test_the_first_row_creates(self):
        form = self.load(['device_type', 'end_of_support'],
                         device_type='C9300-24P', end_of_support='12/31/2027')
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(ModelLifecycle.objects.count(), 1)

    def test_a_second_row_updates_instead_of_failing(self):
        """This used to be a fatal error that failed the whole upload."""
        first = self.load(['device_type', 'end_of_support'],
                          device_type='C9300-24P', end_of_support='12/31/2027')
        self.assertTrue(first.is_valid(), first.errors)
        first.save()

        second = self.load(['device_type', 'end_of_support'],
                           device_type='C9300-24P', end_of_support='06/30/2028')
        self.assertTrue(second.is_valid(), second.errors)
        second.save()

        self.assertEqual(ModelLifecycle.objects.count(), 1)
        self.assertEqual(ModelLifecycle.objects.first().end_of_support,
                         date(2028, 6, 30))

    def test_columns_absent_from_the_sheet_are_left_alone(self):
        """A revised bulletin usually carries one date. It must not wipe the
        cost, the bulletin number or the other dates."""
        first = self.load(['device_type', 'end_of_sale', 'end_of_support',
                           'bulletin_number'],
                          device_type='C9300-24P', end_of_sale='01/15/2026',
                          end_of_support='12/31/2027', bulletin_number='EOL1234')
        self.assertTrue(first.is_valid(), first.errors)
        first.save()

        second = self.load(['device_type', 'end_of_support'],
                           device_type='C9300-24P', end_of_support='06/30/2028')
        self.assertTrue(second.is_valid(), second.errors)
        second.save()

        record = ModelLifecycle.objects.get()
        self.assertEqual(record.end_of_support, date(2028, 6, 30))
        self.assertEqual(record.end_of_sale, date(2026, 1, 15))
        self.assertEqual(record.bulletin_number, 'EOL1234')

    def test_a_different_model_still_gets_its_own_record(self):
        for model in ('C9300-24P', 'C9300-48P'):
            form = self.load(['device_type', 'end_of_support'],
                             device_type=model, end_of_support='12/31/2027')
            self.assertTrue(form.is_valid(), form.errors)
            form.save()
        self.assertEqual(ModelLifecycle.objects.count(), 2)

    def test_module_types_upsert_too(self):
        for _ in range(2):
            form = self.load(['module_type', 'end_of_support'],
                             module_type='C9300-NM-8X', end_of_support='12/31/2027')
            self.assertTrue(form.is_valid(), form.errors)
            form.save()
        self.assertEqual(ModelLifecycle.objects.count(), 1)


class TestDateFormats(LifecycleImportTest):
    def parse(self, value):
        form = self.load(['device_type', 'end_of_support'],
                         device_type='C9300-24P', end_of_support=value)
        return form, form.cleaned_data.get('end_of_support') if form.is_valid() else None

    def test_us_format(self):
        _, parsed = self.parse('12/31/2027')
        self.assertEqual(parsed, date(2027, 12, 31))

    def test_us_format_two_digit_year(self):
        _, parsed = self.parse('12/31/27')
        self.assertEqual(parsed, date(2027, 12, 31))

    def test_iso_still_loads(self):
        """Any sheet written before this still has to import."""
        _, parsed = self.parse('2027-12-31')
        self.assertEqual(parsed, date(2027, 12, 31))

    def test_an_ambiguous_date_is_read_as_us_not_day_first(self):
        """03/04/2026 is 4 March. Accepting day-first as a fallback would make
        it 3 April on some rows and not others, and a silently wrong
        end-of-support date is worse than a rejected one."""
        _, parsed = self.parse('03/04/2026')
        self.assertEqual(parsed, date(2026, 3, 4))

    def test_a_day_first_date_is_rejected_rather_than_misread(self):
        """25/12/2027 has no valid US reading, so it must fail loudly."""
        form, parsed = self.parse('25/12/2027')
        self.assertFalse(form.is_valid())
        self.assertIn('end_of_support', form.errors)

    def test_every_lifecycle_date_accepts_us_format(self):
        """Generated from DATE_FIELDS, so this catches a field added later."""
        from netbox_refresh.forms import DATE_FIELDS

        for name in DATE_FIELDS:
            form = self.load(['device_type', name],
                             device_type='C9300-24P', **{name: '07/04/2026'})
            self.assertTrue(form.is_valid(), f'{name}: {form.errors}')
            self.assertEqual(form.cleaned_data[name], date(2026, 7, 4), name)
            ModelLifecycle.objects.all().delete()

    def test_cost_updated_too(self):
        form = self.load(['device_type', 'cost_updated'],
                         device_type='C9300-24P', cost_updated='07/04/2026')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['cost_updated'], date(2026, 7, 4))
