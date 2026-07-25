import logging
from . import constants as c

logger = logging.getLogger(c.LOGGER_NAME)

def handle_audit_event(event_name: str, payload: dict) -> None:
    logger.info("Audit Event Dispatched: %s -> %s", event_name, payload)