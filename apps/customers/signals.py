from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.customers.models import CustomerProfile, SavedCart, SocialAccountMetadata
from apps.notifications.services import EmailNotificationService

logger = logging.getLogger(__name__)

# Safely import Allauth social signals with type-checker fallbacks
user_signed_up = None
SocialAccount = None

try:
    from allauth.account.signals import user_signed_up  # type: ignore
    from allauth.socialaccount.models import SocialAccount  # type: ignore
except ImportError:
    user_signed_up = None
    SocialAccount = None

@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def auto_provision_customer_profile(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
    """
    Automatically creates a CustomerProfile whenever a new User instance is registered.
    """
    if not created:
        return

    try:
        with transaction.atomic():
            profile, was_created = CustomerProfile.objects.get_or_create(user=instance)
            if was_created:
                logger.info("Successfully provisioned CustomerProfile for User ID: %s", instance.pk)

    except Exception as e:
        logger.error("Failed to provision CustomerProfile for User ID: %s. Error: %s", instance.pk, str(e), exc_info=True)

@receiver(post_save, sender=CustomerProfile)
def initialize_default_customer_assets(sender: Any, instance: CustomerProfile, created: bool, **kwargs: Any) -> None:
    """
    Creates default baseline e-commerce resources (e.g., active SavedCart) for new customer profiles.
    """
    if not created:
        return

    try:
        with transaction.atomic():
            cart, cart_created = SavedCart.objects.get_or_create(
                customer=instance,
                defaults={"name": "My Cart"}
            )
            if cart_created:
                logger.info("Successfully created default SavedCart for CustomerProfile ID: %s", instance.pk)
    except Exception as e:
        logger.error("Failed to provision SavedCart for CustomerProfile ID: %s. Error: %s", instance.pk, str(e), exc_info=True)

if user_signed_up is not None:
    @receiver(user_signed_up)
    def handle_social_signup_profile_mapping(request: Any, user: Any, **kwargs: Any) -> None:
        """
        Maps incoming Google OAuth / Social Account data directly to CustomerProfile upon first-time login/signup
        and triggers signup notification emails (Customer Welcome + Company Admin Alert).
        """
        try:
            with transaction.atomic():
                profile, _ = CustomerProfile.objects.get_or_create(user=user)

                social_login = kwargs.get("sociallogin")
                if social_login:
                    account = social_login.account
                    provider = str(account.provider).upper()
                    uid = account.uid
                    extra_data = account.extra_data or {}

                    SocialAccountMetadata.objects.get_or_create(
                        customer=profile,
                        provider=provider,
                        provider_uid=str(uid)
                    )

                    if not user.first_name and extra_data.get("given_name"):
                        user.first_name = extra_data.get("given_name", "")
                    if not user.last_name and extra_data.get("family_name"):
                        user.last_name = extra_data.get("family_name", "")
                    user.save(update_fields=["first_name", "last_name"])

                    profile.save()
                    logger.info("Successfully mapped Google OAuth profile data for User ID: %s", user.pk)

            # Trigger email dispatches on transaction commit for social signups
            transaction.on_commit(lambda: EmailNotificationService.send_user_registration_emails(user=user, request=request))

        except Exception as e:
            logger.error("Error mapping social account profile data for User ID %s: %s", getattr(user, "pk", "unknown"), str(e), exc_info=True)