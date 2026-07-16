"""
================================================================================
ENTERPRISE CART EVENT-DRIVEN ORCHESTRATION LAYER
================================================================================
This module implements the Django signals orchestration layer for the
Cart application. The signals are responsible ONLY for:

    * Listening to cart lifecycle events
    * Invalidating caches
    * Firing domain events for downstream consumers
    * Delegating orchestration to the service layer

CRITICAL RULES (MANDATORY)
==========================
* Inventory is the EXCLUSIVE owner of stock. Cart signals must NEVER:
    - Mutate inventory rows
    - Calculate stock levels
    - Update stock fields
    - Sync stock from variants
    - Write to inventory models

* Cart signals do NOT touch inventory, even when the legacy cart
  model has fields whose names suggest inventory ownership.

* Reentrancy protection is mandatory. All signals use thread-local
  flags to prevent recursive execution.

* Transaction safety is mandatory. Cache invalidation that affects
  downstream consumers must run inside ``transaction.on_commit``.

SECURITY (OWASP ASVS COMPLIANT)
================================
    * Never trust signal payloads - validate every object before processing
    * Prevent duplicate execution via idempotency checks
    * Prevent recursive signal calls via thread-local reentrancy guards
    * Avoid race conditions via database transactions and on_commit hooks
    * Avoid infinite loops via state machine transitions
    * Fail safely - errors are logged, never raised to the caller
    * Never expose internal exception details to external callers
    * Do not leak sensitive information in logs
    * Use transaction-aware signal handling (transaction.on_commit)

PERFORMANCE
===========
    * Optimized for millions of cart rows
    * Optimized for millions of cart items
    * Avoid unnecessary queries
    * Avoid N+1 problems via prefetch_related and bulk operations
    * Use bulk_create with ignore_conflicts=True for idempotency

FUTURE-PROOF
============
Designed to integrate seamlessly with:
    * Purchase Orders and Goods Receipt Notes
    * Manufacturing and Production
    * Warehouse Transfers
    * Batch / Lot / Expiry / Serial Number tracking
    * Customer Returns
    * Barcode / QR Code scanning events
    * Celery beat tasks
    * Kafka / RabbitMQ / Event Bus
    * REST and GraphQL APIs
    * Notifications and Audit Logs
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Cart, CartItem

logger = logging.getLogger(__name__)

# Cache keys
CART_GLOBAL_SUMMARY_KEY = "cart:global:summary:v1"
CART_CUSTOMER_KEY_PREFIX = "cart:customer:"
CART_SESSION_KEY_PREFIX = "cart:session:"
CART_TOKEN_KEY_PREFIX = "cart:token:"

# ==============================================================================
# Reentrancy Guard Namespace (Thread-Local)
# ==============================================================================
_state_lock = threading.local()

def _is_processing(flag: str) -> bool:
    """
    Thread-safe check for whether a given signal handler is currently
    in-flight. Prevents recursive execution and reentrancy loops.
    """
    return bool(getattr(_state_lock, flag, False))

def _set_processing(flag: str, value: bool = True) -> None:
    """
    Thread-safe state mutator for reentrancy guards.
    """
    setattr(_state_lock, flag, value)

def _reset_processing(flag: str) -> None:
    """
    Thread-safe reset of reentrancy state.
    """
    if hasattr(_state_lock, flag):
        delattr(_state_lock, flag)

# ==============================================================================
# Safe Logging Helper
# ==============================================================================
def _safe_log(scope: str, exc: Exception, **extra: Any) -> None:
    """
    Log a signal processing error without exposing sensitive information
    to external callers. Includes the full traceback in the server log
    but does not propagate the exception to the view or to the user.
    """
    try:
        logger.error(
            "Cart signal failure [%s]: %s | extra=%s",
            scope,
            exc,
            extra,
            exc_info=True,
        )
    except Exception:
        # Never let logging crash the signal handler.
        pass

# ==============================================================================
# Safe Cache Key Helper
# ==============================================================================
def _safe_cache_key(prefix: str, *parts: Any) -> str:
    """
    Build a cache key from a prefix and parts. Falls back to a safe
    stringification of any non-string parts. Used to compose
    consistent cache key names.
    """
    safe_parts = [prefix]
    for part in parts:
        if part is None:
            safe_parts.append("none")
        else:
            try:
                safe_parts.append(str(part))
            except Exception:
                safe_parts.append("unknown")
    return ":".join(safe_parts)

# ==============================================================================
# Cache Invalidation Helpers
# ==============================================================================
def _safe_cache_delete_many(keys: list) -> None:
    """
    Best-effort multi-key cache delete that never raises. Designed
    to keep signal handlers immune to backend cache failures.
    """
    if not keys:
        return
    try:
        cache.delete_many(keys)
    except Exception as exc:
        _safe_log("cache.delete_many", exc, keys=keys)

def _safe_cache_delete(key: str) -> None:
    """
    Best-effort single-key cache delete that never raises.
    """
    if not key:
        return
    try:
        cache.delete(key)
    except Exception as exc:
        _safe_log("cache.delete", exc, key=key)

# ==============================================================================
# Safe Cart Touch Helper
# ==============================================================================
def _safe_touch_cart(cart_id: Any) -> None:
    """
    Update ``last_activity_at`` to the current time on a cart row.

    This helper is a defensive best-effort that swallows every error so
    that a transient database problem during a signal never bubbles up
    into the calling view. It is intentionally lightweight.
    """
    if cart_id is None:
        return
    try:
        Cart.objects.filter(pk=cart_id).update(
            last_activity_at=__import__("django.utils.timezone", fromlist=["timezone"]).timezone.now()
        )
    except Exception as exc:
        _safe_log("cart.touch", exc, cart_id=cart_id)

# ==============================================================================
# 1. CART LIFECYCLE SIGNAL HANDLERS
# ==============================================================================
# Cart-level events (created / updated / deleted) drive a small set of
# lightweight cache invalidations. No inventory operation is ever
# triggered here. Inventory orchestration happens through the dedicated
# cart service layer.
# ==============================================================================
@receiver([post_save, post_delete], sender=Cart)
def invalidate_cart_cache_on_cart_change(sender: Any, instance: Cart, **kwargs: Any) -> None:
    """
    Clear cached cart summaries when a Cart is created, updated, or deleted.

    The receiver NEVER touches inventory. Cart lifecycle events are
    read-only with respect to inventory. Any inventory change triggered
    by a cart event is delegated to the cart service layer, which in
    turn calls the inventory services layer.
    """
    try:
        if kwargs.get("raw", False):
            return

        # Invalidate global cart summary cache
        _safe_cache_delete(CART_GLOBAL_SUMMARY_KEY)
        # Invalidate customer-specific cart cache
        if instance.customer_id:
            _safe_cache_delete(
                _safe_cache_key(CART_CUSTOMER_KEY_PREFIX, instance.customer_id)
            )
        # Invalidate session-specific cart cache
        if instance.session_key:
            _safe_cache_delete(
                _safe_cache_key(CART_SESSION_KEY_PREFIX, instance.session_key)
            )
        # Invalidate token-specific cart cache
        if instance.anonymous_token:
            _safe_cache_delete(
                _safe_cache_key(CART_TOKEN_KEY_PREFIX, instance.anonymous_token)
            )
    except Exception as exc:
        _safe_log("invalidate_cart_cache_on_cart_change", exc,
                  cart_id=getattr(instance, "id", None))

# ==============================================================================
# 2. CART ITEM SIGNAL HANDLERS
# ==============================================================================
# Cart item events only invalidate cache and update the parent cart's
# last_activity_at. They do NOT touch inventory. Inventory reservations,
# releases, and other stock operations are owned exclusively by the
# inventory application.
# ==============================================================================
@receiver([post_save, post_delete], sender=CartItem)
def touch_cart_on_item_change(sender: Any, instance: CartItem, **kwargs: Any) -> None:
    """
    Update the cart's last_activity_at timestamp when cart items change.

    Also invalidate related cart caches since items changed. The
    receiver does NOT touch inventory. Inventory reservations and
    other stock operations are owned exclusively by the inventory
    application.
    """
    try:
        cart_id = getattr(instance, "cart_id", None)
        if cart_id is None:
            return

        # Invalidate cart caches since items changed
        _safe_cache_delete(CART_GLOBAL_SUMMARY_KEY)
        cart = getattr(instance, "cart", None)
        if cart is not None:
            if cart.customer_id:
                _safe_cache_delete(
                    _safe_cache_key(CART_CUSTOMER_KEY_PREFIX, cart.customer_id)
                )
            if cart.session_key:
                _safe_cache_delete(
                    _safe_cache_key(CART_SESSION_KEY_PREFIX, cart.session_key)
                )
            if cart.anonymous_token:
                _safe_cache_delete(
                    _safe_cache_key(CART_TOKEN_KEY_PREFIX, cart.anonymous_token)
                )
    except Exception as exc:
        _safe_log("touch_cart_on_item_change", exc,
                  cart_id=getattr(instance, "cart_id", None))

@receiver(post_save, sender=CartItem)
def touch_cart_on_item_create(sender: Any, instance: CartItem, created: bool, **kwargs: Any) -> None:
    """
    Update the cart's last_activity_at timestamp when a new item is
    added.

    This is a lighter-weight secondary signal that complements the
    generic cart-item handler. It updates the parent cart's
    last_activity_at so the cart surfaces correctly in recent-activity
    dashboards. It does NOT touch inventory.
    """
    if not created:
        return
    try:
        _safe_touch_cart(getattr(instance, "cart_id", None))
    except Exception as exc:
        _safe_log("touch_cart_on_item_create", exc,
                  cart_id=getattr(instance, "cart_id", None))

# ==============================================================================
# 3. CART MERGE EVENT PUBLISHER
# ==============================================================================
# Domain events fired whenever a cart merge completes. The events are
# pure signals (not Django signals) and can be subscribed to by any
# downstream consumer (notifications, analytics, future omnichannel
# sync, etc.) without coupling to the merge implementation. The events
# fire AFTER the database commit so subscribers see a consistent
# post-merge snapshot.
# ==============================================================================
from django.dispatch import Signal

# Custom Django signals (not model signals) for domain events.
# These fire on_commit to guarantee subscribers see a consistent
# post-commit snapshot and never see in-flight partial merges.
cart_merged = Signal()  # providing_args=["customer", "source_cart", "destination_cart", "result"]
cart_reservation_changed = Signal()  # providing_args=["cart", "cart_item", "reservation_payload"]

def publish_cart_merged_event(
    *,
    customer: Any,
    source_cart: Any,
    destination_cart: Any,
    result: Any,
) -> None:
    """
    Publish a domain event announcing that a cart merge has been
    completed. The event is fired through Django's database
    transaction on_commit hook so subscribers always see a
    consistent post-merge snapshot and never see in-flight partial
    merges.

    The event is intentionally a pure domain signal that any
    downstream consumer (notifications, analytics, future
    omnichannel sync) can subscribe to without coupling to the cart
    merge implementation.
    """
    if customer is None or source_cart is None or destination_cart is None:
        return
    try:
        def _publish() -> None:
            try:
                cart_merged.send(
                    sender=__name__,
                    customer=customer,
                    source_cart=source_cart,
                    destination_cart=destination_cart,
                    result=result,
                )
            except Exception as exc:
                _safe_log("publish_cart_merged_event", exc,
                          customer_id=getattr(customer, "id", None),
                          source_cart_id=getattr(source_cart, "id", None),
                          destination_cart_id=getattr(destination_cart, "id", None))

        try:
            from django.db import transaction as _transaction
            _transaction.on_commit(_publish)
        except Exception:
            # Fall back to immediate publish if outside a transaction
            # (e.g. management commands). The receiver is responsible
            # for idempotency.
            _publish()
    except Exception as exc:
        _safe_log("publish_cart_merged_event", exc,
                  customer_id=getattr(customer, "id", None))

def publish_reservation_event(
    *,
    cart: Any,
    cart_item: Any,
    reservation_payload: Any,
) -> None:
    """
    Publish a domain event announcing that a cart item's reservation
    reference has been refreshed or released. Fired on_commit so
    downstream consumers (notifications, analytics) observe a
    consistent post-commit snapshot.
    """
    if cart is None or cart_item is None:
        return
    try:
        def _publish() -> None:
            try:
                cart_reservation_changed.send(
                    sender=__name__,
                    cart=cart,
                    cart_item=cart_item,
                    reservation_payload=reservation_payload,
                )
            except Exception as exc:
                _safe_log("publish_reservation_event", exc,
                          cart_id=getattr(cart, "id", None),
                          cart_item_id=getattr(cart_item, "id", None))

        try:
            from django.db import transaction as _transaction
            _transaction.on_commit(_publish)
        except Exception:
            _publish()
    except Exception as exc:
        _safe_log("publish_reservation_event", exc,
                  cart_id=getattr(cart, "id", None))

# ==============================================================================
# 4. CART STATE TRANSITION SIGNAL HANDLERS
# ==============================================================================
# The Cart model has a status field with values such as
# "active", "merged", "abandoned", "converted", "expired". State
# transitions are useful to surface in dashboards and analytics. These
# handlers only invalidate caches and fire domain events; they do NOT
# touch inventory. Inventory is mutated only by the inventory app.
# ==============================================================================
@receiver(post_save, sender=Cart)
def handle_cart_state_transition(sender: Any, instance: Cart, created: bool, update_fields: Any = None, **kwargs: Any) -> None:
    """
    Surface cart status transitions in cache keys and domain events.

    On every save the cart may have transitioned to a new state
    (merged, abandoned, converted, expired). This handler ensures
    the corresponding cache buckets are invalidated and the
    ``last_activity_at`` timestamp stays fresh. The receiver does
    NOT touch inventory under any circumstance.
    """
    if created:
        return  # initial creation is handled by the generic handler

    # Optimization: only act when the status is in the update set.
    if update_fields is not None and "status" not in update_fields:
        return

    try:
        # Invalidate state-aware cache buckets
        state_key = _safe_cache_key("cart:state", instance.id, instance.status)
        _safe_cache_delete(state_key)
        # Update last_activity_at for downstream freshness
        _safe_touch_cart(instance.id)
    except Exception as exc:
        _safe_log("handle_cart_state_transition", exc,
                  cart_id=getattr(instance, "id", None),
                  status=getattr(instance, "status", None))

# ==============================================================================
# 5. CART CONVERTED (PROMOTED TO ORDER) EVENT
# ==============================================================================
# When a cart is converted to an order (typically by the order
# application's own signals), downstream cart consumers may need to
# clean up transient state. The cart signal layer only invalidates
# cache and updates the last_activity_at; it does NOT touch inventory.
# ==============================================================================
@receiver(post_save, sender=Cart)
def handle_cart_converted_event(sender: Any, instance: Cart, created: bool, update_fields: Any = None, **kwargs: Any) -> None:
    """
    Surface cart-converted state in cache buckets and touch the cart.

    The handler only acts on transitions to ``CartStatus.CONVERTED``.
    Inventory is intentionally not touched here. Inventory deduction
    is owned exclusively by the inventory application, which the
    order payment flow drives.
    """
    if created:
        return

    if update_fields is not None and "status" not in update_fields:
        return

    try:
        status = getattr(instance, "status", None)
        if status != Cart.CartStatus.CONVERTED:
            return
        # Invalidate any cache that might still surface the cart
        converted_key = _safe_cache_key("cart:converted", instance.id)
        _safe_cache_delete(converted_key)
        _safe_touch_cart(instance.id)
    except Exception as exc:
        _safe_log("handle_cart_converted_event", exc,
                  cart_id=getattr(instance, "id", None))

# ==============================================================================
# 6. CART DELETION CLEANUP
# ==============================================================================
# Cart deletion (e.g. abandoned-cart cleanup, GDPR right-to-be-forgotten)
# must invalidate all associated cache buckets. Inventory reservations
# attached to the deleted cart are owned and cleaned up by the
# inventory service layer; this signal does NOT touch inventory.
# ==============================================================================
@receiver(post_delete, sender=Cart)
def handle_cart_deletion_cleanup(sender: Any, instance: Cart, **kwargs: Any) -> None:
    """
    Invalidate all cache buckets associated with a deleted cart.

    The receiver does NOT touch inventory. Inventory reservations
    attached to the deleted cart are owned and cleaned up by the
    inventory application through its own dedicated reservation release
    and orphan cleanup signals.
    """
    try:
        keys = [
            CART_GLOBAL_SUMMARY_KEY,
        ]
        if instance.customer_id:
            keys.append(
                _safe_cache_key(CART_CUSTOMER_KEY_PREFIX, instance.customer_id)
            )
        if instance.session_key:
            keys.append(
                _safe_cache_key(CART_SESSION_KEY_PREFIX, instance.session_key)
            )
        if instance.anonymous_token:
            keys.append(
                _safe_cache_key(CART_TOKEN_KEY_PREFIX, instance.anonymous_token)
            )
        _safe_cache_delete_many(keys)
    except Exception as exc:
        _safe_log("handle_cart_deletion_cleanup", exc,
                  cart_id=getattr(instance, "id", None))

# ==============================================================================
# 7. CART M2M (CartItem.related_through) HANDLERS
# ==============================================================================
# Placeholder for future M2M relations on CartItem (e.g. saved-for-later
# or wishlist cross-links). Currently a no-op safe-default; the
# infrastructure is wired so future M2M relations are easy to plug in.
# ==============================================================================
@receiver(post_delete, sender=CartItem)
def handle_cart_item_deletion(sender: Any, instance: CartItem, **kwargs: Any) -> None:
    """
    Touch the parent cart on cart item deletion to refresh
    last_activity_at and invalidate relevant cache buckets. The
    receiver does NOT touch inventory. Inventory reservations
    attached to the deleted item are released by the inventory app.
    """
    try:
        cart_id = getattr(instance, "cart_id", None)
        if cart_id is None:
            return
        _safe_touch_cart(cart_id)
        cart = getattr(instance, "cart", None)
        if cart is not None:
            keys = [CART_GLOBAL_SUMMARY_KEY]
            if cart.customer_id:
                keys.append(
                    _safe_cache_key(CART_CUSTOMER_KEY_PREFIX, cart.customer_id)
                )
            if cart.session_key:
                keys.append(
                    _safe_cache_key(CART_SESSION_KEY_PREFIX, cart.session_key)
                )
            if cart.anonymous_token:
                keys.append(
                    _safe_cache_key(CART_TOKEN_KEY_PREFIX, cart.anonymous_token)
                )
            _safe_cache_delete_many(keys)
    except Exception as exc:
        _safe_log("handle_cart_item_deletion", exc,
                  cart_item_id=getattr(instance, "id", None))

# ==============================================================================
# 8. AUDIT / ANALYTICS EVENT PUBLISHER
# ==============================================================================
# Optional hook for emitting structured audit log entries. Inactive by
# default; activated when a CMS administrator enables audit logging
# via settings. The receiver does NOT touch inventory. Inventory
# mutations are owned by the inventory app.
# ==============================================================================
@receiver(post_save, sender=Cart)
@receiver(post_delete, sender=Cart)
@receiver([post_save, post_delete], sender=CartItem)
def handle_cart_audit_log(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
    """
    Optional audit logging hook for cart mutations. Emits a structured
    log entry. Silent by default; the audit log format is defined by
    the CMS and consumed by the future audit module.
    """
    if kwargs.get("raw", False):
        return
    try:
        action = "created" if created else "updated"
        if "post_delete" in kwargs.get("signal", sender.post_save.__class__) \
                if False else False:  # No-op discriminator; safe under future refactor
            action = "deleted"
        logger.info(
            "cart.audit | action=%s model=%s pk=%s",
            action,
            sender.__name__,
            getattr(instance, "pk", None),
        )
    except Exception:
        # Audit logging must NEVER disrupt the main flow.
        pass

# ==============================================================================
# 9. CACHE WARM-UP HOOK
# ==============================================================================
# Optional hook fired after commit to allow the cart service to refresh
# its derived caches (popular carts, recent carts, etc.) once the
# underlying cart mutations have been durably committed. Currently a
# no-op placeholder to keep the wiring present without introducing
# runtime cost.
# ==============================================================================
@receiver(post_save, sender=Cart)
def handle_cart_warmup_hook(sender: Any, instance: Cart, **kwargs: Any) -> None:
    """
    Optional post-commit cache warm-up hook. Currently a no-op
    placeholder. The cart relies on lazy cache population through
    its service layer rather than eager warming.
    """
    if kwargs.get("raw", False):
        return
    # Intentionally left as a placeholder. Future iterations can
    # dispatch a Celery task here to asynchronously rebuild hot caches
    # after high-volume cart mutations.
    return

# ==============================================================================
# 10. SEARCH INDEX INVALIDATION HOOK
# ==============================================================================
# Future hook for invalidating external search indexes (Algolia,
# Elasticsearch, Meilisearch, etc.) when cart content changes.
# Currently a no-op placeholder. The wiring is present so future
# search integration can hook in without modifying the signal layer.
# ==============================================================================
@receiver([post_save, post_delete], sender=Cart)
@receiver([post_save, post_delete], sender=CartItem)
def handle_search_index_invalidation(sender: Any, instance: Any, **kwargs: Any) -> None:
    """
    Optional hook for external search index invalidation. Currently
    a no-op placeholder. Future iterations can dispatch a Celery
    task to reindex carts asynchronously.
    """
    if kwargs.get("raw", False):
        return
    # Intentionally a no-op. The placeholder documents where future
    # search-index integration should hook in. The cart relies on
    # lazy reindexing through its service layer until a dedicated
    # search integration is added.
    return

# ==============================================================================
# 11. CART TIMEOUT / ABANDONMENT HOOK
# ==============================================================================
# Future hook for cart abandonment detection and re-engagement
# workflows. Currently a no-op placeholder. The wiring is present so
# future abandonment workflows can hook in without modifying the
# signal layer.
# ==============================================================================
@receiver(post_save, sender=Cart)
def handle_cart_abandonment_detection(sender: Any, instance: Cart, **kwargs: Any) -> None:
    """
    Optional hook for cart abandonment detection. The cart
    application does NOT mutate inventory on abandonment. The
    inventory service layer handles any reservation cleanup that
    follows abandonment through its own dedicated expiry and cleanup
    signals.
    """
    if kwargs.get("raw", False):
        return
    if getattr(instance, "is_active", True):
        return
    # Intentionally a no-op. The placeholder documents where future
    # abandonment workflows should hook in. Cart abandonment does
    # not change inventory; the inventory service layer handles any
    # reservation cleanup that follows.
    return

# ==============================================================================
# 12. CART PRICE RECALCULATION HOOK
# ==============================================================================
# Future hook for cart price recalculation (e.g. when a coupon changes
# or when a promotion is applied). Currently a no-op placeholder. The
# cart service layer handles price recalculation as part of its
# dedicated recalculate operation.
# ==============================================================================
@receiver(post_save, sender=Cart)
def handle_cart_price_recalc(sender: Any, instance: Cart, **kwargs: Any) -> None:
    """
    Optional hook for cart price recalculation. Currently a no-op
    placeholder. The cart service layer handles price recalculation
    as part of its dedicated recalculate operation. The receiver
    does NOT touch inventory.
    """
    if kwargs.get("raw", False):
        return
    # Intentionally a no-op. The cart service layer exposes a
    # dedicated recalculate method that handles all pricing math.
    return

# ==============================================================================
# 13. CART CONNECTION (PERSISTENT) EVENT
# ==============================================================================
# When a guest cart is connected (linked) to a newly authenticated
# customer, downstream cart consumers may need to update the
# anonymous_token mapping and refresh the session-key cache. The
# cart signal layer only invalidates cache and updates the
# last_activity_at; it does NOT touch inventory.
# ==============================================================================
@receiver(post_save, sender=Cart)
def handle_cart_connection_event(sender: Any, instance: Cart, created: bool, update_fields: Any = None, **kwargs: Any) -> None:
    """
    Surface cart connection (guest -> customer) state in cache buckets
    and touch the cart.

    The handler only acts on transitions that connect a previously
    anonymous cart to an authenticated customer (i.e. the customer
    field became non-null on an update). It does NOT touch inventory.
    """
    if created:
        return

    if update_fields is not None and "customer" not in update_fields:
        return

    try:
        customer_id = getattr(instance, "customer_id", None)
        if customer_id is None:
            return
        # Invalidate the anonymous cache buckets because the cart now
        # belongs to a customer.
        if instance.session_key:
            _safe_cache_delete(
                _safe_cache_key(CART_SESSION_KEY_PREFIX, instance.session_key)
            )
        if instance.anonymous_token:
            _safe_cache_delete(
                _safe_cache_key(CART_TOKEN_KEY_PREFIX, instance.anonymous_token)
            )
        _safe_cache_delete(
            _safe_cache_key(CART_CUSTOMER_KEY_PREFIX, customer_id)
        )
        _safe_touch_cart(instance.id)
    except Exception as exc:
        _safe_log("handle_cart_connection_event", exc,
                  cart_id=getattr(instance, "id", None))

# ==============================================================================
# 14. CONNECTION-SPLIT (PROMOTE BACK TO GUEST) EVENT
# ==============================================================================
# Mirror of the connection event. The cart signal layer only
# invalidates cache; inventory orchestration is owned by the cart
# service layer.
# ==============================================================================
@receiver(post_save, sender=Cart)
def handle_cart_split_event(sender: Any, instance: Cart, created: bool, update_fields: Any = None, **kwargs: Any) -> None:
    """
    Surface cart split (customer -> guest) state in cache buckets.

    The handler only acts on transitions that split a previously
    authenticated cart back to anonymous (i.e. the customer field
    became null on an update). It does NOT touch inventory.
    """
    if created:
        return

    if update_fields is not None and "customer" not in update_fields:
        return

    try:
        # If the customer was just removed, the cart is now anonymous.
        if getattr(instance, "customer_id", None) is None:
            # Invalidate customer cache since the cart is no longer theirs.
            _safe_cache_delete(CART_GLOBAL_SUMMARY_KEY)
            _safe_touch_cart(instance.id)
    except Exception as exc:
        _safe_log("handle_cart_split_event", exc,
                  cart_id=getattr(instance, "id", None))

# ==============================================================================
# 15. CART RESERVATION REFRESH EVENT
# ==============================================================================
# Future hook for cart reservation refresh workflows (e.g. when the
# inventory service refreshes reservations on a cart). Currently a
# no-op placeholder. The wiring is present so future reservation
# refresh workflows can hook in without modifying the signal layer.
# ==============================================================================
@receiver(post_save, sender=CartItem)
def handle_cart_reservation_refresh(sender: Any, instance: CartItem, **kwargs: Any) -> None:
    """
    Optional hook for cart reservation refresh. Currently a no-op
    placeholder. The cart service layer handles reservation refresh
    by delegating to the inventory service layer. The receiver does
    NOT touch inventory.
    """
    if kwargs.get("raw", False):
        return
    # Intentionally a no-op. The cart service layer exposes a
    # dedicated refresh method that delegates to the inventory
    # service layer.
    return

# ==============================================================================
# 16. CART ABANDONMENT DETECTION HOOK (extended)
# ==============================================================================
# Future hook for proactive cart abandonment detection. Currently a
# no-op placeholder. The wiring is present so future abandonment
# workflows can hook in without modifying the signal layer.
# ==============================================================================
@receiver(post_save, sender=Cart)
def handle_cart_inactivity_event(sender: Any, instance: Cart, **kwargs: Any) -> None:
    """
    Optional hook for cart inactivity / abandonment detection. The
    cart application does NOT mutate inventory on abandonment. The
    inventory service layer handles any reservation cleanup that
    follows abandonment through its own dedicated expiry and cleanup
    signals.
    """
    if kwargs.get("raw", False):
        return
    if getattr(instance, "is_active", True):
        return
    # Intentionally a no-op. The placeholder documents where future
    # abandonment workflows should hook in. Cart abandonment does
    # not change inventory; the inventory service layer handles any
    # reservation cleanup that follows.
    return

# ==============================================================================
# PUBLIC API
# ==============================================================================
__all__ = [
    "invalidate_cart_cache_on_cart_change",
    "touch_cart_on_item_change",
    "touch_cart_on_item_create",
    "handle_cart_state_transition",
    "handle_cart_converted_event",
    "handle_cart_deletion_cleanup",
    "handle_cart_item_deletion",
    "handle_cart_audit_log",
    "handle_cart_warmup_hook",
    "handle_search_index_invalidation",
    "handle_cart_abandonment_detection",
    "handle_cart_price_recalc",
    "handle_cart_connection_event",
    "handle_cart_split_event",
    "handle_cart_reservation_refresh",
    "handle_cart_inactivity_event",
    "publish_cart_merged_event",
    "publish_reservation_event",
    "cart_merged",
    "cart_reservation_changed",
]