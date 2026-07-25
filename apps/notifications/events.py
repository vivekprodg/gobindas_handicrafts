import logging

from django.core.cache import cache
from . import constants as c

logger = logging.getLogger(c.LOGGER_NAME)

def invalidate_notification_cache() -> None:
    """
    Purges cached templates and notification settings.
    """
    try:
        cache.delete(c.CACHE_KEY_TEMPLATES.format(ns=c.CACHE_NAMESPACE))
        cache.delete(c.CACHE_KEY_NOTIFICATION_SETTING.format(ns=c.CACHE_NAMESPACE))
    except Exception as exc:
        logger.warning("Failed to purge notifications cache: %s", exc)