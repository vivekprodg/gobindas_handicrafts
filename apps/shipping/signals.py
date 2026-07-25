from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .events import invalidate_shipping_cache
from .models import ShippingMethod, ShippingSettings, ShippingZone, WeightTierRate

@receiver([post_save, post_delete], sender=ShippingSettings)
@receiver([post_save, post_delete], sender=ShippingZone)
@receiver([post_save, post_delete], sender=ShippingMethod)
@receiver([post_save, post_delete], sender=WeightTierRate)
def on_shipping_configuration_changed(sender, instance, **kwargs):
    """
    Invalidates shipping rate caches whenever rules are modified in Admin.
    """
    invalidate_shipping_cache()