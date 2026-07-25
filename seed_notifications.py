import os
import django

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.notifications.models import NotificationSetting, NotificationTemplate
from apps.notifications.events import invalidate_notification_cache

def seed_notification_settings():
    print("Seeding Notification Engine Configuration...")

    # 1. Seed or update global NotificationSetting singleton
    setting, created = NotificationSetting.objects.get_or_create(id=1)
    setting.name = "Global Notification Configuration"
    setting.is_active = True
    setting.provider = NotificationSetting.ProviderChoices.CUSTOM_SMTP
    setting.smtp_host = os.getenv("EMAIL_HOST", "smtp.gmail.com")
    setting.smtp_port = int(os.getenv("EMAIL_PORT", "587"))
    setting.encryption = NotificationSetting.EncryptionChoices.STARTTLS
    setting.auth_mode = NotificationSetting.AuthModeChoices.PLAIN
    setting.smtp_username = os.getenv("EMAIL_HOST_USER", "")
    setting.smtp_password = os.getenv("EMAIL_HOST_PASSWORD", "")
    setting.default_from_email = os.getenv("DEFAULT_FROM_EMAIL", "noreply@gobindashandicrafts.com")
    setting.default_sender_name = "Gobindas Handicrafts"
    setting.company_notification_email = os.getenv("COMPANY_ADMIN_EMAIL", "admin@gobindashandicrafts.com")
    setting.timeout = 15
    setting.max_retries = 5
    setting.retry_delay = 300
    setting.save()

    print(f"-> Seeded NotificationSetting: {setting.name} (Host: {setting.smtp_host}:{setting.smtp_port})")

    # 2. Seed Default Notification Templates
    welcome_template, _ = NotificationTemplate.objects.get_or_create(
        code="welcome_customer",
        channel="email",
        defaults={
            "title": "Welcome Customer Email Template",
            "subject_template": "Welcome to Gobindas Handicrafts, {{ user.first_name|default:user.username }}!",
            "body_template": "Welcome to Gobindas Handicrafts! Your account has been registered.",
            "is_active": True,
        }
    )

    admin_alert_template, _ = NotificationTemplate.objects.get_or_create(
        code="new_user_alert_admin",
        channel="email",
        defaults={
            "title": "New User Alert for Company Admin",
            "subject_template": "New Registration: {{ user.username }}",
            "body_template": "A new customer has registered on Gobindas Handicrafts.",
            "is_active": True,
        }
    )

    print("-> Seeded default Notification Templates.")

    # 3. Clear notification caches
    invalidate_notification_cache()
    print("-> Cleared notification system caches successfully.")
    print("Notification seeding finished!")

if __name__ == "__main__":
    seed_notification_settings()