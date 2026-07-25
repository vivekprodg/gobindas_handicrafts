import logging
from typing import Any, Dict, Optional, Tuple

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template import Context, Template
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.foundation.models import SiteSettings
from . import constants as c
from .exceptions import NotificationDispatchError, NotificationTemplateNotFoundError
from .models import NotificationLog, NotificationPreference, NotificationSetting, NotificationTemplate
from .selectors import get_active_notification_setting, get_template_by_code, get_user_preferences

logger = logging.getLogger(c.LOGGER_NAME)

class EmailNotificationService:
    """
    Renders and dispatches HTML & plain-text email alerts.
    Dynamically loads custom SMTP connection parameters and sender credentials from NotificationSetting.
    """

    @classmethod
    def get_email_credentials(cls) -> Tuple[str, str, NotificationSetting]:
        """
        Fetches dynamic sender email address, company notification email, and active
        NotificationSetting model instance.
        """
        setting = get_active_notification_setting(use_cache=True)

        sender_email = setting.default_from_email or getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@gobindashandicrafts.com")
        company_email = setting.company_notification_email or getattr(settings, "COMPANY_ADMIN_EMAIL", "admin@gobindashandicrafts.com")
        sender_name = setting.default_sender_name or "Gobindas Handicrafts"

        # Check SiteSettings CMS overrides if applicable
        try:
            site_settings = SiteSettings.objects.first()
            if site_settings:
                if site_settings.sender_email_address:
                    sender_email = site_settings.sender_email_address
                if site_settings.company_notification_email:
                    company_email = site_settings.company_notification_email
                if site_settings.sender_display_name:
                    sender_name = site_settings.sender_display_name
        except Exception as exc:
            logger.warning("Could not check SiteSettings fallback for email credentials: %s", exc)

        formatted_from_email = f"{sender_name} <{sender_email}>" if sender_name else sender_email
        return formatted_from_email, company_email, setting

    @classmethod
    def send_email(
        cls,
        recipient_email: str,
        subject: str,
        body_html: str,
        body_text: str = "",
        user: Optional[Any] = None,
        template: Optional[NotificationTemplate] = None,
        context_data: Optional[Dict[str, Any]] = None,
    ) -> NotificationLog:
        from_email, _, setting = cls.get_email_credentials()

        log = NotificationLog.objects.create(
            recipient=recipient_email,
            user=user,
            channel=c.NotificationChannel.EMAIL,
            template=template,
            subject=subject,
            body=body_html or body_text,
            status=c.NotificationStatus.QUEUED,
            context_data=context_data or {},
        )

        try:
            # Construct dynamic SMTP backend connection configured via Admin
            connection = setting.get_smtp_connection()

            msg = EmailMultiAlternatives(
                subject=subject,
                body=body_text or body_html,
                from_email=from_email,
                to=[recipient_email],
                connection=connection,
            )
            if body_html:
                msg.attach_alternative(body_html, "text/html")

            msg.send(fail_silently=False)
            log.mark_sent()
            logger.info("Email notification successfully sent to %s [Log #%s]", recipient_email, log.pk)
        except Exception as exc:
            log.mark_failed(error=str(exc))
            logger.exception("Failed to send email to %s: %s", recipient_email, exc)

        return log

    @classmethod
    def send_user_registration_emails(cls, user: Any, request: Optional[Any] = None) -> None:
        """
        Dispatches two automated emails upon account sign-up (Form or Google OAuth):
        1. Welcome confirmation email to the user / organization.
        2. Registration alert email to the company notification email configured in Admin.
        """
        if not user or not getattr(user, "email", None):
            logger.warning("Skipping registration emails: Invalid user object or missing email address.")
            return

        profile = getattr(user, "customer_profile", None)
        from_email, company_admin_email, setting = cls.get_email_credentials()

        # Resolve primary address if saved
        primary_address = None
        if profile and hasattr(profile, "addresses"):
            primary_address = profile.addresses.filter(is_active=True).first()

        context = {
            "user": user,
            "profile": profile,
            "primary_address": primary_address,
            "site_title": "Gobindas Handicrafts",
            "support_email": from_email,
            "registered_at": timezone.now(),
        }

        # 1. Dispatch Welcome Email to Customer / Organization
        try:
            subject_customer = f"Welcome to Gobindas Handicrafts, {user.first_name or user.username}!"
            body_html_customer = render_to_string("notifications/emails/welcome_customer.html", context, request=request)
            cls.send_email(
                recipient_email=user.email,
                subject=subject_customer,
                body_html=body_html_customer,
                user=user,
                context_data={"email_type": "welcome_customer"},
            )
            logger.info("Welcome email sent to customer: %s", user.email)
        except Exception as exc:
            logger.exception("Failed to send welcome email to customer %s: %s", user.email, exc)

        # 2. Dispatch Registration Alert Email to Company Admin
        if company_admin_email:
            try:
                account_type = profile.get_account_type_display() if profile else "Individual"
                subject_admin = f"New Account Registration: {user.username} ({account_type})"
                body_html_admin = render_to_string("notifications/emails/new_user_alert_admin.html", context, request=request)
                cls.send_email(
                    recipient_email=company_admin_email,
                    subject=subject_admin,
                    body_html=body_html_admin,
                    user=None,
                    context_data={"email_type": "admin_registration_alert"},
                )
                logger.info("New registration alert email sent to company admin: %s", company_admin_email)
            except Exception as exc:
                logger.exception("Failed to send registration alert to company admin: %s", exc)

class SMSNotificationService:
    """
    Renders and dispatches SMS text alerts.
    """

    @classmethod
    def send_sms(
        cls,
        phone_number: str,
        message_text: str,
        user: Optional[Any] = None,
        template: Optional[NotificationTemplate] = None,
        context_data: Optional[Dict[str, Any]] = None,
    ) -> NotificationLog:
        log = NotificationLog.objects.create(
            recipient=phone_number,
            user=user,
            channel=c.NotificationChannel.SMS,
            template=template,
            subject="SMS Alert",
            body=message_text,
            status=c.NotificationStatus.QUEUED,
            context_data=context_data or {},
        )

        try:
            log.mark_sent()
            logger.info("SMS notification sent to %s [Log #%s]", phone_number, log.pk)
        except Exception as exc:
            log.mark_failed(error=str(exc))
            logger.exception("Failed to dispatch SMS to %s: %s", phone_number, exc)

        return log

class NotificationService:
    """
    Central orchestration engine for rendering templates and selecting dispatch channels.
    """

    @classmethod
    def dispatch_templated_notification(
        cls,
        template_code: str,
        recipient: str,
        context: Dict[str, Any],
        channel: str = c.NotificationChannel.EMAIL,
        user: Optional[Any] = None,
    ) -> NotificationLog:
        template_obj = get_template_by_code(template_code, channel=channel)

        if user:
            prefs = get_user_preferences(user)
            if channel == c.NotificationChannel.EMAIL and not prefs.email_notifications:
                logger.info("Email notifications disabled for user %s. Skipping.", user)
                return NotificationLog(recipient=recipient, status=c.NotificationStatus.FAILED, error_message="User opted out.")
            if channel == c.NotificationChannel.SMS and not prefs.sms_notifications:
                logger.info("SMS notifications disabled for user %s. Skipping.", user)
                return NotificationLog(recipient=recipient, status=c.NotificationStatus.FAILED, error_message="User opted out.")

        raw_subject = template_obj.subject_template if template_obj else f"Notice: {template_code}"
        raw_body = template_obj.body_template if template_obj else str(context)

        rendered_subject = Template(raw_subject).render(Context(context))
        rendered_body = Template(raw_body).render(Context(context))

        if channel == c.NotificationChannel.EMAIL:
            return EmailNotificationService.send_email(
                recipient_email=recipient,
                subject=rendered_subject,
                body_html=rendered_body,
                user=user,
                template=template_obj,
                context_data=context,
            )
        elif channel == c.NotificationChannel.SMS:
            return SMSNotificationService.send_sms(
                phone_number=recipient,
                message_text=rendered_body,
                user=user,
                template=template_obj,
                context_data=context,
            )

        raise NotificationDispatchError(_("Unsupported channel type."))