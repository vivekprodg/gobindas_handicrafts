import logging
from django.db.models.signals import post_save, post_delete, m2m_changed
from django.dispatch import receiver
from django.core.cache import cache

from apps.homepage.services import invalidate_homepage_cache
from .models import (
    Category,
    Product,
    Artisan,
    CatalogSettings,
    ProductVariant,
    RecentlyViewedProduct,
)
from .services import invalidate_catalog_cache

logger = logging.getLogger(__name__)


@receiver([post_save, post_delete], sender=Product)
@receiver([post_save, post_delete], sender=Category)
@receiver([post_save, post_delete], sender=Artisan)
@receiver([post_save, post_delete], sender=CatalogSettings)
def handle_catalog_cms_change(sender, instance, **kwargs):
    """
    Clears catalog query cache, homepage cached payloads, and granular key definitions
    safely whenever core catalog entities undergo structural database changes.
    """
    if kwargs.get('raw', False):
        return

    try:
        # Granular invalidations to ensure minimal stale caching states
        if isinstance(instance, Product):
            cache.delete(f"catalog:product:slug:{instance.slug}")
            cache.delete(f"catalog:product:id:{instance.id}")
            cache.delete(f"catalog:product:{instance.id}:related")
            cache.delete(f"catalog:product:{instance.id}:upsell")
            cache.delete(f"catalog:product:{instance.id}:cross_sell")
            if instance.category:
                cache.delete(f"catalog:cat:slug:{instance.category.slug}")
        elif isinstance(instance, Category):
            cache.delete(f"catalog:cat:slug:{instance.slug}")
            cache.delete("catalog:active_categories_hierarchy")
        elif isinstance(instance, Artisan):
            cache.delete(f"catalog:artisan:slug:{instance.slug}")
        elif isinstance(instance, CatalogSettings):
            cache.delete("catalog:settings")

        # Global list / landing query invalidations
        cache.delete("catalog:trending_products")
        cache.delete("catalog:popular_products")
        cache.delete("catalog:new_arrivals")

        # Core global fallback invalidations
        invalidate_catalog_cache()
        invalidate_homepage_cache()

    except Exception as e:
        logger.error(f"Error invalidating catalog caches on CMS change: {e}", exc_info=True)


@receiver(post_save, sender=ProductVariant)
def handle_variant_inventory_change(sender, instance, **kwargs):
    """
    Synchronizes parent product's stock status automatically based on the sum
    of stock quantities across its active variants, preventing stale inventory cues.
    """
    if kwargs.get('raw', False):
        return

    try:
        product = instance.product
        active_variants = product.variants.filter(is_active=True)
        
        if active_variants.exists():
            total_stock = sum(variant.stock_quantity for variant in active_variants)
            
            # Fetch thresholds safely from global settings configurations
            settings = CatalogSettings.objects.first()
            warning_threshold = settings.show_stock_warning_threshold if settings else 5
            
            # Resolve the parent stock status boundaries
            if total_stock == 0:
                new_status = Product.StockChoices.OUT_OF_STOCK
            elif total_stock <= warning_threshold:
                new_status = Product.StockChoices.LOW_STOCK
            else:
                new_status = Product.StockChoices.IN_STOCK
                
            if product.stock_status != new_status:
                product.stock_status = new_status
                # Save specifically using update_fields to prevent deep recursive save triggers
                product.save(update_fields=['stock_status', 'updated_at'])
                
                # Invalidate related product details cache
                cache.delete(f"catalog:product:slug:{product.slug}")
                cache.delete(f"catalog:product:id:{product.id}")

    except Exception as e:
        logger.error(f"Error synchronizing variant stock levels onto parent Product {instance.product_id}: {e}", exc_info=True)


@receiver(post_save, sender=RecentlyViewedProduct)
def handle_recently_viewed_cache_invalidation(sender, instance, created, **kwargs):
    """
    Maintains clean browse layers by invalidating recently viewed caches
    on newly tracked interactions.
    """
    if created:
        try:
            cache.delete(f"catalog:recently_viewed:user:{instance.user_id}")
            cache.delete(f"catalog:recently_viewed:session:{instance.session_key}")
        except Exception as e:
            logger.error(f"Error invalidating recently viewed browsing caches: {e}", exc_info=True)


@receiver(m2m_changed, sender=Product.related_products.through)
@receiver(m2m_changed, sender=Product.upsell_products.through)
@receiver(m2m_changed, sender=Product.cross_sell_products.through)
def handle_product_recommendation_m2m_change(sender, instance, action, **kwargs):
    """
    Ensures recommendation calculations and recommendation scores remain fresh 
    by automatically invalidating corresponding key caches on relation changes.
    """
    if action in ["post_add", "post_remove", "post_clear"]:
        try:
            cache.delete(f"catalog:product:{instance.id}:related")
            cache.delete(f"catalog:product:{instance.id}:upsell")
            cache.delete(f"catalog:product:{instance.id}:cross_sell")
            
            # Invalidate trending cache structures
            cache.delete("catalog:trending_products")
            cache.delete("catalog:popular_products")
        except Exception as e:
            logger.error(f"Error handling recommendation M2M mapping invalidations: {e}", exc_info=True)