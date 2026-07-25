from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.notifications"
    verbose_name = _("Multi-Channel Notification & Alert Engine")

    def ready(self) -> None:
        try:
            from . import signals  # noqa: F401
        except ImportError:
            pass