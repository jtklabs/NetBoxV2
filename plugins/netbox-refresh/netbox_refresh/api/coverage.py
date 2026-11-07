from netbox.api.serializers import NetBoxModelSerializer
from netbox.api.viewsets import NetBoxModelViewSet
from rest_framework import serializers

from netbox_refresh.coverage import (
    ASSIGNMENT_FIELDS, CONTRACT_FIELDS, ENTITLEMENT_FIELDS,
    LicenseAssignmentFilterSet, LicenseEntitlementFilterSet, SupportContractFilterSet,
)
from netbox_refresh.models import LicenseAssignment, LicenseEntitlement, SupportContract

COMMON_FIELDS = ('url', 'id', 'display', 'description', 'comments', 'tags', 'custom_fields', 'created', 'last_updated')


class LicenseEntitlementSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name='plugins-api:netbox_refresh-api:licenseentitlement-detail')

    class Meta:
        model = LicenseEntitlement
        fields = (*COMMON_FIELDS, *ENTITLEMENT_FIELDS)
        brief_fields = ('url', 'id', 'display', 'product', 'reference')


class LicenseAssignmentSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name='plugins-api:netbox_refresh-api:licenseassignment-detail')

    class Meta:
        model = LicenseAssignment
        fields = (*COMMON_FIELDS, *ASSIGNMENT_FIELDS)
        brief_fields = ('url', 'id', 'display')


class SupportContractSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name='plugins-api:netbox_refresh-api:supportcontract-detail')
    coverage_status = serializers.CharField(read_only=True)

    class Meta:
        model = SupportContract
        fields = (*COMMON_FIELDS, *CONTRACT_FIELDS, 'coverage_status')
        brief_fields = ('url', 'id', 'display', 'provider', 'reference')


class LicenseEntitlementViewSet(NetBoxModelViewSet):
    queryset = LicenseEntitlement.objects.select_related('manufacturer').prefetch_related('tags')
    serializer_class = LicenseEntitlementSerializer
    filterset_class = LicenseEntitlementFilterSet


class LicenseAssignmentViewSet(NetBoxModelViewSet):
    queryset = LicenseAssignment.objects.select_related('device', 'entitlement').prefetch_related('tags')
    serializer_class = LicenseAssignmentSerializer
    filterset_class = LicenseAssignmentFilterSet


class SupportContractViewSet(NetBoxModelViewSet):
    queryset = SupportContract.objects.prefetch_related('devices', 'tags')
    serializer_class = SupportContractSerializer
    filterset_class = SupportContractFilterSet
