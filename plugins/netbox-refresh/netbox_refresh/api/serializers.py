from django.contrib.contenttypes.models import ContentType
from netbox.api.fields import ContentTypeField
from netbox.api.serializers import NetBoxModelSerializer
from rest_framework import serializers
from utilities.api import get_serializer_for_model

from netbox_refresh.choices import SoftwareSourceChoices
from netbox_refresh.models import (
    SOFTWARE_LIFECYCLE_FIELDS,
    DeviceSoftware,
    ModelLifecycle,
    ReplacementPrice,
    SoftwareStandard,
    SoftwareVersion,
)

__all__ = (
    'ModelLifecycleSerializer',
    'ReplacementPriceSerializer',
    'SoftwareVersionSerializer',
    'SoftwareStandardSerializer',
    'DeviceSoftwareSerializer',
    'SoftwareReportSerializer',
)


class ModelLifecycleSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_refresh-api:modellifecycle-detail'
    )
    assigned_object_type = ContentTypeField(queryset=ContentType.objects.all())
    assigned_object = serializers.SerializerMethodField(read_only=True)
    replacement = serializers.SerializerMethodField(read_only=True)
    installed_count = serializers.IntegerField(read_only=True)
    extended_cost = serializers.SerializerMethodField(read_only=True)
    status = serializers.CharField(read_only=True)
    effective_end_of_life = serializers.DateField(read_only=True)

    class Meta:
        model = ModelLifecycle
        fields = (
            'url', 'id', 'display',
            'assigned_object_type', 'assigned_object_id', 'assigned_object',
            'announcement_date', 'end_of_sale', 'end_of_sw_maintenance',
            'end_of_security_support', 'end_of_routine_failure_analysis',
            'end_of_service_attach', 'end_of_service_contract_renewal', 'end_of_support',
            'effective_end_of_life',
            'status', 'bulletin_number', 'bulletin_url',
            'replacement_device_type', 'replacement_module_type', 'replacement',
            'replacement_notes', 'replacement_cost', 'currency', 'cost_updated',
            'installed_count', 'extended_cost',
            'source', 'last_synced', 'last_checked',
            'description', 'comments', 'tags', 'custom_fields', 'created', 'last_updated',
        )
        brief_fields = ('url', 'id', 'display', 'assigned_object', 'end_of_support', 'status')

    def _nested(self, obj):
        if obj is None:
            return None
        serializer = get_serializer_for_model(obj)
        return serializer(obj, nested=True, context=self.context).data

    def get_assigned_object(self, obj):
        return self._nested(obj.assigned_object)

    def get_replacement(self, obj):
        return self._nested(obj.replacement)

    def get_extended_cost(self, obj):
        value = obj.extended_cost
        return str(value) if value is not None else None


class ReplacementPriceSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_refresh-api:replacementprice-detail'
    )

    class Meta:
        model = ReplacementPrice
        fields = (
            'url', 'id', 'display', 'device_type', 'module_type', 'region', 'site',
            'cost', 'currency', 'cost_updated',
            'description', 'comments', 'tags', 'custom_fields',
            'created', 'last_updated',
        )
        brief_fields = ('url', 'id', 'display', 'device_type', 'module_type',
                        'cost', 'currency')

    def validate(self, data):
        data = super().validate(data)
        # Mirrors the model clean(): exactly one hardware model, exactly one
        # scope. Serializer-level so an API writer gets a field-level message
        # rather than a 500 from the DB check constraints.
        instance = self.instance
        device_type = data.get('device_type', instance.device_type if instance else None)
        module_type = data.get('module_type', instance.module_type if instance else None)
        if bool(device_type) == bool(module_type):
            raise serializers.ValidationError(
                'Price a device type or a module type — exactly one.'
            )
        region = data.get('region', instance.region if instance else None)
        site = data.get('site', instance.site if instance else None)
        if bool(region) == bool(site):
            raise serializers.ValidationError(
                'Scope the price to a region or a site — exactly one.'
            )
        return data


class SoftwareVersionSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_refresh-api:softwareversion-detail'
    )
    download_url = serializers.CharField(read_only=True)
    installed_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = SoftwareVersion
        fields = (
            'url', 'id', 'display', 'platform', 'version', 'release_date', *SOFTWARE_LIFECYCLE_FIELDS,
            'image_filename', 'image_url', 'image_size',
            'checksum_type', 'checksum', 'download_url', 'installed_count',
            'description', 'comments', 'tags', 'custom_fields', 'created', 'last_updated',
        )
        brief_fields = ('url', 'id', 'display', 'version', 'platform')


class SoftwareStandardSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_refresh-api:softwarestandard-detail'
    )
    is_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = SoftwareStandard
        fields = (
            'url', 'id', 'display',
            'device_types', 'platforms',
            'approved_versions', 'preferred_version', 'valid_from', 'valid_to',
            'is_active', 'description', 'comments', 'tags', 'custom_fields',
            'created', 'last_updated',
        )
        brief_fields = ('url', 'id', 'display', 'device_types', 'platforms',
                        'valid_from', 'valid_to')

    def validate(self, data):
        data = super().validate(data)
        # Mirrors SoftwareStandardForm.clean(): the model cannot check scope
        # rules before its M2M rows exist, and API writers deserve the same
        # protection as the UI.
        instance = self.instance
        device_types = data.get(
            'device_types',
            list(instance.device_types.all()) if instance else [],
        )
        platforms = data.get(
            'platforms',
            list(instance.platforms.all()) if instance else [],
        )
        if not device_types and not platforms:
            raise serializers.ValidationError(
                'A standard must cover at least one device type or platform.'
            )
        valid_from = data.get('valid_from', instance.valid_from if instance else None)
        valid_to = data.get('valid_to', instance.valid_to if instance else None)
        if valid_from:
            conflict = SoftwareStandard.conflicting_standards(
                device_types, platforms, valid_from, valid_to,
                exclude_pk=instance.pk if instance else None,
            ).first()
            if conflict:
                raise serializers.ValidationError(
                    'This overlaps an existing standard sharing part of its '
                    'scope (%s). Close that one out with an end date first.' % conflict
                )
        return data


class DeviceSoftwareSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_refresh-api:devicesoftware-detail'
    )
    # Derived, read-only: compliance is computed from the standard in force, so
    # writing it would be meaningless. Exposed here so a caller can read the
    # verdict without re-implementing the rules.
    compliance_status = serializers.CharField(read_only=True)
    is_stale = serializers.BooleanField(read_only=True)
    as_of = serializers.DateTimeField(read_only=True)

    class Meta:
        model = DeviceSoftware
        fields = (
            'url', 'id', 'display', 'device', 'software_version', 'raw_version',
            'raw_report', 'source', 'collected_at', 'last_checked', 'as_of',
            'compliance_status', 'is_stale',
            'exempt', 'exempt_reason', 'exempt_approved_by', 'exempt_approved_on',
            'exempt_review_by',
            'description', 'comments', 'tags', 'custom_fields', 'created', 'last_updated',
        )
        brief_fields = ('url', 'id', 'display', 'device', 'software_version', 'compliance_status')


class SoftwareReportSerializer(serializers.Serializer):
    """One running-version reading pushed in by a collector.

    Deliberately not a ModelSerializer: collectors identify devices and
    platforms by name, send the version as a raw string, and should not have to
    know about SoftwareVersion primary keys or create catalogue rows themselves.
    """

    device = serializers.IntegerField(
        required=False, help_text='NetBox device ID'
    )
    device_name = serializers.CharField(
        required=False, allow_blank=True, help_text='Used when no device ID is given'
    )
    version = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, max_length=100,
        help_text='Raw version string. Omit or send "" when nothing was collected — '
                  'never a placeholder, which would be stored as a real version.',
    )
    platform = serializers.CharField(
        required=False, allow_blank=True,
        help_text='Platform name or slug; defaults to the device platform',
    )
    source = serializers.ChoiceField(
        choices=SoftwareSourceChoices, default=SoftwareSourceChoices.SOURCE_API
    )
    collected_at = serializers.DateTimeField(
        required=False, help_text='When the reading was taken at the device'
    )
    raw = serializers.CharField(
        required=False, allow_blank=True,
        help_text='Verbatim collector output the version came from, e.g. sysDescr',
    )

    def validate(self, data):
        if not data.get('device') and not data.get('device_name'):
            raise serializers.ValidationError(
                'Provide either device (ID) or device_name.'
            )
        return data
