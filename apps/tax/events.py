import logging
from django.core.cache import cache
from . import constants as c

logger = logging.getLogger(c.LOGGER_NAME)

def invalidate_tax_cache() -> None:
    """
    Purges cached tax settings and zone rules.
    """
    try:
        cache.delete(c.CACHE_KEY_GLOBAL_SETTINGS.format(ns=c.CACHE_NAMESPACE))
        cache.delete(c.CACHE_KEY_TAX_CLASSES.format(ns=c.CACHE_NAMESPACE))
        cache.delete(c.CACHE_KEY_TAX_ZONES.format(ns=c.CACHE_NAMESPACE))
    except Exception as exc:
        logger.warning("Tax cache purge error: %s", exc)