from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from apps.homepage.services import invalidate_homepage_cache
from .models import Category, Product, Artisan, CatalogSettings
from .services import invalidate_catalog_cache

@receiver([post_save, post_delete], sender=Product)
@receiver([post_save, post_delete], sender=Category)
@receiver([post_save, post_delete], sender=Artisan)
@receiver([post_save, post_delete], sender=CatalogSettings)
def handle_catalog_cms_change(sender, instance, **kwargs):
    """
    Clears catalog query cache and homepage cached payload on any modifications.
    """
    invalidate_catalog_cache()
    invalidate_homepage_cache()