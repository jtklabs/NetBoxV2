from utilities.choices import ChoiceSet


class LifecycleSourceChoices(ChoiceSet):
    key = 'ModelLifecycle.source'

    SOURCE_MANUAL = 'manual'
    SOURCE_CISCO = 'cisco-eox'

    CHOICES = [
        (SOURCE_MANUAL, 'Manual', 'blue'),
        (SOURCE_CISCO, 'Cisco EoX', 'cyan'),
    ]


class LifecycleStatusChoices(ChoiceSet):
    """Derived status — computed from the dates, never stored.

    Two states look alike on the dates alone and mean opposite things:
    'unknown' is a model nobody has looked into, 'not-announced' is one that
    WAS checked (last_checked, or a Cisco sync) and the vendor had nothing to
    announce. The first is a to-do; the second is good news and a date to
    re-check from.
    """

    STATUS_NOT_ANNOUNCED = 'not-announced'
    STATUS_EOS_ANNOUNCED = 'eos-announced'
    STATUS_END_OF_SALE = 'end-of-sale'
    STATUS_END_OF_SUPPORT = 'end-of-support'
    STATUS_UNKNOWN = 'unknown'

    CHOICES = [
        (STATUS_NOT_ANNOUNCED, 'EoL not announced', 'green'),
        (STATUS_EOS_ANNOUNCED, 'EoL announced', 'cyan'),
        (STATUS_END_OF_SALE, 'Past end of sale', 'orange'),
        (STATUS_END_OF_SUPPORT, 'Past end of support', 'red'),
        (STATUS_UNKNOWN, 'Unknown', 'gray'),
    ]


class SoftwareSourceChoices(ChoiceSet):
    """Where a device's running-version reading came from.

    Provenance is tracked because the readings are not equally trustworthy: a
    hand-typed version is a claim, an SNMP reading is an observation, and the
    report should let you tell them apart.
    """

    key = 'DeviceSoftware.source'

    SOURCE_MANUAL = 'manual'
    SOURCE_SNMP = 'snmp'
    SOURCE_API = 'api'
    SOURCE_IMPORT = 'import'
    SOURCE_DIODE = 'diode'

    CHOICES = [
        (SOURCE_MANUAL, 'Manual entry', 'blue'),
        (SOURCE_SNMP, 'SNMP scan', 'green'),
        (SOURCE_API, 'API', 'cyan'),
        (SOURCE_IMPORT, 'Bulk import', 'gray'),
        (SOURCE_DIODE, 'Diode', 'purple'),
    ]


class ComplianceStatusChoices(ChoiceSet):
    """Derived compliance state — computed, never stored.

    Several states rather than a boolean, because the three ways of *not* being
    compliant are not the same thing and must not be reported as one:

      Unknown      we have never collected a version. Reporting this as
                   compliant is the single most common way these programs end
                   up lying to the people who depend on them.
      No standard  the device runs known code but nobody has defined what it
                   should run. That is a gap in the standards, not in the fleet.
      Exempt       deliberately excluded ("do not upgrade") — still counted and
                   still displayed, never silently dropped from a view.
      Exempt
      (expired)    exempt, but past the date the exemption was to be reviewed.
                   Surfaced separately so exemptions cannot quietly become
                   permanent.
    """

    STATUS_COMPLIANT = 'compliant'
    STATUS_NON_COMPLIANT = 'non-compliant'
    STATUS_EXEMPT = 'exempt'
    STATUS_EXEMPT_EXPIRED = 'exempt-expired'
    STATUS_UNKNOWN = 'unknown'
    STATUS_NO_STANDARD = 'no-standard'
    STATUS_CONFLICT = 'standard-conflict'

    CHOICES = [
        (STATUS_COMPLIANT, 'Compliant', 'green'),
        (STATUS_NON_COMPLIANT, 'Non-compliant', 'red'),
        (STATUS_EXEMPT, 'Do not upgrade', 'purple'),
        (STATUS_EXEMPT_EXPIRED, 'Exemption expired', 'orange'),
        (STATUS_UNKNOWN, 'Unknown', 'gray'),
        (STATUS_NO_STANDARD, 'No standard defined', 'blue'),
        (STATUS_CONFLICT, 'Conflicting tag standards', 'orange'),
    ]


class ChecksumTypeChoices(ChoiceSet):
    """Which digest the stored checksum is.

    Vendors publish different ones for the same image, so the type travels with
    the value rather than us assuming MD5 and being wrong half the time.
    """

    TYPE_MD5 = 'md5'
    TYPE_SHA256 = 'sha256'
    TYPE_SHA512 = 'sha512'

    CHOICES = [
        (TYPE_MD5, 'MD5', 'gray'),
        (TYPE_SHA256, 'SHA-256', 'blue'),
        (TYPE_SHA512, 'SHA-512', 'green'),
    ]
