from typing import Any, Dict, List
from .models import AuditLog, SecurityEventLog

class AuditSerializer:
    @staticmethod
    def serialize_log(log: AuditLog) -> Dict[str, Any]:
        if not log:
            return {}

        return {
            "id": log.pk,
            "actor": log.actor.username if log.actor else "System",
            "action": log.action,
            "action_display": log.get_action_display(),
            "severity": log.severity,
            "severity_display": log.get_severity_display(),
            "target_repr": log.target_repr or "",
            "changes": log.changes or {},
            "ip_address": log.ip_address or "",
            "timestamp": log.timestamp.isoformat() if log.timestamp else "",
        }

    @classmethod
    def serialize_logs_many(cls, logs: List[AuditLog]) -> List[Dict[str, Any]]:
        return [cls.serialize_log(l) for l in logs if l]