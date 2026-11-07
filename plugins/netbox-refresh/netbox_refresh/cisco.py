"""Cisco EoX API client.

Docs: https://developer.cisco.com/docs/support-apis/eox/

The bits of this API that surprise people, all handled below:
  * Every date is an object -- {"value": "2009-12-28", "dateFormat": "YYYY-MM-DD"} --
    and an unknown date is an empty STRING, not null and not a missing key.
  * "No EoX record" is HTTP 200 with an EOXError nested inside the record.
    Error SSA_ERR_026 also fires for perfectly current hardware that simply has
    no EoL announced yet, so it means "nothing today", not "bad PID".
  * Migration fields are frequently a single space rather than empty, and
    MigrationProductId is only trustworthy when MigrationOption is "Enter PID(s)".
  * Response records echo the input in EOXInputValue, whitespace-padded, and the
    array is not 1:1 with the request (a PID with several migration paths
    returns several records), so correlate on the stripped value.
  * The PID list lives in the URL PATH and the spec caps that segment at 250
    characters as well as 20 PIDs. Twenty real PIDs ("WS-C3850-48P-L" is 14
    characters) blow through 250, so batches are cut on BOTH limits — see
    batch_product_ids(). Verified against the published spec 2026-08-20
    (https://developer.cisco.com/docs/support-apis/eox/): endpoint, the two
    limits, responseencoding, the PaginationResponseRecord/EOXRecord field
    names and the value/dateFormat date shape all match what is used below;
    auth is client_credentials with the id and secret in the form body, token
    lifetime 3600s, sent as "Authorization: Bearer".
"""

import logging
import time
from datetime import datetime
from urllib.parse import quote

import requests

logger = logging.getLogger('netbox.plugins.netbox_refresh')

TOKEN_URL = 'https://id.cisco.com/oauth2/default/v1/token'
EOX_BY_PID_URL = 'https://apix.cisco.com/supporttools/eox/rest/5/EOXByProductID/{page}/{pids}'

# Cisco accepts 20 product IDs per call — and the comma-joined, URL-encoded
# list sits in a path segment the spec limits to 250 characters. Both bind.
BATCH_SIZE = 20
MAX_PID_PATH_CHARS = 250
# Unofficial but consistently reported: 5 calls/sec, 5000/day. Stay under it.
REQUEST_PAUSE_SECONDS = 0.25
MAX_ATTEMPTS = 4

# Error IDs that mean "Cisco has no EoL data for this", which is a normal answer.
NO_DATA_ERRORS = {'SSA_ERR_026', 'SSA_ERR_023', 'SSA_ERR_024', 'SSA_ERR_022',
                  'SSA_ERR_015', 'SSA_ERR_030'}
# Transient server-side failures worth retrying.
RETRY_ERRORS = {'SSA_ERR_011', 'SSA_ERR_012', 'SSA_ERR_013', 'SSA_ERR_014',
                'SSA_ERR_018', 'SSA_ERR_033', 'SSA_ERR_037', 'SSA_GENERIC_ERR'}


class CiscoEoxError(Exception):
    """Raised when a batch could not be retrieved at all."""


def _encode_pids(product_ids):
    # PIDs legitimately contain / = +, and they sit in a path segment.
    return ','.join(quote(pid, safe='') for pid in product_ids)


def batch_product_ids(product_ids):
    """Yield batches that respect both of Cisco's limits on one request.

    At most BATCH_SIZE PIDs, and at most MAX_PID_PATH_CHARS once comma-joined
    and URL-encoded. A single PID that alone exceeds the path limit is yielded
    on its own and left for the API to reject — dropping it silently would
    make a model vanish from the sync with no trace.
    """
    batch = []
    for pid in product_ids:
        candidate = batch + [pid]
        if batch and (len(candidate) > BATCH_SIZE
                      or len(_encode_pids(candidate)) > MAX_PID_PATH_CHARS):
            yield batch
            batch = [pid]
        else:
            batch = candidate
    if batch:
        yield batch


def _clean(value):
    """Cisco pads strings with spaces and uses ' ' as an empty placeholder."""
    if value is None:
        return ''
    return str(value).strip()


def _date(record, key):
    """Pull a date out of the {'value': ..., 'dateFormat': ...} wrapper."""
    field = record.get(key) or {}
    raw = _clean(field.get('value') if isinstance(field, dict) else field)
    if not raw:
        return None
    fmt = _clean(field.get('dateFormat')) if isinstance(field, dict) else ''
    patterns = ['%Y-%m-%d', '%d-%b-%Y', '%Y/%m/%d']
    if fmt.upper().startswith('DD-MMM'):
        patterns.insert(0, '%d-%b-%Y')
    for pattern in patterns:
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    logger.warning('netbox_refresh: unparseable %s date %r', key, raw)
    return None


class CiscoEoxClient:
    def __init__(self, client_id, client_secret, session=None):
        if not client_id or not client_secret:
            raise CiscoEoxError(
                'Cisco API credentials are not configured. Set cisco_client_id and '
                'cisco_client_secret in PLUGINS_CONFIG, or the CISCO_CLIENT_ID and '
                'CISCO_CLIENT_SECRET environment variables.'
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session or requests.Session()
        self._token = None
        self._expires_at = 0.0

    def _get_token(self):
        if self._token and time.time() < self._expires_at:
            return self._token
        try:
            response = self.session.post(
                TOKEN_URL,
                data={
                    'grant_type': 'client_credentials',
                    'client_id': self.client_id,
                    'client_secret': self.client_secret,
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise CiscoEoxError('token request failed: %s' % exc)
        if response.status_code != 200:
            raise CiscoEoxError('token request failed (HTTP %s): %s'
                                % (response.status_code, response.text[:200]))
        payload = response.json()
        self._token = payload.get('access_token')
        if not self._token:
            raise CiscoEoxError('token response contained no access_token')
        self._expires_at = time.time() + float(payload.get('expires_in', 3600)) - 60
        return self._token

    def check_auth(self):
        """Obtain a token and say how long it is good for. Nothing else.

        For validating a new credential pair before a sync (the management
        command's --check-auth): it exercises exactly the OAuth path and no
        EoX lookup, so a failure here is a credential problem, full stop.
        """
        self._token = None
        self._get_token()
        return max(0, int(self._expires_at - time.time()))

    def _request(self, url):
        last_error = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.session.get(
                    url,
                    headers={'Authorization': 'Bearer ' + self._get_token(),
                             'Accept': 'application/json'},
                    params={'responseencoding': 'json'},
                    timeout=60,
                )
            except requests.RequestException as exc:
                last_error = CiscoEoxError(str(exc))
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError:
                        last_error = CiscoEoxError('non-JSON response: %s' % response.text[:200])
                elif response.status_code == 401:
                    self._token = None  # expired mid-run; refresh and retry
                    last_error = CiscoEoxError('unauthorized')
                elif response.status_code in (403, 429) or response.status_code >= 500:
                    # 403 is also how Cisco signals "Developer Over Qps".
                    last_error = CiscoEoxError('HTTP %s: %s'
                                               % (response.status_code, response.text[:200]))
                else:
                    raise CiscoEoxError('HTTP %s: %s'
                                        % (response.status_code, response.text[:200]))
            if attempt < MAX_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))
        raise last_error or CiscoEoxError('request failed')

    def fetch(self, product_ids):
        """Look up up to BATCH_SIZE PIDs. Returns {pid_upper: parsed_or_None}.

        None means Cisco has no EoL data for that PID (which includes current
        hardware with nothing announced yet).
        """
        product_ids = list(product_ids)
        if len(product_ids) > BATCH_SIZE:
            raise CiscoEoxError('fetch() takes at most %d PIDs; use batch_product_ids()'
                                % BATCH_SIZE)
        results = {pid.upper(): None for pid in product_ids}
        encoded = _encode_pids(product_ids)
        if len(encoded) > MAX_PID_PATH_CHARS and len(product_ids) > 1:
            raise CiscoEoxError('PID list is %d characters; Cisco allows %d per request. '
                                'Use batch_product_ids().' % (len(encoded), MAX_PID_PATH_CHARS))

        page = 1
        while True:
            payload = self._request(EOX_BY_PID_URL.format(page=page, pids=encoded))
            for record in payload.get('EOXRecord') or []:
                if not isinstance(record, dict):
                    continue
                parsed = self._parse_record(record)
                if parsed is None:
                    continue
                key = parsed.pop('_input')
                # Several records can come back for one PID (multiple migration
                # paths). Keep the first with real dates; merge in a migration
                # target if a later record supplies one.
                existing = results.get(key)
                if existing is None:
                    results[key] = parsed
                elif not existing.get('replacement_pid') and parsed.get('replacement_pid'):
                    existing['replacement_pid'] = parsed['replacement_pid']
                    existing['replacement_notes'] = (
                        existing.get('replacement_notes') or parsed.get('replacement_notes')
                    )
            pagination = payload.get('PaginationResponseRecord') or {}
            try:
                last = int(pagination.get('LastIndex') or 1)
            except (TypeError, ValueError):
                last = 1
            if page >= last:
                break
            page += 1
            time.sleep(REQUEST_PAUSE_SECONDS)
        return results

    @staticmethod
    def _parse_record(record):
        key = _clean(record.get('EOXInputValue')).upper()
        if not key:
            key = _clean(record.get('EOLProductID')).upper()
        if not key:
            return None

        error = record.get('EOXError') or {}
        if isinstance(error, list):
            error = error[0] if error else {}
        error_id = _clean(error.get('ErrorID'))
        if error_id:
            if error_id in NO_DATA_ERRORS:
                return None  # normal: nothing announced (or unknown PID)
            raise CiscoEoxError('%s: %s' % (error_id, _clean(error.get('ErrorDescription'))))

        migration = record.get('EOXMigrationDetails') or {}
        option = _clean(migration.get('MigrationOption'))
        migration_pid = _clean(migration.get('MigrationProductId'))
        # Only trust the PID when Cisco says it supplied one, and when it is a
        # single token — the field sometimes holds a list or free text.
        if option.lower() != 'enter pid(s)' or ',' in migration_pid or ' ' in migration_pid:
            migration_pid = ''

        notes = ' '.join(
            part for part in (
                _clean(migration.get('MigrationStrategy')),
                _clean(migration.get('MigrationInformation')),
                _clean(migration.get('MigrationProductName')),
            ) if part
        )

        return {
            '_input': key,
            'eol_product_id': _clean(record.get('EOLProductID')),
            'announcement_date': _date(record, 'EOXExternalAnnouncementDate'),
            'end_of_sale': _date(record, 'EndOfSaleDate'),
            'end_of_sw_maintenance': _date(record, 'EndOfSWMaintenanceReleases'),
            'end_of_security_support': _date(record, 'EndOfSecurityVulSupportDate'),
            'end_of_routine_failure_analysis': _date(record, 'EndOfRoutineFailureAnalysisDate'),
            'end_of_service_attach': _date(record, 'EndOfSvcAttachDate'),
            'end_of_service_contract_renewal': _date(record, 'EndOfServiceContractRenewal'),
            'end_of_support': _date(record, 'LastDateOfSupport'),
            'bulletin_number': _clean(record.get('ProductBulletinNumber'))[:50],
            'bulletin_url': _clean(record.get('LinkToProductBulletinURL'))[:500],
            'replacement_pid': migration_pid,
            'replacement_notes': notes,
        }
