from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from netbox.plugins import PluginTemplateExtension

from netbox_refresh.models import (
    DeviceSoftware, LicenseAssignment, ModelLifecycle, SoftwareStandard, SupportContract,
)

PLUGIN_SETTINGS = settings.PLUGINS_CONFIG.get('netbox_refresh', {})


class BaseLifecycleCard(PluginTemplateExtension):
    """Show the lifecycle record for whichever hardware model the page is about."""

    def _lifecycle_for(self, obj):
        if obj is None:
            return None
        content_type = ContentType.objects.get_for_model(obj)
        return ModelLifecycle.objects.filter(
            assigned_object_type=content_type, assigned_object_id=obj.pk
        ).first()

    def _target(self):
        return self.context.get('object')

    def _render(self):
        return self.render(
            'netbox_refresh/inc/lifecycle_card.html',
            extra_context={'lifecycle': self._lifecycle_for(self._target())},
        )

    def right_page(self):
        if PLUGIN_SETTINGS.get('lifecycle_card_position', 'right_page') == 'right_page':
            return self._render()
        return ''

    def left_page(self):
        if PLUGIN_SETTINGS.get('lifecycle_card_position') == 'left_page':
            return self._render()
        return ''


class DeviceTypeLifecycleCard(BaseLifecycleCard):
    models = ['dcim.devicetype']


class ModuleTypeLifecycleCard(BaseLifecycleCard):
    models = ['dcim.moduletype']


class DeviceLifecycleCard(BaseLifecycleCard):
    """On a device page the relevant lifecycle is its device type's."""

    models = ['dcim.device']

    def _target(self):
        device = self.context.get('object')
        return device.device_type if device else None


class ModuleLifecycleCard(BaseLifecycleCard):
    models = ['dcim.module']

    def _target(self):
        module = self.context.get('object')
        return module.module_type if module else None


class DeviceSoftwareCard(PluginTemplateExtension):
    """Running software and compliance, on the device page itself.

    This is where most people will actually meet the feature — nobody opens a
    compliance report to ask about one device. An exempt device says so here
    rather than showing nothing.
    """

    models = ['dcim.device']

    def full_width_page(self):
        device = self.context.get('object')
        if device is None:
            return ''
        record = DeviceSoftware.objects.filter(device=device).select_related(
            'software_version', 'software_version__platform'
        ).first()
        return self.render(
            'netbox_refresh/inc/device_software_card.html',
            extra_context={'software': record, 'device': device},
        )


class BaseStandardCard(PluginTemplateExtension):
    """The software standard in force for whatever this page is about."""

    def _scope(self):
        return self.context.get('object')

    def _standard_for(self, obj):
        if obj is None:
            return None
        from netbox_refresh.compliance import active_standards

        field = 'device_types' if obj._meta.model_name == 'devicetype' else 'platforms'
        return active_standards().filter(
            **{field: obj.pk}
        ).prefetch_related('approved_versions').first()

    def right_page(self):
        scope = self._scope()
        return self.render(
            'netbox_refresh/inc/software_standard_card.html',
            extra_context={'standard': self._standard_for(scope), 'scope': scope},
        )


class ReplacementPricingCard(PluginTemplateExtension):
    """On the page of a model that REPLACES something: where it will be needed.

    Jason's flow, verbatim: "if they have a 3560 switch and the replacement
    is a 9350, when you go to the 9350 you would see all the sites that had
    the 3560." This card is that view's front door — feeder models, the unit
    count, how many prices are recorded, and the worksheet link.
    """

    models = ['dcim.devicetype']

    def right_page(self):
        from dcim.models import Device

        device_type = self.context.get('object')
        if device_type is None:
            return ''
        feeders = list(ModelLifecycle.objects.filter(
            replacement_device_type=device_type
        ).prefetch_related('assigned_object_type'))
        if not feeders:
            # This model replaces nothing; a permanently-empty card would just
            # be noise on every ordinary device type page.
            return ''
        feeder_type_ids = [f.assigned_object_id for f in feeders
                           if f.assigned_object_type.model == 'devicetype']
        units = Device.objects.filter(device_type_id__in=feeder_type_ids).count()
        sites = (Device.objects.filter(device_type_id__in=feeder_type_ids)
                 .values('site_id').distinct().count())
        prices = device_type.replacement_prices.count()
        return self.render(
            'netbox_refresh/inc/replacement_pricing_card.html',
            extra_context={
                'device_type': device_type,
                'feeders': feeders,
                'units': units,
                'sites': sites,
                'prices': prices,
            },
        )


class DeviceTypeStandardCard(BaseStandardCard):
    models = ['dcim.devicetype']


class PlatformStandardCard(BaseStandardCard):
    models = ['dcim.platform']


class DeviceCoverageCard(PluginTemplateExtension):
    models = ['dcim.device']

    def right_page(self):
        device = self.context['object']
        user = self.context['request'].user
        show_licenses = user.has_perm('netbox_refresh.view_licenseassignment')
        show_contracts = user.has_perm('netbox_refresh.view_supportcontract')
        if not (show_licenses or show_contracts):
            return ''
        return self.render(
            'netbox_refresh/inc/device_coverage.html',
            extra_context={
                'show_licenses': show_licenses,
                'show_contracts': show_contracts,
                'assignments': LicenseAssignment.objects.restrict(user, 'view').filter(
                    device=device
                ).select_related('entitlement', 'device'),
                'contracts': SupportContract.objects.restrict(user, 'view').filter(devices=device),
            },
        )


template_extensions = (
    DeviceTypeLifecycleCard,
    ModuleTypeLifecycleCard,
    DeviceLifecycleCard,
    ModuleLifecycleCard,
    DeviceSoftwareCard,
    DeviceCoverageCard,
    DeviceTypeStandardCard,
    PlatformStandardCard,
    ReplacementPricingCard,
)
