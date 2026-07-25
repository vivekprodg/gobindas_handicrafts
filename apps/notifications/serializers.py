from typing import Any, Dict, List
from .models import NotificationLog, NotificationTemplate

class NotificationSerializer:
    @staticmethod
    def serialize_template(template: NotificationTemplate) -> Dict[str, Any]:
        if not template:
            return {}

        return {
            "id": template.pk,
            "code": template.code,
            "title": template.title,
            "channel": template.channel,
            "subject_template": template.subject_template or "",
            "is_active": template.is_active,
        }

    @staticmethod
    def serialize_log(log: NotificationLog) -> Dict[str, Any]:
        if not log:
            return {}

        return {
            "id": log.pk,
            "recipient": log.recipient,
            "channel": log.channel,
            "channel_display": log.get_channel_display(),
            "subject": log.subject or "",
            "status": log.status,
            "status_display": log.get_status_display(),
            "sent_at": log.sent_at.isoformat() if log.sent_at else None,
            "created_at": log.created_at.isoformat(),
        }

    @classmethod
    def serialize_logs_many(cls, logs: List[NotificationLog]) -> List[Dict[str, Any]]:
        return [cls.serialize_log(log) for log in logs if log]