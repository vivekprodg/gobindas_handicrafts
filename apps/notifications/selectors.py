from typing import Any, List, Optional

from django.core.cache import cache
from django.db.models import QuerySet

from . import constants as c
from .models import NotificationLog, NotificationPreference, NotificationSetting, NotificationTemplate

def get_active_notification_setting(use_cache: bool = True) -> NotificationSetting:
    """
    Fetches the active global NotificationSetting instance from cache or database.
    If no record exists, creates a default one.
    """
    key = c.CACHE_KEY_NOTIFICATION_SETTING.format(ns=c.CACHE_NAMESPACE)
    if use_cache:
        setting = cache.get(key)
        if setting is not None:
            return setting

    setting = NotificationSetting.objects.first()
    if setting is None:
        setting = NotificationSetting.objects.create(
            name="Global Notification Configuration",
            is_active=True,
            provider=NotificationSetting.ProviderChoices.CUSTOM_SMTP,
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            encryption=NotificationSetting.EncryptionChoices.STARTTLS,
            default_from_email="noreply@gobindashandicrafts.com",
            default_sender_name="Gobindas Handicrafts",
            company_notification_email="admin@gobindashandicrafts.com",
        )

    if use_cache:
        cache.set(key, setting, c.CACHE_TIMEOUT_NOTIFICATIONS)

    return setting

def get_template_by_code(code: str, channel: str = c.NotificationChannel.EMAIL) -> Optional[NotificationTemplate]:
    """
    Looks up active template matching trigger code and channel.
    """
    if not code:
        return None

    clean_code = str(code).strip().lower()
    return NotificationTemplate.objects.filter(
        code=clean_code,
        channel=channel,
        is_active=True,
    ).first()

def get_user_notification_logs(user: Any, channel: Optional[str] = None) -> QuerySet[NotificationLog]:
    if not user or not getattr(user, "is_authenticated", False):
        return NotificationLog.objects.none()

    qs = NotificationLog.objects.filter(user=user)
    if channel:
        qs = qs.filter(channel=channel)
    return qs.order_by("-created_at")

def get_user_preferences(user: Any) -> NotificationPreference:
    if not user or not getattr(user, "is_authenticated", False):
        return NotificationPreference()

    pref, _ = NotificationPreference.objects.get_or_create(user=user)
    return pref