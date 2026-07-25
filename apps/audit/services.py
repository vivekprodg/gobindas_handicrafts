import logging
from typing import Any, Dict, Optional

from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from . import constants as c
from .models import AuditLog, SecurityEventLog

logger = logging.getLogger(c.LOGGER_NAME)

class AuditService:
    """
    Central service for creating system audit logs and recording security events.
    """

    @classmethod
    def log_action(
        cls,
        action: str,
        actor: Optional[Any] = None,
        target_object: Optional[Any] = None,
        changes: Optional[Dict[str, Any]] = None,
        severity: str = c.AuditSeverity.INFO,
        ip_address: str = "",
        user_agent: str = "",
    ) -> Optional[AuditLog]:
        try:
            content_type = None
            object_id = None
            target_repr = None

            if target_object and getattr(target_object, "pk", None):
                content_type = ContentType.objects.get_for_model(target_object)
                object_id = str(target_object.pk)
                target_repr = str(target_object)

            log_entry = AuditLog.objects.create(
                actor=actor if actor and getattr(actor, "is_authenticated", False) else None,
                action=action,
                severity=severity,
                content_type=content_type,
                object_id=object_id,
                target_repr=target_repr,
                changes=changes or {},
                ip_address=ip_address,
                user_agent=user_agent,
                timestamp=timezone.now(),
            )
            return log_entry
        except Exception as exc:
            logger.exception("Failed to write audit log entry: %s", exc)
            return None

    @classmethod
    def log_security_event(
        cls,
        event_type: str,
        ip_address: str,
        user: Optional[Any] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Optional[SecurityEventLog]:
        try:
            return SecurityEventLog.objects.create(
                event_type=event_type,
                ip_address=ip_address,
                user=user if user and getattr(user, "is_authenticated", False) else None,
                details=details or {},
            )
        except Exception as exc:
            logger.exception("Failed to log security event: %s", exc)
            return None