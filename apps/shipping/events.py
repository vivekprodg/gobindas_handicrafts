import logging
from django.core.cache import cache
from . import constants as c

logger = logging.getLogger(c.LOGGER_NAME)

def invalidate_shipping_cache() -> None:
    """
    Purges cached shipping rate configuration.
    """
    try:
        cache.delete(c.CACHE_KEY_GLOBAL_SETTINGS.format(ns=c.CACHE_NAMESPACE))
        cache.delete(c.CACHE_KEY_SHIPPING_METHODS.format(ns=c.CACHE_NAMESPACE))
    except Exception as exc:
        logger.warning("Shipping cache purge failed: %s", exc)