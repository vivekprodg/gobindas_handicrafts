from typing import Any, Dict, Optional

from django.conf import settings
from django.core.mail.backends.smtp import EmailBackend
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.foundation.models import CMSBaseModel, SingletonCMSModel
from .constants import NotificationChannel, NotificationStatus

class NotificationSetting(SingletonCMSModel):
    """
    CMS-driven global notification & SMTP server settings.
    Manages custom domain SMTP, Gmail, Yahoo, Outlook, Hotmail, and fallback identities.
    """

    class ProviderChoices(models.TextChoices):
        CUSTOM_SMTP = "custom", _("Custom SMTP")
        GMAIL = "gmail", _("Gmail")
        YAHOO = "yahoo", _("Yahoo Mail")
        OUTLOOK = "outlook", _("Outlook / Hotmail")
        AMAZON_SES = "ses", _("Amazon SES SMTP")

    class EncryptionChoices(models.TextChoices):
        STARTTLS = "tls", _("STARTTLS (Explicit TLS)")
        SSL = "ssl", _("SSL/TLS (Implicit SSL)")
        NONE = "none", _("None / Unencrypted")

    class AuthModeChoices(models.TextChoices):
        PLAIN = "plain", _("Plain")
        LOGIN = "login", _("Login")

    # General Configuration
    name = models.CharField(
        max_length=150,
        default="Global Notification Configuration",
        verbose_name=_("Name"),
        help_text=_("Name descriptor for this setting profile."),
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name=_("Is Active"),
        help_text=_("Enable/disable this configuration profile globally."),
    )

    # Provider Settings
    provider = models.CharField(
        max_length=30,
        choices=ProviderChoices.choices,
        default=ProviderChoices.CUSTOM_SMTP,
        verbose_name=_("Provider"),
        help_text=_("Select a pre-configured provider or configure custom SMTP."),
    )
    smtp_host = models.CharField(
        max_length=255,
        default="smtp.gmail.com",
        verbose_name=_("SMTP Host"),
        help_text=_("The SMTP server host."),
    )
    smtp_port = models.PositiveIntegerField(
        default=587,
        validators=[MinValueValidator(1), MaxValueValidator(65535)],
        verbose_name=_("SMTP Port"),
        help_text=_("Network port for connections."),
    )
    encryption = models.CharField(
        max_length=20,
        choices=EncryptionChoices.choices,
        default=EncryptionChoices.STARTTLS,
        verbose_name=_("Encryption"),
        help_text=_("SSL/TLS connection scheme."),
    )
    auth_mode = models.CharField(
        max_length=20,
        choices=AuthModeChoices.choices,
        default=AuthModeChoices.PLAIN,
        verbose_name=_("Auth Mode"),
        help_text=_("SMTP authentication mechanism."),
    )

    # Authentication Credentials
    smtp_username = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name=_("SMTP Username"),
        help_text=_("Handshake username."),
    )
    smtp_password = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name=_("SMTP Password"),
        help_text=_("Handshake credential/password."),
    )

    # Default Sender Identity
    default_from_email = models.EmailField(
        default="noreply@gobindashandicrafts.com",
        verbose_name=_("Default From Email"),
        help_text=_("Sender envelope email address."),
    )
    default_sender_name = models.CharField(
        max_length=150,
        default="Gobindas Handicrafts",
        verbose_name=_("Default Sender Name"),
        help_text=_("Sender visual name display."),
    )
    company_notification_email = models.EmailField(
        default="admin@gobindashandicrafts.com",
        verbose_name=_("Company Notification Email"),
        help_text=_("Email address receiving user registration and administrative alerts."),
    )

    # Reliability & Timeouts
    timeout = models.PositiveIntegerField(
        default=15,
        validators=[MinValueValidator(1), MaxValueValidator(300)],
        verbose_name=_("Timeout"),
        help_text=_("Connection timeout threshold in seconds."),
    )
    max_retries = models.PositiveIntegerField(
        default=5,
        validators=[MinValueValidator(0), MaxValueValidator(20)],
        verbose_name=_("Max Retries"),
        help_text=_("Max sending attempts allowed for failed logs."),
    )
    retry_delay = models.PositiveIntegerField(
        default=300,
        validators=[MinValueValidator(0), MaxValueValidator(86400)],
        verbose_name=_("Retry Delay"),
        help_text=_("Seconds to wait before scheduling another retry."),
    )

    class Meta:
        verbose_name = _("Notification Setting")
        verbose_name_plural = _("Notification Settings")

    def __str__(self) -> str:
        return f"{self.name} [{self.get_provider_display()}]"

    def get_smtp_connection(self) -> EmailBackend:
        """
        Constructs and returns a dynamic Django EmailBackend instance
        initialized with the active database parameters.
        """
        use_tls = self.encryption == self.EncryptionChoices.STARTTLS
        use_ssl = self.encryption == self.EncryptionChoices.SSL

        return EmailBackend(
            host=self.smtp_host,
            port=self.smtp_port,
            username=self.smtp_username,
            password=self.smtp_password,
            use_tls=use_tls,
            use_ssl=use_ssl,
            timeout=self.timeout,
            fail_silently=False,
        )

class NotificationTemplate(CMSBaseModel):
    """
    CMS-managed templates for Emails, SMS text messages, and Push notifications.
    """
    code = models.CharField(
        max_length=100,
        db_index=True,
        verbose_name=_("Template Code"),
        help_text=_("Unique trigger code (e.g. 'order_placed', 'welcome_customer', 'new_user_alert_admin')."),
    )
    title = models.CharField(
        max_length=200,
        verbose_name=_("Template Name"),
    )
    channel = models.CharField(
        max_length=30,
        choices=NotificationChannel.CHOICES,
        default=NotificationChannel.EMAIL,
        db_index=True,
        verbose_name=_("Notification Channel"),
    )
    subject_template = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Subject Line Template (Email/Push)"),
        help_text=_("Supports template variables (e.g., 'Welcome to {{ site_title }}, {{ user.username }}')."),
    )
    body_template = models.TextField(
        verbose_name=_("Body Markup / Text Template"),
        help_text=_("HTML/Plain text content using Django template context variables."),
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Notification Template")
        verbose_name_plural = _("Notification Templates")
        ordering = ["code", "channel"]
        constraints = [
            models.UniqueConstraint(fields=["code", "channel"], name="unique_template_code_per_channel"),
        ]

    def __str__(self) -> str:
        return f"[{self.get_channel_display()}] {self.title} ({self.code})"

class NotificationLog(CMSBaseModel):
    """
    Immutable audit log recording sent emails, SMS, and in-app alerts.
    """
    recipient = models.CharField(
        max_length=255,
        db_index=True,
        verbose_name=_("Recipient Address / Phone / ID"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="notification_logs",
        verbose_name=_("Recipient Account"),
    )
    channel = models.CharField(
        max_length=30,
        choices=NotificationChannel.CHOICES,
        default=NotificationChannel.EMAIL,
        db_index=True,
        verbose_name=_("Channel"),
    )
    template = models.ForeignKey(
        NotificationTemplate,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="logs",
        verbose_name=_("Source Template"),
    )
    subject = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Dispatched Subject"),
    )
    body = models.TextField(
        verbose_name=_("Rendered Content"),
    )
    status = models.CharField(
        max_length=30,
        choices=NotificationStatus.CHOICES,
        default=NotificationStatus.QUEUED,
        db_index=True,
        verbose_name=_("Delivery Status"),
    )
    error_message = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Error Message (if failed)"),
    )
    sent_at = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Dispatched Timestamp"),
    )
    context_data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Context Data Snapshot"),
    )

    class Meta:
        verbose_name = _("Notification Audit Log")
        verbose_name_plural = _("Notification Audit Logs")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "status"]),
            models.Index(fields=["channel", "status"]),
        ]

    def mark_sent(self) -> None:
        self.status = NotificationStatus.SENT
        self.sent_at = timezone.now()
        self.save(update_fields=["status", "sent_at", "updated_at"])

    def mark_failed(self, error: str = "") -> None:
        self.status = NotificationStatus.FAILED
        self.error_message = str(error)
        self.save(update_fields=["status", "error_message", "updated_at"])

    def __str__(self) -> str:
        return f"{self.get_channel_display()} to {self.recipient} [{self.get_status_display()}]"

class NotificationPreference(CMSBaseModel):
    """
    Customer preferences for opt-in/opt-out notification channels.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_preferences",
        verbose_name=_("Customer Account"),
    )
    email_notifications = models.BooleanField(
        default=True,
        verbose_name=_("Transactional Email Notifications"),
    )
    sms_notifications = models.BooleanField(
        default=True,
        verbose_name=_("SMS Shipping Updates"),
    )
    marketing_emails = models.BooleanField(
        default=False,
        verbose_name=_("Promotional Offers & Newsletters"),
    )

    class Meta:
        verbose_name = _("Customer Notification Preference")
        verbose_name_plural = _("Customer Notification Preferences")

    def __str__(self) -> str:
        return f"Preferences for {self.user}"