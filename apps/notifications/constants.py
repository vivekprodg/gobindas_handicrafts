from typing import Final, Tuple

class NotificationChannel:
    EMAIL: Final[str] = "email"
    SMS: Final[str] = "sms"
    PUSH: Final[str] = "push"
    IN_APP: Final[str] = "in_app"

    CHOICES: Final[Tuple[Tuple[str, str], ...]] = (
        (EMAIL, "Email Dispatch"),
        (SMS, "SMS Mobile Text"),
        (PUSH, "Web Push Notification"),
        (IN_APP, "In-App Customer Alert"),
    )

class NotificationStatus:
    QUEUED: Final[str] = "queued"
    SENT: Final[str] = "sent"
    FAILED: Final[str] = "failed"
    READ: Final[str] = "read"

    CHOICES: Final[Tuple[Tuple[str, str], ...]] = (
        (QUEUED, "Queued in Outbox"),
        (SENT, "Dispatched / Sent"),
        (FAILED, "Delivery Failed"),
        (READ, "Read by Recipient"),
    )

LOGGER_NAME: Final[str] = "apps.notifications"
CACHE_NAMESPACE: Final[str] = "notifications"
CACHE_KEY_TEMPLATES: Final[str] = "{ns}:templates_dict:v1"
CACHE_KEY_NOTIFICATION_SETTING: Final[str] = "{ns}:notification_setting:v1"
CACHE_TIMEOUT_NOTIFICATIONS: Final[int] = 3600  # 60 Minutes