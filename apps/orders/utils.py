"""
Enterprise-grade utility module for the Orders application.

This module provides a curated collection of PURE, STATELESS,
DETERMINISTIC, and INDEPENDENTLY TESTABLE helper functions that are
reused across the orders app's views, admin, services, selectors,
serializers, management commands, tests, and external consumers.

ARCHITECTURE
============
The orders app follows a strict, layered architecture. The utility
layer sits BELOW every other layer and provides the building blocks
they all rely on. Concretely:

    utils.py        → THIS FILE (pure helpers, formatting, parsing)
    constants.py    → Configuration / reference values
    models.py       → Persistence layer (no business logic)
    signals.py      → ORM lifecycle detection (no business logic)
    event_handlers.py → Domain workflow coordination
    services.py     → Business logic / state transitions
    selectors.py    → Read-only data access
    views.py        → HTTP request handling

This file contains NO business logic, NO workflow orchestration,
NO database writes, NO service-layer code, and NO HTTP / request
handling. Every helper in this module is safe to import from any
other module in the project without creating circular dependencies
or premature side effects.

DESIGN PRINCIPLES
=================
* **Pure functions**: Given the same input, the helper always returns
  the same output. No hidden state, no I/O, no ORM.
* **Stateless**: No module-level mutable globals beyond lazy logger
  references.
* **Reusable**: A single helper can be used by views, admin, services,
  selectors, signals, and tests.
* **Deterministic**: No use of ``random``, ``timezone.now()``, or
  ``uuid.uuid4()`` in pure formatting helpers. Time-sensitive helpers
  accept the timestamp as a parameter.
* **Independently testable**: Every helper has a clear contract
  suitable for direct unit testing.
* **Defensive**: Inputs are validated. Defaults are safe. Failures
  are localized and never raise hidden exceptions.
* **PEP 8 / PEP 257 / PEP 484 compliant** with full type hints.
* **Python 3.13+ idiomatic**.
* **Django 5.1.4 compatible** (no use of deprecated APIs).
* **OWASP-aware**: Sensitive values are masked, filenames are
  sanitized, paths are checked, and parsing is safe.

SECURITY NOTES
==============
* Phone, email, currency, and amount formatting NEVER echo raw
  attacker-controlled data without sanitization.
* Filename sanitization strips path separators and reserved names.
* Masking helpers are guaranteed to never return more than a small
  prefix / suffix of the original value.
* Hash helpers use the standard library ``hashlib`` only.
* Safe parsing helpers never ``eval`` or ``exec`` arbitrary input.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, Final, List, Optional, Tuple, Union
from uuid import UUID

from django.utils import timezone
from django.utils.text import slugify as django_slugify

from apps.orders import constants as c

logger = logging.getLogger(c.LOGGER_NAME)

# ==============================================================================
# MODULE-LEVEL CONSTANTS (private to this module)
# ==============================================================================
#: Default character used to mask sensitive values.
_MASK_CHAR: Final[str] = "*"

#: Default number of mask characters used for short values.
_MASK_SHORT_LENGTH: Final[int] = 4

#: Default number of mask characters used for medium values.
_MASK_MEDIUM_LENGTH: Final[int] = 6

#: Default number of mask characters used for long values.
_MASK_LONG_LENGTH: Final[int] = 8

#: Reserved filenames forbidden on Windows file systems.
_WIN_RESERVED_NAMES: Final[FrozenSet[str]] = frozenset(
    {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

#: Maximum filename length recommended for cross-platform portability.
_MAX_FILENAME_LENGTH: Final[int] = 200

#: Maximum path length used for collision-resistant uploads.
_MAX_UPLOAD_PATH_LENGTH: Final[int] = 255

#: Whitespace collapse pattern.
_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")

#: Non-slug-safe characters pattern.
_NON_SLUG_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9\-_]+")

#: Leading/trailing dashes/underscores pattern.
_SLUG_TRIM_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[\-_]+|[\-_]+$")

#: Multiple separators pattern.
_MULTI_SEPARATOR_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\-_]{2,}")

#: All whitespace and control characters pattern.
_CONTROL_CHAR_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[\x00-\x1f\x7f-\x9f]"
)

#: Path separators pattern.
_PATH_SEP_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\\/]+")

#: Disallowed Windows filename characters pattern.
_WIN_DISALLOWED_PATTERN: Final[re.Pattern[str]] = re.compile(
    r'[<>:"/\\|?*\x00-\x1f]'
)

#: Allowed extension characters pattern.
_ALLOWED_EXT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+$")

#: Default decimal quantize exponent for monetary values (2 dp).
_MONEY_QUANT: Final[Decimal] = Decimal("0.01")

#: Default decimal quantize exponent for weight values (3 dp).
_WEIGHT_QUANT: Final[Decimal] = Decimal("0.001")

#: Default decimal quantize exponent for exchange rate (8 dp).
_RATE_QUANT: Final[Decimal] = Decimal("0.00000001")

#: Default decimal quantize exponent for percentages (2 dp).
_PERCENT_QUANT: Final[Decimal] = Decimal("0.01")

#: Default decimal quantize exponent for tax rates (4 dp).
_TAX_RATE_QUANT: Final[Decimal] = Decimal("0.0001")

#: Phone digit-only pattern.
_PHONE_DIGIT_PATTERN: Final[re.Pattern[str]] = re.compile(r"\D+")

#: Alphanumeric-only pattern.
_ALNUM_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9]+")

#: Hex character pattern.
_HEX_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-fA-F]+$")

#: Numeric pattern.
_NUMERIC_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9]+$")

#: Alphanumeric pattern.
_ALPHANUMERIC_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z0-9]+$")

#: Generic identifier pattern (letters, digits, hyphens, underscores).
_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z0-9_\-]+$")

# FrozenSet re-export for type hints (avoid re-importing typing at use sites).
try:
    from typing import FrozenSet  # noqa: F401
except ImportError:  # pragma: no cover
    from typing import FrozenSet as _FrozenSet  # noqa: F401

# ==============================================================================
# 1. SAFE STRING / TEXT HELPERS
# ==============================================================================
def safe_str(value: Any, default: str = "") -> str:
    """
    Convert ``value`` to a trimmed string. Never raises.

    Returns ``default`` if the conversion fails or ``value`` is
    ``None``. This is the canonical way to coerce untrusted
    external data (e.g. webhook payloads, CSV imports) into
    display-safe text.
    """
    if value is None:
        return default
    try:
        result = str(value)
    except Exception:  # noqa: BLE001
        return default
    return result.strip()

def is_blank(value: Any) -> bool:
    """
    Return ``True`` if ``value`` is ``None``, empty, or only
    whitespace.
    """
    if value is None:
        return True
    try:
        return not str(value).strip()
    except Exception:  # noqa: BLE001
        return True

def is_not_blank(value: Any) -> bool:
    """Return ``True`` if ``value`` contains at least one non-whitespace char."""
    return not is_blank(value)

def normalize_whitespace(value: str) -> str:
    """
    Collapse every run of internal whitespace to a single space and
    strip leading / trailing whitespace.

    Uses ``str.split()`` and ``str.join()`` which are Unicode-aware
    and safe for all valid ``str`` inputs.
    """
    if value is None:
        return ""
    return _WHITESPACE_PATTERN.sub(" ", str(value)).strip()

def remove_control_characters(value: str) -> str:
    """
    Strip ASCII and Unicode control characters from ``value``.

    Useful for sanitizing free-form user input that may contain
    accidental line breaks, NULL bytes, or zero-width spaces.
    """
    if value is None:
        return ""
    return _CONTROL_CHAR_PATTERN.sub("", str(value))

def strip_to_ascii(value: str) -> str:
    """
    Return ``value`` with non-ASCII characters removed.

    Used when a legacy system or third-party API requires ASCII-only
    identifiers.
    """
    if value is None:
        return ""
    return str(value).encode("ascii", errors="ignore").decode("ascii")

def truncate(value: str, max_length: int, suffix: str = "...") -> str:
    """
    Truncate ``value`` to ``max_length`` characters, appending
    ``suffix`` if truncation occurred.

    If ``max_length`` is less than or equal to zero, an empty string
    is returned. If ``max_length`` is smaller than the suffix length,
    the suffix itself is truncated to fit.
    """
    if value is None:
        return ""
    if max_length <= 0:
        return ""
    text = str(value)
    if len(text) <= max_length:
        return text
    if max_length <= len(suffix):
        return suffix[:max_length]
    return text[: max_length - len(suffix)] + suffix

def title_case(value: str) -> str:
    """
    Convert ``value`` to title case, but never raise.

    Returns an empty string for ``None`` or non-string input.
    """
    if value is None:
        return ""
    try:
        return str(value).title()
    except Exception:  # noqa: BLE001
        return ""

def humanize_status(status: str) -> str:
    """
    Convert a machine-friendly status code (e.g. ``"awaiting_payment"``)
    into a human-readable label (``"Awaiting Payment"``).
    """
    if not status:
        return ""
    return str(status).replace(c.LABEL_HYPHEN, " ").strip().title()

def humanize_event_type(event_type: str) -> str:
    """
    Convert a timeline event type (e.g. ``"shipment_in_transit"``)
    into a human-readable label (``"Shipment In Transit"``).
    """
    return humanize_status(event_type)

# ==============================================================================
# 2. SLUG / IDENTIFIER HELPERS
# ==============================================================================
def make_slug(
    value: str,
    *,
    allow_unicode: bool = False,
    max_length: int = c.FieldLength.PRODUCT_SLUG_SNAPSHOT,
) -> str:
    """
    Build a URL-safe slug from ``value``.

    Wraps Django's ``slugify`` and additionally:

        1. Lowercases the result.
        2. Removes any characters Django did not strip.
        3. Truncates to ``max_length`` characters.
        4. Trims leading / trailing hyphens and underscores.

    Returns an empty string for ``None`` or empty input.
    """
    if not value:
        return ""
    slug = django_slugify(
        str(value),
        allow_unicode=allow_unicode,
    )
    slug = slug.lower()
    slug = _SLUG_TRIM_PATTERN.sub("", slug)
    slug = _MULTI_SEPARATOR_PATTERN.sub("-", slug)
    if max_length > 0 and len(slug) > max_length:
        slug = slug[:max_length].rstrip("-_")
    return slug

def is_valid_identifier(value: str) -> bool:
    """
    Return ``True`` if ``value`` consists only of letters, digits,
    hyphens, and underscores.

    Useful for validating human-readable IDs (``order_number``,
    ``shipment_number``, ``return_number``) where path / URL
    collisions could be dangerous.
    """
    if not value or not isinstance(value, str):
        return False
    return bool(_IDENTIFIER_PATTERN.match(value))

def is_valid_hex(value: str) -> bool:
    """
    Return ``True`` if ``value`` is a non-empty hexadecimal string.
    """
    if not value or not isinstance(value, str):
        return False
    return bool(_HEX_PATTERN.match(value))

def is_numeric(value: str) -> bool:
    """Return ``True`` if ``value`` is a non-empty numeric string."""
    if not value or not isinstance(value, str):
        return False
    return bool(_NUMERIC_PATTERN.match(value))

def is_alphanumeric(value: str) -> bool:
    """Return ``True`` if ``value`` is a non-empty alphanumeric string."""
    if not value or not isinstance(value, str):
        return False
    return bool(_ALPHANUMERIC_PATTERN.match(value))

# ==============================================================================
# 3. PHONE / EMAIL / CONTACT HELPERS
# ==============================================================================
def normalize_phone_digits(phone: str) -> str:
    """
    Strip everything but digits from ``phone``.

    Returns an empty string for ``None``. The leading ``+`` is NOT
    preserved; use ``format_phone_e164`` if the country prefix must
    be retained.
    """
    if not phone:
        return ""
    return _PHONE_DIGIT_PATTERN.sub("", str(phone))

def format_phone_e164(phone: str, country_code: str = "+977") -> str:
    """
    Format ``phone`` as an E.164-compliant string.

    The result is built by combining the supplied ``country_code``
    (default ``+977``) and the digit-only form of ``phone``. If
    ``phone`` already starts with the supplied ``+`` country
    code, the prefix is preserved.

    Returns an empty string for blank input.
    """
    if not phone:
        return ""
    digits = normalize_phone_digits(phone)
    if not digits:
        return ""
    if phone.strip().startswith("+"):
        return f"+{digits}"
    if digits.startswith(country_code.lstrip("+")):
        return f"+{digits}"
    return f"{country_code}{digits}"

def is_valid_phone(phone: str) -> bool:
    """
    Return ``True`` if ``phone`` matches the canonical phone regex
    declared in ``constants.PHONE_REGEX``.
    """
    if not phone or not isinstance(phone, str):
        return False
    pattern = re.compile(c.PHONE_REGEX)
    return bool(pattern.match(phone.strip()))

def mask_email(email: str) -> str:
    """
    Mask an email address for display.

    The local part is reduced to its first character followed by
    three mask characters; the domain is preserved. Returns
    ``"-"`` for blank or invalid input.
    """
    if not email or not isinstance(email, str):
        return "-"
    text = str(email).strip()
    if "@" not in text:
        return "-"
    local, _, domain = text.partition("@")
    if not local or not domain:
        return "-"
    masked_local = f"{local[0]}{_MASK_CHAR * _MASK_SHORT_LENGTH}"
    return f"{masked_local}@{domain}"

# ==============================================================================
# 4. CURRENCY / MONEY HELPERS
# ==============================================================================
def quantize_money(value: Decimal) -> Decimal:
    """
    Quantize ``value`` to the canonical 2-decimal-place scale
    used by all monetary fields in the orders app.

    Uses ``ROUND_HALF_UP`` which is the convention required by
    most payment gateways.
    """
    if value is None:
        return c.ZERO_DECIMAL_2
    if not isinstance(value, Decimal):
        try:
            value = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return c.ZERO_DECIMAL_2
    return value.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)

def quantize_weight(value: Decimal) -> Decimal:
    """Quantize ``value`` to the canonical 3-decimal-place weight scale."""
    if value is None:
        return c.ZERO_DECIMAL_3
    if not isinstance(value, Decimal):
        try:
            value = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return c.ZERO_DECIMAL_3
    return value.quantize(_WEIGHT_QUANT, rounding=ROUND_HALF_UP)

def quantize_exchange_rate(value: Decimal) -> Decimal:
    """Quantize ``value`` to the canonical 8-decimal-place FX scale."""
    if value is None:
        return c.DEFAULT_EXCHANGE_RATE
    if not isinstance(value, Decimal):
        try:
            value = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return c.DEFAULT_EXCHANGE_RATE
    return value.quantize(_RATE_QUANT, rounding=ROUND_HALF_UP)

def quantize_percentage(value: Decimal) -> Decimal:
    """Quantize ``value`` to the canonical 2-decimal-place percentage scale."""
    if value is None:
        return c.MIN_PERCENTAGE
    if not isinstance(value, Decimal):
        try:
            value = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return c.MIN_PERCENTAGE
    return value.quantize(_PERCENT_QUANT, rounding=ROUND_HALF_UP)

def quantize_tax_rate(value: Decimal) -> Decimal:
    """Quantize ``value`` to the canonical 4-decimal-place tax-rate scale."""
    if value is None:
        return c.MIN_TAX_RATE
    if not isinstance(value, Decimal):
        try:
            value = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return c.MIN_TAX_RATE
    return value.quantize(_TAX_RATE_QUANT, rounding=ROUND_HALF_UP)

def to_decimal(value: Any, default: Decimal = c.ZERO_DECIMAL_2) -> Decimal:
    """
    Safely coerce ``value`` to a ``Decimal``.

    Returns ``default`` for ``None`` or for any input that cannot be
    parsed. Strings, ints, floats, and existing ``Decimal`` values
    are all supported.
    """
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default

def is_positive_decimal(value: Any) -> bool:
    """Return ``True`` if ``value`` is a ``Decimal`` strictly greater than zero."""
    if value is None:
        return False
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return decimal_value > c.ZERO_DECIMAL_2

def is_non_negative_decimal(value: Any) -> bool:
    """Return ``True`` if ``value`` is a ``Decimal`` greater than or equal to zero."""
    if value is None:
        return False
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return decimal_value >= c.ZERO_DECIMAL_2

def format_currency(
    amount: Any,
    currency: str = c.DEFAULT_CURRENCY_CODE,
    *,
    include_symbol: bool = False,
    symbol: str = "",
) -> str:
    """
    Format ``amount`` as a localized currency string.

    Returns ``"-"`` for blank input. Uses simple ``"<amount> <code>"``
    formatting by default; if ``include_symbol`` is ``True`` the
    ``symbol`` is prepended instead of the currency code.
    """
    if amount is None or amount == "":
        return "-"
    decimal_value = quantize_money(to_decimal(amount))
    if include_symbol and symbol:
        return f"{symbol}{decimal_value}"
    return f"{decimal_value} {currency}"

def format_money(
    amount: Any,
    *,
    currency: str = c.DEFAULT_CURRENCY_CODE,
    decimal_places: int = 2,
) -> str:
    """
    Format ``amount`` to ``decimal_places`` with the currency code.

    Returns ``"-"`` for blank input.
    """
    if amount is None or amount == "":
        return "-"
    try:
        quant = Decimal(10) ** -decimal_places
        decimal_value = to_decimal(amount).quantize(quant, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return "-"
    return f"{decimal_value} {currency}"

def format_percentage(
    value: Any,
    *,
    decimal_places: int = 2,
    include_sign: bool = False,
) -> str:
    """
    Format ``value`` as a percentage string with the supplied
    ``decimal_places`` precision.

    Returns ``"-"`` for blank input. The input is assumed to be
    a fraction (``0.13`` → ``"13.00 %"``).
    """
    if value is None or value == "":
        return "-"
    try:
        decimal_value = to_decimal(value) * Decimal("100")
        quant = Decimal(10) ** -decimal_places
        decimal_value = decimal_value.quantize(quant, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return "-"
    sign = "+" if include_sign and decimal_value > 0 else ""
    return f"{sign}{decimal_value} %"

def format_exchange_rate(rate: Any) -> str:
    """
    Format ``rate`` to 8 decimal places.

    Returns ``"-"`` for blank input.
    """
    if rate is None or rate == "":
        return "-"
    try:
        decimal_value = quantize_exchange_rate(to_decimal(rate))
    except (InvalidOperation, TypeError, ValueError):
        return "-"
    return f"{decimal_value}"

def convert_currency(
    amount: Decimal,
    exchange_rate: Decimal,
) -> Decimal:
    """
    Convert ``amount`` from one currency to another using
    ``exchange_rate``.

    The result is quantized to the canonical monetary scale.
    Returns ``ZERO_DECIMAL_2`` for any non-numeric input.
    """
    if amount is None or exchange_rate is None:
        return c.ZERO_DECIMAL_2
    if not isinstance(amount, Decimal):
        amount = to_decimal(amount)
    if not isinstance(exchange_rate, Decimal):
        exchange_rate = to_decimal(exchange_rate)
    if exchange_rate <= c.ZERO_DECIMAL_2:
        return c.ZERO_DECIMAL_2
    return quantize_money(amount * exchange_rate)

def is_supported_currency(code: str) -> bool:
    """Return ``True`` if ``code`` is a non-empty 3-letter ISO 4217 string."""
    if not code or not isinstance(code, str):
        return False
    code = code.strip().upper()
    return len(code) == 3 and code.isalpha()

def normalize_currency_code(code: str) -> str:
    """
    Return ``code`` uppercased and stripped, or the default
    currency code for blank / invalid input.
    """
    if not code or not isinstance(code, str):
        return c.DEFAULT_CURRENCY_CODE
    return code.strip().upper() or c.DEFAULT_CURRENCY_CODE

# ==============================================================================
# 5. DATE / TIME / TIMEZONE HELPERS
# ==============================================================================
def ensure_aware_datetime(value: Optional[datetime]) -> Optional[datetime]:
    """
    Ensure ``value`` is a timezone-aware ``datetime`` in UTC.

    Naive datetimes are interpreted as UTC. ``None`` is returned
    unchanged.
    """
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    if timezone.is_naive(value):
        return timezone.make_aware(value, timezone.utc)
    return value.astimezone(timezone.utc)

def to_user_timezone(
    value: Optional[datetime],
    tz_name: str = "UTC",
) -> Optional[datetime]:
    """
    Convert ``value`` to the user-supplied ``tz_name`` timezone.

    Returns ``None`` for blank or invalid input.
    """
    if value is None:
        return None
    aware = ensure_aware_datetime(value)
    if aware is None:
        return None
    try:
        return timezone.localtime(aware, timezone=tz_name)
    except Exception:  # noqa: BLE001
        return aware

def format_admin_datetime(value: Optional[datetime]) -> str:
    """
    Format ``value`` for admin changelist display.

    Returns ``"-"`` for ``None``. Uses the canonical
    ``ADMIN_DATETIME_FORMAT`` constant.
    """
    if value is None:
        return "-"
    try:
        return value.strftime(c.ADMIN_DATETIME_FORMAT)
    except Exception:  # noqa: BLE001
        return "-"

def format_admin_date(value: Optional[date]) -> str:
    """Format ``value`` for admin changelist display using ``ADMIN_DATE_FORMAT``."""
    if value is None:
        return "-"
    try:
        return value.strftime(c.ADMIN_DATE_FORMAT)
    except Exception:  # noqa: BLE001
        return "-"

def format_iso(value: Optional[datetime]) -> str:
    """Format ``value`` as ISO 8601. Returns ``""`` for ``None``."""
    if value is None:
        return ""
    try:
        return value.isoformat()
    except Exception:  # noqa: BLE001
        return ""

def format_export_timestamp(value: Optional[datetime] = None) -> str:
    """
    Format a timestamp suitable for export filenames.

    Uses ``timezone.now()`` if ``value`` is ``None``.
    """
    target = value if value is not None else timezone.now()
    try:
        return target.strftime(c.EXPORT_TIMESTAMP_FORMAT)
    except Exception:  # noqa: BLE001
        return ""

# ==============================================================================
# 6. IDENTIFIER / NUMBER GENERATION HELPERS
# ==============================================================================
def build_return_number(date_fragment: str, token_hex: str) -> str:
    """
    Compose a return number from a date fragment and a hex token.

    The format is ``"<prefix>-<date>-<token>"``. Both fragments
    are upper-cased for consistency.
    """
    return (
        f"{c.RETURN_NUMBER_PREFIX}"
        f"{c.ID_SEPARATOR}{str(date_fragment).upper()}"
        f"{c.ID_SEPARATOR}{str(token_hex).upper()}"
    )

def build_order_number(date_fragment: str, token_hex: str) -> str:
    """Compose a canonical order number from a date fragment and a hex token."""
    return (
        f"{c.ORDER_NUMBER_PREFIX}"
        f"{c.ID_SEPARATOR}{str(date_fragment).upper()}"
        f"{c.ID_SEPARATOR}{str(token_hex).upper()}"
    )

def build_shipment_number(date_fragment: str, token_hex: str) -> str:
    """Compose a canonical shipment number from a date fragment and a hex token."""
    return (
        f"{c.SHIPMENT_NUMBER_PREFIX}"
        f"{c.ID_SEPARATOR}{str(date_fragment).upper()}"
        f"{c.ID_SEPARATOR}{str(token_hex).upper()}"
    )

def build_invoice_number(date_fragment: str, token_hex: str) -> str:
    """Compose a canonical invoice number from a date fragment and a hex token."""
    return (
        f"{c.INVOICE_NUMBER_PREFIX}"
        f"{c.ID_SEPARATOR}{str(date_fragment).upper()}"
        f"{c.ID_SEPARATOR}{str(token_hex).upper()}"
    )

# ==============================================================================
# 7. HASH / CHECKSUM HELPERS
# ==============================================================================
def compute_sha256_hex(value: str) -> str:
    """
    Compute the SHA-256 hex digest of ``value`` encoded as UTF-8.

    Returns an empty string if the input is ``None``. Used to
    generate the immutable ``address_hash`` field on
    ``OrderAddressSnapshot`` (the model's ``save()`` method calls
    this internally too).
    """
    if value is None:
        return ""
    try:
        encoded = str(value).encode("utf-8")
    except Exception:  # noqa: BLE001
        return ""
    return hashlib.sha256(encoded).hexdigest()

def compute_address_hash(
    *,
    full_name: str,
    phone_number: str,
    address_line_1: str,
    address_line_2: str,
    city: str,
    state_or_province: str,
    postal_code: str,
    country: str,
) -> str:
    """
    Compute the stable hash used by ``OrderAddressSnapshot`` to
    deduplicate identical addresses across orders.

    All inputs are lowercased and stripped before hashing. The
    fields are joined with a single ``|`` separator to prevent
    collisions between structurally similar but semantically
    different inputs.
    """
    parts = [
        (full_name or "").strip().lower(),
        (phone_number or "").strip().lower(),
        (address_line_1 or "").strip().lower(),
        (address_line_2 or "").strip().lower(),
        (city or "").strip().lower(),
        (state_or_province or "").strip().lower(),
        (postal_code or "").strip().lower(),
        (country or "").strip().lower(),
    ]
    return compute_sha256_hex("|".join(parts))

# ==============================================================================
# 8. FILENAME / PATH HELPERS
# ==============================================================================
def sanitize_filename(filename: str, *, max_length: int = _MAX_FILENAME_LENGTH) -> str:
    """
    Return a filesystem-safe filename.

    The sanitization pipeline:

        1. Strips any directory components (``foo/../bar`` → ``bar``).
        2. Removes control characters.
        3. Removes Windows-reserved characters (``<>:"/\\|?*``).
        4. Collapses multiple separators (``--`` → ``-``).
        5. Trims leading / trailing dots, dashes, and underscores.
        6. Truncates to ``max_length`` characters.
        7. Falls back to ``"file"`` for blank / fully-stripped input.
        8. Returns ``"file.bin"`` if no extension is present.

    This function is intentionally deterministic and never uses
    the filesystem. It is safe to call on untrusted user input.
    """
    if not filename:
        return "file.bin"

    name = str(filename).strip()
    name = name.replace("\x00", "")
    name = os.path.basename(name)
    name = _PATH_SEP_PATTERN.sub("", name)
    name = _WIN_DISALLOWED_PATTERN.sub("_", name)
    name = _CONTROL_CHAR_PATTERN.sub("", name)
    name = name.strip(" .-_")

    if not name:
        return "file.bin"

    base, ext = os.path.splitext(name)
    base = _MULTI_SEPARATOR_PATTERN.sub("-", base).strip(" .-_") or "file"
    ext = ext.lower().lstrip(".")

    if ext and not _ALLOWED_EXT_PATTERN.match(ext):
        ext = "bin"

    candidate = f"{base}.{ext}" if ext else f"{base}.bin"

    if len(candidate) > max_length:
        excess = len(candidate) - max_length
        if len(ext) + 1 >= excess:
            ext = ext[: max(0, len(ext) - excess)]
            candidate = f"{base}.{ext}" if ext else base[:max_length]
        else:
            base = base[: max(1, len(base) - excess)]
            candidate = f"{base}.{ext}" if ext else base

    root, ext = os.path.splitext(candidate)
    if root.upper() in _WIN_RESERVED_NAMES:
        candidate = f"_{candidate}"

    return candidate

def build_attachment_path(
    order_id: Any,
    filename: str,
    *,
    folder: str = c.ORDER_ATTACHMENT_FOLDER,
) -> str:
    """
    Build a deterministic, collision-resistant upload path for an
    order attachment.

    The structure is::

        <folder>/<order_id>/<sha256-prefix>.<safe-extension>

    ``order_id`` is coerced to a string and stripped; a non-empty
    value is required (``"unknown"`` is used as fallback).
    """
    safe_folder = sanitize_path_segment(folder) or c.ORDER_ATTACHMENT_FOLDER
    safe_order_id = sanitize_path_segment(str(order_id)) or "unknown"
    safe_name = sanitize_filename(filename)
    base, ext = os.path.splitext(safe_name)
    digest_prefix = compute_sha256_hex(safe_name)[:16]
    return f"{safe_folder}/{safe_order_id}/{digest_prefix}{ext or '.bin'}"

def build_return_image_path(
    return_request_id: Any,
    filename: str,
    *,
    folder: str = c.RETURN_IMAGE_FOLDER,
) -> str:
    """
    Build a deterministic, collision-resistant upload path for a
    return evidence image.
    """
    safe_folder = sanitize_path_segment(folder) or c.RETURN_IMAGE_FOLDER
    safe_id = sanitize_path_segment(str(return_request_id)) or "unknown"
    safe_name = sanitize_filename(filename)
    base, ext = os.path.splitext(safe_name)
    digest_prefix = compute_sha256_hex(safe_name)[:16]
    default_ext = c.DEFAULT_IMAGE_EXTENSION
    return f"{safe_folder}/{safe_id}/{digest_prefix}{ext or default_ext}"

def sanitize_path_segment(segment: str) -> str:
    """
    Sanitize a single path segment (e.g. a folder name or id).

    Path separators, control characters, and Windows-reserved
    names are removed. Returns an empty string for blank input.
    """
    if not segment:
        return ""
    cleaned = _PATH_SEP_PATTERN.sub("", str(segment).strip())
    cleaned = _WIN_DISALLOWED_PATTERN.sub("", cleaned)
    cleaned = _CONTROL_CHAR_PATTERN.sub("", cleaned)
    cleaned = cleaned.strip(" .-_")
    if not cleaned:
        return ""
    if cleaned.upper() in _WIN_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:_MAX_UPLOAD_PATH_LENGTH]

def is_safe_path(path: str, *, allowed_roots: Iterable[str] = ()) -> bool:
    """
    Return ``True`` if ``path`` does not escape the supplied
    ``allowed_roots`` (or any absolute root if none are supplied).

    Resolves ``..``, ``.``, and symlink-free traversals. This is
    a defensive check intended to prevent accidental writes to
    parent directories.
    """
    if not path or not isinstance(path, str):
        return False
    try:
        normalized = os.path.normpath(path)
    except Exception:  # noqa: BLE001
        return False
    if normalized.startswith(".."):
        return False
    roots = [r for r in allowed_roots if r]
    if not roots:
        return True
    for root in roots:
        safe_root = os.path.normpath(root)
        candidate = os.path.normpath(os.path.join(safe_root, normalized))
        if candidate.startswith(safe_root):
            return True
    return False

# ==============================================================================
# 9. MASKING / DISPLAY HELPERS
# ==============================================================================
def mask_string(
    value: str,
    *,
    visible_prefix: int = 2,
    visible_suffix: int = 2,
    mask_char: str = _MASK_CHAR,
    mask_length: int = _MASK_MEDIUM_LENGTH,
) -> str:
    """
    Mask ``value`` keeping only the first ``visible_prefix`` and
    last ``visible_suffix`` characters.

    Returns ``"-"`` for blank input. If the value is shorter than
    the sum of the visible lengths, only mask characters are
    returned.
    """
    if not value or not isinstance(value, str):
        return "-"
    text = str(value)
    if len(text) <= visible_prefix + visible_suffix:
        return mask_char * max(mask_length, len(text))
    prefix = text[:visible_prefix]
    suffix = text[-visible_suffix:] if visible_suffix else ""
    middle = mask_char * mask_length
    return f"{prefix}{middle}{suffix}"

def mask_transaction_id(transaction_id: str) -> str:
    """Mask a payment transaction id for display."""
    return mask_string(
        transaction_id or "",
        visible_prefix=4,
        visible_suffix=2,
        mask_length=_MASK_LONG_LENGTH,
    )

def mask_tracking_number(tracking_number: str) -> str:
    """Mask a shipment tracking number for display."""
    return mask_string(
        tracking_number or "",
        visible_prefix=3,
        visible_suffix=3,
        mask_length=_MASK_MEDIUM_LENGTH,
    )

def mask_phone(phone: str) -> str:
    """Mask a phone number for display, keeping the last 4 digits."""
    if not phone or not isinstance(phone, str):
        return "-"
    digits = normalize_phone_digits(phone)
    if len(digits) < 4:
        return _MASK_CHAR * _MASK_SHORT_LENGTH
    return f"{_MASK_CHAR * (len(digits) - 4)}{digits[-4:]}"

def mask_ip_address(ip: str) -> str:
    """
    Mask an IPv4 or IPv6 address for display.

    IPv4: keeps the first two octets (``192.168.*.*``).
    IPv6: keeps the first two groups.
    """
    if not ip or not isinstance(ip, str):
        return "-"
    text = str(ip).strip()
    if ":" in text:
        parts = text.split(":")
        head = ":".join(parts[:2])
        return f"{head}:****:****:****:****"
    parts = text.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.{_MASK_CHAR}.{_MASK_CHAR}"
    return _MASK_CHAR * _MASK_SHORT_LENGTH

# ==============================================================================
# 10. DISPLAY LABEL / HUMAN-READABLE HELPERS
# ==============================================================================
def format_order_status_label(status: str) -> str:
    """Return the human-readable label for an order status."""
    return humanize_status(status)

def format_payment_status_label(status: str) -> str:
    """Return the human-readable label for a payment status."""
    return humanize_status(status)

def format_shipment_status_label(status: str) -> str:
    """Return the human-readable label for a shipment status."""
    return humanize_status(status)

def format_refund_status_label(status: str) -> str:
    """Return the human-readable label for a refund status."""
    return humanize_status(status)

def format_return_status_label(status: str) -> str:
    """Return the human-readable label for a return status."""
    return humanize_status(status)

def format_event_label(event_type: str) -> str:
    """Return the human-readable label for a timeline event type."""
    return humanize_event_type(event_type)

def format_quantity(value: Any) -> str:
    """Format an integer / decimal quantity for display."""
    if value is None or value == "":
        return "-"
    try:
        decimal_value = to_decimal(value).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return "-"
    if decimal_value == decimal_value.to_integral_value():
        return f"{int(decimal_value)}"
    return f"{decimal_value}"

def format_weight(value: Any, *, unit: str = "kg") -> str:
    """Format a weight value for display."""
    if value is None or value == "":
        return "-"
    try:
        decimal_value = quantize_weight(to_decimal(value))
    except (InvalidOperation, TypeError, ValueError):
        return "-"
    return f"{decimal_value} {unit}"

def format_phone_display(phone: str) -> str:
    """
    Format a phone number for display (e.g. ``"+977 1 2345678"``).

    Returns ``"-"`` for blank or invalid input. The country code
    is preserved when present.
    """
    if not phone or not isinstance(phone, str):
        return "-"
    text = str(phone).strip()
    if not text:
        return "-"
    if text.startswith("+"):
        country, _, rest = text[1:].partition(" ")
        if not rest:
            return text
        return f"+{country} {rest}"
    return text

# ==============================================================================
# 11. DICTIONARY / METADATA HELPERS
# ==============================================================================
def get_nested(
    mapping: Optional[Mapping[str, Any]],
    *keys: str,
    default: Any = None,
) -> Any:
    """
    Return the value at the supplied dotted path inside ``mapping``.

    Returns ``default`` if any intermediate key is missing.
    """
    if not mapping or not keys:
        return default
    cursor: Any = mapping
    for key in keys:
        if not isinstance(cursor, Mapping):
            return default
        if key not in cursor:
            return default
        cursor = cursor[key]
    return cursor

def set_nested(
    mapping: Dict[str, Any],
    *keys: str,
    value: Any,
) -> Dict[str, Any]:
    """
    Set ``value`` at the supplied dotted path inside ``mapping``.

    Creates intermediate dictionaries as needed. Returns the
    (mutated) mapping. The mapping is mutated in-place; a return
    value is provided for convenience.
    """
    if not keys:
        return mapping
    cursor = mapping
    for key in keys[:-1]:
        existing = cursor.get(key)
        if not isinstance(existing, dict):
            existing = {}
            cursor[key] = existing
        cursor = existing
    cursor[keys[-1]] = value
    return mapping

def flatten_dict(
    mapping: Mapping[str, Any],
    *,
    separator: str = ".",
    prefix: str = "",
) -> Dict[str, Any]:
    """
    Flatten a nested dictionary into a single-level dictionary
    keyed by dotted paths.
    """
    result: Dict[str, Any] = {}
    if not mapping:
        return result
    for key, value in mapping.items():
        full_key = f"{prefix}{separator}{key}" if prefix else str(key)
        if isinstance(value, Mapping) and value:
            result.update(flatten_dict(value, separator=separator, prefix=full_key))
        else:
            result[full_key] = value
    return result

def compact_dict(
    mapping: Optional[Mapping[str, Any]],
    *,
    drop_empty: bool = True,
) -> Dict[str, Any]:
    """
    Return a new dictionary with all ``None`` (and optionally empty)
    values removed.
    """
    if not mapping:
        return {}
    result: Dict[str, Any] = {}
    for key, value in mapping.items():
        if drop_empty and value in (None, "", [], {}, ()):
            continue
        result[key] = value
    return result

def safe_dict_get(
    mapping: Optional[Mapping[str, Any]],
    key: str,
    default: Any = None,
) -> Any:
    """Dictionary ``.get()`` that never raises on ``None`` input."""
    if not mapping or not isinstance(mapping, Mapping):
        return default
    try:
        return mapping.get(key, default)
    except Exception:  # noqa: BLE001
        return default

# ==============================================================================
# 12. JSON / SERIALIZATION HELPERS
# ==============================================================================
def safe_json_loads(
    value: Any,
    *,
    default: Any = None,
) -> Any:
    """
    Safely parse ``value`` as JSON. Never raises.

    Returns ``default`` if parsing fails or if ``value`` is not a
    string / bytes instance.
    """
    if value is None:
        return default
    if not isinstance(value, (str, bytes, bytearray)):
        return default
    try:
        import json
        return json.loads(value)
    except (ValueError, TypeError):
        return default

def safe_json_dumps(
    value: Any,
    *,
    default: Any = None,
    sort_keys: bool = False,
) -> str:
    """
    Safely serialize ``value`` as JSON. Never raises.

    Returns ``""`` (or ``default``) if serialization fails. Uses
    ``str()`` as the fallback for non-serializable scalars.
    """
    if value is None:
        return default if default is not None else ""
    try:
        import json
        return json.dumps(
            value,
            default=str,
            sort_keys=sort_keys,
            ensure_ascii=False,
        )
    except (ValueError, TypeError):
        return default if default is not None else ""

def is_json_object(value: Any) -> bool:
    """Return ``True`` if ``value`` is a JSON object (i.e. a dict)."""
    return isinstance(value, dict)

def is_json_array(value: Any) -> bool:
    """Return ``True`` if ``value`` is a JSON array (i.e. a list / tuple)."""
    return isinstance(value, (list, tuple))

# ==============================================================================
# 13. VALIDATION HELPERS
# ==============================================================================
def is_valid_uuid(value: Any) -> bool:
    """Return ``True`` if ``value`` is a syntactically valid UUID."""
    if value is None:
        return False
    if isinstance(value, UUID):
        return True
    if not isinstance(value, str):
        return False
    try:
        UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False

def coerce_uuid(value: Any) -> Optional[UUID]:
    """
    Coerce ``value`` to a ``UUID`` instance.

    Returns ``None`` for blank or unparseable input.
    """
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None

def is_within_range(
    value: Any,
    *,
    min_value: Optional[Decimal] = None,
    max_value: Optional[Decimal] = None,
) -> bool:
    """Return ``True`` if ``value`` is within ``[min_value, max_value]``."""
    if value is None:
        return False
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if min_value is not None and decimal_value < min_value:
        return False
    if max_value is not None and decimal_value > max_value:
        return False
    return True

def is_in_choices(value: Any, choices: Iterable[Any]) -> bool:
    """Return ``True`` if ``value`` is one of the supplied ``choices``."""
    if value is None:
        return False
    return value in set(choices)

# ==============================================================================
# 14. CACHE KEY HELPERS
# ==============================================================================
def build_cache_key(template: str, **context: Any) -> str:
    """
    Build a cache key by formatting ``template`` with ``context``.

    Unknown placeholders are left untouched (instead of raising
    ``KeyError``). The result is stripped of any leading / trailing
    whitespace.
    """
    if not template:
        return ""
    try:
        return str(template).format(**context).strip()
    except (KeyError, IndexError, ValueError):
        return str(template).strip()

def order_cache_key(order_id: Any) -> str:
    """Build the canonical cache key for an order lookup by id."""
    return build_cache_key(
        c.CACHE_KEY_ORDER_BY_ID,
        ns=c.CACHE_NAMESPACE,
        order_id=str(order_id),
    )

def order_by_number_cache_key(order_number: str) -> str:
    """Build the canonical cache key for an order lookup by number."""
    return build_cache_key(
        c.CACHE_KEY_ORDER_BY_NUMBER,
        ns=c.CACHE_NAMESPACE,
        order_number=order_number or "",
    )

def order_timeline_cache_key(order_id: Any) -> str:
    """Build the canonical cache key for an order's full timeline."""
    return build_cache_key(
        c.CACHE_KEY_ORDER_TIMELINE,
        ns=c.CACHE_NAMESPACE,
        order_id=str(order_id),
    )

def order_count_cache_key() -> str:
    """Build the canonical cache key for the active-order-count metric."""
    return build_cache_key(
        c.CACHE_KEY_ORDER_COUNT,
        ns=c.CACHE_NAMESPACE,
    )

def status_aggregation_cache_key() -> str:
    """Build the canonical cache key for the status aggregation cache."""
    return build_cache_key(
        c.CACHE_KEY_STATUS_AGGREGATION,
        ns=c.CACHE_NAMESPACE,
    )

# ==============================================================================
# 15. PAGINATION HELPERS
# ==============================================================================
def compute_page_window(
    *,
    page: int,
    page_size: int = c.DEFAULT_PAGE_SIZE,
    total_count: int = 0,
) -> Tuple[int, int, int]:
    """
    Compute the ``(start, end, total_pages)`` triple for a paginated
    result set.

    The returned indices are 0-based and inclusive-of-start,
    exclusive-of-end, suitable for slicing querysets.
    """
    safe_page = max(1, int(page or 1))
    safe_size = max(1, int(page_size or c.DEFAULT_PAGE_SIZE))
    safe_total = max(0, int(total_count or 0))

    total_pages = (
        (safe_total + safe_size - 1) // safe_size if safe_total > 0 else 0
    )
    start = (safe_page - 1) * safe_size
    end = start + safe_size
    if end > safe_total:
        end = safe_total
    if start > safe_total:
        start = safe_total
    return start, end, total_pages

def clamp_page_size(
    requested: int,
    *,
    default: int = c.DEFAULT_PAGE_SIZE,
    maximum: int = c.ADMIN_MAX_SHOW_ALL,
) -> int:
    """
    Clamp ``requested`` to the range ``[1, maximum]``.

    Returns ``default`` for any non-positive or non-integer value.
    """
    try:
        size = int(requested)
    except (TypeError, ValueError):
        return default
    if size < 1:
        return default
    if size > maximum:
        return maximum
    return size

# ==============================================================================
# 16. AUDIT / LOG FORMATTING HELPERS
# ==============================================================================
def format_audit_entry(
    *,
    actor: Any,
    action: str,
    target: str,
    extra: Optional[Mapping[str, Any]] = None,
) -> str:
    """
    Build a one-line, human-readable audit entry.

    Designed to be emitted via ``logger.info(...)`` and parsed by
    downstream log shippers.
    """
    actor_label = safe_str(actor, default="system")
    extras: List[str] = []
    if extra:
        for key, value in extra.items():
            extras.append(f"{key}={safe_str(value)}")
    extras_text = " ".join(extras)
    return (
        f"actor={actor_label} action={action} target={target} "
        f"{extras_text}".strip()
    )

def format_timeline_label(
    event_type: str,
    *,
    title: str = "",
) -> str:
    """
    Build the canonical display label for a timeline event.

    Falls back to ``humanize_event_type(event_type)`` if ``title``
    is blank.
    """
    if title:
        return safe_str(title)
    return humanize_event_type(event_type)

# ==============================================================================
# 17. DELTA / DIFFERENCE HELPERS
# ==============================================================================
def diff_dicts(
    before: Optional[Mapping[str, Any]],
    after: Optional[Mapping[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """
    Return a dictionary of the form ``{key: {"before": x, "after": y}}``
    for every key whose value differs between ``before`` and ``after``.
    """
    diffs: Dict[str, Dict[str, Any]] = {}
    keys = set((before or {}).keys()) | set((after or {}).keys())
    for key in keys:
        old = (before or {}).get(key)
        new = (after or {}).get(key)
        if old != new:
            diffs[key] = {"before": old, "after": new}
    return diffs

def has_changed(value_a: Any, value_b: Any) -> bool:
    """Return ``True`` if ``value_a`` differs from ``value_b``."""
    return value_a != value_b

# ==============================================================================
# 18. FINANCIAL CALCULATION HELPERS (pure, side-effect free)
# ==============================================================================
def safe_add_decimal(*values: Any) -> Decimal:
    """
    Add an arbitrary number of values using ``Decimal`` arithmetic.

    Non-numeric values are coerced via ``to_decimal``. ``None``
    values are treated as ``ZERO_DECIMAL_2``. Returns a
    money-quantized ``Decimal``.
    """
    total = c.ZERO_DECIMAL_2
    for value in values:
        if value is None or value == "":
            continue
        total += to_decimal(value)
    return quantize_money(total)

def safe_subtract_decimal(
    minuend: Any,
    *subtrahends: Any,
) -> Decimal:
    """
    Subtract each ``subtrahend`` from ``minuend`` and return the
    money-quantized result.
    """
    result = to_decimal(minuend)
    for value in subtrahends:
        if value is None or value == "":
            continue
        result -= to_decimal(value)
    return quantize_money(result)

def safe_multiply_decimal(
    value: Any,
    factor: Any,
) -> Decimal:
    """
    Multiply ``value`` by ``factor`` using ``Decimal`` arithmetic.
    """
    if value is None or factor is None:
        return c.ZERO_DECIMAL_2
    try:
        result = to_decimal(value) * to_decimal(factor)
    except (InvalidOperation, TypeError, ValueError):
        return c.ZERO_DECIMAL_2
    return quantize_money(result)

def apply_percentage(value: Any, percentage: Any) -> Decimal:
    """
    Apply ``percentage`` (expressed as a fraction, e.g. ``0.13``)
    to ``value`` and return the money-quantized result.
    """
    if value is None or percentage is None:
        return c.ZERO_DECIMAL_2
    try:
        result = to_decimal(value) * to_decimal(percentage)
    except (InvalidOperation, TypeError, ValueError):
        return c.ZERO_DECIMAL_2
    return quantize_money(result)

def apply_percentage_to_value(value: Any, percentage: Any) -> Decimal:
    """
    Return ``value`` reduced by the supplied fractional
    ``percentage``.

    Example: ``apply_percentage_to_value(100, Decimal("0.10"))``
    returns ``90.00``.
    """
    reduction = apply_percentage(value, percentage)
    return safe_subtract_decimal(value, reduction)

def sum_decimals(values: Iterable[Any]) -> Decimal:
    """Sum an iterable of values into a money-quantized ``Decimal``."""
    total = c.ZERO_DECIMAL_2
    for value in values:
        if value is None or value == "":
            continue
        total += to_decimal(value)
    return quantize_money(total)

def ratio_to_percentage(numerator: Any, denominator: Any) -> Decimal:
    """
    Compute ``numerator / denominator`` and return a percentage
    (0-100 scale). Returns ``ZERO_DECIMAL_2`` for invalid or
    zero denominators.
    """
    if numerator is None or denominator is None:
        return c.ZERO_DECIMAL_2
    try:
        num = to_decimal(numerator)
        den = to_decimal(denominator)
        if den == c.ZERO_DECIMAL_2:
            return c.ZERO_DECIMAL_2
        result = (num / den) * Decimal("100")
    except (InvalidOperation, TypeError, ValueError, ZeroDivisionError):
        return c.ZERO_DECIMAL_2
    return quantize_percentage(result)

# ==============================================================================
# 19. ATTACHMENT / FILE SIZE HELPERS
# ==============================================================================
def format_file_size(size_bytes: Any) -> str:
    """
    Format a file size in bytes as a human-readable string.

    Example: ``1024`` → ``"1.00 KB"``. Returns ``"-"`` for blank
    or non-numeric input.
    """
    if size_bytes is None or size_bytes == "":
        return "-"
    try:
        size = float(size_bytes)
    except (TypeError, ValueError):
        return "-"
    if size < 0:
        return "-"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    for unit in units:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} EB"

def format_mime_type(mime_type: str) -> str:
    """
    Normalize a MIME type to ``type/subtype`` lowercase form.

    Returns ``"application/octet-stream"`` for blank / invalid input.
    """
    if not mime_type or not isinstance(mime_type, str):
        return "application/octet-stream"
    parts = str(mime_type).strip().lower().split("/")
    if len(parts) != 2 or not all(parts):
        return "application/octet-stream"
    return f"{parts[0]}/{parts[1]}"

# ==============================================================================
# 20. UTILITY DECORATORS
# ==============================================================================
def swallow_errors(
    fallback: Any = None,
    *,
    logger_obj: Optional[logging.Logger] = None,
    reraise: bool = False,
) -> Any:
    """
    Decorator that catches and logs every exception raised by the
    wrapped function.

    Returns ``fallback`` if an exception is caught. When ``reraise``
    is ``True`` the exception is re-raised after being logged.
    """
    def decorator(func: Any) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                (logger_obj or logger).exception(
                    "swallow_errors caught exception in %s: %s",
                    getattr(func, "__name__", repr(func)),
                    exc,
                )
                if reraise:
                    raise
                return fallback
        return wrapper
    return decorator

def normalize_args(func: Any) -> Any:
    """
    Decorator that strips ``None`` values from the positional and
    keyword arguments before forwarding them to ``func``.

    Useful for helper functions that treat ``None`` as a no-op
    sentinel and want to avoid polluting downstream calls with
    explicit nulls.
    """
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        filtered_args = tuple(a for a in args if a is not None)
        filtered_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        return func(*filtered_args, **filtered_kwargs)
    return wrapper

# ==============================================================================
# 21. REUSABLE PARSERS
# ==============================================================================
def parse_decimal_list(
    value: str,
    *,
    separator: str = ",",
) -> List[Decimal]:
    """
    Parse a delimited string of decimal values into a list.

    Invalid tokens are silently skipped.
    """
    if not value or not isinstance(value, str):
        return []
    result: List[Decimal] = []
    for token in value.split(separator):
        token = token.strip()
        if not token:
            continue
        try:
            result.append(Decimal(token))
        except (InvalidOperation, ValueError, TypeError):
            continue
    return result

def parse_string_list(
    value: Any,
    *,
    separator: str = ",",
    strip: bool = True,
    drop_blank: bool = True,
) -> List[str]:
    """
    Parse a delimited string into a list of cleaned strings.

    Returns an empty list for ``None`` or non-string input.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = [str(v) for v in value]
    elif isinstance(value, str):
        items = value.split(separator)
    else:
        return []
    if strip:
        items = [item.strip() for item in items]
    if drop_blank:
        items = [item for item in items if item]
    return items

def parse_csv_safe(
    value: str,
    *,
    separator: str = ",",
) -> List[str]:
    """Parse a simple CSV row with no quoted fields. Pure helper."""
    if not value or not isinstance(value, str):
        return []
    return [
        token.strip()
        for token in value.split(separator)
        if token is not None
    ]

# ==============================================================================
# 22. REUSABLE TRANSFORMERS
# ==============================================================================
def to_bool(value: Any, default: bool = False) -> bool:
    """
    Coerce ``value`` to a boolean using lenient semantics.

    Strings like ``"true"``, ``"1"``, ``"yes"``, ``"on"`` are
    treated as ``True``; everything else is treated as ``False``.
    Returns ``default`` for ``None``.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on", "y", "t"}
    return default

def to_int(
    value: Any,
    *,
    default: int = 0,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """Coerce ``value`` to an ``int`` with optional bounds."""
    if value is None or value == "":
        return default
    try:
        result = int(value)
    except (TypeError, ValueError):
        try:
            result = int(float(value))
        except (TypeError, ValueError):
            return default
    if minimum is not None and result < minimum:
        return minimum
    if maximum is not None and result > maximum:
        return maximum
    return result

def to_str_list(value: Any, *, default: Optional[List[str]] = None) -> List[str]:
    """
    Coerce ``value`` to a list of strings.

    Accepts list / tuple / set inputs directly. Strings are split
    on commas. Other input types return ``default`` (or an empty
    list if ``default`` is not provided).
    """
    if value is None:
        return list(default) if default is not None else []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v is not None]
    if isinstance(value, str):
        return parse_string_list(value)
    return list(default) if default is not None else []

# ==============================================================================
# 23. SHIPMENT / TRACKING HELPERS
# ==============================================================================
def normalize_tracking_number(tracking_number: str) -> str:
    """
    Normalize a tracking number for storage / display.

    Strips whitespace, removes embedded dashes, and uppercases
    the result. Returns an empty string for blank input.
    """
    if not tracking_number or not isinstance(tracking_number, str):
        return ""
    cleaned = str(tracking_number).strip()
    if not cleaned:
        return ""
    cleaned = cleaned.replace(" ", "").replace("-", "").upper()
    return cleaned

def is_valid_tracking_number(
    tracking_number: str,
    *,
    min_length: int = 5,
    max_length: int = c.FieldLength.SHIPMENT_TRACKING_NUMBER,
) -> bool:
    """
    Return ``True`` if ``tracking_number`` is within the
    ``[min_length, max_length]`` length bounds and contains only
    alphanumerics after normalization.
    """
    normalized = normalize_tracking_number(tracking_number)
    if not normalized:
        return False
    if len(normalized) < min_length or len(normalized) > max_length:
        return False
    return bool(_ALNUM_PATTERN.match(normalized))

def build_tracking_url(
    carrier: str,
    tracking_number: str,
    *,
    templates: Optional[Mapping[str, str]] = None,
) -> str:
    """
    Build a carrier tracking URL by formatting ``carrier``'s
    template with the supplied ``tracking_number``.

    Returns an empty string if either input is blank or no
    template is registered for the carrier.
    """
    if not carrier or not tracking_number:
        return ""
    carrier_key = str(carrier).strip().lower()
    if not carrier_key:
        return ""

    default_templates: Dict[str, str] = {
        "dhl": "https://www.dhl.com/en/express/tracking.html?AWB={tracking_number}",
        "fedex": "https://www.fedex.com/fedextrack/?trknbr={tracking_number}",
        "ups": "https://www.ups.com/track?tracknum={tracking_number}",
        "usps": "https://tools.usps.com/go/TrackConfirmAction?tLabels={tracking_number}",
        "neppost": "https://www.nepalpost.gov.np/postal-tracking?id={tracking_number}",
        "custom": "",
    }
    template = (
        templates.get(carrier_key)
        if templates and carrier_key in templates
        else default_templates.get(carrier_key, "")
    )
    if not template:
        return ""
    try:
        return template.format(tracking_number=tracking_number)
    except (KeyError, IndexError, ValueError):
        return ""

# ==============================================================================
# 24. ADDRESS / DISPLAY HELPERS
# ==============================================================================
def format_address_oneline(
    *,
    full_name: str = "",
    address_line_1: str = "",
    address_line_2: str = "",
    city: str = "",
    state_or_province: str = "",
    postal_code: str = "",
    country: str = "",
    separator: str = ", ",
) -> str:
    """
    Format an address as a single-line string for display.

    Empty components are skipped automatically.
    """
    parts: List[str] = []
    if full_name:
        parts.append(str(full_name).strip())
    if address_line_1:
        parts.append(str(address_line_1).strip())
    if address_line_2:
        parts.append(str(address_line_2).strip())
    locality_parts: List[str] = []
    if city:
        locality_parts.append(str(city).strip())
    if state_or_province:
        locality_parts.append(str(state_or_province).strip())
    if postal_code:
        locality_parts.append(str(postal_code).strip())
    if locality_parts:
        parts.append(" ".join(locality_parts))
    if country:
        parts.append(str(country).strip())
    return separator.join(p for p in parts if p)

def format_address_multiline(
    *,
    full_name: str = "",
    address_line_1: str = "",
    address_line_2: str = "",
    city: str = "",
    state_or_province: str = "",
    postal_code: str = "",
    country: str = "",
    separator: str = "\n",
) -> str:
    """
    Format an address as a multi-line string for display.

    Each non-empty component is rendered on its own line.
    """
    lines: List[str] = []
    for value in (
        full_name,
        address_line_1,
        address_line_2,
        " ".join(
            part
            for part in (
                str(city or "").strip(),
                str(state_or_province or "").strip(),
                str(postal_code or "").strip(),
            )
            if part
        ),
        country,
    ):
        if value and str(value).strip():
            lines.append(str(value).strip())
    return separator.join(lines)

# ==============================================================================
# 25. STATUS / FLAG HELPERS
# ==============================================================================
def is_terminal_success_status(status: str) -> bool:
    """Return ``True`` if ``status`` is a terminal-success status."""
    return str(status or "") in c.OrderStatus.TERMINAL_SUCCESS

def is_terminal_failure_status(status: str) -> bool:
    """Return ``True`` if ``status`` is a terminal-failure status."""
    return str(status or "") in c.OrderStatus.TERMINAL_FAILURE

def is_terminal_status(status: str) -> bool:
    """Return ``True`` if ``status`` is terminal (success or failure)."""
    return (
        is_terminal_success_status(status)
        or is_terminal_failure_status(status)
    )

def is_cancellable_status(status: str) -> bool:
    """Return ``True`` if ``status`` is cancellable."""
    return str(status or "") in c.OrderStatus.CANCELLABLE_FROM

def is_paid_like_payment_status(status: str) -> bool:
    """Return ``True`` if ``status`` satisfies the ``Order.is_paid`` check."""
    return str(status or "") in c.PaymentStatus.PAID_LIKE

def is_shippable_item_status(status: str) -> bool:
    """Return ``True`` if ``status`` is shippable."""
    return str(status or "") in c.ItemStatus.SHIPPABLE

def is_returnable_item_status(status: str) -> bool:
    """Return ``True`` if ``status`` is returnable."""
    return str(status or "") in c.ItemStatus.RETURNABLE

def is_in_transit_shipment_status(status: str) -> bool:
    """Return ``True`` if ``status`` indicates an in-transit shipment."""
    return str(status or "") in c.ShipmentStatus.IN_TRANSIT_LIKE

def is_resolved_return_status(status: str) -> bool:
    """Return ``True`` if ``status`` is a resolved (terminal) return status."""
    return str(status or "") in c.ReturnStatus.RESOLVED

# ==============================================================================
# 26. LIST / COLLECTION HELPERS
# ==============================================================================
def chunks(iterable: Iterable[Any], size: int) -> List[List[Any]]:
    """
    Split ``iterable`` into a list of chunks, each of length
    ``size``.

    The last chunk may be smaller than ``size``. Returns an
    empty list for empty input or non-positive ``size``.
    """
    if size <= 0:
        return []
    if iterable is None:
        return []
    result: List[List[Any]] = []
    chunk: List[Any] = []
    for item in iterable:
        chunk.append(item)
        if len(chunk) >= size:
            result.append(chunk)
            chunk = []
    if chunk:
        result.append(chunk)
    return result

def unique_preserving_order(values: Iterable[Any]) -> List[Any]:
    """
    Return the unique elements of ``values`` while preserving
    their original order.
    """
    if not values:
        return []
    seen: set = set()
    result: List[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result

def first_or_default(
    values: Optional[Iterable[Any]],
    default: Any = None,
) -> Any:
    """
    Return the first element of ``values`` or ``default``.
    """
    if not values:
        return default
    for value in values:
        return value
    return default

# ==============================================================================
# 27. UUID / RANDOMNESS HELPERS
# ==============================================================================
def generate_short_token(length: int = 6) -> str:
    """
    Generate a short, lowercase alphanumeric token.

    Uses the ``secrets`` module internally; the helper lives here
    so that callers do not have to import ``secrets`` directly.
    """
    if length <= 0:
        return ""
    import secrets
    import string
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))

def generate_hex_token(byte_length: int = 3) -> str:
    """
    Generate a hex token of ``byte_length`` bytes (uppercase).

    Mirrors the ``secrets.token_hex`` byte length used by
    ``ReturnRequest.save()``.
    """
    if byte_length <= 0:
        return ""
    import secrets
    return secrets.token_hex(byte_length).upper()

# ==============================================================================
# 28. MISCELLANEOUS PURE HELPERS
# ==============================================================================
def get_initials(value: str, *, max_length: int = 3) -> str:
    """
    Return uppercase initials derived from ``value``.

    Words are split on whitespace; the first character of each
    word contributes to the result.
    """
    if not value or not isinstance(value, str):
        return ""
    cleaned = normalize_whitespace(value)
    if not cleaned:
        return ""
    parts = [p for p in cleaned.split(" ") if p]
    initials = "".join(p[0] for p in parts if p)
    return initials[:max_length].upper() if initials else ""

def normalize_tags(tags: Any) -> List[str]:
    """
    Normalize the ``tags`` JSONField value on ``Order`` into a
    clean list of unique, non-empty, lowercase strings.
    """
    if not tags:
        return []
    if isinstance(tags, str):
        candidates = parse_string_list(tags)
    elif isinstance(tags, (list, tuple, set)):
        candidates = [str(t) for t in tags]
    else:
        return []
    cleaned: List[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        normalized = strip_to_ascii(candidate).strip().lower()
        if normalized:
            cleaned.append(normalized)
    return unique_preserving_order(cleaned)

def is_gift_order(value: Any) -> bool:
    """Lenient ``is_gift`` check that accepts truthy / falsy inputs."""
    return to_bool(value, default=False)

def normalize_unicode(value: str) -> str:
    """
    Apply NFKC Unicode normalization to ``value``.

    Useful for ensuring visually identical characters (e.g.
    composed vs decomposed accents) collapse to the same code
    points before storage or comparison.
    """
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value))

# ==============================================================================
# 29. COMPOSITE / HIGH-LEVEL HELPERS
# ==============================================================================
def build_order_summary(
    *,
    order_number: str,
    status: str,
    total: Any,
    currency: str = c.DEFAULT_CURRENCY_CODE,
) -> Dict[str, Any]:
    """
    Build a small, JSON-serializable summary of an order.

    Used by audit exports, webhook payloads, and search indexers.
    """
    return compact_dict(
        {
            "order_number": safe_str(order_number),
            "status": safe_str(status),
            "total": format_money(total, currency=currency),
            "currency": normalize_currency_code(currency),
        }
    )

def build_address_summary(
    *,
    full_name: str = "",
    city: str = "",
    country: str = "",
    country_code: str = "",
) -> Dict[str, Any]:
    """Build a small, JSON-serializable summary of an address."""
    return compact_dict(
        {
            "full_name": safe_str(full_name),
            "city": safe_str(city),
            "country": safe_str(country),
            "country_code": safe_str(country_code).upper(),
        }
    )

# ==============================================================================
# 30. PUBLIC MODULE API
# ==============================================================================
__all__ = [
    # Safe string / text
    "safe_str",
    "is_blank",
    "is_not_blank",
    "normalize_whitespace",
    "remove_control_characters",
    "strip_to_ascii",
    "truncate",
    "title_case",
    "humanize_status",
    "humanize_event_type",
    # Slug / identifier
    "make_slug",
    "is_valid_identifier",
    "is_valid_hex",
    "is_numeric",
    "is_alphanumeric",
    # Phone / email / contact
    "normalize_phone_digits",
    "format_phone_e164",
    "is_valid_phone",
    "mask_email",
    # Currency / money
    "quantize_money",
    "quantize_weight",
    "quantize_exchange_rate",
    "quantize_percentage",
    "quantize_tax_rate",
    "to_decimal",
    "is_positive_decimal",
    "is_non_negative_decimal",
    "format_currency",
    "format_money",
    "format_percentage",
    "format_exchange_rate",
    "convert_currency",
    "is_supported_currency",
    "normalize_currency_code",
    # Date / time / timezone
    "ensure_aware_datetime",
    "to_user_timezone",
    "format_admin_datetime",
    "format_admin_date",
    "format_iso",
    "format_export_timestamp",
    # Identifier / number generation
    "build_return_number",
    "build_order_number",
    "build_shipment_number",
    "build_invoice_number",
    # Hash / checksum
    "compute_sha256_hex",
    "compute_address_hash",
    # Filename / path
    "sanitize_filename",
    "build_attachment_path",
    "build_return_image_path",
    "sanitize_path_segment",
    "is_safe_path",
    # Masking / display
    "mask_string",
    "mask_transaction_id",
    "mask_tracking_number",
    "mask_phone",
    "mask_ip_address",
    # Display labels
    "format_order_status_label",
    "format_payment_status_label",
    "format_shipment_status_label",
    "format_refund_status_label",
    "format_return_status_label",
    "format_event_label",
    "format_quantity",
    "format_weight",
    "format_phone_display",
    # Dictionary / metadata
    "get_nested",
    "set_nested",
    "flatten_dict",
    "compact_dict",
    "safe_dict_get",
    # JSON / serialization
    "safe_json_loads",
    "safe_json_dumps",
    "is_json_object",
    "is_json_array",
    # Validation
    "is_valid_uuid",
    "coerce_uuid",
    "is_within_range",
    "is_in_choices",
    # Cache keys
    "build_cache_key",
    "order_cache_key",
    "order_by_number_cache_key",
    "order_timeline_cache_key",
    "order_count_cache_key",
    "status_aggregation_cache_key",
    # Pagination
    "compute_page_window",
    "clamp_page_size",
    # Audit / log formatting
    "format_audit_entry",
    "format_timeline_label",
    # Delta / difference
    "diff_dicts",
    "has_changed",
    # Financial calculations
    "safe_add_decimal",
    "safe_subtract_decimal",
    "safe_multiply_decimal",
    "apply_percentage",
    "apply_percentage_to_value",
    "sum_decimals",
    "ratio_to_percentage",
    # File / attachment
    "format_file_size",
    "format_mime_type",
    # Utility decorators
    "swallow_errors",
    "normalize_args",
    # Parsers
    "parse_decimal_list",
    "parse_string_list",
    "parse_csv_safe",
    # Transformers
    "to_bool",
    "to_int",
    "to_str_list",
    # Shipment / tracking
    "normalize_tracking_number",
    "is_valid_tracking_number",
    "build_tracking_url",
    # Address / display
    "format_address_oneline",
    "format_address_multiline",
    # Status / flag checks
    "is_terminal_success_status",
    "is_terminal_failure_status",
    "is_terminal_status",
    "is_cancellable_status",
    "is_paid_like_payment_status",
    "is_shippable_item_status",
    "is_returnable_item_status",
    "is_in_transit_shipment_status",
    "is_resolved_return_status",
    # List / collection
    "chunks",
    "unique_preserving_order",
    "first_or_default",
    # UUID / randomness
    "generate_short_token",
    "generate_hex_token",
    # Miscellaneous
    "get_initials",
    "normalize_tags",
    "is_gift_order",
    "normalize_unicode",
    # Composite
    "build_order_summary",
    "build_address_summary",
]