from django.urls import path
from netbox.api.routers import NetBoxRouter

from netbox_refresh.api.coverage import (LicenseAssignmentViewSet, LicenseEntitlementViewSet, SupportContractViewSet)
from netbox_refresh.api.views import (
    ReplacementPriceViewSet,
    DeviceSoftwareViewSet,
    ModelLifecycleViewSet,
    SoftwareRecommendationView,
    SoftwareStandardViewSet,
    SoftwareVersionViewSet,
)

from netbox_refresh.api.operations import (
    DeviceCollectionStateViewSet, DeviceObservationViewSet, DeviceInventoryChangeViewSet,
    DeviceRelationshipViewSet, LifecycleActionViewSet, ReadinessView, ActionRefreshView,
)

router = NetBoxRouter()
router.register('collection-health', DeviceCollectionStateViewSet)
router.register('observations', DeviceObservationViewSet)
router.register('inventory-changes', DeviceInventoryChangeViewSet)
router.register('relationships', DeviceRelationshipViewSet)
router.register('actions', LifecycleActionViewSet)
router.register('lifecycles', ModelLifecycleViewSet)
router.register('replacement-prices', ReplacementPriceViewSet)
router.register('software-versions', SoftwareVersionViewSet)
router.register('software-standards', SoftwareStandardViewSet)
router.register('device-software', DeviceSoftwareViewSet)
router.register('license-entitlements', LicenseEntitlementViewSet)
router.register('license-assignments', LicenseAssignmentViewSet)
router.register('support-contracts', SupportContractViewSet)
urlpatterns = [
    path('readiness/', ReadinessView.as_view(), name='readiness'),
    path('actions/refresh/', ActionRefreshView.as_view(), name='action-refresh'),
    path('device-software/recommendation/', SoftwareRecommendationView.as_view(), name='software-recommendation'),
] + router.urls
