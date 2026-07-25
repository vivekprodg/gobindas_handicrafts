from typing import Any, Dict, Optional

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.foundation.models import CMSBaseModel
from .constants import AuditAction, AuditSeverity

class AuditLog(CMSBaseModel):
    """
    Immutable ledger of model mutations, user security events, and administrative actions.
    """
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="audit_logs",
        verbose_name=_("Actor / User"),
    )
    action = models.CharField(
        max_length=50,
        choices=AuditAction.CHOICES,
        default=AuditAction.UPDATE,
        db_index=True,
        verbose_name=_("Action Performed"),
    )
    severity = models.CharField(
        max_length=20,
        choices=AuditSeverity.CHOICES,
        default=AuditSeverity.INFO,
        db_index=True,
        verbose_name=_("Event Severity"),
    )

    # Generic Foreign Key to Target Object
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        verbose_name=_("Target Content Type"),
    )
    object_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Target Object Primary Key"),
    )
    target_object = GenericForeignKey("content_type", "object_id")
    target_repr = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Target String Representation"),
    )

    changes = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Field Diffs / Change Snapshot"),
        help_text=_("JSON dict mapping field names to {'old': val, 'new': val}."),
    )
    ip_address = models.GenericIPAddressField(
        blank=True,
        null=True,
        verbose_name=_("Client IP Address"),
    )
    user_agent = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Client User Agent"),
    )
    timestamp = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        verbose_name=_("Audit Timestamp"),
    )

    class Meta:
        verbose_name = _("Audit Log Record")
        verbose_name_plural = _("Audit Log Records")
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["action", "-timestamp"]),
            models.Index(fields=["severity", "-timestamp"]),
            models.Index(fields=["actor", "-timestamp"]),
            models.Index(fields=["content_type", "object_id"]),
        ]

    def __str__(self) -> str:
        actor_name = self.actor.username if self.actor else "System"
        target = self.target_repr or f"Object #{self.object_id}"
        return f"[{self.get_severity_display()}] {actor_name} -> {self.get_action_display()} on {target}"

class SecurityEventLog(CMSBaseModel):
    """
    Tracking for brute-force attempts, unauthorized access, and suspicious activities.
    """
    event_type = models.CharField(
        max_length=100,
        db_index=True,
        verbose_name=_("Security Event Type"),
    )
    ip_address = models.GenericIPAddressField(
        db_index=True,
        verbose_name=_("Source IP Address"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="security_events",
        verbose_name=_("Target User Account (if any)"),
    )
    details = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Event Parameters & Headers"),
    )
    is_resolved = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name=_("Resolved by Administrator"),
    )

    class Meta:
        verbose_name = _("Security Event Log")
        verbose_name_plural = _("Security Event Logs")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        status = "Resolved" if self.is_resolved else "Unresolved"
        return f"Security Alert: {self.event_type} from IP {self.ip_address} [{status}]"