from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .events import invalidate_tax_cache
from .models import TaxClass, TaxRule, TaxSettings, TaxZone

@receiver([post_save, post_delete], sender=TaxSettings)
@receiver([post_save, post_delete], sender=TaxClass)
@receiver([post_save, post_delete], sender=TaxZone)
@receiver([post_save, post_delete], sender=TaxRule)
def on_tax_configuration_changed(sender, instance, **kwargs):
    """
    Invalidates tax caches whenever rules or settings are mutated.
    """
    invalidate_tax_cache()