import logging
from django.core.cache import cache
from . import constants as c

logger = logging.getLogger(c.LOGGER_NAME)

def invalidate_payments_cache() -> None:
    try:
        cache.delete(c.CACHE_KEY_GATEWAY_SETTINGS.format(ns=c.CACHE_NAMESPACE))
    except Exception as exc:
        logger.warning("Failed to purge payment settings cache: %s", exc)