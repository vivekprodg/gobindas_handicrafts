from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, Final, List, Optional, Tuple
from uuid import UUID

from django.utils import timezone
from django.utils.text import slugify as django_slugify

from apps.orders import constants as c

logger = logging.getLogger(c.LOGGER_NAME)

_MASK_CHAR: Final[str] = "*"
_WIN_RESERVED_NAMES: Final[set] = {"CON", "PRN", "AUX", "NUL"}
_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")
_CONTROL_CHAR_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_PATH_SEP_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\\/]+")
_WIN_DISALLOWED_PATTERN: Final[re.Pattern[str]] = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MONEY_QUANT: Final[Decimal] = Decimal("0.01")
_WEIGHT_QUANT: Final[Decimal] = Decimal("0.001")

def safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        return str(value).strip()
    except Exception:
        return default

def is_blank(value: Any) -> bool:
    return value is None or not str(value).strip()

def is_not_blank(value: Any) -> bool:
    return not is_blank(value)

def normalize_whitespace(value: str) -> str:
    if not value:
        return ""
    return _WHITESPACE_PATTERN.sub(" ", str(value)).strip()

def truncate(value: str, max_length: int, suffix: str = "...") -> str:
    if not value or max_length <= 0:
        return ""
    text = str(value)
    if len(text) <= max_length:
        return text
    return text[: max_length - len(suffix)] + suffix

def humanize_status(status: str) -> str:
    if not status:
        return ""
    return str(status).replace("_", " ").strip().title()

def humanize_event_type(event_type: str) -> str:
    return humanize_status(event_type)

def quantize_money(value: Any) -> Decimal:
    if value is None:
        return c.ZERO_DECIMAL_2
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
        return dec.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return c.ZERO_DECIMAL_2

def quantize_weight(value: Any) -> Decimal:
    if value is None:
        return c.ZERO_DECIMAL_3
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
        return dec.quantize(_WEIGHT_QUANT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return c.ZERO_DECIMAL_3

def to_decimal(value: Any, default: Decimal = c.ZERO_DECIMAL_2) -> Decimal:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default

def format_money(amount: Any, currency: str = c.DEFAULT_CURRENCY_CODE) -> str:
    if amount is None or amount == "":
        return "-"
    return f"{quantize_money(amount)} {currency}"

def format_iso(value: Optional[datetime]) -> str:
    if not value:
        return ""
    try:
        return value.isoformat()
    except Exception:
        return ""

def format_export_timestamp(value: Optional[datetime] = None) -> str:
    dt = value or timezone.now()
    return dt.strftime(c.EXPORT_TIMESTAMP_FORMAT)

def compute_sha256_hex(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

def sanitize_filename(filename: str, max_length: int = 200) -> str:
    if not filename:
        return "file.bin"
    name = str(filename).strip().replace("\x00", "")
    name = os.path.basename(name)
    name = _PATH_SEP_PATTERN.sub("", name)
    name = _WIN_DISALLOWED_PATTERN.sub("_", name)
    name = _CONTROL_CHAR_PATTERN.sub("", name).strip(" .-_")
    return name[:max_length] if name else "file.bin"

def mask_string(value: str, visible_prefix: int = 2, visible_suffix: int = 2) -> str:
    if not value:
        return "-"
    text = str(value)
    if len(text) <= visible_prefix + visible_suffix:
        return _MASK_CHAR * len(text)
    return f"{text[:visible_prefix]}{_MASK_CHAR * 6}{text[-visible_suffix:]}"

def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return "-"
    local, domain = email.split("@", 1)
    return f"{local[0]}****@{domain}"

def safe_json_loads(value: Any, default: Any = None) -> Any:
    if not isinstance(value, (str, bytes)):
        return default
    try:
        return json.loads(value)
    except Exception:
        return default

def coerce_uuid(value: Any) -> Optional[UUID]:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except Exception:
        return None

def order_cache_key(order_id: Any) -> str:
    return c.CACHE_KEY_ORDER_BY_ID.format(ns=c.CACHE_NAMESPACE, order_id=str(order_id))

def to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return bool(value)

__all__ = [
    "safe_str", "is_blank", "is_not_blank", "normalize_whitespace", "truncate",
    "humanize_status", "humanize_event_type", "quantize_money", "quantize_weight",
    "to_decimal", "format_money", "format_iso", "format_export_timestamp",
    "compute_sha256_hex", "sanitize_filename", "mask_string", "mask_email",
    "safe_json_loads", "coerce_uuid", "order_cache_key", "to_bool",
]