from typing import Optional
from django.utils.translation import gettext_lazy as _

class AuditError(Exception):
    """Base exception for audit domain errors."""
    default_code: str = "audit_error"
    default_message: str = _("An audit logging error occurred.")

    def __init__(self, message: Optional[str] = None, code: Optional[str] = None):
        self.message = str(message or self.default_message)
        self.code = str(code or self.default_code)
        super().__init__(self.message)

class AuditRecordNotFoundError(AuditError):
    default_code = "audit_record_not_found"
    default_message = _("The requested audit log entry could not be found.")

class AuditLogExportError(AuditError):
    default_code = "audit_export_error"
    default_message = _("Failed to generate audit log export archive.")