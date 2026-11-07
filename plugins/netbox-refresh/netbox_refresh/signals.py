"""Protect scope conditions from disappearing when their objects are deleted."""

from dcim.models import DeviceType, Platform
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from extras.models import Tag


@receiver(pre_delete, sender=Tag)
@receiver(pre_delete, sender=DeviceType)
@receiver(pre_delete, sender=Platform)
def protect_combined_software_scope(sender, instance, using, **kwargs):
    if sender is Tag:
        standards = instance.scoped_software_standards.using(using).filter(
            Q(device_types__isnull=False) | Q(platforms__isnull=False),
        )
    else:
        standards = instance.software_standards.using(using).filter(device_tags__isnull=False)
    if standard := standards.first():
        raise ProtectedError(
            'Remove or replace this scope condition on the software standard before deleting it.',
            [standard],
        )
