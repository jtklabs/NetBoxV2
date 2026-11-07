"""The date a refresh actually has to happen by.

End-of-security-support and end-of-support are two different kinds of over,
and the earlier one binds. A model can stay under a support contract for years
after its last security fix — TAC still takes the call, the box still cannot be
patched — so planning against end-of-support alone schedules the refresh for
after the estate is already carrying unpatchable hardware.
"""

from datetime import date, timedelta

from dcim.models import DeviceType, Manufacturer
from django.test import TestCase

from netbox_refresh.choices import LifecycleStatusChoices
from netbox_refresh.models import ModelLifecycle, effective_end_of_life_expression


class EffectiveEndOfLifeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mfr = Manufacturer.objects.create(name='Cisco', slug='cisco')

    def record(self, slug, **dates):
        dt = DeviceType.objects.create(manufacturer=self.mfr, model=slug.upper(),
                                       slug=slug)
        record = ModelLifecycle(assigned_object=dt, **dates)
        record.save()
        return record

    def test_security_ending_first_is_what_binds(self):
        """The case this exists for."""
        r = self.record('a', end_of_security_support=date(2027, 1, 1),
                        end_of_support=date(2030, 1, 1))
        self.assertEqual(r.effective_end_of_life, date(2027, 1, 1))

    def test_support_ending_first_is_used_when_it_does(self):
        r = self.record('b', end_of_security_support=date(2030, 1, 1),
                        end_of_support=date(2027, 1, 1))
        self.assertEqual(r.effective_end_of_life, date(2027, 1, 1))

    def test_only_one_published_is_still_an_answer(self):
        """Vendors publish the two at different times; the known one stands."""
        self.assertEqual(
            self.record('c', end_of_support=date(2027, 1, 1)).effective_end_of_life,
            date(2027, 1, 1))
        self.assertEqual(
            self.record('d', end_of_security_support=date(2028, 1, 1)).effective_end_of_life,
            date(2028, 1, 1))

    def test_neither_published_is_no_answer(self):
        self.assertIsNone(self.record('e').effective_end_of_life)

    def test_status_follows_the_binding_date(self):
        """A model whose security support has passed is end-of-support, even
        with years of contract left. This is the visible consequence."""
        past = date.today() - timedelta(days=1)
        r = self.record('f', end_of_security_support=past,
                        end_of_support=date.today() + timedelta(days=3650))
        self.assertEqual(r.status, LifecycleStatusChoices.STATUS_END_OF_SUPPORT)

    def test_a_model_still_getting_security_fixes_is_not_end_of_support(self):
        future = date.today() + timedelta(days=365)
        r = self.record('g', end_of_security_support=future, end_of_support=future)
        self.assertNotEqual(r.status, LifecycleStatusChoices.STATUS_END_OF_SUPPORT)


class TheSqlFormAgreesWithThePythonForm(EffectiveEndOfLifeTest):
    """The report filters in SQL and the page renders from the property. If
    they disagree, the report shows one date and the record shows another."""

    def annotated(self, pk):
        return (ModelLifecycle.objects
                .annotate(eol=effective_end_of_life_expression())
                .get(pk=pk).eol)

    def test_they_match_for_every_combination(self):
        cases = {
            'sec-first': dict(end_of_security_support=date(2027, 1, 1),
                              end_of_support=date(2030, 1, 1)),
            'sup-first': dict(end_of_security_support=date(2030, 1, 1),
                              end_of_support=date(2027, 1, 1)),
            'sec-only': dict(end_of_security_support=date(2028, 1, 1)),
            'sup-only': dict(end_of_support=date(2029, 1, 1)),
            'neither': {},
        }
        for slug, dates in cases.items():
            record = self.record(slug, **dates)
            self.assertEqual(self.annotated(record.pk), record.effective_end_of_life,
                             f'{slug}: SQL and Python disagree')

    def test_a_partly_published_model_is_not_hidden_by_the_sql(self):
        """LEAST returns NULL when any argument is NULL on MySQL, Oracle and
        SQLite. On those this would drop every model with only one date
        published — silently, from a report about what to replace."""
        record = self.record('partial', end_of_support=date(2029, 1, 1))
        self.assertEqual(self.annotated(record.pk), date(2029, 1, 1))


class TheAnnotationMustNotShadowTheProperty(EffectiveEndOfLifeTest):
    """A same-named annotation is fine until a query returns a row.

    Django assigns each annotation onto the instance it loads, and a read-only
    property has no setter — so this failed only with data present, which is
    to say only in production. The empty-queryset check passed happily.
    """

    def test_a_query_that_returns_rows_still_works(self):
        from netbox_refresh.models import EFFECTIVE_EOL_ALIAS

        self.record('shadow', end_of_support=date(2029, 1, 1))
        rows = list(ModelLifecycle.objects.annotate(
            **{EFFECTIVE_EOL_ALIAS: effective_end_of_life_expression()}))
        self.assertEqual(len(rows), 1)
        self.assertEqual(getattr(rows[0], EFFECTIVE_EOL_ALIAS), date(2029, 1, 1))
        # And the property is untouched by it.
        self.assertEqual(rows[0].effective_end_of_life, date(2029, 1, 1))

    def test_the_alias_is_not_the_property_name(self):
        from netbox_refresh.models import EFFECTIVE_EOL_ALIAS

        self.assertNotEqual(EFFECTIVE_EOL_ALIAS, 'effective_end_of_life')


class FunctionalEndOfLifePageTest(TestCase):
    def test_edit_preview_and_api_use_the_derived_date(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        manufacturer = Manufacturer.objects.create(name='Cisco', slug='cisco')
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model='C9300', slug='c9300')
        self.client.force_login(get_user_model().objects.create_superuser('lifecycle-editor', password='test'))
        add = self.client.get(reverse('plugins:netbox_refresh:modellifecycle_add'), {'device_type': device_type.pk})
        self.assertContains(add, 'functional-eol.js')
        self.assertTrue(add.context['form'].fields['effective_end_of_life'].disabled)
        self.assertIsNone(add.context['form']['effective_end_of_life'].value())

        lifecycle = ModelLifecycle.objects.create(
            assigned_object=device_type, end_of_support=date(2030, 1, 1),
        )
        edit = self.client.get(reverse('plugins:netbox_refresh:modellifecycle_edit', args=[lifecycle.pk]))
        self.assertContains(edit, 'value="2030-01-01"')
        self.assertContains(edit, 'Functional end of life')

        url = reverse('plugins-api:netbox_refresh-api:modellifecycle-detail', args=[lifecycle.pk])
        update = self.client.patch(url, {
            'end_of_security_support': '2029-01-01', 'effective_end_of_life': '2040-01-01',
        }, content_type='application/json')
        self.assertEqual(update.status_code, 200, update.content)
        self.assertEqual(update.json()['effective_end_of_life'], '2029-01-01')
        self.assertContains(self.client.get(lifecycle.get_absolute_url()), 'Functional end of life')
