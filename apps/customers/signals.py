from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.customers.models import CustomerProfile, SavedCart

logger = logging.getLogger(__name__)


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def auto_provision_customer_profile(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
    """
    Intercepts User creation to automatically provision a linked CustomerProfile.
    Uses transaction.atomic to ensure database integrity and get_or_create
    for strict idempotency, preventing duplicates across concurrent requests.
    """
    if not created:
        return

    try:
        with transaction.atomic():
            profile, was_created = CustomerProfile.objects.get_or_create(user=instance)
            if was_created:
                logger.info("Successfully provisioned CustomerProfile for User ID: %s", instance.pk)
    except Exception as e:
        logger.error(
            "Failed to provision CustomerProfile for User ID: %s. Error: %s", 
            instance.pk, 
            str(e), 
            exc_info=True
        )


@receiver(post_save, sender=CustomerProfile)
def initialize_default_customer_assets(sender: Any, instance: CustomerProfile, created: bool, **kwargs: Any) -> None:
    """
    Monitors CustomerProfile creation to scaffold baseline ecommerce dependencies.
    Automatically provisions a default active SavedCart for immediate storefront usage.
    """
    if not created:
        return

    try:
        with transaction.atomic():
            cart, cart_created = SavedCart.objects.get_or_create(
                customer=instance,
                defaults={'name': 'My Cart'}
            )
            if cart_created:
                logger.info("Successfully provisioned default SavedCart for CustomerProfile ID: %s", instance.pk)
    except Exception as e:
        logger.error(
            "Failed to provision default SavedCart for CustomerProfile ID: %s. Error: %s", 
            instance.pk, 
            str(e), 
            exc_info=True
        )