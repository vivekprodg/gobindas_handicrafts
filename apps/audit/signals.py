from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from .constants import AuditAction, AuditSeverity
from .services import AuditService

@receiver(user_logged_in)
def on_user_logged_in(sender, request, user, **kwargs):
    ip = request.META.get("REMOTE_ADDR", "") if request else ""
    ua = request.META.get("HTTP_USER_AGENT", "") if request else ""
    AuditService.log_action(
        action=AuditAction.LOGIN,
        actor=user,
        severity=AuditSeverity.INFO,
        ip_address=ip,
        user_agent=ua,
    )

@receiver(user_logged_out)
def on_user_logged_out(sender, request, user, **kwargs):
    ip = request.META.get("REMOTE_ADDR", "") if request else ""
    ua = request.META.get("HTTP_USER_AGENT", "") if request else ""
    AuditService.log_action(
        action=AuditAction.LOGOUT,
        actor=user,
        severity=AuditSeverity.INFO,
        ip_address=ip,
        user_agent=ua,
    )

@receiver(user_login_failed)
def on_user_login_failed(sender, credentials, request, **kwargs):
    ip = request.META.get("REMOTE_ADDR", "") if request else ""
    username = credentials.get("username", "unknown")
    AuditService.log_security_event(
        event_type="FAILED_LOGIN_ATTEMPT",
        ip_address=ip,
        details={"attempted_username": username},
    )