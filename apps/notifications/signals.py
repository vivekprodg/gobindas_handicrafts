from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .events import invalidate_notification_cache
from .models import NotificationSetting, NotificationTemplate

@receiver([post_save, post_delete], sender=NotificationTemplate)
def on_template_changed(sender, instance, **kwargs):
    invalidate_notification_cache()

@receiver([post_save, post_delete], sender=NotificationSetting)
def on_notification_setting_changed(sender, instance, **kwargs):
    invalidate_notification_cache()