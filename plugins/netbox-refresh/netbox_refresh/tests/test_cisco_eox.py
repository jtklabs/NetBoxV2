"""The Cisco EoX client and sync, against a fake of Cisco's API.

A fake rather than the live API, on purpose: every test run must not spend
API quota or need a credential, and the interesting behaviours are exactly
the ones a live call cannot be made to produce on demand — a token expiring
mid-run, a 429, a PID list that is too long, a record with a bare-space
migration field. The fake speaks the response shapes from Cisco's published
spec (developer.cisco.com/docs/support-apis/eox/), checked 2026-08-20.
"""

import json
from datetime import date
from unittest import mock
from urllib.parse import unquote

from dcim.models import DeviceType, Manufacturer
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from netbox_refresh import cisco
from netbox_refresh.choices import LifecycleSourceChoices, LifecycleStatusChoices
from netbox_refresh.cisco import (
    BATCH_SIZE, MAX_PID_PATH_CHARS, CiscoEoxClient, CiscoEoxError, batch_product_ids,
)
from netbox_refresh.models import ModelLifecycle


def _d(value):
    """A Cisco date object: {"value": ..., "dateFormat": ...}."""
    return {'value': value, 'dateFormat': 'YYYY-MM-DD'}


def eox_record(pid, **overrides):
    """One EOXRecord as Cisco returns it — padded input, nested dates, noise."""
    record = {
        'EOLProductID': pid,
        'ProductIDDescription': 'Catalyst switch',
        'ProductBulletinNumber': 'EOL%s' % (abs(hash(pid)) % 100000),
        'LinkToProductBulletinURL': 'https://www.cisco.com/c/en/us/products/eos-eol.html',
        'EOXExternalAnnouncementDate': _d('2024-01-31'),
        'EndOfSaleDate': _d('2024-07-31'),
        'EndOfSWMaintenanceReleases': _d('2025-07-31'),
        'EndOfSecurityVulSupportDate': _d('2027-07-31'),
        'EndOfRoutineFailureAnalysisDate': _d('2025-07-31'),
        'EndOfSvcAttachDate': _d('2025-07-31'),
        'EndOfServiceContractRenewal': _d('2029-04-30'),
        'LastDateOfSupport': _d('2029-07-31'),
        'UpdatedTimeStamp': _d('2024-02-01'),
        'EOXMigrationDetails': {
            'PIDActiveFlag': 'Y',
            'MigrationInformation': ' ',
            'MigrationOption': 'Enter PID(s)',
            'MigrationProductId': 'C9300-48P',
            'MigrationProductName': ' ',
            'MigrationStrategy': ' ',
            'MigrationProductInfoURL': ' ',
        },
        'EOXInputType': 'ShowEOXByPids',
        'EOXInputValue': ' %s ' % pid,       # Cisco pads the echo
    }
    record.update(overrides)
    return record


def no_data_record(pid):
    """Cisco's "no EoL data" — HTTP 200 with an error nested in the record."""
    return {
        'EOLProductID': '',
        'EOXInputType': 'ShowEOXByPids',
        'EOXInputValue': pid,
        'EOXError': {
            'ErrorID': 'SSA_ERR_026',
            'ErrorDescription': 'EOX information does not exist for the following product ID(s):',
            'ErrorDataType': 'PRODUCT_ID',
            'ErrorDataValue': pid,
        },
    }


class FakeResponse:
    def __init__(self, status=200, payload=None, text=''):
        self.status_code = status
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else '')

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


class FakeCiscoApi:
    """Just enough of id.cisco.com + apix.cisco.com to drive the client.

    Records every call so tests can assert on what went over the wire:
    the token form body, the Bearer header, the URL the PIDs landed in.
    """

    def __init__(self, records=None, token_lifetime=3600):
        self.records = {k.upper(): v for k, v in (records or {}).items()}
        self.token_lifetime = token_lifetime
        self.token_posts = []
        self.gets = []
        self.tokens_issued = 0
        self.fail_next_get_with = []     # queue of status codes to return first
        self.pages = None                # override: callable(pids, page) -> payload

    # requests.Session surface the client uses
    def post(self, url, data=None, timeout=None):
        self.token_posts.append({'url': url, 'data': dict(data or {})})
        if data.get('client_secret') != 'good-secret':
            return FakeResponse(401, {'error': 'invalid_client'})
        self.tokens_issued += 1
        return FakeResponse(200, {
            'access_token': 'tok-%d' % self.tokens_issued,
            'token_type': 'Bearer',
            'expires_in': self.token_lifetime,
        })

    def get(self, url, headers=None, params=None, timeout=None):
        self.gets.append({'url': url, 'headers': dict(headers or {}), 'params': dict(params or {})})
        if self.fail_next_get_with:
            status = self.fail_next_get_with.pop(0)
            return FakeResponse(status, None, text='failure %s' % status)
        page, pids = self._parse(url)
        if self.pages is not None:
            return FakeResponse(200, self.pages(pids, page))
        out = []
        for pid in pids:
            entry = self.records.get(pid.upper())
            if entry is None:
                out.append(no_data_record(pid))
            elif isinstance(entry, list):
                out.extend(entry)
            else:
                out.append(entry)
        return FakeResponse(200, {
            'PaginationResponseRecord': {'PageIndex': 1, 'LastIndex': 1,
                                         'TotalRecords': len(out), 'PageRecords': len(out)},
            'EOXRecord': out,
        })

    @staticmethod
    def _parse(url):
        tail = url.split('/EOXByProductID/', 1)[1]
        page, pid_segment = tail.split('/', 1)
        return int(page), [unquote(p) for p in pid_segment.split(',')]


def client_with(api):
    return CiscoEoxClient('client-id', 'good-secret', session=api)


class AuthTest(SimpleTestCase):
    def test_token_request_is_client_credentials_in_the_form_body(self):
        api = FakeCiscoApi({'WS-C3850-48P-L': eox_record('WS-C3850-48P-L')})
        client_with(api).fetch(['WS-C3850-48P-L'])
        post = api.token_posts[0]
        self.assertEqual(post['url'], cisco.TOKEN_URL)
        self.assertEqual(post['data'], {'grant_type': 'client_credentials',
                                        'client_id': 'client-id',
                                        'client_secret': 'good-secret'})

    def test_token_is_sent_as_bearer_and_reused_until_it_expires(self):
        api = FakeCiscoApi({'A': eox_record('A'), 'B': eox_record('B')})
        client = client_with(api)
        client.fetch(['A'])
        client.fetch(['B'])
        self.assertEqual(api.tokens_issued, 1, 'a fresh token per call would burn quota')
        self.assertTrue(all(g['headers']['Authorization'] == 'Bearer tok-1' for g in api.gets))
        self.assertEqual(api.gets[0]['params'], {'responseencoding': 'json'})

    def test_a_401_mid_run_refreshes_the_token_and_retries(self):
        api = FakeCiscoApi({'A': eox_record('A')})
        api.fail_next_get_with = [401]
        with mock.patch.object(cisco.time, 'sleep'):
            client_with(api).fetch(['A'])
        self.assertEqual(api.tokens_issued, 2)
        self.assertEqual(api.gets[-1]['headers']['Authorization'], 'Bearer tok-2')

    def test_bad_credentials_are_a_clear_error(self):
        api = FakeCiscoApi()
        with self.assertRaises(CiscoEoxError) as ctx:
            CiscoEoxClient('client-id', 'wrong', session=api).check_auth()
        self.assertIn('HTTP 401', str(ctx.exception))

    def test_check_auth_reports_lifetime_without_looking_anything_up(self):
        api = FakeCiscoApi(token_lifetime=3599)
        seconds = client_with(api).check_auth()
        self.assertGreater(seconds, 3400)
        self.assertEqual(api.gets, [])

    def test_missing_credentials_refuse_to_construct(self):
        with self.assertRaises(CiscoEoxError):
            CiscoEoxClient('', '')


class BatchingTest(SimpleTestCase):
    """Cisco's two limits on one request: 20 PIDs and 250 path characters."""

    def test_twenty_short_pids_fit_one_batch(self):
        pids = ['C%02d' % i for i in range(20)]
        self.assertEqual(list(batch_product_ids(pids)), [pids])

    def test_the_twenty_first_starts_a_new_batch(self):
        pids = ['C%02d' % i for i in range(21)]
        batches = list(batch_product_ids(pids))
        self.assertEqual([len(b) for b in batches], [20, 1])

    def test_real_length_pids_split_on_the_path_limit_before_twenty(self):
        """Twenty 'WS-C3850-48P-L'-sized PIDs are ~300 characters joined."""
        pids = ['WS-C3850-48P-L%02d' % i for i in range(20)]
        batches = list(batch_product_ids(pids))
        self.assertGreater(len(batches), 1)
        for batch in batches:
            self.assertLessEqual(len(cisco._encode_pids(batch)), MAX_PID_PATH_CHARS)
            self.assertLessEqual(len(batch), BATCH_SIZE)
        self.assertEqual([p for b in batches for p in b], pids, 'nothing dropped or reordered')

    def test_encoding_counts_toward_the_limit(self):
        """'/' becomes %2F — three characters where the PID had one."""
        pids = ['A/B/C/D/E/F/G/H/I/J'] * 12      # 19 chars raw, 37 encoded
        batches = list(batch_product_ids(pids))
        for batch in batches:
            self.assertLessEqual(len(cisco._encode_pids(batch)), MAX_PID_PATH_CHARS)

    def test_a_single_oversized_pid_is_still_sent_not_dropped(self):
        pid = 'X' * 300
        self.assertEqual(list(batch_product_ids([pid, 'A'])), [[pid], ['A']])

    def test_fetch_refuses_a_batch_that_breaks_either_limit(self):
        api = FakeCiscoApi()
        client = client_with(api)
        with self.assertRaises(CiscoEoxError):
            client.fetch(['C%02d' % i for i in range(21)])
        with self.assertRaises(CiscoEoxError):
            client.fetch(['WS-C3850-48P-L%02d' % i for i in range(20)])
        self.assertEqual(api.gets, [], 'nothing may go over the wire')


class ResponseParsingTest(SimpleTestCase):
    def fetch(self, records, pids):
        return client_with(FakeCiscoApi(records)).fetch(pids)

    def test_dates_and_bulletin_are_read_from_the_nested_shape(self):
        got = self.fetch({'WS-C3850-48P-L': eox_record('WS-C3850-48P-L')}, ['ws-c3850-48p-l'])
        rec = got['WS-C3850-48P-L']
        self.assertEqual(rec['end_of_sale'], date(2024, 7, 31))
        self.assertEqual(rec['end_of_support'], date(2029, 7, 31))
        self.assertEqual(rec['end_of_security_support'], date(2027, 7, 31))
        self.assertEqual(rec['announcement_date'], date(2024, 1, 31))
        self.assertTrue(rec['bulletin_url'].startswith('https://'))

    def test_the_padded_echo_is_correlated_on_the_stripped_value(self):
        """EOXInputValue comes back as ' PID ' — the key must still match."""
        got = self.fetch({'A': eox_record('A')}, ['A'])
        self.assertIsNotNone(got['A'])

    def test_an_empty_date_string_is_none_not_an_error(self):
        got = self.fetch({'A': eox_record('A', EndOfSecurityVulSupportDate=_d(''))}, ['A'])
        self.assertIsNone(got['A']['end_of_security_support'])
        self.assertEqual(got['A']['end_of_support'], date(2029, 7, 31))

    def test_no_data_error_is_none_not_an_exception(self):
        got = self.fetch({}, ['CURRENT-1'])
        self.assertEqual(got, {'CURRENT-1': None})

    def test_an_unexpected_error_id_raises(self):
        bad = no_data_record('A')
        bad['EOXError']['ErrorID'] = 'SSA_ERR_099'
        with self.assertRaises(CiscoEoxError):
            self.fetch({'A': bad}, ['A'])

    def test_migration_pid_is_trusted_only_when_cisco_says_it_is_a_pid(self):
        trusted = self.fetch({'A': eox_record('A')}, ['A'])['A']
        self.assertEqual(trusted['replacement_pid'], 'C9300-48P')
        freeform = eox_record('B')
        freeform['EOXMigrationDetails'].update({
            'MigrationOption': 'Enter Product Name(s)',
            'MigrationProductId': 'C9300-48P',
        })
        self.assertEqual(self.fetch({'B': freeform}, ['B'])['B']['replacement_pid'], '')
        listed = eox_record('C')
        listed['EOXMigrationDetails']['MigrationProductId'] = 'C9300-48P, C9300-24P'
        self.assertEqual(self.fetch({'C': listed}, ['C'])['C']['replacement_pid'], '')

    def test_several_records_for_one_pid_merge_rather_than_clobber(self):
        first = eox_record('A')
        first['EOXMigrationDetails']['MigrationProductId'] = ' '
        first['EOXMigrationDetails']['MigrationOption'] = ' '
        second = eox_record('A')
        got = self.fetch({'A': [first, second]}, ['A'])['A']
        self.assertEqual(got['replacement_pid'], 'C9300-48P')

    def test_pagination_is_followed(self):
        api = FakeCiscoApi()

        def pages(pids, page):
            record = eox_record('P%d' % page)
            return {
                'PaginationResponseRecord': {'PageIndex': page, 'LastIndex': 3,
                                             'TotalRecords': 3, 'PageRecords': 1},
                'EOXRecord': [record],
            }
        api.pages = pages
        with mock.patch.object(cisco.time, 'sleep'):
            got = client_with(api).fetch(['P1', 'P2', 'P3'])
        self.assertEqual(len(api.gets), 3)
        self.assertTrue(all(got[k] is not None for k in ('P1', 'P2', 'P3')))

    def test_server_errors_are_retried_then_raised(self):
        api = FakeCiscoApi({'A': eox_record('A')})
        api.fail_next_get_with = [500, 429]
        with mock.patch.object(cisco.time, 'sleep'):
            self.assertIsNotNone(client_with(api).fetch(['A'])['A'])
        api.fail_next_get_with = [500] * cisco.MAX_ATTEMPTS
        with mock.patch.object(cisco.time, 'sleep'):
            with self.assertRaises(CiscoEoxError):
                client_with(api).fetch(['A'])


class SyncEndToEndTest(TestCase):
    """The whole sync against the fake: records created, checks stamped,
    manual records left alone, replacements linked."""

    @classmethod
    def setUpTestData(cls):
        cls.mfr = Manufacturer.objects.create(name='Cisco', slug='cisco')
        cls.old = DeviceType.objects.create(manufacturer=cls.mfr, model='WS-C3850-48P-L',
                                            slug='ws-c3850-48p-l', part_number='WS-C3850-48P-L')
        cls.new = DeviceType.objects.create(manufacturer=cls.mfr, model='C9300-48P',
                                            slug='c9300-48p', part_number='C9300-48P')
        cls.manual_dt = DeviceType.objects.create(manufacturer=cls.mfr, model='C2960X',
                                                  slug='c2960x', part_number='WS-C2960X-48FPD-L')
        manual = ModelLifecycle(assigned_object=cls.manual_dt, end_of_support=date(2031, 1, 1),
                                source=LifecycleSourceChoices.SOURCE_MANUAL)
        manual.save()

    def run_sync(self, api, **kwargs):
        from netbox_refresh import sync as sync_module
        with mock.patch.object(sync_module, 'get_credentials', return_value=('id', 'good-secret')), \
             mock.patch.object(sync_module, 'CiscoEoxClient',
                               lambda cid, secret: CiscoEoxClient(cid, secret, session=api)), \
             mock.patch.object(cisco.time, 'sleep'):
            return sync_module.sync(logger_fn=lambda m: None, **kwargs)

    def test_announced_current_and_manual_each_land_correctly(self):
        api = FakeCiscoApi({
            'WS-C3850-48P-L': eox_record('WS-C3850-48P-L'),
            # C9300-48P current: no record -> Cisco says no data
            'WS-C2960X-48FPD-L': eox_record('WS-C2960X-48FPD-L'),
        })
        summary = self.run_sync(api)

        announced = ModelLifecycle.objects.get(assigned_object_id=self.old.pk)
        self.assertEqual(announced.end_of_support, date(2029, 7, 31))
        self.assertEqual(announced.source, LifecycleSourceChoices.SOURCE_CISCO)
        self.assertEqual(announced.last_checked, timezone.localdate())
        self.assertEqual(announced.replacement_device_type, self.new,
                         'the migration PID matched a type we stock')
        # The fixture's end-of-sale (2024-07-31) is already past; last date of
        # support (2029-07-31) is not — so this is "past end of sale".
        self.assertEqual(announced.status, LifecycleStatusChoices.STATUS_END_OF_SALE)

        current = ModelLifecycle.objects.get(assigned_object_id=self.new.pk)
        self.assertIsNone(current.end_of_support)
        self.assertEqual(current.last_checked, timezone.localdate())
        self.assertEqual(current.status, LifecycleStatusChoices.STATUS_NOT_ANNOUNCED)

        manual = ModelLifecycle.objects.get(assigned_object_id=self.manual_dt.pk)
        self.assertEqual(manual.end_of_support, date(2031, 1, 1), 'manual must not be overwritten')
        self.assertIsNone(manual.last_checked)

        self.assertEqual(summary['created'], 1)
        self.assertEqual(summary['not_announced'], 1)
        self.assertEqual(summary['skipped_manual'], 1)
        self.assertEqual(summary['replacements_linked'], 1)

    def test_a_second_run_is_an_update_not_a_duplicate(self):
        api = FakeCiscoApi({'WS-C3850-48P-L': eox_record('WS-C3850-48P-L')})
        self.run_sync(api)
        self.run_sync(api)
        self.assertEqual(ModelLifecycle.objects.filter(assigned_object_id=self.old.pk).count(), 1)

    def test_dry_run_writes_nothing(self):
        api = FakeCiscoApi({'WS-C3850-48P-L': eox_record('WS-C3850-48P-L')})
        self.run_sync(api, dry_run=True)
        self.assertFalse(ModelLifecycle.objects.filter(assigned_object_id=self.old.pk).exists())
        self.assertFalse(ModelLifecycle.objects.filter(assigned_object_id=self.new.pk).exists())

    def test_a_failed_batch_leaves_records_untouched_and_is_counted(self):
        api = FakeCiscoApi({'WS-C3850-48P-L': eox_record('WS-C3850-48P-L')})
        api.fail_next_get_with = [500] * cisco.MAX_ATTEMPTS
        summary = self.run_sync(api)
        self.assertGreater(summary['errors'], 0)
        self.assertFalse(ModelLifecycle.objects.filter(assigned_object_id=self.old.pk).exists())


class SystemJobRegistrationTest(SimpleTestCase):
    """The sync schedules itself only when it could actually run."""

    def registered(self, config):
        from netbox.registry import registry
        from netbox_refresh import _register_eox_system_job
        from netbox_refresh.jobs import CiscoEoxSyncJob
        registry['system_jobs'].pop(CiscoEoxSyncJob, None)
        with self.settings(PLUGINS_CONFIG={'netbox_refresh': config}):
            _register_eox_system_job()
        entry = registry['system_jobs'].pop(CiscoEoxSyncJob, None)
        return entry

    def test_registered_weekly_by_default_when_credentials_exist(self):
        entry = self.registered({'cisco_client_id': 'id', 'cisco_client_secret': 's'})
        self.assertEqual(entry, {'interval': 10080})

    def test_not_registered_without_credentials(self):
        self.assertIsNone(self.registered({'cisco_client_id': '', 'cisco_client_secret': ''}))

    def test_zero_interval_turns_the_schedule_off(self):
        self.assertIsNone(self.registered({'cisco_client_id': 'id', 'cisco_client_secret': 's',
                                           'cisco_sync_interval_minutes': 0}))

    def test_interval_is_configurable(self):
        entry = self.registered({'cisco_client_id': 'id', 'cisco_client_secret': 's',
                                 'cisco_sync_interval_minutes': 1440})
        self.assertEqual(entry['interval'], 1440)


class CandidateSelectionTest(TestCase):
    """Which types the sync looks up. The first live run found none: the
    scanner creates types with the PID in `model` and no part_number, and
    names the manufacturer whatever entPhysicalMfgName said."""

    def setUp(self):
        from netbox_refresh.sync import candidate_types, pid_for
        self.candidate_types = candidate_types
        self.pid_for = pid_for

    def test_a_scanner_created_type_with_no_part_number_is_looked_up_by_model(self):
        mfr = Manufacturer.objects.create(name='Cisco', slug='cisco')
        dt = DeviceType.objects.create(manufacturer=mfr, model='WS-C3650-48PS-L',
                                       slug='ws-c3650-48ps-l')      # no part_number
        self.assertEqual([self.pid_for(o) for o in self.candidate_types()], ['WS-C3650-48PS-L'])
        self.assertEqual(dt.part_number, '')

    def test_part_number_wins_over_model_when_present(self):
        mfr = Manufacturer.objects.create(name='Cisco', slug='cisco')
        DeviceType.objects.create(manufacturer=mfr, model='Catalyst 9300 48-port PoE+',
                                  slug='c9300-48p', part_number='C9300-48P')
        self.assertEqual([self.pid_for(o) for o in self.candidate_types()], ['C9300-48P'])

    def test_manufacturer_spelled_the_entity_mib_way_still_matches(self):
        for name in ('Cisco Systems, Inc.', 'cisco', 'Cisco Systems'):
            mfr = Manufacturer.objects.create(name=name, slug=name.lower().replace(' ', '-').replace(',', '').replace('.', ''))
            DeviceType.objects.create(manufacturer=mfr, model='PID-%s' % mfr.pk, slug='pid-%s' % mfr.pk)
        self.assertEqual(len(self.candidate_types()), 3)

    def test_other_manufacturers_are_left_alone(self):
        mfr = Manufacturer.objects.create(name='Juniper Networks', slug='juniper')
        DeviceType.objects.create(manufacturer=mfr, model='EX4300-48P', slug='ex4300-48p')
        self.assertEqual(self.candidate_types(), [])

    def test_replacement_resolves_by_model_too(self):
        from netbox_refresh.sync import _resolve_replacement
        mfr = Manufacturer.objects.create(name='Cisco', slug='cisco')
        old = DeviceType.objects.create(manufacturer=mfr, model='WS-C3650-48PS-L', slug='old')
        new = DeviceType.objects.create(manufacturer=mfr, model='C9300L-48P-4G-E', slug='new')
        self.assertEqual(_resolve_replacement(old, 'c9300l-48p-4g-e'), new)
