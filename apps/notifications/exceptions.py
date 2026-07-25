from typing import Optional
from django.utils.translation import gettext_lazy as _

class NotificationError(Exception):
    """Base exception for notification domain errors."""
    default_code: str = "notification_error"
    default_message: str = _("A notification processing error occurred.")

    def __init__(self, message: Optional[str] = None, code: Optional[str] = None):
        self.message = str(message or self.default_message)
        self.code = str(code or self.default_code)
        super().__init__(self.message)

class NotificationTemplateNotFoundError(NotificationError):
    default_code = "template_not_found"
    default_message = _("The requested notification template could not be found.")

class NotificationDispatchError(NotificationError):
    default_code = "dispatch_error"
    default_message = _("Failed to deliver notification via the selected channel provider.")