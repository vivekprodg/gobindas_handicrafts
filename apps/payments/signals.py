from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .events import invalidate_payments_cache
from .models import PaymentSettings

@receiver([post_save, post_delete], sender=PaymentSettings)
def on_payment_settings_changed(sender, instance, **kwargs):
    invalidate_payments_cache()