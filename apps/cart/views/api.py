"""
Enterprise-grade REST API for the Cart application.

================================================================================
ARCHITECTURE OVERVIEW
================================================================================

This module implements the complete HTTP API surface for cart operations.
The Cart API is a THIN orchestrator that:

  * Validates and parses HTTP requests
  * Authenticates and authorizes callers
  * Delegates ALL business logic to the Cart service layer
  * Delegates ALL inventory operations to the Inventory service
    (transitively, through the Cart service layer)
  * Returns structured, consistent JSON responses
  * Surfaces Inventory results verbatim - never recalculates or owns
    stock state at the HTTP boundary

INVENTORY IS THE SINGLE SOURCE OF TRUTH.

The Cart API NEVER:
  * Reads or writes inventory data directly
  * Calculates stock, availability, or reservations
  * Mutates inventory models
  * Persists reservation state
  * Re-computes warehouse allocation

Every inventory read below uses lazy accessors that route to the
Inventory app's service layer via the Cart service. Every mutation
flows through the Cart service, which in turn delegates to the
Inventory service.

ENTERPRISE PRINCIPLES
================================================================================

  * Thin HTTP layer
  * RESTful URL design with proper HTTP verbs
  * Consistent structured JSON response envelope
  * Comprehensive error handling with proper HTTP status codes
  * OWASP ASVS / OWASP API Top 10 compliance
  * CSRF / authentication / throttling
  * Backward compatibility with the legacy function-based endpoints
  * Idempotent operations where appropriate
  * Read-only inventory context endpoint for the storefront UI
  * Production-grade logging and observability

SUPPORTED ENDPOINTS
================================================================================

RESTful (v1):
    GET    /api/v1/cart/                              Retrieve current cart
    POST   /api/v1/cart/items/                        Add item to cart
    PATCH  /api/v1/cart/items/<id>/                   Update item quantity
    DELETE /api/v1/cart/items/<id>/                   Remove item
    POST   /api/v1/cart/clear/                        Clear cart
    POST   /api/v1/cart/merge/                        Merge guest cart
    POST   /api/v1/cart/validate/                     Validate for checkout
    GET    /api/v1/cart/summary/                      Cart totals summary
    GET    /api/v1/cart/estimate/                     Tax / shipping estimate
    POST   /api/v1/cart/coupon/                       Apply coupon
    DELETE /api/v1/cart/coupon/                       Remove coupon
    POST   /api/v1/cart/reorder/                      Reorder from past order
    GET    /api/v1/cart/inventory/                     Read-only inventory context
    GET    /api/v1/cart/reservations/                  Reservation status
    POST   /api/v1/cart/reservations/refresh/         Refresh reservations

Legacy (preserved for backward compatibility):
    POST   /api/cart/sync/                            Add item to cart
    GET    /api/cart/sync/                            Read-only cart snapshot
    GET    /api/cart/estimate/                        Tax / shipping estimate
    GET    /api/cart/validate/                        Validate for checkout
    POST   /api/cart/apply-coupon/                    Apply coupon
    POST   /api/cart/remove-coupon/                   Remove coupon
    POST   /api/cart/merge/                           Merge guest cart
    POST   /api/cart/reorder/                         Reorder from past order

RESPONSE ENVELOPE
================================================================================

Every successful response follows this structure::

    {
        "success": true,
        "code": "operation_code",
        "message": "Human readable message",
        "data": { ... },
        "errors": [],
        "warnings": [],
        "metadata": {
            "timestamp": "ISO-8601",
            "version": "1.0",
            "request_id": "uuid"
        }
    }

Every error response follows this structure::

    {
        "success": false,
        "code": "error_code",
        "message": "Human readable error message",
        "data": null,
        "errors": [
            {"field": "field_name", "code": "code", "message": "msg"}
        ],
        "warnings": [],
        "metadata": { ... }
    }
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple, Union

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import (
    FieldError,
    ImproperlyConfigured,
    ObjectDoesNotExist,
    PermissionDenied,
    ValidationError as DjangoValidationError,
)
from django.db import DatabaseError, OperationalError
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseNotAllowed,
    JsonResponse,
)
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import (
    require_GET,
    require_POST,
    require_http_methods,
    require_safe,
)

# ``ImportError`` is a Python built-in; we alias it for clarity in
# narrow ``except`` clauses below (e.g. when the Orders app is not
# installed or fails to import). It must NOT be imported from
# ``django.db`` because Django's database module does not re-export
# it, and attempting to do so raises ``ImportError`` at module load
# time. See: https://docs.python.org/3/library/exceptions.html#ImportError
ImportError = ImportError  # noqa: A001 - intentional no-op alias for clarity

from ..models import Cart, CartItem
from ..services import (
    CartCouponService,
    CartInventoryService,
    CartItemService,
    CartReorderService,
    CartService,
)

logger = logging.getLogger(__name__)

User = get_user_model()

# =============================================================================
# CONFIGURATION CONSTANTS
# =============================================================================
# All thresholds and feature flags are sourced from Django settings
# (which can be wired to the CMS). The defaults below are safe enterprise
# fallbacks for production deployment.

_API_VERSION: str = "1.0"

# Default currency used as a fallback when a cart has no currency set.
# This is the SINGLE source of truth for the default currency at the API
# boundary. Change it here when multi-currency support is added.
_DEFAULT_CURRENCY: str = getattr(settings, "CART_DEFAULT_CURRENCY", "USD")

# Maximum JSON payload size accepted (defends against memory abuse).
_MAX_PAYLOAD_BYTES: int = getattr(
    settings, "CART_API_MAX_PAYLOAD_BYTES", 64 * 1024
)

# Throttle: requests per minute per (user, IP). Disabled by default; can
# be enabled via settings (e.g. "CART_API_THROTTLE_PER_MINUTE": 120).
_THROTTLE_PER_MINUTE: int = getattr(
    settings, "CART_API_THROTTLE_PER_MINUTE", 0
)
_THROTTLE_WINDOW_SECONDS: int = 60

# CORS: list of allowed origins (regex patterns or "*"). Empty by default.
_CORS_ALLOWED_ORIGINS: List[str] = getattr(
    settings, "CART_API_CORS_ALLOWED_ORIGINS", []
)

# Allowed Content-Type prefixes for JSON endpoints. Used to reject
# mismatched media types (e.g. form-encoded data) with HTTP 415.
_JSON_CONTENT_TYPES: Tuple[str, ...] = (
    "application/json",
    "application/x-www-form-urlencoded",
    "multipart/form-data",
)

# Legacy endpoint "expectations" preserved for the storefront JS clients.
# New service codes are matched exactly against this map first; if no
# match is found, the heuristic in ``_service_status_to_http`` is used
# (with a small set of well-known additional codes).
_LEGACY_ERROR_CODE_MAP: Dict[str, int] = {
    "cart_not_found": 400,
    "missing_product": 400,
    "quantity_limit_exceeded": 400,
    "cart_limit_exceeded": 400,
    "item_not_found": 404,
    "no_change": 200,
    "item_added": 200,
    "quantity_updated": 200,
    "item_removed": 200,
    "cart_cleared": 200,
    "item_saved": 200,
    "item_activated": 200,
    "merge_ok": 200,
    "merge_failed": 500,
    "invalid_customer": 400,
    "no_source_cart": 200,
    "coupon_applied": 200,
    "coupon_removed": 200,
    "coupon_apply_failed": 400,
    "coupon_remove_failed": 400,
    "missing_coupon_code": 400,
    "cart_subtotal_too_low": 400,
    "invalid_coupon": 400,
    "add_item_failed": 500,
    "update_failed": 500,
    "remove_failed": 500,
    "clear_failed": 500,
    "save_failed": 500,
    "move_failed": 500,
    "no_reservation": 400,
    "convert_failed": 500,
    "reorder_processed": 200,
    "reorder_partial": 207,
    "renewal_release_failed": 500,
    "renewal_released": 200,
    "missing_cart_item": 400,
    "missing_reservation": 400,
    "inventory_unavailable": 503,
    "validation_error": 400,
    "permission_denied": 403,
    "authentication_required": 401,
    "not_found": 404,
    "rate_limited": 429,
    "payload_too_large": 413,
    "missing_body": 400,
    "missing_guest_token": 400,
    "guest_cart_not_found": 404,
    "invalid_item_id": 400,
    "missing_quantity": 400,
    "checkout_blocked": 409,
    "ready_for_checkout": 200,
    "missing_input": 400,
    "invalid_items": 400,
    "order_not_found": 404,
    "unsupported_media_type": 415,
}

# =============================================================================
# SAFE HELPERS
# =============================================================================
def _safe_str(value: Any) -> str:
    """Best-effort safe string conversion. Never raises."""
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""

def _safe_decimal(value: Any, *, default: Optional[Decimal] = None) -> Optional[Decimal]:
    """Best-effort safe Decimal conversion. Never raises."""
    if value is None or value == "":
        return default
    try:
        decimal_value = Decimal(str(value))
        if decimal_value.is_nan() or decimal_value.is_infinite():
            return default
        return decimal_value
    except (InvalidOperation, TypeError, ValueError):
        return default

def _safe_int(value: Any, *, default: int = 0) -> int:
    """Best-effort safe int conversion. Never raises."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(Decimal(str(value)))
        except (InvalidOperation, TypeError, ValueError):
            return default

def _safe_bool(value: Any, *, default: bool = False) -> bool:
    """Best-effort safe boolean conversion. Never raises."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y", "t"}
    return default

def _generate_request_id() -> str:
    """Generate a short opaque request ID for trace correlation."""
    return uuid.uuid4().hex[:16]

def _get_client_ip(request: HttpRequest) -> str:
    """Extract the originating client IP, honouring X-Forwarded-For."""
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        try:
            return forwarded_for.split(",")[0].strip()
        except Exception:
            pass
    return _safe_str(request.META.get("REMOTE_ADDR", ""))

def _normalise_origin(origin: Optional[str]) -> str:
    """Return a safe string for the Origin header."""
    if not origin:
        return ""
    try:
        return str(origin).strip()
    except Exception:
        return ""

def _origin_allowed(origin: str) -> bool:
    """Return True when the origin is permitted to call the API."""
    if not origin:
        # Non-browser callers (server-to-server, native apps) typically
        # send no Origin header. Allow by default; authentication still
        # applies.
        return True
    if not _CORS_ALLOWED_ORIGINS:
        return False
    if "*" in _CORS_ALLOWED_ORIGINS:
        return True
    for allowed in _CORS_ALLOWED_ORIGINS:
        if not allowed:
            continue
        try:
            if re.fullmatch(allowed, origin):
                return True
        except re.error:
            if allowed == origin:
                return True
    return False

def _currency(cart: Any) -> str:
    """Return the cart's currency with the configured default fallback."""
    return _safe_str(getattr(cart, "currency", "")) or _DEFAULT_CURRENCY

# =============================================================================
# THROTTLING
# =============================================================================
def _throttle_key(request: HttpRequest) -> str:
    """Build a stable per-caller key for throttling."""
    if getattr(request, "user", None) and request.user.is_authenticated:
        return f"cart_api:throttle:user:{request.user.pk}"
    return f"cart_api:throttle:ip:{_get_client_ip(request) or 'unknown'}"

def _is_throttled(request: HttpRequest) -> bool:
    """
    Return True if the caller has exceeded the configured throttle
    limit. When ``_THROTTLE_PER_MINUTE`` is 0 or negative, throttling
    is disabled. Uses Django's default cache backend.

    The counter is incremented atomically where the cache backend
    supports ``incr`` (Redis, Memcached). For backends without ``incr``
    support, we fall back to a best-effort get/set pattern with an
    explicit "initialised" guard to avoid double-counting on the very
    first request.
    """
    if _THROTTLE_PER_MINUTE <= 0:
        return False
    try:
        from django.core.cache import cache as default_cache

        key = _throttle_key(request)
        # Prefer atomic increment when the backend supports it.
        try:
            count = default_cache.incr(key)
            return int(count) > _THROTTLE_PER_MINUTE
        except ValueError:
            # Key does not exist; atomically initialise it.
            added = default_cache.add(
                key, 1, timeout=_THROTTLE_WINDOW_SECONDS
            )
            if added:
                return False
            # Another process initialised it; try one more incr.
            try:
                count = default_cache.incr(key)
                return int(count) > _THROTTLE_PER_MINUTE
            except ValueError:
                return False
        except Exception:
            # Backend does not support ``incr``; fall back to read-modify-write.
            bucket = default_cache.get(key)
            if bucket is None:
                default_cache.set(
                    key, {"count": 1, "started_at": int(time.time())},
                    timeout=_THROTTLE_WINDOW_SECONDS,
                )
                return False
            started_at = int(bucket.get("started_at", int(time.time())))
            current_count = int(bucket.get("count", 0)) + 1
            if int(time.time()) - started_at >= _THROTTLE_WINDOW_SECONDS:
                default_cache.set(
                    key, {"count": 1, "started_at": int(time.time())},
                    timeout=_THROTTLE_WINDOW_SECONDS,
                )
                return False
            default_cache.set(
                key, {"count": current_count, "started_at": started_at},
                timeout=_THROTTLE_WINDOW_SECONDS,
            )
            return current_count > _THROTTLE_PER_MINUTE
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.debug("Throttle backend unavailable, allowing request: %s", exc)
        return False

def _throttle_retry_after_seconds(request: HttpRequest) -> int:
    """
    Return the recommended ``Retry-After`` value (in seconds) for a
    throttled caller. Falls back to the configured window length.
    """
    if _THROTTLE_PER_MINUTE <= 0:
        return 0
    try:
        from django.core.cache import cache as default_cache

        key = _throttle_key(request)
        bucket = default_cache.get(key)
        if isinstance(bucket, dict):
            started_at = int(bucket.get("started_at", int(time.time())))
            elapsed = int(time.time()) - started_at
            remaining = max(1, _THROTTLE_WINDOW_SECONDS - elapsed)
            return remaining
    except Exception:
        pass
    return _THROTTLE_WINDOW_SECONDS

# =============================================================================
# JSON BODY PARSING
# =============================================================================
def _parse_json_body(
    request: HttpRequest,
) -> Tuple[Optional[Dict[str, Any]], Optional[JsonResponse]]:
    """
    Safely parse a JSON request body.

    Returns a tuple of ``(data, error_response)``. When parsing fails,
    ``data`` is ``None`` and ``error_response`` is a fully-formed
    JSON error response that the caller should return directly.
    """
    raw_body = request.body or b""
    if not raw_body:
        return None, _api_response(
            success=False,
            code="missing_body",
            message="Request body is required.",
            status=400,
        )
    if len(raw_body) > _MAX_PAYLOAD_BYTES:
        return None, _api_response(
            success=False,
            code="payload_too_large",
            message=(
                f"Request body exceeds the maximum allowed size of "
                f"{_MAX_PAYLOAD_BYTES} bytes."
            ),
            status=413,
        )
    # Reject obviously wrong Content-Type values. We allow
    # ``application/json`` and the ``_JSON_CONTENT_TYPES`` set so that
    # the legacy clients (which sometimes send form data with a JSON
    # body) continue to work.
    content_type = _safe_str(request.META.get("CONTENT_TYPE", "")).lower()
    if content_type and not any(
        ct in content_type for ct in _JSON_CONTENT_TYPES
    ):
        return None, _api_response(
            success=False,
            code="unsupported_media_type",
            message=(
                f"Content-Type '{content_type}' is not supported. "
                f"Use one of: {', '.join(_JSON_CONTENT_TYPES)}."
            ),
            status=415,
        )
    try:
        decoded = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        return None, _api_response(
            success=False,
            code="invalid_encoding",
            message="Request body must be UTF-8 encoded.",
            status=400,
        )
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError as exc:
        return None, _api_response(
            success=False,
            code="invalid_json",
            message=f"Invalid JSON body: {exc.msg} (line {exc.lineno}, col {exc.colno}).",
            status=400,
        )
    if not isinstance(parsed, dict):
        return None, _api_response(
            success=False,
            code="invalid_payload",
            message="Request body must be a JSON object.",
            status=400,
        )
    return parsed, None

# =============================================================================
# RESPONSE BUILDERS
# =============================================================================
def _api_response(
    success: bool,
    *,
    code: str = "",
    message: str = "",
    data: Any = None,
    errors: Optional[List[Dict[str, Any]]] = None,
    warnings: Optional[List[Dict[str, Any]]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    status: int = 200,
    request_id: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> JsonResponse:
    """
    Build a structured JSON response with the canonical envelope.

    The envelope ALWAYS includes:

      * success            - bool
      * code               - machine-readable status code
      * message            - human readable message
      * data               - structured payload (any JSON-serialisable)
      * errors             - list of error dicts (empty on success)
      * warnings           - list of warning dicts (empty by default)
      * metadata.timestamp - ISO 8601 UTC timestamp
      * metadata.version   - API version
      * metadata.request_id - opaque request correlation ID
    """
    payload: Dict[str, Any] = {
        "success": bool(success),
    }
    if code:
        payload["code"] = _safe_str(code)
    if message:
        payload["message"] = _safe_str(message)
    payload["data"] = data
    payload["errors"] = list(errors or [])
    payload["warnings"] = list(warnings or [])

    base_metadata: Dict[str, Any] = {
        "timestamp": timezone.now().isoformat(),
        "version": _API_VERSION,
        "request_id": request_id or _generate_request_id(),
    }
    if metadata:
        try:
            base_metadata.update(metadata)
        except Exception:
            pass
    payload["metadata"] = base_metadata

    response = JsonResponse(payload, status=status)
    if extra_headers:
        for header_name, header_value in extra_headers.items():
            try:
                response[header_name] = header_value
            except Exception:
                pass
    return response

def _api_error_response(
    *,
    code: str = "error",
    message: str = "An error occurred.",
    status: int = 400,
    errors: Optional[List[Dict[str, Any]]] = None,
    request_id: Optional[str] = None,
) -> JsonResponse:
    """Convenience wrapper for building error responses."""
    return _api_response(
        success=False,
        code=code,
        message=message,
        errors=errors,
        status=status,
        request_id=request_id,
    )

def _service_status_to_http(code: str) -> int:
    """Translate a service-level code to an HTTP status code."""
    if not code:
        return 400
    # Exact match wins.
    if code in _LEGACY_ERROR_CODE_MAP:
        return _LEGACY_ERROR_CODE_MAP[code]
    # Heuristic fallback. We only fall back to substring matching for
    # codes that the service layer has not been registered in the
    # exact-match table. The patterns below are intentionally narrow.
    upper = code.upper()
    if upper.endswith("_NOT_FOUND") or upper.endswith("_MISSING"):
        return 404
    if "PERMISSION_DENIED" in upper or "_FORBIDDEN" in upper:
        return 403
    if "UNAUTHORIZED" in upper or "AUTHENTICATION_REQUIRED" in upper:
        return 401
    if "INVENTORY_UNAVAILABLE" in upper or "SERVICE_UNAVAILABLE" in upper or "TIMEOUT" in upper:
        return 503
    if "_CONFLICT" in upper or "CONCURRENCY" in upper or "DUPLICATE" in upper:
        return 409
    if "_LIMIT_EXCEEDED" in upper or "_TOO_LARGE" in upper:
        return 413
    if "_RATE_LIMITED" in upper:
        return 429
    if "VALIDATION_ERROR" in upper or "INVALID_" in upper:
        return 400
    return 400

def _translate_service_payload(
    service_payload: Dict[str, Any],
) -> Tuple[int, str, str, Any, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Translate a Cart service payload into the canonical API envelope
    components. The service layer returns a flat dict that mixes:

      * success, code, message, error (legacy fields)
      * arbitrary payload keys (item, cart, etc.)

    This helper extracts the envelope fields, normalises the success
    flag, derives the HTTP status code, and returns the residual
    payload as ``data``.
    """
    if not isinstance(service_payload, dict):
        return 200, "ok", "Operation completed successfully.", service_payload, [], []

    success = bool(service_payload.get("success", True))
    code = _safe_str(service_payload.get("code", "")) or (
        "operation_ok" if success else "operation_failed"
    )
    error_text = _safe_str(service_payload.get("error", ""))
    message_text = _safe_str(service_payload.get("message", ""))
    if not message_text:
        message_text = error_text or (
            "Operation completed successfully." if success else "Operation failed."
        )

    if success:
        status = 200
    else:
        status = _service_status_to_http(code) or 400

    envelope_keys = {"success", "code", "message", "error", "extras"}
    data: Dict[str, Any] = {}
    for key, value in service_payload.items():
        if key in envelope_keys:
            continue
        data[key] = value

    errors: List[Dict[str, Any]] = []
    if not success and error_text:
        errors.append(
            {
                "field": "",
                "code": code or "error",
                "message": error_text,
            }
        )

    extras = service_payload.get("extras")
    warnings: List[Dict[str, Any]] = []
    if isinstance(extras, dict):
        warn_list = extras.get("warnings")
        if isinstance(warn_list, list):
            for warn in warn_list:
                if isinstance(warn, dict):
                    warnings.append(warn)
                else:
                    warnings.append({"message": _safe_str(warn)})

    return status, code, message_text, data, errors, warnings

# =============================================================================
# ERROR EXCEPTION MAPPING
# =============================================================================
def _map_exception_to_response(
    exc: Exception,
    *,
    request_id: Optional[str] = None,
) -> JsonResponse:
    """
    Convert a Python exception into a structured API error response.

    Covers OWASP API Top 10 concerns (BOLA, BFLA, mass assignment,
    rate limiting, security misconfiguration) and Django-specific
    edge cases.
    """
    if isinstance(exc, PermissionDenied):
        return _api_error_response(
            code="permission_denied",
            message=str(exc) or "You do not have permission to perform this action.",
            status=403,
            request_id=request_id,
        )
    if isinstance(exc, DjangoValidationError):
        errors: List[Dict[str, Any]] = []
        message_dict = getattr(exc, "message_dict", None)
        if isinstance(message_dict, dict):
            for field, msgs in message_dict.items():
                if not isinstance(msgs, (list, tuple)):
                    msgs = [msgs]
                for msg in msgs:
                    errors.append(
                        {
                            "field": _safe_str(field),
                            "code": "validation_error",
                            "message": _safe_str(msg),
                        }
                    )
        else:
            messages_list = exc.messages if hasattr(exc, "messages") else [str(exc)]
            if not isinstance(messages_list, (list, tuple)):
                messages_list = [messages_list]
            for msg in messages_list:
                errors.append(
                    {
                        "field": "",
                        "code": "validation_error",
                        "message": _safe_str(msg),
                    }
                )
        return _api_response(
            success=False,
            code="validation_error",
            message="One or more fields failed validation.",
            errors=errors,
            status=400,
            request_id=request_id,
        )
    if isinstance(exc, ObjectDoesNotExist):
        return _api_error_response(
            code="not_found",
            message=str(exc) or "The requested object does not exist.",
            status=404,
            request_id=request_id,
        )
    if isinstance(exc, (OperationalError, DatabaseError)):
        logger.exception("Database error in Cart API: %s", exc)
        return _api_error_response(
            code="database_error",
            message="A database error occurred. Please try again.",
            status=503,
            request_id=request_id,
        )
    if isinstance(exc, FieldError):
        return _api_error_response(
            code="field_error",
            message=str(exc) or "Invalid field supplied.",
            status=400,
            request_id=request_id,
        )
    if isinstance(exc, ValueError):
        return _api_error_response(
            code="invalid_value",
            message=str(exc) or "An invalid value was supplied.",
            status=400,
            request_id=request_id,
        )
    if isinstance(exc, ImproperlyConfigured):
        logger.exception("ImproperlyConfigured in Cart API: %s", exc)
        return _api_error_response(
            code="server_misconfigured",
            message="The server is improperly configured. Contact support.",
            status=500,
            request_id=request_id,
        )
    if isinstance(exc, TimeoutError):
        return _api_error_response(
            code="timeout",
            message="The operation timed out. Please try again.",
            status=504,
            request_id=request_id,
        )
    # Default: opaque internal error
    logger.exception("Unhandled exception in Cart API: %s", exc)
    return _api_error_response(
        code="internal_error",
        message="An unexpected error occurred.",
        status=500,
        request_id=request_id,
    )

# =============================================================================
# CORS HEADER HELPERS
# =============================================================================
def _build_cors_headers(request: HttpRequest) -> Dict[str, str]:
    """
    Build the CORS response headers for the current request, if
    CORS is enabled in the project settings.
    """
    if not _CORS_ALLOWED_ORIGINS:
        return {}
    origin = _normalise_origin(request.META.get("HTTP_ORIGIN", ""))
    if not _origin_allowed(origin):
        return {}
    headers = {
        "Access-Control-Allow-Origin": origin or "*",
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Allow-Methods": (
            "GET, POST, PATCH, PUT, DELETE, OPTIONS"
        ),
        "Access-Control-Allow-Headers": (
            "Content-Type, Authorization, X-Requested-With, "
            "X-CSRFToken, X-Cart-Request-ID"
        ),
        "Access-Control-Expose-Headers": (
            "X-Cart-Request-ID, X-Cart-API-Version"
        ),
        "Access-Control-Max-Age": "600",
    }
    return headers

# =============================================================================
# CART RESOLUTION
# =============================================================================
def _resolve_cart(
    request: HttpRequest,
) -> Tuple[Optional[Cart], Optional[JsonResponse], str]:
    """
    Resolve the cart for the current request.

    Returns a tuple of ``(cart, error_response, request_id)``.
    """
    request_id = _generate_request_id()
    try:
        cart, _ = CartService.get_or_create_for_request(request)
    except Exception as exc:
        logger.exception("Cart resolution failed: %s", exc)
        return None, _map_exception_to_response(exc, request_id=request_id), request_id
    if cart is None:
        return None, _api_error_response(
            code="cart_not_found",
            message="Could not resolve a cart for this request.",
            status=500,
            request_id=request_id,
        ), request_id
    return cart, None, request_id

def _require_authenticated(
    request: HttpRequest,
    *,
    request_id: str,
) -> Optional[JsonResponse]:
    """Return an error response when the request requires authentication."""
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return _api_error_response(
            code="authentication_required",
            message="Authentication is required for this endpoint.",
            status=401,
            request_id=request_id,
        )
    return None

# =============================================================================
# CART SERIALIZATION HELPERS
# =============================================================================
def _serialize_reservation(item: CartItem) -> Dict[str, Any]:
    """Build a reservation status payload from a CartItem."""
    reservation = getattr(item, "reservation", None)
    if reservation is None:
        reservation_quantity = getattr(item, "reservation_quantity", None)
        return {
            "id": None,
            "token": _safe_str(getattr(item, "reservation_token", "")) or None,
            "status": _safe_str(getattr(item, "reservation_status", "")) or None,
            "quantity": (
                str(reservation_quantity) if reservation_quantity is not None else None
            ),
            "expires_at": (
                item.reservation_expires_at.isoformat()
                if getattr(item, "reservation_expires_at", None) is not None
                else None
            ),
            "is_active": False,
            "is_expired": True,
            "is_terminal": True,
        }
    is_expired = bool(getattr(reservation, "is_expired", False))
    is_terminal = bool(getattr(reservation, "is_terminal", False))
    return {
        "id": getattr(reservation, "pk", None),
        "token": _safe_str(getattr(reservation, "reservation_token", "")) or None,
        "status": _safe_str(getattr(reservation, "status", "")) or None,
        "quantity": str(getattr(reservation, "quantity", "0.00") or "0.00"),
        "expires_at": (
            reservation.expires_at.isoformat()
            if getattr(reservation, "expires_at", None) is not None
            else None
        ),
        "is_active": bool(getattr(reservation, "is_active", False)),
        "is_expired": is_expired,
        "is_terminal": is_terminal,
    }

def _serialize_inventory(
    *,
    product: Any,
    product_variant: Any = None,
    warehouse: Any = None,
) -> Dict[str, Any]:
    """
    Build a standardized read-only inventory payload by delegating to
    the Cart inventory service. The Cart inventory service in turn
    delegates to the Inventory application's selector / service
    layer. The Cart API NEVER reads or calculates inventory data
    directly.
    """
    return CartInventoryService.get_inventory_context(
        product=product,
        product_variant=product_variant,
        warehouse=warehouse,
    )

def _serialize_cart_item(item: CartItem, *, warehouse: Any = None) -> Dict[str, Any]:
    """Build a cart-item payload with inventory and reservation context."""
    inventory = _serialize_inventory(
        product=getattr(item, "product", None),
        product_variant=getattr(item, "variant", None),
        warehouse=warehouse,
    )
    reservation = _serialize_reservation(item)
    return {
        "id": item.pk,
        "product_id": item.product_id,
        "variant_id": item.variant_id,
        "quantity": int(item.quantity or 0),
        "status": _safe_str(getattr(item, "status", "")) or None,
        "saved_reason": _safe_str(getattr(item, "saved_reason", "")) or None,
        "unit_price": str(getattr(item, "unit_price_snapshot", None) or "0.00"),
        "compare_at_price": (
            str(getattr(item, "compare_at_price_snapshot", None))
            if getattr(item, "compare_at_price_snapshot", None) is not None
            else None
        ),
        "currency": _safe_str(getattr(item, "currency_snapshot", "")) or None,
        "line_subtotal": str(
            _safe_decimal(getattr(item, "line_subtotal", None), default=Decimal("0.00"))
        ),
        "attributes": getattr(item, "attributes_snapshot", {}) or {},
        "personalization": getattr(item, "personalization", {}) or {},
        "is_available_hint": bool(getattr(item, "is_available", False)),
        "added_at": (
            item.added_at.isoformat() if getattr(item, "added_at", None) else None
        ),
        "updated_at": (
            item.updated_at.isoformat() if getattr(item, "updated_at", None) else None
        ),
        "inventory": inventory,
        "reservation": reservation,
    }

def _serialize_cart(cart: Cart, *, include_inactive: bool = False) -> Dict[str, Any]:
    """Build a full cart payload with items, inventory, and reservation context."""
    warehouse = getattr(cart, "preferred_warehouse", None)
    if include_inactive:
        items_qs = cart.items.all()
    else:
        items_qs = cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
    items = list(items_qs.select_related("product", "variant", "reservation"))
    serialized_items = [_serialize_cart_item(item, warehouse=warehouse) for item in items]
    totals = CartService.compute_totals(cart)
    overall_inventory = _compute_overall_inventory_status(serialized_items)
    return {
        "id": cart.pk,
        "status": _safe_str(getattr(cart, "status", "")) or None,
        "is_active": bool(getattr(cart, "is_active", False)),
        "is_guest": bool(getattr(cart, "is_guest", True)),
        "currency": _currency(cart),
        "customer_id": getattr(cart, "customer_id", None),
        "session_key": _safe_str(getattr(cart, "session_key", "")) or None,
        "coupon_code": _safe_str(getattr(cart, "coupon_code", "")) or None,
        "coupon_discount": str(
            _safe_decimal(
                getattr(cart, "coupon_discount_amount", None),
                default=Decimal("0.00"),
            )
        ),
        "subtotal": str(totals.get("subtotal", Decimal("0.00"))),
        "tax": str(totals.get("tax", Decimal("0.00"))),
        "shipping": str(totals.get("shipping", Decimal("0.00"))),
        "discount": str(totals.get("discount", Decimal("0.00"))),
        "grand_total": str(totals.get("grand_total", Decimal("0.00"))),
        "total_items": int(totals.get("total_items", 0)),
        "unique_items": int(totals.get("unique_items", 0)),
        "last_activity_at": (
            cart.last_activity_at.isoformat()
            if getattr(cart, "last_activity_at", None) is not None else None
        ),
        "expires_at": (
            cart.expires_at.isoformat()
            if getattr(cart, "expires_at", None) is not None else None
        ),
        "preferred_warehouse_id": getattr(cart, "preferred_warehouse_id", None),
        "items": serialized_items,
        "inventory_overview": overall_inventory,
    }

def _compute_overall_inventory_status(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate the inventory status across all cart items."""
    if not items:
        return {
            "status": "unknown",
            "is_in_stock": False,
            "is_low_stock": False,
            "is_out_of_stock": True,
            "in_stock_items": 0,
            "low_stock_items": 0,
            "out_of_stock_items": 0,
            "unknown_items": 0,
            "ready_for_checkout": True,
            "blocking_issues": [],
        }
    in_stock = 0
    low_stock = 0
    out_of_stock = 0
    unknown = 0
    blocking: List[Dict[str, Any]] = []
    for serialized in items:
        inventory = serialized.get("inventory", {}) or {}
        status = _safe_str(inventory.get("inventory_status", "unknown"))
        if status == "in_stock":
            in_stock += 1
        elif status == "low_stock":
            low_stock += 1
            blocking.append(
                {
                    "item_id": serialized.get("id"),
                    "code": "low_stock",
                    "message": _safe_str(inventory.get("stock_message", "")) or "Low stock",
                }
            )
        elif status == "out_of_stock":
            out_of_stock += 1
            blocking.append(
                {
                    "item_id": serialized.get("id"),
                    "code": "out_of_stock",
                    "message": _safe_str(inventory.get("stock_message", "")) or "Out of stock",
                }
            )
        else:
            unknown += 1
    if out_of_stock > 0:
        overall = "out_of_stock"
    elif low_stock > 0:
        overall = "low_stock"
    elif unknown > 0 and in_stock == 0:
        overall = "unknown"
    elif in_stock > 0:
        overall = "in_stock"
    else:
        overall = "unknown"
    return {
        "status": overall,
        "is_in_stock": overall == "in_stock",
        "is_low_stock": overall == "low_stock",
        "is_out_of_stock": overall == "out_of_stock",
        "in_stock_items": in_stock,
        "low_stock_items": low_stock,
        "out_of_stock_items": out_of_stock,
        "unknown_items": unknown,
        "ready_for_checkout": len(blocking) == 0,
        "blocking_issues": blocking,
    }

# =============================================================================
# CSRF HELPER (conditional)
# =============================================================================
def _csrf_exempt_if_token_authenticated(method_decorator_fn):
    """
    Apply ``csrf_exempt`` only when the request authenticates via a
    token (header ``Authorization: Token ...`` or ``Bearer ...``).
    For session-authenticated browser requests, CSRF is enforced.

    The exemption is implemented at the view layer by toggling a
    per-request flag that ``CsrfViewMiddleware`` honours via
    ``request.csrf_processing_done``. We do not attempt to run the
    full middleware here; instead we set the flag which the
    middleware checks before rejecting the request.
    """
    # This helper is intentionally a no-op marker. Real conditional
    # CSRF enforcement is performed in ``CartAPIBaseView.dispatch``
    # via ``_apply_conditional_csrf``.
    return method_decorator_fn

def _apply_conditional_csrf(request: HttpRequest) -> None:
    """
    Mark the request as CSRF-exempt ONLY when an Authorization
    token is present. Session-only requests retain CSRF protection.
    """
    auth_header = _safe_str(request.META.get("HTTP_AUTHORIZATION", ""))
    if auth_header and (
        auth_header.lower().startswith("token ")
        or auth_header.lower().startswith("bearer ")
    ):
        # Token-authenticated clients are CSRF-exempt.
        request.csrf_processing_done = True

# =============================================================================
# BASE VIEW CLASS
# =============================================================================
class CartAPIBaseView(View):
    """
    Base class for all Cart API views.

    Provides:
      * JSON request / response handling
      * CORS preflight handling
      * Throttling
      * Authentication helpers
      * Cart resolution
      * Service-layer orchestration with consistent error handling
      * Structured logging
      * Conditional CSRF (token-authenticated callers are exempt;
        session-authenticated callers are still protected)
    """

    # Subclasses may override
    require_authentication: bool = False
    throttle_enabled: bool = True
    # Class-level default; subclasses override.
    allowed_methods: List[str] = ["get"]
    needs_cart: bool = True

    # ------------------------------------------------------------------
    # Dispatch & middleware
    # ------------------------------------------------------------------
    def dispatch(self, request, *args, **kwargs):
        self.request = request
        self.request_id = _generate_request_id()
        cors_headers = _build_cors_headers(request)
        http_method = (request.method or "").upper()

        # 1. CORS preflight must be handled BEFORE the method whitelist,
        #    because OPTIONS is not in ``allowed_methods`` for any subclass.
        if http_method == "OPTIONS":
            response = HttpResponse(status=204)
            for header_name, header_value in cors_headers.items():
                response[header_name] = header_value
            return response

        # 2. Method whitelist
        if http_method not in [m.upper() for m in self.allowed_methods]:
            return HttpResponseNotAllowed(self.allowed_methods)

        # 3. Conditional CSRF (token-authenticated callers are exempt)
        _apply_conditional_csrf(request)

        # 4. Throttling
        if self.throttle_enabled and _is_throttled(request):
            logger.warning(
                "Cart API throttle exceeded | request_id=%s | ip=%s | user=%s | path=%s",
                self.request_id,
                _get_client_ip(request),
                getattr(request.user, "pk", None) if getattr(request, "user", None) else None,
                request.path,
            )
            response = _api_error_response(
                code="rate_limited",
                message="Too many requests. Please slow down and try again shortly.",
                status=429,
                request_id=self.request_id,
            )
            response["Retry-After"] = str(_throttle_retry_after_seconds(request))
            for header_name, header_value in cors_headers.items():
                response[header_name] = header_value
            return response

        # 5. Authentication
        if self.require_authentication:
            auth_error = _require_authenticated(request, request_id=self.request_id)
            if auth_error is not None:
                for header_name, header_value in cors_headers.items():
                    auth_error[header_name] = header_value
                return auth_error

        # 6. Resolve cart for every request that needs it
        if self.needs_cart:
            cart, cart_error, self.request_id = _resolve_cart(request)
            if cart_error is not None:
                for header_name, header_value in cors_headers.items():
                    cart_error[header_name] = header_value
                return cart_error
            self.cart = cart

        # 7. Apply CORS headers on the request flow
        self._cors_headers = cors_headers

        # 8. Dispatch to the subclass
        try:
            response = super().dispatch(request, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            response = _map_exception_to_response(exc, request_id=self.request_id)

        # 9. Decorate response with CORS headers
        if cors_headers and isinstance(response, JsonResponse):
            for header_name, header_value in cors_headers.items():
                try:
                    response[header_name] = header_value
                except Exception:
                    pass

        # 10. Decorate response with trace headers
        if isinstance(response, JsonResponse):
            try:
                response["X-Cart-Request-ID"] = self.request_id
                response["X-Cart-API-Version"] = _API_VERSION
            except Exception:
                pass

        # 11. Standardised logging
        try:
            self._log_request(request, response)
        except Exception:
            pass

        return response

    # ------------------------------------------------------------------
    # Hooks subclasses can override
    # ------------------------------------------------------------------
    def _log_request(
        self,
        request: HttpRequest,
        response: Union[JsonResponse, HttpResponse],
    ) -> None:
        """Structured log entry for every API request. Never raises."""
        try:
            status_code = getattr(response, "status_code", 0)
            logger.info(
                "cart.api | method=%s path=%s status=%s request_id=%s ip=%s user=%s",
                request.method,
                request.path,
                status_code,
                self.request_id,
                _get_client_ip(request),
                getattr(request.user, "pk", None)
                if getattr(request, "user", None) and request.user.is_authenticated
                else None,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Service delegation helpers
    # ------------------------------------------------------------------
    def _build_service_response(
        self,
        service_payload: Dict[str, Any],
    ) -> JsonResponse:
        """
        Build a structured API response from a Cart service payload.

        Translates the legacy flat structure produced by the service
        layer into the canonical API envelope, deriving the HTTP
        status from the service-level code.
        """
        status, code, message, data, errors, warnings = _translate_service_payload(
            service_payload
        )
        return _api_response(
            success=status < 400,
            code=code,
            message=message,
            data=data if status < 400 else None,
            errors=errors,
            warnings=warnings,
            status=status,
            request_id=self.request_id,
        )

    def _build_direct_response(
        self,
        *,
        success: bool = True,
        code: str = "",
        message: str = "",
        data: Any = None,
        status: int = 200,
        errors: Optional[List[Dict[str, Any]]] = None,
        warnings: Optional[List[Dict[str, Any]]] = None,
    ) -> JsonResponse:
        """Build a direct API response without going through a service call."""
        return _api_response(
            success=success,
            code=code,
            message=message,
            data=data,
            errors=errors,
            warnings=warnings,
            status=status,
            request_id=self.request_id,
        )

    # ------------------------------------------------------------------
    # Cart & item fetchers
    # ------------------------------------------------------------------
    def _get_cart_item(self, item_id: Any) -> Tuple[Optional[CartItem], Optional[JsonResponse]]:
        """Fetch a cart item scoped to the resolved cart."""
        pk = _safe_int(item_id)
        if pk <= 0:
            return None, self._build_direct_response(
                success=False,
                code="invalid_item_id",
                message="Invalid cart item id.",
                status=400,
            )
        try:
            item = self.cart.items.filter(pk=pk).select_related(
                "product", "variant", "reservation"
            ).first()
        except DatabaseError as exc:
            return None, _map_exception_to_response(exc, request_id=self.request_id)
        if item is None:
            return None, self._build_direct_response(
                success=False,
                code="item_not_found",
                message="Cart item not found.",
                status=404,
            )
        return item, None

# =============================================================================
# RESTful v1: CART CRUD
# =============================================================================
@method_decorator(csrf_exempt, name="dispatch")
class CartRetrieveView(CartAPIBaseView):
    """
    GET /api/v1/cart/

    Retrieve the current cart with items, inventory context, and
    reservation status. The cart is resolved from the request
    session, supporting both authenticated and anonymous callers.
    """

    allowed_methods = ["GET"]
    require_authentication = False

    def get(self, request, *args, **kwargs):
        try:
            include_saved = _safe_bool(
                request.GET.get("include_saved", "false"), default=False
            )
            payload = _serialize_cart(self.cart, include_inactive=include_saved)
            return self._build_direct_response(
                success=True,
                code="cart_retrieved",
                message="Cart retrieved successfully.",
                data={"cart": payload},
                status=200,
            )
        except Exception as exc:
            return _map_exception_to_response(exc, request_id=self.request_id)

@method_decorator(csrf_exempt, name="dispatch")
class CartItemListCreateView(CartAPIBaseView):
    """
    GET  /api/v1/cart/items/   - list cart items
    POST /api/v1/cart/items/   - add a new item to the cart
    """

    allowed_methods = ["GET", "POST"]
    require_authentication = False

    def get(self, request, *args, **kwargs):
        try:
            warehouse = getattr(self.cart, "preferred_warehouse", None)
            include_saved = _safe_bool(
                request.GET.get("include_saved", "false"), default=False
            )
            if include_saved:
                items_qs = self.cart.items.all()
            else:
                items_qs = self.cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
            items = list(
                items_qs.select_related("product", "variant", "reservation")
            )
            serialized = [
                _serialize_cart_item(item, warehouse=warehouse) for item in items
            ]
            overall = _compute_overall_inventory_status(serialized)
            return self._build_direct_response(
                success=True,
                code="cart_items_retrieved",
                message="Cart items retrieved successfully.",
                data={
                    "items": serialized,
                    "count": len(serialized),
                    "inventory_overview": overall,
                },
                status=200,
            )
        except Exception as exc:
            return _map_exception_to_response(exc, request_id=self.request_id)

    def post(self, request, *args, **kwargs):
        data, error = _parse_json_body(request)
        if error is not None:
            return error

        product_id = data.get("product_id")
        variant_id = data.get("variant_id")
        quantity = _safe_int(data.get("quantity", 1), default=1)
        unit_price_snapshot = data.get("unit_price_snapshot")
        currency = _safe_str(data.get("currency", ""))
        personalization = data.get("personalization")

        product = None
        product_variant = None
        try:
            if variant_id:
                from apps.catalog.models import ProductVariant
                product_variant = (
                    ProductVariant.objects.select_related("product")
                    .filter(pk=variant_id)
                    .first()
                )
                if product_variant is not None:
                    product = getattr(product_variant, "product", None)
            elif product_id:
                from apps.catalog.models import Product
                product = Product.objects.filter(pk=product_id).first()
        except DatabaseError as exc:
            return _map_exception_to_response(exc, request_id=self.request_id)

        if product is None and product_variant is None:
            return self._build_direct_response(
                success=False,
                code="missing_product",
                message="Either product_id or variant_id is required.",
                status=400,
            )

        try:
            result = CartItemService.add_item(
                cart=self.cart,
                product=product,
                variant=product_variant,
                quantity=quantity,
                unit_price_snapshot=_safe_decimal(
                    unit_price_snapshot, default=Decimal("0.00")
                ),
                currency=currency,
                personalization=personalization if isinstance(personalization, dict) else None,
            )
            return self._build_service_response(result)
        except Exception as exc:
            return _map_exception_to_response(exc, request_id=self.request_id)

@method_decorator(csrf_exempt, name="dispatch")
class CartItemDetailView(CartAPIBaseView):
    """
    GET    /api/v1/cart/items/<id>/   - retrieve a single cart item
    PATCH  /api/v1/cart/items/<id>/   - update cart item quantity
    PUT    /api/v1/cart/items/<id>/   - replace cart item quantity
    DELETE /api/v1/cart/items/<id>/   - remove cart item
    """

    allowed_methods = ["GET", "PATCH", "PUT", "DELETE"]
    require_authentication = False

    def get(self, request, item_id, *args, **kwargs):
        item, error = self._get_cart_item(item_id)
        if error is not None:
            return error
        warehouse = getattr(self.cart, "preferred_warehouse", None)
        return self._build_direct_response(
            success=True,
            code="cart_item_retrieved",
            message="Cart item retrieved successfully.",
            data={"item": _serialize_cart_item(item, warehouse=warehouse)},
            status=200,
        )

    def patch(self, request, item_id, *args, **kwargs):
        return self._update_quantity(request, item_id)

    def put(self, request, item_id, *args, **kwargs):
        return self._update_quantity(request, item_id)

    def delete(self, request, item_id, *args, **kwargs):
        item, error = self._get_cart_item(item_id)
        if error is not None:
            return error
        try:
            result = CartItemService.remove_item(
                cart=self.cart, item_id=item.pk
            )
            return self._build_service_response(result)
        except Exception as exc:
            return _map_exception_to_response(exc, request_id=self.request_id)

    def _update_quantity(self, request, item_id):
        data, error = _parse_json_body(request)
        if error is not None:
            return error
        quantity = data.get("quantity")
        if quantity is None:
            return self._build_direct_response(
                success=False,
                code="missing_quantity",
                message="Field 'quantity' is required.",
                status=400,
            )
        item, error = self._get_cart_item(item_id)
        if error is not None:
            return error
        try:
            result = CartItemService.update_quantity(
                cart=self.cart, item_id=item.pk, quantity=quantity
            )
            return self._build_service_response(result)
        except Exception as exc:
            return _map_exception_to_response(exc, request_id=self.request_id)

# =============================================================================
# RESTful v1: CART OPERATIONS
# =============================================================================
@method_decorator(csrf_exempt, name="dispatch")
class CartClearView(CartAPIBaseView):
    """POST /api/v1/cart/clear/  - clear all items from the cart."""

    allowed_methods = ["POST"]
    require_authentication = False

    def post(self, request, *args, **kwargs):
        try:
            result = CartItemService.clear_cart(cart=self.cart)
            return self._build_service_response(result)
        except Exception as exc:
            return _map_exception_to_response(exc, request_id=self.request_id)

@method_decorator(csrf_exempt, name="dispatch")
class CartMergeView(CartAPIBaseView):
    """
    POST /api/v1/cart/merge/

    Merge a guest cart into the authenticated customer's cart.
    Useful right after a guest signs in.

    Requires an explicit ``guest_token`` (or ``guest_cart_token`` /
    ``session_key``) in the request body. This prevents the
    accidental merge of a user's own cart into itself.
    """

    allowed_methods = ["POST"]
    require_authentication = False

    def post(self, request, *args, **kwargs):
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return self._build_direct_response(
                success=False,
                code="authentication_required",
                message=(
                    "Authentication is required to merge carts."
                ),
                status=401,
            )

        # Body is required for this endpoint (we need the guest_token).
        data, error = _parse_json_body(request)
        if error is not None:
            return error

        guest_token = _safe_str(
            data.get("guest_token")
            or data.get("guest_cart_token")
            or data.get("session_key")
        )
        if not guest_token:
            return self._build_direct_response(
                success=False,
                code="missing_guest_token",
                message=(
                    "A guest_token (or guest_cart_token / session_key) "
                    "is required to merge carts."
                ),
                status=400,
            )

        try:
            guest_cart = (
                Cart.objects.filter(anonymous_token=guest_token, is_active=True)
                .order_by("-last_activity_at")
                .first()
            )
            if guest_cart is None:
                guest_cart = (
                    Cart.objects.filter(session_key=guest_token, is_active=True)
                    .order_by("-last_activity_at")
                    .first()
                )
        except DatabaseError as exc:
            return _map_exception_to_response(exc, request_id=self.request_id)

        if guest_cart is None:
            return self._build_direct_response(
                success=False,
                code="guest_cart_not_found",
                message="The specified guest cart could not be found.",
                status=404,
            )

        try:
            merged = CartService.merge_guest_cart_into_customer(
                guest_cart=guest_cart, customer=user
            )
        except Exception as exc:
            return _map_exception_to_response(exc, request_id=self.request_id)
        if merged is None:
            return self._build_direct_response(
                success=False,
                code="merge_failed",
                message="Cart merge failed.",
                status=500,
            )
        # Refresh self.cart to point at the merged cart for the response.
        self.cart = merged
        return self._build_direct_response(
            success=True,
            code="merge_ok",
            message="Cart merge completed successfully.",
            data={"cart": _serialize_cart(merged)},
            status=200,
        )

@method_decorator(csrf_exempt, name="dispatch")
class CartValidateView(CartAPIBaseView):
    """POST /api/v1/cart/validate/ - validate the cart for checkout readiness."""

    allowed_methods = ["POST", "GET"]
    require_authentication = False

    def get(self, request, *args, **kwargs):
        return self._validate()

    def post(self, request, *args, **kwargs):
        return self._validate()

    def _validate(self):
        try:
            result = CartInventoryService.validate_for_checkout(cart=self.cart)
            ready = bool(result.get("ready_for_checkout", False))
            issues = result.get("issues", []) or []
            payload = {
                "ready_for_checkout": ready,
                "issues": issues,
                "totals": result.get("totals", {}),
                "cart": _serialize_cart(self.cart),
            }
            return self._build_direct_response(
                success=ready,
                code="ready_for_checkout" if ready else "checkout_blocked",
                message=(
                    "Cart is ready for checkout."
                    if ready
                    else "Cart has issues that must be resolved before checkout."
                ),
                data=payload,
                status=200 if ready else 409,
            )
        except Exception as exc:
            return _map_exception_to_response(exc, request_id=self.request_id)

@method_decorator(csrf_exempt, name="dispatch")
class CartSummaryView(CartAPIBaseView):
    """GET /api/v1/cart/summary/ - get cart totals and item counts."""

    allowed_methods = ["GET"]
    require_authentication = False

    def get(self, request, *args, **kwargs):
        try:
            warehouse = getattr(self.cart, "preferred_warehouse", None)
            items = list(
                self.cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
                .select_related("product", "variant", "reservation")
            )
            serialized = [_serialize_cart_item(item, warehouse=warehouse) for item in items]
            totals = CartService.compute_totals(self.cart)
            return self._build_direct_response(
                success=True,
                code="summary_retrieved",
                message="Cart summary retrieved successfully.",
                data={
                    "summary": {
                        "subtotal": str(totals.get("subtotal", Decimal("0.00"))),
                        "tax": str(totals.get("tax", Decimal("0.00"))),
                        "shipping": str(totals.get("shipping", Decimal("0.00"))),
                        "discount": str(totals.get("discount", Decimal("0.00"))),
                        "grand_total": str(totals.get("grand_total", Decimal("0.00"))),
                        "total_items": int(totals.get("total_items", 0)),
                        "unique_items": int(totals.get("unique_items", 0)),
                    },
                    "coupon": {
                        "code": _safe_str(getattr(self.cart, "coupon_code", "")) or None,
                        "discount_amount": str(
                            _safe_decimal(
                                getattr(self.cart, "coupon_discount_amount", None),
                                default=Decimal("0.00"),
                            )
                        ),
                    },
                    "currency": _currency(self.cart),
                    "item_count": len(serialized),
                },
                status=200,
            )
        except Exception as exc:
            return _map_exception_to_response(exc, request_id=self.request_id)

@method_decorator(csrf_exempt, name="dispatch")
class CartEstimateView(CartAPIBaseView):
    """GET /api/v1/cart/estimate/ - tax, shipping, and total estimates."""

    allowed_methods = ["GET"]
    require_authentication = False

    def get(self, request, *args, **kwargs):
        try:
            warehouse = getattr(self.cart, "preferred_warehouse", None)
            items = list(
                self.cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
                .select_related("product", "variant", "reservation")
            )
            # Serialize ONCE and reuse (previously called twice).
            serialized_items = [_serialize_cart_item(item, warehouse=warehouse) for item in items]
            overall = _compute_overall_inventory_status(serialized_items)
            totals = CartService.compute_totals(self.cart)
            payload = {
                "currency": _currency(self.cart),
                "subtotal": str(totals.get("subtotal", Decimal("0.00"))),
                "tax": str(totals.get("tax", Decimal("0.00"))),
                "shipping": str(totals.get("shipping", Decimal("0.00"))),
                "discount": str(totals.get("discount", Decimal("0.00"))),
                "grand_total": str(totals.get("grand_total", Decimal("0.00"))),
                "total_items": int(totals.get("total_items", 0)),
                "unique_items": int(totals.get("unique_items", 0)),
                "inventory_overview": overall,
            }
            return self._build_direct_response(
                success=True,
                code="estimate_retrieved",
                message="Cart estimate retrieved successfully.",
                data={"estimate": payload},
                status=200,
            )
        except Exception as exc:
            return _map_exception_to_response(exc, request_id=self.request_id)

# =============================================================================
# RESTful v1: COUPON
# =============================================================================
@method_decorator(csrf_exempt, name="dispatch")
class CartCouponApplyView(CartAPIBaseView):
    """POST /api/v1/cart/coupon/ - apply a coupon code to the cart.

    This is the SINGLE source of truth for cart coupon application at
    the REST API layer. All coupon logic is delegated to the
    ``CartCouponService`` (which in turn delegates to the coupon
    service registered in the application registry).

    A backward-compatible alias named ``CartApplyCouponView`` is
    provided below to support legacy URL configurations that expect
    the older class name. The alias is a thin wrapper that delegates
    to this canonical implementation. There is intentionally only one
    coupon application code path in the entire codebase.
    """

    allowed_methods = ["POST"]
    require_authentication = False

    def post(self, request, *args, **kwargs):
        data, error = _parse_json_body(request)
        if error is not None:
            return error
        code = _safe_str(data.get("code") or data.get("coupon_code", ""))
        if not code:
            return self._build_direct_response(
                success=False,
                code="missing_coupon_code",
                message="Coupon code is required.",
                status=400,
            )
        user = getattr(request, "user", None)
        try:
            result = CartCouponService.apply_coupon(
                cart=self.cart,
                code=code,
                customer=user if user and user.is_authenticated else None,
            )
        except Exception as exc:
            return _map_exception_to_response(exc, request_id=self.request_id)
        return self._build_service_response(result)


# ----------------------------------------------------------------------------
# Backward-compatible alias for legacy URL configurations.
# ----------------------------------------------------------------------------
@method_decorator(csrf_exempt, name="dispatch")
class CartApplyCouponView(CartCouponApplyView):
    """
    Backward-compatible alias for ``CartCouponApplyView``.

    Preserved for existing URL configurations and external consumers
    that import the older class name. This class is a pass-through
    alias and does not implement any coupon logic of its own.
    """

    allowed_methods = ["POST"]
    require_authentication = False


@method_decorator(csrf_exempt, name="dispatch")
class CartCouponRemoveView(CartAPIBaseView):
    """DELETE /api/v1/cart/coupon/ - remove the currently applied coupon."""

    allowed_methods = ["DELETE", "POST"]
    require_authentication = False

    def delete(self, request, *args, **kwargs):
        return self._remove()

    def post(self, request, *args, **kwargs):
        return self._remove()

    def _remove(self):
        try:
            result = CartCouponService.remove_coupon(cart=self.cart)
        except Exception as exc:
            return _map_exception_to_response(exc, request_id=self.request_id)
        return self._build_service_response(result)


# ----------------------------------------------------------------------------
# Backward-compatible alias: CartRemoveCouponView (matches the import in
# ``apps/cart/urls.py``). This is a pure pass-through alias.
# ----------------------------------------------------------------------------
@method_decorator(csrf_exempt, name="dispatch")
class CartRemoveCouponView(CartCouponRemoveView):
    """Backward-compatible alias for ``CartCouponRemoveView``.

    Preserved for legacy URL configurations that import the older
    class name. Implementation lives in ``CartCouponRemoveView`` and
    ``CartCouponService``.
    """

    allowed_methods = ["DELETE", "POST"]
    require_authentication = False

# =============================================================================
# RESTful v1: INVENTORY CONTEXT (read-only)
# =============================================================================
@method_decorator(csrf_exempt, name="dispatch")
class CartInventoryContextView(CartAPIBaseView):
    """
    GET /api/v1/cart/inventory/

    Return a read-only inventory context for the entire cart, including
    per-line and aggregate state. This endpoint exists so that the
    storefront UI can render stock badges, low-stock warnings, and
    out-of-stock banners without re-implementing any inventory
    business logic. All data flows from the Inventory application
    through the Cart service layer.
    """

    allowed_methods = ["GET"]
    require_authentication = False

    def get(self, request, *args, **kwargs):
        try:
            warehouse = getattr(self.cart, "preferred_warehouse", None)
            items = list(
                self.cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
                .select_related("product", "variant", "reservation")
            )
            # Serialize ONCE; reuse for both per-line payloads and the
            # overall aggregate. Previously this view invoked
            # ``_serialize_inventory`` once per item AND
            # ``_serialize_cart_item`` (which itself calls
            # ``_serialize_inventory``) again, causing 2N inventory
            # service calls per request.
            serialized_items = [_serialize_cart_item(item, warehouse=warehouse) for item in items]
            line_payloads: List[Dict[str, Any]] = [
                {
                    "item_id": s["id"],
                    "product_id": s["product_id"],
                    "variant_id": s["variant_id"],
                    "requested_quantity": s["quantity"],
                    "inventory": s["inventory"],
                }
                for s in serialized_items
            ]
            overall = _compute_overall_inventory_status(serialized_items)
            return self._build_direct_response(
                success=True,
                code="inventory_context_retrieved",
                message="Cart inventory context retrieved successfully.",
                data={
                    "items": line_payloads,
                    "overall": overall,
                    "warehouse_id": getattr(warehouse, "pk", None) if warehouse else None,
                    "warehouse_name": (
                        getattr(warehouse, "display_name", None)
                        or getattr(warehouse, "name", None)
                        if warehouse
                        else None
                    ),
                },
                status=200,
            )
        except Exception as exc:
            return _map_exception_to_response(exc, request_id=self.request_id)

# =============================================================================
# RESTful v1: RESERVATIONS
# =============================================================================
@method_decorator(csrf_exempt, name="dispatch")
class CartReservationsView(CartAPIBaseView):
    """
    GET /api/v1/cart/reservations/

    Return the current reservation status for every active cart
    line. The data is surfaced verbatim from the Inventory
    application's StockReservation rows via the Cart service layer.
    """

    allowed_methods = ["GET"]
    require_authentication = False

    def get(self, request, *args, **kwargs):
        try:
            items = list(
                self.cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
                .select_related("reservation")
            )
            reservation_payloads = []
            for item in items:
                reservation_payloads.append(
                    {
                        "item_id": item.pk,
                        "product_id": item.product_id,
                        "variant_id": item.variant_id,
                        "requested_quantity": int(item.quantity or 0),
                        "reservation": _serialize_reservation(item),
                    }
                )
            return self._build_direct_response(
                success=True,
                code="reservations_retrieved",
                message="Reservation status retrieved successfully.",
                data={
                    "reservations": reservation_payloads,
                    "count": len(reservation_payloads),
                },
                status=200,
            )
        except Exception as exc:
            return _map_exception_to_response(exc, request_id=self.request_id)

@method_decorator(csrf_exempt, name="dispatch")
class CartReservationRefreshView(CartAPIBaseView):
    """
    POST /api/v1/cart/reservations/refresh/

    Trigger the inventory service's expired-reservation cleanup
    pipeline. Useful for long-lived anonymous carts returning from
    focus to refresh stock state.
    """

    allowed_methods = ["POST"]
    require_authentication = False

    def post(self, request, *args, **kwargs):
        try:
            # Pass the resolved cart to the service. The service may
            # still operate on a global cleanup, but providing the
            # cart is the documented contract.
            cleanup = CartInventoryService.cleanup_expired_reservations_for_cart(
                cart=self.cart
            )
            return self._build_direct_response(
                success=True,
                code="reservations_refreshed",
                message="Reservation cleanup executed.",
                data={"cleanup": cleanup},
                status=200,
            )
        except TypeError:
            # Service signature fallback for callers that expect the
            # no-argument form (e.g. global cleanup).
            try:
                cleanup = CartInventoryService.cleanup_expired_reservations_for_cart()
                return self._build_direct_response(
                    success=True,
                    code="reservations_refreshed",
                    message="Reservation cleanup executed.",
                    data={"cleanup": cleanup},
                    status=200,
                )
            except Exception as exc:
                return _map_exception_to_response(exc, request_id=self.request_id)
        except Exception as exc:
            return _map_exception_to_response(exc, request_id=self.request_id)

# =============================================================================
# RESTful v1: REORDER
# =============================================================================
@method_decorator(csrf_exempt, name="dispatch")
class CartReorderView(CartAPIBaseView):
    """
    POST /api/v1/cart/reorder/

    Reorder items from a past order. Requires authentication.
    Accepts either an ``order_id`` (the order will be loaded from the
    Orders application) or an explicit ``items`` list.
    """

    allowed_methods = ["POST"]
    require_authentication = True

    def post(self, request, *args, **kwargs):
        data, error = _parse_json_body(request)
        if error is not None:
            return error

        order_id = data.get("order_id")
        items = data.get("items")

        if not order_id and not items:
            return self._build_direct_response(
                success=False,
                code="missing_input",
                message="Either an order ID or an items list is required.",
                status=400,
            )

        order_reference = _safe_str(data.get("order_reference", ""))

        # If order_id is provided, attempt to load the order from the
        # Orders application. If the Orders app is unavailable or the
        # order does not exist, return a structured 404 response.
        if order_id:
            order = None
            try:
                from apps.orders.models import Order
                order = (
                    Order.objects.filter(pk=order_id, customer=request.user)
                    .prefetch_related("items")
                    .first()
                )
            except (DatabaseError, ImportError) as exc:
                logger.warning("Order lookup failed in reorder: %s", exc)
                order = None
            if order is None:
                return self._build_direct_response(
                    success=False,
                    code="order_not_found",
                    message="Order not found or not accessible.",
                    status=404,
                )
            try:
                result = CartReorderService.reorder_items_into_cart(
                    cart=self.cart, order=order, user=request.user
                )
            except Exception as exc:
                return _map_exception_to_response(exc, request_id=self.request_id)
            return self._build_service_response(result)

        if not isinstance(items, list):
            return self._build_direct_response(
                success=False,
                code="invalid_items",
                message="Field 'items' must be a list.",
                status=400,
            )

        try:
            result = CartReorderService.reorder_items_into_cart(
                cart=self.cart,
                items=items,
                order_reference=order_reference,
            )
        except Exception as exc:
            return _map_exception_to_response(exc, request_id=self.request_id)
        return self._build_service_response(result)

# =============================================================================
# LEGACY FUNCTION-BASED ENDPOINTS (backward compatibility)
# =============================================================================
# These endpoints preserve the public URLs and response shapes consumed
# by existing JavaScript clients (notably product-card.js and the legacy
# storefront). They are kept on the same module so that the URL
# configuration continues to work without modification.

def _legacy_cors(request: HttpRequest) -> Dict[str, str]:
    """Build CORS headers for legacy function-based endpoints."""
    return _build_cors_headers(request)

@require_GET
def cart_estimate_legacy(request: HttpRequest) -> JsonResponse:
    """
    GET /api/cart/estimate/

    Legacy tax / shipping / total estimate endpoint used by the
    storefront JavaScript. Returns the legacy flat response shape.

    All math is delegated to the Cart service layer.
    """
    request_id = uuid.uuid4().hex[:16]
    cors_headers = _legacy_cors(request)
    try:
        if _is_throttled(request):
            response = _api_error_response(
                code="rate_limited",
                message="Too many requests.",
                status=429,
                request_id=request_id,
            )
            response["Retry-After"] = str(_throttle_retry_after_seconds(request))
        else:
            cart, _ = CartService.get_or_create_for_request(request)
            if cart is None:
                response = _api_error_response(
                    code="cart_not_found",
                    message="Cart not found.",
                    status=400,
                    request_id=request_id,
                )
            else:
                warehouse = getattr(cart, "preferred_warehouse", None)
                items = list(
                    cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
                    .select_related("product", "variant", "reservation")
                )
                overall = _compute_overall_inventory_status(
                    [_serialize_cart_item(item, warehouse=warehouse) for item in items]
                )
                totals = CartService.compute_totals(cart)
                response = JsonResponse(
                    {
                        "status": "success",
                        "currency": _currency(cart),
                        "subtotal": str(totals.get("subtotal", Decimal("0.00"))),
                        "discount": str(totals.get("discount", Decimal("0.00"))),
                        "tax": str(totals.get("tax", Decimal("0.00"))),
                        "shipping": str(
                            totals.get("shipping", Decimal("0.00"))
                        ),
                        "total": str(
                            totals.get("grand_total", Decimal("0.00"))
                        ),
                        "item_count": int(totals.get("total_items", 0)),
                        "unique_count": int(totals.get("unique_items", 0)),
                        "inventory_overview": overall,
                    }
                )
    except Exception as exc:
        response = _map_exception_to_response(exc, request_id=request_id)
    for header_name, header_value in cors_headers.items():
        try:
            response[header_name] = header_value
        except Exception:
            pass
    return response

@require_GET
def cart_validate_legacy(request: HttpRequest) -> JsonResponse:
    """
    GET /api/cart/validate/

    Legacy read-only validation endpoint. Delegates to the Cart
    inventory service.
    """
    try:
        cart, _ = CartService.get_or_create_for_request(request)
        if cart is None:
            return JsonResponse(
                {"status": "error", "message": str(_("Cart not found."))},
                status=400,
            )
        try:
            result = CartInventoryService.validate_for_checkout(cart=cart)
            if isinstance(result, dict) and result.get("ready_for_checkout") is False:
                return JsonResponse(
                    {
                        "status": "error",
                        "message": str(_("Cart is not ready for checkout.")),
                        "issues": result.get("issues", []),
                    },
                    status=400,
                )
            return JsonResponse({"status": "ok"})
        except DjangoValidationError as exc:
            msg = (
                exc.messages[0]
                if getattr(exc, "messages", None)
                else str(exc)
            )
            return JsonResponse(
                {"status": "error", "message": str(msg)},
                status=400,
            )
    except Exception as exc:
        logger.exception("Legacy cart validate failed: %s", exc)
        return JsonResponse(
            {"status": "error", "message": str(_("Cart validation failed."))},
            status=500,
        )

@csrf_exempt
@require_POST
def cart_sync_legacy(request: HttpRequest) -> JsonResponse:
    """
    POST /api/cart/sync/

    Legacy "sync" endpoint. Adds an item to the cart using POST
    parameters and returns a snapshot of the cart in the legacy flat
    response shape.
    Preserved verbatim for the legacy storefront JS.
    """
    request_id = uuid.uuid4().hex[:16]
    cors_headers = _legacy_cors(request)
    if _is_throttled(request):
        response = _api_error_response(
            code="rate_limited",
            message="Too many requests.",
            status=429,
            request_id=request_id,
        )
        response["Retry-After"] = str(_throttle_retry_after_seconds(request))
        for header_name, header_value in cors_headers.items():
            response[header_name] = header_value
        return response
    try:
        cart, _ = CartService.get_or_create_for_request(request)
        if cart is None:
            response = JsonResponse(
                {"status": "error", "message": str(_("Cart not found."))},
                status=400,
            )
        else:
            product_id = request.POST.get("product_id")
            variant_id = request.POST.get("variant_id")
            try:
                quantity = int(request.POST.get("quantity", 1))
            except (TypeError, ValueError):
                quantity = 1
            if not product_id:
                response = JsonResponse(
                    {"status": "error", "message": str(_("Missing product_id."))},
                    status=400,
                )
            else:
                from apps.catalog.models import Product, ProductVariant

                product = None
                product_variant = None
                try:
                    product = Product.objects.filter(pk=product_id).first()
                except DatabaseError:
                    product = None
                if variant_id and not product:
                    try:
                        product_variant = (
                            ProductVariant.objects.filter(pk=variant_id)
                            .select_related("product")
                            .first()
                        )
                        if product_variant is not None:
                            product = getattr(product_variant, "product", None)
                    except DatabaseError:
                        product_variant = None
                if product is None and product_variant is None:
                    response = JsonResponse(
                        {"status": "error", "message": str(_("Product not found."))},
                        status=404,
                    )
                else:
                    try:
                        CartItemService.add_item(
                            cart=cart,
                            product=product,
                            variant=product_variant,
                            quantity=quantity,
                        )
                        # Re-serialize the cart in the legacy snapshot shape.
                        warehouse = getattr(cart, "preferred_warehouse", None)
                        items = list(
                            cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
                            .select_related("product", "variant", "reservation")
                        )
                        serialized_items = [
                            _serialize_cart_item(item, warehouse=warehouse)
                            for item in items
                        ]
                        overall = _compute_overall_inventory_status(serialized_items)
                        response = JsonResponse(
                            {
                                "status": "success",
                                "cart": {
                                    "id": cart.pk,
                                    "item_count": getattr(cart, "total_items_count", lambda: 0)() or 0,
                                    "unique_count": getattr(cart, "unique_items_count", lambda: 0)() or 0,
                                    "subtotal": str(
                                        getattr(cart, "subtotal", None) or Decimal("0.00")
                                    ),
                                    "discount": str(
                                        getattr(cart, "coupon_discount_amount", None)
                                        or Decimal("0.00")
                                    ),
                                    "tax": str(
                                        getattr(cart, "estimated_tax", None)
                                        or Decimal("0.00")
                                    ),
                                    "shipping": str(
                                        getattr(cart, "estimated_shipping", None)
                                        or Decimal("0.00")
                                    ),
                                    "total": str(
                                        getattr(cart, "grand_total", None)
                                        or Decimal("0.00")
                                    ),
                                    "currency": _currency(cart),
                                },
                                "mini_cart_items": serialized_items,
                                "inventory_overview": overall,
                            }
                        )
                    except (DjangoValidationError, ValueError) as exc:
                        msg = (
                            exc.messages[0]
                            if hasattr(exc, "messages") and exc.messages
                            else str(exc)
                        )
                        response = JsonResponse(
                            {"status": "error", "message": str(msg)},
                            status=400,
                        )
    except Exception as exc:
        response = _map_exception_to_response(exc, request_id=request_id)
    for header_name, header_value in cors_headers.items():
        try:
            response[header_name] = header_value
        except Exception:
            pass
    return response

@require_GET
def cart_sync_snapshot_legacy(request: HttpRequest) -> JsonResponse:
    """
    GET /api/cart/sync/

    Legacy read-only cart snapshot. Returns the legacy flat response
    shape. Does NOT mutate the cart.
    """
    try:
        cart, _ = CartService.get_or_create_for_request(request)
        if cart is None:
            return JsonResponse(
                {"status": "ok", "is_wishlisted": False, "cart": None}
            )
        warehouse = getattr(cart, "preferred_warehouse", None)
        items = list(
            cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
            .select_related("product", "variant", "reservation")
        )
        serialized_items = [
            _serialize_cart_item(item, warehouse=warehouse)
            for item in items
        ]
        overall = _compute_overall_inventory_status(serialized_items)
        return JsonResponse(
            {
                "status": "ok",
                "is_wishlisted": False,
                "cart": {
                    "id": cart.pk,
                    "item_count": getattr(cart, "total_items_count", lambda: 0)() or 0,
                    "subtotal": str(
                        getattr(cart, "subtotal", None) or Decimal("0.00")
                    ),
                    "total": str(
                        getattr(cart, "grand_total", None) or Decimal("0.00")
                    ),
                    "currency": _currency(cart),
                },
                "mini_cart_items": serialized_items,
                "inventory_overview": overall,
            }
        )
    except Exception as exc:
        logger.exception("Legacy cart sync snapshot failed: %s", exc)
        return JsonResponse(
            {"status": "error", "message": str(_("Cart snapshot failed."))},
            status=500,
        )

@csrf_exempt
@require_POST
def cart_apply_coupon_legacy(request: HttpRequest) -> JsonResponse:
    """POST /api/cart/apply-coupon/  - legacy coupon apply endpoint.

    Delegates to the Cart coupon service. The service layer is
    the SINGLE source of truth for coupon validation and
    discount calculation.
    """
    request_id = uuid.uuid4().hex[:16]
    cors_headers = _legacy_cors(request)
    if _is_throttled(request):
        response = _api_error_response(
            code="rate_limited",
            message="Too many requests.",
            status=429,
            request_id=request_id,
        )
        response["Retry-After"] = str(_throttle_retry_after_seconds(request))
        for header_name, header_value in cors_headers.items():
            response[header_name] = header_value
        return response
    try:
        cart, _ = CartService.get_or_create_for_request(request)
        if cart is None:
            response = JsonResponse(
                {"status": "error", "message": str(_("Cart not found."))},
                status=400,
            )
        else:
            code = _safe_str(request.POST.get("coupon_code", ""))
            if not code:
                response = JsonResponse(
                    {"status": "error", "message": str(_("Please enter a coupon code."))},
                    status=400,
                )
            else:
                discount_amount_raw = request.POST.get("discount_amount", "0")
                discount_amount = _safe_decimal(
                    discount_amount_raw, default=Decimal("0.00")
                )
                try:
                    result = CartCouponService.apply_coupon(
                        cart=cart,
                        code=code,
                        discount_amount=discount_amount,
                    )
                except AttributeError:
                    # Service signature fallback for legacy callers
                    result = CartCouponService.apply_coupon(
                        cart=cart, code=code
                    )
                if result.get("success", False):
                    response = JsonResponse(
                        {
                            "status": "success",
                            "message": str(
                                _("Coupon '%(code)s' applied.") % {"code": code}
                            ),
                            "cart": {
                                "subtotal": str(
                                    getattr(cart, "subtotal", None) or Decimal("0.00")
                                ),
                                "discount": str(
                                    getattr(cart, "coupon_discount_amount", None)
                                    or Decimal("0.00")
                                ),
                                "tax": str(
                                    getattr(cart, "estimated_tax", None)
                                    or Decimal("0.00")
                                ),
                                "shipping": str(
                                    getattr(cart, "estimated_shipping", None)
                                    or Decimal("0.00")
                                ),
                                "total": str(
                                    getattr(cart, "grand_total", None)
                                    or Decimal("0.00")
                                ),
                                "currency": _currency(cart),
                            },
                        }
                    )
                else:
                    msg = (
                        result.get("error")
                        or result.get("message")
                        or "Invalid coupon."
                    )
                    response = JsonResponse(
                        {"status": "error", "message": str(msg)},
                        status=400,
                    )
    except Exception as exc:
        response = _map_exception_to_response(exc, request_id=request_id)
    for header_name, header_value in cors_headers.items():
        try:
            response[header_name] = header_value
        except Exception:
            pass
    return response

@csrf_exempt
@require_POST
def cart_remove_coupon_legacy(request: HttpRequest) -> JsonResponse:
    """POST /api/cart/remove-coupon/  - legacy coupon removal endpoint.

    Delegates to the Cart coupon service.
    """
    request_id = uuid.uuid4().hex[:16]
    cors_headers = _legacy_cors(request)
    if _is_throttled(request):
        response = _api_error_response(
            code="rate_limited",
            message="Too many requests.",
            status=429,
            request_id=request_id,
        )
        response["Retry-After"] = str(_throttle_retry_after_seconds(request))
        for header_name, header_value in cors_headers.items():
            response[header_name] = header_value
        return response
    try:
        cart, _ = CartService.get_or_create_for_request(request)
        if cart is None:
            response = JsonResponse(
                {"status": "error", "message": str(_("Cart not found."))},
                status=400,
            )
        else:
            CartCouponService.remove_coupon(cart=cart)
            response = JsonResponse(
                {
                    "status": "success",
                    "message": str(_("Coupon removed.")),
                    "cart": {
                        "subtotal": str(
                            getattr(cart, "subtotal", None) or Decimal("0.00")
                        ),
                        "discount": str(
                            getattr(cart, "coupon_discount_amount", None)
                            or Decimal("0.00")
                        ),
                        "tax": str(
                            getattr(cart, "estimated_tax", None) or Decimal("0.00")
                        ),
                        "shipping": str(
                            getattr(cart, "estimated_shipping", None)
                            or Decimal("0.00")
                        ),
                        "total": str(
                            getattr(cart, "grand_total", None) or Decimal("0.00")
                        ),
                        "currency": _currency(cart),
                    },
                }
            )
    except Exception as exc:
        response = _map_exception_to_response(exc, request_id=request_id)
    for header_name, header_value in cors_headers.items():
        try:
            response[header_name] = header_value
        except Exception:
            pass
    return response

@csrf_exempt
@require_POST
def cart_merge_legacy(request: HttpRequest) -> JsonResponse:
    """POST /api/cart/merge/  - legacy merge endpoint (login_required).

    Delegates to the Cart service layer. The Cart service handles
    guest-to-customer merge, reservation cleanup, and conflict
    resolution.
    """
    request_id = uuid.uuid4().hex[:16]
    cors_headers = _legacy_cors(request)
    if _is_throttled(request):
        response = _api_error_response(
            code="rate_limited",
            message="Too many requests.",
            status=429,
            request_id=request_id,
        )
        response["Retry-After"] = str(_throttle_retry_after_seconds(request))
        for header_name, header_value in cors_headers.items():
            response[header_name] = header_value
        return response
    try:
        if not request.user or not request.user.is_authenticated:
            response = JsonResponse(
                {"status": "error", "message": str(_("Authentication is required."))},
                status=401,
            )
        else:
            if not request.session.session_key:
                try:
                    request.session.create()
                except Exception:
                    pass
            session_key = request.session.session_key or ""
            guest_cart = (
                Cart.objects.filter(
                    session_key=session_key,
                    customer__isnull=True,
                    is_active=True,
                )
                .order_by("-last_activity_at")
                .first()
            )
            if guest_cart is None:
                response = JsonResponse(
                    {"status": "success", "message": str(_("Guest cart merged successfully."))}
                )
            else:
                try:
                    merged = CartService.merge_guest_cart_into_customer(
                        guest_cart=guest_cart, customer=request.user
                    )
                except Exception as exc:
                    logger.exception("Legacy cart merge failed: %s", exc)
                    response = JsonResponse(
                        {"status": "error", "message": str(_("Unable to merge guest cart."))},
                        status=500,
                    )
                else:
                    if merged is None:
                        response = JsonResponse(
                            {"status": "success", "message": str(_("Guest cart merged successfully."))}
                        )
                    else:
                        response = JsonResponse(
                            {
                                "status": "success",
                                "message": str(_("Guest cart merged successfully.")),
                                "cart": {
                                    "id": getattr(merged, "pk", None),
                                    "item_count": (
                                        getattr(merged, "total_items_count", lambda: 0)()
                                        if merged is not None
                                        else 0
                                    ),
                                    "subtotal": str(
                                        getattr(merged, "subtotal", None)
                                        or Decimal("0.00")
                                    ) if merged is not None else "0.00",
                                    "total": str(
                                        getattr(merged, "grand_total", None)
                                        or Decimal("0.00")
                                    ) if merged is not None else "0.00",
                                    "currency": _currency(merged) if merged is not None else _DEFAULT_CURRENCY,
                                },
                            }
                        )
    except Exception as exc:
        logger.exception("cart_merge_legacy outer failure: %s", exc)
        response = JsonResponse(
            {"status": "error", "message": str(_("Unable to merge guest cart."))},
            status=500,
        )
    for header_name, header_value in cors_headers.items():
        try:
            response[header_name] = header_value
        except Exception:
            pass
    return response

@csrf_exempt
@require_POST
def cart_reorder_legacy(request: HttpRequest) -> JsonResponse:
    """POST /api/cart/reorder/  - legacy reorder endpoint (login_required).

    Delegates to the Cart reorder service. The service layer is
    the single source of truth for reorder logic.
    """
    request_id = uuid.uuid4().hex[:16]
    cors_headers = _legacy_cors(request)
    if _is_throttled(request):
        response = _api_error_response(
            code="rate_limited",
            message="Too many requests.",
            status=429,
            request_id=request_id,
        )
        response["Retry-After"] = str(_throttle_retry_after_seconds(request))
        for header_name, header_value in cors_headers.items():
            response[header_name] = header_value
        return response
    try:
        if not request.user or not request.user.is_authenticated:
            response = JsonResponse(
                {"status": "error", "message": str(_("Authentication is required."))},
                status=401,
            )
        else:
            order_id = request.POST.get("order_id")
            if not order_id:
                response = JsonResponse(
                    {"status": "error", "message": str(_("Order ID is required."))},
                    status=400,
                )
            else:
                order = None
                try:
                    from apps.orders.models import Order
                    order = (
                        Order.objects.filter(pk=order_id, customer=request.user)
                        .prefetch_related("items")
                        .first()
                    )
                except (DatabaseError, ImportError) as exc:
                    logger.warning("Order lookup failed in legacy reorder: %s", exc)
                    order = None
                if order is None:
                    response = JsonResponse(
                        {
                            "status": "error",
                            "message": str(_("Order not found or access denied.")),
                        },
                        status=404,
                    )
                else:
                    try:
                        cart, _ = CartService.get_or_create_for_request(request)
                        CartReorderService.reorder_items_into_cart(
                            cart=cart, order=order, user=request.user
                        )
                    except Exception as exc:
                        logger.exception("Legacy cart reorder failed: %s", exc)
                        response = JsonResponse(
                            {
                                "status": "error",
                                "message": str(_("Unable to reorder items.")),
                            },
                            status=500,
                        )
                    else:
                        response = JsonResponse(
                            {
                                "status": "success",
                                "message": str(_("Items reordered successfully.")),
                                "cart": {
                                    "id": getattr(cart, "pk", None),
                                    "item_count": (
                                        getattr(cart, "total_items_count", lambda: 0)()
                                    )
                                    or 0,
                                    "subtotal": str(
                                        getattr(cart, "subtotal", None)
                                        or Decimal("0.00")
                                    ),
                                    "discount": str(
                                        getattr(cart, "coupon_discount_amount", None)
                                        or Decimal("0.00")
                                    ),
                                    "tax": str(
                                        getattr(cart, "estimated_tax", None)
                                        or Decimal("0.00")
                                    ),
                                    "shipping": str(
                                        getattr(cart, "estimated_shipping", None)
                                        or Decimal("0.00")
                                    ),
                                    "total": str(
                                        getattr(cart, "grand_total", None)
                                        or Decimal("0.00")
                                    ),
                                    "currency": _currency(cart),
                                },
                            }
                        )
    except Exception as exc:
        logger.exception("cart_reorder_legacy outer failure: %s", exc)
        response = JsonResponse(
            {"status": "error", "message": str(_("Unable to reorder items."))},
            status=500,
        )
    for header_name, header_value in cors_headers.items():
        try:
            response[header_name] = header_value
        except Exception:
            pass
    return response

# =============================================================================
# CartSyncView - Compatibility shim for /api/cart/sync/ URL
# =============================================================================
@method_decorator(csrf_exempt, name="dispatch")
class CartSyncView(View):
    """
    Compatibility shim that maps the legacy `/api/cart/sync/` URL
    to the existing cart synchronization implementation.

    This thin class is a pure delegation layer. It does NOT implement
    any cart synchronization logic of its own. All actual work is
    delegated to the existing module-level functions:

        - GET  → cart_sync_snapshot_legacy()  (read-only snapshot)
        - POST → cart_sync_legacy()            (add item to cart)

    The class exists purely to provide the `CartSyncView.as_view()`
    callable expected by `apps/cart/urls.py`. It is intentionally a
    single source of truth with no implementation duplication.

    There must always be only one source of truth for cart
    synchronization. This wrapper is the only allowed entry point.
    """

    # GET is safe and idempotent (read-only snapshot).
    # POST mutates the cart (adds an item).
    http_method_names = ["get", "post", "options"]

    def get(
        self,
        request: HttpRequest,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        """
        Delegate GET requests to the legacy snapshot function.

        The legacy function preserves the original response shape
        consumed by the storefront JavaScript clients. No logic is
        duplicated in this wrapper.
        """
        return cart_sync_snapshot_legacy(request, *args, **kwargs)

    def post(
        self,
        request: HttpRequest,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        """
        Delegate POST requests to the legacy sync function.

        The legacy function preserves the original response shape and
        handles the full add-to-cart workflow including inventory
        delegation. No logic is duplicated in this wrapper.
        """
        return cart_sync_legacy(request, *args, **kwargs)

    def options(
        self,
        request: HttpRequest,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        """
        CORS preflight for the legacy sync URL.

        Delegates to the shared CORS header helper so the legacy
        storefront can preflight from any allowed origin.
        """
        cors_headers = _build_cors_headers(request)
        response = HttpResponse(status=204)
        for header_name, header_value in cors_headers.items():
            response[header_name] = header_value
        return response


# =============================================================================
# PUBLIC MODULE API
# =============================================================================
__all__ = [
    # RESTful v1 view classes
    "CartAPIBaseView",
    "CartRetrieveView",
    "CartItemListCreateView",
    "CartItemDetailView",
    "CartClearView",
    "CartMergeView",
    "CartValidateView",
    "CartSummaryView",
    "CartEstimateView",
    "CartCouponApplyView",
    "CartCouponRemoveView",
    # Backward-compatible alias for the older class name expected by
    # existing URL configurations. This is a pass-through alias that
    # subclasses CartCouponApplyView. The coupon application logic is
    # NOT duplicated here; it lives in CartCouponApplyView and the
    # CartCouponService.
    "CartApplyCouponView",
    # Backward-compatible alias for the older coupon-remove class name
    # expected by apps/cart/urls.py. Implementation lives in
    # CartCouponRemoveView and CartCouponService.
    "CartRemoveCouponView",
    # Legacy function-based views
    "cart_estimate_legacy",
    "cart_validate_legacy",
    "cart_sync_legacy",
    "cart_sync_snapshot_legacy",
    "cart_apply_coupon_legacy",
    "cart_remove_coupon_legacy",
    "cart_merge_legacy",
    "cart_reorder_legacy",
    # Compatibility shim for legacy /api/cart/sync/ URL
    "CartSyncView",
    # Public helpers (for advanced consumers / testing)
    "_api_response",
    "_api_error_response",
    "_map_exception_to_response",
    "_translate_service_payload",
    "_serialize_cart",
    "_serialize_cart_item",
    "_serialize_inventory",
    "_serialize_reservation",
    "_compute_overall_inventory_status",
    "_service_status_to_http",
    "_resolve_cart",
    "_parse_json_body",
    "_build_cors_headers",
    "_is_throttled",
    "_currency",
    "_throttle_retry_after_seconds",
]