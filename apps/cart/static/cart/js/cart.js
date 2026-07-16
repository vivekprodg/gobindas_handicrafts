/**
 * Cart Interaction Engine
 * =============================================================================
 * 
 * Powers AJAX quantity updates, remove buttons, save-for-later, mini-cart
 * badge sync, and the live cart summary.
 * 
 * ARCHITECTURE PRINCIPLES
 * -----------------------
 * 1. INVENTORY IS THE SINGLE SOURCE OF TRUTH.
 *    This module NEVER calculates stock, availability, or reservation state.
 *    Every inventory decision is delegated to the Inventory-backed backend
 *    API. The frontend ONLY renders, never decides.
 * 
 * 2. CMS-DRIVEN CONFIGURATION.
 *    Every endpoint, selector, message, threshold, and label is read from
 *    data attributes, settings payloads, or API responses. NO hardcoded
 *    business rules.
 * 
 * 3. PROGRESSIVE ENHANCEMENT.
 *    The engine is a THIN enhancement layer over fully-functional server-
 *    rendered HTML. If JavaScript fails to load, the cart still works.
 * 
 * 4. EVENT-DRIVEN, MEMORY-EFFICIENT.
 *    A single delegated listener handles every cart interaction on the
 *    page. No per-card listeners. No memory leaks. Lazy initialization.
 * 
 * 5. SECURITY-FIRST.
 *    CSRF protection, XSS escaping, URL sanitization, content security
 *    policy compliance. All user input is treated as untrusted.
 * 
 * 6. ACCESSIBILITY-FIRST.
 *    ARIA live regions, keyboard navigation, focus management, screen reader
 *    announcements for all dynamic state changes.
 * 
 * 7. RESILIENT.
 *    AbortController for request cancellation. Debounce/throttle for
 *    high-frequency events. Graceful degradation on network failures.
 *    Idempotent re-initialization.
 * 
 * 8. NO REQUIRED FIELDS.
 *    Every payload field is treated as optional. Missing or null values
 *    NEVER crash the engine.
 * 
 * 9. BACKWARD COMPATIBLE.
 *    The legacy public API (window.CartEngine, data attributes, custom
 *    events) is preserved so existing call-sites continue to function.
 * 
 * @module CartInteractionEngine
 * @version 4.0.0
 */
(function () {
    'use strict';

    // =====================================================================
    // CONFIGURATION
    // =====================================================================
    // All defaults can be overridden at runtime by:
    //   1. window.GOBINDAS_CART_CONFIG (global object defined before this script)
    //   2. <html data-cart-config='{"key":"value"}'> (per-page configuration)
    //   3. data-* attributes on individual elements (per-element overrides)
    //
    // Every value is CMS-driven. NO business rules are hardcoded.
    // =====================================================================

    const DEFAULT_CONFIG = Object.freeze({
        // Network behaviour
        network: {
            timeoutMs: 12000,           // Request timeout
            retryAttempts: 2,            // Retry count for idempotent operations
            retryDelayMs: 800,           // Delay between retries
            credentials: 'same-origin',  // Fetch credentials mode
        },

        // Animation timings (ms)
        animation: {
            rowExit: 320,
            toastIn: 220,
            toastOut: 200,
            badgePulse: 600,
        },

        // Debounce / throttle windows (ms)
        timing: {
            debounceQty: 600,            // Quantity input debounce
            debounceSearch: 250,         // (reserved for future)
            throttleScroll: 100,         // (reserved for future)
        },

        // Selectors (data-attribute-overridable via config.selectors)
        selectors: {
            cartRoot:       '[data-cart-root]',
            cartItem:       '[data-cart-item-id], [data-cart-item], [data-line-item-id]',
            qtyForm:        '.cart-quantity-form, [data-cart-quantity-form]',
            qtyInput:       '.cart-qty-input, input[data-cart-qty-input]',
            qtyStep:        '.qty-step, [data-qty-step]',
            lineRow:        '.cart-line-row, [data-cart-line-row]',
            removeForm:     '.cart-item-action-form, [data-cart-remove-form]',
            saveForm:       '.cart-save-form, [data-cart-save-form]',
            moveForm:       '.cart-move-to-cart-form, [data-cart-move-form]',
            couponForm:     '.cart-coupon-form, [data-cart-coupon-form]',
            couponInput:    '.cart-coupon-input, input[data-cart-coupon-input]',
            clearLink:      '.cart-clear-link, [data-cart-clear]',
            miniCart:       '[data-mini-cart], .mini-cart-wrapper',
            miniCartContent:'[data-mini-cart-content], .mini-cart-content',
            miniCartCount:  '.mini-cart-count, [data-mini-cart-count], [data-cart-count]',
            headerCartCount:'#cart-counter, [data-cart-count], [data-header-cart-count]',
            summaryBody:    '.cart-summary-body, [data-cart-summary]',
            liveRegion:     '[data-cart-live-region]',
            loadingOverlay: '[data-cart-loading]',
            checkoutCta:    '[data-checkout-cta], [data-cart-checkout]',
        },

        // Live region politeness levels
        aria: {
            livePoliteness: 'polite',     // polite | assertive
            announceTimeoutMs: 150,
        },

        // Request deduplication (per-endpoint + per-id)
        dedupe: {
            enabled: true,
            inflightTTL: 8000,
        },

        // Toast notification defaults
        toast: {
            maxConcurrent: 4,
            duration: 4500,
            position: 'top-right',         // top-right | top-left | bottom-right | bottom-left
        },
    });

    // =====================================================================
    // DEFERRED-ERROR HELPERS
    // =====================================================================

    function safeLog(scope, err, extra) {
        try {
            const message = (err && (err.message || String(err))) || 'Unknown error';
            if (typeof console !== 'undefined' && console.error) {
                console.error('[CartEngine]', scope, message, extra || '');
            }
        } catch (_) { /* never let logging crash the engine */ }
    }

    function safeWarn(scope, msg, extra) {
        try {
            if (typeof console !== 'undefined' && console.warn) {
                console.warn('[CartEngine]', scope, msg, extra || '');
            }
        } catch (_) { /* noop */ }
    }

    // =====================================================================
    // UTILITIES
    // =====================================================================

    /**
     * Deep merge utility. Merges `source` over `target`. Arrays and primitives
     * in source replace target values; plain objects are deep-merged.
     */
    function deepMerge(target, source) {
        if (target === null || typeof target !== 'object') return source;
        if (source === null || typeof source !== 'object') return source;
        if (Array.isArray(source)) return cloneValue(source);
        if (Array.isArray(target)) return cloneValue(source);
        const out = cloneValue(target) || {};
        for (const key of Object.keys(source)) {
            const t = out[key];
            const s = source[key];
            if (
                t && typeof t === 'object' && !Array.isArray(t) &&
                s && typeof s === 'object' && !Array.isArray(s)
            ) {
                out[key] = deepMerge(t, s);
            } else if (s === undefined) {
                // preserve target
            } else {
                out[key] = cloneValue(s);
            }
        }
        return out;
    }

    function cloneValue(v) {
        if (v === null || typeof v !== 'object') return v;
        if (Array.isArray(v)) return v.map(cloneValue);
        const out = {};
        for (const k of Object.keys(v)) out[k] = cloneValue(v[k]);
        return out;
    }

    function resolveConfig() {
        let merged = cloneValue(DEFAULT_CONFIG);
        try {
            if (typeof window !== 'undefined' && window.GOBINDAS_CART_CONFIG &&
                typeof window.GOBINDAS_CART_CONFIG === 'object') {
                merged = deepMerge(merged, window.GOBINDAS_CART_CONFIG);
            }
            const root = document.documentElement;
            if (root && root.dataset && root.dataset.cartConfig) {
                const parsed = safeJsonParse(root.dataset.cartConfig, null);
                if (parsed && typeof parsed === 'object') {
                    merged = deepMerge(merged, parsed);
                }
            }
        } catch (e) {
            safeLog('resolveConfig', e);
        }
        // Freeze top-level for safety, but allow sub-object mutation
        // where needed.
        return merged;
    }

    function safeJsonParse(text, fallback) {
        if (text === null || text === undefined || text === '') return fallback;
        try { return JSON.parse(text); } catch (_) { return fallback; }
    }

    /**
     * Safe HTML escaping for text content and attribute insertion.
     * Returns "" for null / undefined. Never throws.
     */
    function escapeHtml(value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;')
            .replace(/`/g, '&#96;');
    }

    function escapeAttr(value) {
        return escapeHtml(value);
    }

    /**
     * Sanitize a URL for safe assignment to href / src. Rejects
     * javascript:, data: (non-image), vbscript: schemes.
     */
    function sanitizeUrl(url) {
        if (typeof url !== 'string' || url.length === 0) return '';
        const t = url.trim();
        if (/^javascript:/i.test(t)) return '';
        if (/^vbscript:/i.test(t)) return '';
        if (/^data:/i.test(t) && !/^data:image\//i.test(t)) return '';
        return t;
    }

    function readCsrfToken() {
        try {
            const meta = document.querySelector('meta[name="csrf-token"]');
            if (meta && meta.content) return meta.content;
            const input = document.querySelector('input[name="csrfmiddlewaretoken"]');
            if (input && input.value) return input.value;
        } catch (_) { /* noop */ }
        return '';
    }

    /**
     * Debounce wrapper. Returns a debounced function with a `.cancel()` method.
     */
    function debounce(fn, wait) {
        let timer = null;
        let lastArgs = null;
        const debounced = function () {
            lastArgs = Array.prototype.slice.call(arguments);
            if (timer !== null) clearTimeout(timer);
            timer = setTimeout(() => {
                timer = null;
                try { fn.apply(null, lastArgs); } catch (e) { safeLog('debounced', e); }
            }, wait);
        };
        debounced.cancel = function () {
            if (timer !== null) { clearTimeout(timer); timer = null; }
        };
        debounced.flush = function () {
            if (timer !== null) { clearTimeout(timer); timer = null; }
            try { fn.apply(null, lastArgs || []); } catch (e) { safeLog('debounced.flush', e); }
        };
        return debounced;
    }

    /**
     * Throttle wrapper. Ensures fn is called at most once per `wait` ms.
     */
    function throttle(fn, wait) {
        let last = 0;
        let timer = null;
        let lastArgs = null;
        return function () {
            const now = Date.now();
            const remaining = wait - (now - last);
            lastArgs = Array.prototype.slice.call(arguments);
            if (remaining <= 0) {
                last = now;
                try { fn.apply(null, lastArgs); } catch (e) { safeLog('throttled', e); }
            } else if (timer === null) {
                timer = setTimeout(() => {
                    last = Date.now();
                    timer = null;
                    try { fn.apply(null, lastArgs); } catch (e) { safeLog('throttled.delayed', e); }
                }, remaining);
            }
        };
    }

    /**
     * Return the closest matching ancestor for `element`. Safe against
     * elements that are not in the DOM.
     */
    function closest(el, selector) {
        if (!el || typeof el.closest !== 'function') return null;
        try { return el.closest(selector); } catch (_) { return null; }
    }

    /**
     * Normalize a value into a positive integer >= 1, with an upper bound.
     */
    function normalizeQuantity(value, min, max) {
        const lo = (typeof min === 'number' && isFinite(min)) ? min : 1;
        const hi = (typeof max === 'number' && isFinite(max)) ? max : 999;
        let n = parseInt(value, 10);
        if (!isFinite(n) || isNaN(n)) n = lo;
        if (n < lo) n = lo;
        if (n > hi) n = hi;
        return n;
    }

    /**
     * Read a configuration override from an element's data attributes.
     * Supports dot-notation paths, e.g. 'animation.rowExit'.
     */
    function readElementConfig(el, path, fallback) {
        if (!el || !el.dataset) return fallback;
        if (!path) return fallback;
        const key = 'cart' + path
            .split('.')
            .map((s, i) => i === 0 ? s : (s.charAt(0).toUpperCase() + s.slice(1)))
            .join('');
        const v = el.dataset[key];
        if (v === undefined || v === '') return fallback;
        // Attempt boolean / number coercion for known types
        if (v === 'true') return true;
        if (v === 'false') return false;
        const asNum = Number(v);
        if (!isNaN(asNum) && v.trim() !== '') return asNum;
        return v;
    }

    // =====================================================================
    // REQUEST LAYER (Fetch with AbortController, retries, dedupe, CSRF)
    // =====================================================================

    /**
     * In-flight request registry for deduplication.
     * Map<key, {controller, timestamp, promise}>
     */
    const inflight = new Map();

    function buildDedupeKey(method, url, body) {
        return method.toUpperCase() + '|' + url + '|' + (body || '');
    }

    /**
     * Send a request with timeout, abort, retry, and dedupe support.
     * 
     * @param {Object} options
     * @param {string} options.url
     * @param {string} [options.method='GET']
     * @param {Object|string|FormData} [options.body]
     * @param {Object} [options.headers]
     * @param {number} [options.timeoutMs]
     * @param {boolean} [options.retry]
     * @param {string} [options.dedupeKey]
     * @param {string} [options.scope]  - scope for error messages
     * @returns {Promise<{ok: boolean, status: number, data: any, raw: any}>}
     */
    function sendRequest(options) {
        const cfg = CONFIG;
        const method = (options.method || 'GET').toUpperCase();
        const url = sanitizeUrl(options.url);
        if (!url) {
            return Promise.resolve({
                ok: false, status: 0, data: null, raw: null,
                error: 'invalid_url',
            });
        }

        const timeoutMs = (typeof options.timeoutMs === 'number' && options.timeoutMs > 0)
            ? options.timeoutMs : cfg.network.timeoutMs;
        const retry = options.retry !== false && method === 'GET';
        const retryAttempts = Math.max(0, cfg.network.retryAttempts | 0);
        const dedupeKey = options.dedupeKey
            || (cfg.dedupe.enabled ? buildDedupeKey(method, url, options.body ? String(options.body) : '') : null);

        if (dedupeKey && inflight.has(dedupeKey)) {
            const entry = inflight.get(dedupeKey);
            if (Date.now() - entry.timestamp < cfg.dedupe.inflightTTL) {
                return entry.promise;
            }
            inflight.delete(dedupeKey);
        }

        const controller = new AbortController();
        const timer = setTimeout(() => {
            try { controller.abort(); } catch (_) { /* noop */ }
        }, timeoutMs);

        const execute = function () {
            const headers = Object.assign({
                'X-Requested-With': 'XMLHttpRequest',
                'Accept': 'application/json, text/html;q=0.9, */*;q=0.5',
            }, options.headers || {});
            const csrf = readCsrfToken();
            if (csrf && method !== 'GET' && !headers['X-CSRFToken']) {
                headers['X-CSRFToken'] = csrf;
            }
            let body = options.body;
            if (body && typeof body === 'object' && !(body instanceof FormData)) {
                if (!headers['Content-Type']) headers['Content-Type'] = 'application/json';
                body = JSON.stringify(body);
            } else if (body instanceof FormData) {
                // browser will set the multipart boundary
                delete headers['Content-Type'];
            }

            return fetch(url, {
                method: method,
                credentials: cfg.network.credentials,
                headers: headers,
                body: method === 'GET' || method === 'HEAD' ? undefined : body,
                signal: controller.signal,
            }).then(function (response) {
                clearTimeout(timer);
                const status = response.status;
                return response.text().then(function (text) {
                    let data = null;
                    if (text) {
                        const ct = response.headers.get('content-type') || '';
                        if (ct.indexOf('application/json') !== -1 || text[0] === '{' || text[0] === '[') {
                            data = safeJsonParse(text, null);
                        } else {
                            data = { __html: text, __isHtml: true };
                        }
                    }
                    return {
                        ok: response.ok,
                        status: status,
                        data: data,
                        raw: text,
                    };
                });
            });
        };

        const promise = (function attempt(remaining) {
            return execute().catch(function (err) {
                clearTimeout(timer);
                const aborted = (err && err.name === 'AbortError');
                if (!aborted && retry && remaining > 0) {
                    return new Promise(function (resolve) {
                        setTimeout(function () {
                            attempt(remaining - 1).then(resolve, resolve);
                        }, cfg.network.retryDelayMs);
                    });
                }
                return {
                    ok: false, status: 0, data: null, raw: null,
                    error: aborted ? 'aborted' : 'network',
                    exception: err,
                };
            });
        })(retryAttempts);

        if (dedupeKey) {
            inflight.set(dedupeKey, {
                controller: controller,
                timestamp: Date.now(),
                promise: promise,
            });
            // Clean up after resolution
            promise.finally(function () {
                if (inflight.get(dedupeKey) &&
                    inflight.get(dedupeKey).promise === promise) {
                    inflight.delete(dedupeKey);
                }
            });
        }

        return promise;
    }

    // =====================================================================
    // RESOLVED CONFIG
    // =====================================================================
    const CONFIG = resolveConfig();

    // =====================================================================
    // TOAST NOTIFICATIONS
    // =====================================================================

    const TOAST_TYPES = Object.freeze({
        success: { icon: '\u2713', bg: '#2E7D32' },
        error:   { icon: '\u2715', bg: '#C62828' },
        warning: { icon: '!',     bg: '#9A7B54' },
        info:    { icon: 'i',     bg: '#2E5984' },
    });

    const toastContainer = (function buildContainer() {
        if (typeof document === 'undefined' || !document.body) return null;
        try {
            const el = document.createElement('div');
            el.className = 'cart-toast-stack';
            el.setAttribute('role', 'region');
            el.setAttribute('aria-label', 'Cart notifications');
            el.setAttribute('data-cart-toast-stack', '');
            // Inline minimal styles so the toasts work even without
            // the cart.css file having loaded yet.
            el.style.position = 'fixed';
            el.style.zIndex = '2147483600';
            el.style.pointerEvents = 'none';
            el.style.display = 'flex';
            el.style.flexDirection = 'column';
            el.style.gap = '8px';
            el.style.maxWidth = '360px';
            const pos = (CONFIG.toast && CONFIG.toast.position) || 'top-right';
            if (pos.indexOf('right') !== -1) { el.style.right = '16px'; el.style.left = 'auto'; }
            else                              { el.style.left = '16px';  el.style.right = 'auto'; }
            if (pos.indexOf('top') !== -1)    { el.style.top = '16px';    el.style.bottom = 'auto'; }
            else                              { el.style.bottom = '16px'; el.style.top = 'auto'; }
            document.body.appendChild(el);
            return el;
        } catch (e) { return null; }
    })();

    const activeToasts = new Set();

    function showToast(message, type) {
        try {
            if (!message) return;
            const kind = (type && TOAST_TYPES[type]) ? type : 'info';
            const cfg = TOAST_TYPES[kind];
            if (!toastContainer) return;
            // Enforce max concurrent
            if (activeToasts.size >= (CONFIG.toast.maxConcurrent || 4)) {
                const first = activeToasts.values().next().value;
                if (first) dismissToast(first);
            }
            const el = document.createElement('div');
            el.className = 'cart-toast cart-toast-' + kind;
            el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
            el.setAttribute('aria-live', kind === 'error' ? 'assertive' : 'polite');
            el.style.pointerEvents = 'auto';
            el.style.padding = '10px 14px';
            el.style.borderRadius = '6px';
            el.style.background = cfg.bg;
            el.style.color = '#FFFFFF';
            el.style.fontSize = '14px';
            el.style.boxShadow = '0 6px 24px rgba(0,0,0,0.18)';
            el.style.display = 'flex';
            el.style.alignItems = 'center';
            el.style.gap = '8px';
            el.style.opacity = '0';
            el.style.transform = 'translateY(-6px)';
            el.style.transition = 'opacity 220ms ease, transform 220ms ease';
            el.innerHTML =
                '<span aria-hidden="true" style="font-weight:700;width:18px;height:18px;border-radius:50%;background:rgba(255,255,255,0.2);display:inline-flex;align-items:center;justify-content:center;font-size:12px;">' +
                escapeHtml(cfg.icon) + '</span>' +
                '<span class="cart-toast-message" style="flex:1;">' + escapeHtml(message) + '</span>';
            toastContainer.appendChild(el);
            activeToasts.add(el);
            // Animate in
            requestAnimationFrame(function () {
                el.style.opacity = '1';
                el.style.transform = 'translateY(0)';
            });
            // Auto-dismiss
            setTimeout(function () { dismissToast(el); }, CONFIG.toast.duration || 4500);
        } catch (e) { safeLog('showToast', e); }
    }

    function dismissToast(el) {
        try {
            if (!el || el.dataset.dismissing === '1') return;
            el.dataset.dismissing = '1';
            el.style.opacity = '0';
            el.style.transform = 'translateY(-6px)';
            setTimeout(function () {
                if (el.parentNode) el.parentNode.removeChild(el);
                activeToasts.delete(el);
            }, 200);
        } catch (e) { safeLog('dismissToast', e); }
    }

    // =====================================================================
    // LIVE REGION (Accessibility)
    // =====================================================================

    const liveRegion = (function buildLiveRegion() {
        if (typeof document === 'undefined' || !document.body) return null;
        try {
            let el = document.querySelector(CONFIG.selectors.liveRegion);
            if (!el) {
                el = document.createElement('div');
                el.setAttribute('data-cart-live-region', '');
                el.setAttribute('role', 'status');
                el.setAttribute('aria-live', CONFIG.aria.livePoliteness);
                el.setAttribute('aria-atomic', 'true');
                Object.assign(el.style, {
                    position: 'absolute',
                    width: '1px', height: '1px',
                    padding: '0', margin: '-1px',
                    overflow: 'hidden',
                    clip: 'rect(0,0,0,0)',
                    whiteSpace: 'nowrap', border: '0',
                });
                document.body.appendChild(el);
            }
            return el;
        } catch (e) { return null; }
    })();

    function announce(message) {
        try {
            if (!liveRegion || !message) return;
            // Toggle to force re-announcement
            liveRegion.textContent = '';
            setTimeout(function () {
                if (liveRegion) liveRegion.textContent = message;
            }, 30);
        } catch (e) { safeLog('announce', e); }
    }

    // =====================================================================
    // LOADING STATE
    // =====================================================================

    function setLoadingState(card, isLoading) {
        try {
            if (!card) return;
            card.dataset.cartLoading = isLoading ? '1' : '0';
            if (isLoading) {
                card.classList.add('is-cart-loading');
                card.setAttribute('aria-busy', 'true');
            } else {
                card.classList.remove('is-cart-loading');
                card.removeAttribute('aria-busy');
            }
            // Optionally disable qty inputs
            const inputs = card.querySelectorAll('input, button');
            for (let i = 0; i < inputs.length; i++) {
                if (isLoading) inputs[i].setAttribute('data-cart-pending', '1');
                else inputs[i].removeAttribute('data-cart-pending');
                if (isLoading) inputs[i].setAttribute('disabled', 'disabled');
                else inputs[i].removeAttribute('disabled');
            }
        } catch (e) { safeLog('setLoadingState', e); }
    }

    function setCartRootLoading(isLoading) {
        try {
            const root = document.querySelector(CONFIG.selectors.cartRoot);
            if (!root) return;
            if (isLoading) {
                root.classList.add('is-cart-root-loading');
                root.setAttribute('aria-busy', 'true');
            } else {
                root.classList.remove('is-cart-root-loading');
                root.removeAttribute('aria-busy');
            }
        } catch (e) { safeLog('setCartRootLoading', e); }
    }

    // =====================================================================
    // INVENTORY CONTEXT NORMALIZATION
    // =====================================================================
    // The frontend NEVER calculates or trusts inventory state. Every
    // inventory-related field displayed on the page is normalized from
    // the API response or from a server-rendered data attribute. The
    // frontend only renders; it never decides.

    function emptyInventoryContext() {
        return {
            exists: false,
            inventory_status: 'unknown',
            is_in_stock: false,
            is_low_stock: false,
            is_out_of_stock: true,
            available_quantity: '0.00',
            reserved_quantity: '0.00',
            free_stock: '0.00',
            stock_message: 'Stock status unavailable',
            warehouse_summary: '',
            warehouse_count: 0,
        };
    }

    function normalizeInventoryContext(raw) {
        const out = emptyInventoryContext();
        if (!raw || typeof raw !== 'object') return out;
        for (const k in raw) {
            if (Object.prototype.hasOwnProperty.call(raw, k)) {
                out[k] = raw[k];
            }
        }
        // Derive canonical booleans ONLY when status is explicit; we do
        // not interpret numbers or other fields as inventory state.
        if (typeof out.inventory_status === 'string') {
            const s = out.inventory_status.toLowerCase();
            out.is_in_stock = (s === 'in_stock');
            out.is_low_stock = (s === 'low_stock');
            out.is_out_of_stock = (s === 'out_of_stock');
        } else {
            out.inventory_status = 'unknown';
        }
        return out;
    }

    /**
     * Read the inventory context for a single cart item. Source priority:
     *   1. data-inventory (JSON, server-rendered)
     *   2. data-inventory-status (status-only, with default empty)
     * The frontend NEVER calculates stock from product or variant fields.
     */
    function readItemInventoryContext(itemEl) {
        if (!itemEl || !itemEl.dataset) return emptyInventoryContext();
        const raw = itemEl.dataset.inventory;
        if (raw) {
            const parsed = safeJsonParse(raw, null);
            if (parsed && typeof parsed === 'object') {
                return normalizeInventoryContext(parsed);
            }
        }
        if (itemEl.dataset.inventoryStatus) {
            return normalizeInventoryContext({
                inventory_status: itemEl.dataset.inventoryStatus,
            });
        }
        return emptyInventoryContext();
    }

    // =====================================================================
    // RENDERING: inventory messages, warnings, reservation
    // =====================================================================
    // The frontend only renders backend-supplied messages. We never
    // generate inventory messages locally.

    /**
     * Update a single cart item's inventory-driven UI tokens from the
     * server response. NEVER mutates server data; only updates DOM
     * tokens so the user can see the authoritative state.
     */
    function applyItemInventoryFromResponse(itemEl, responseData) {
        if (!itemEl) return;
        if (!responseData || typeof responseData !== 'object') return;
        const ctx = normalizeInventoryContext(responseData);
        try {
            itemEl.dataset.inventoryStatus = ctx.inventory_status;
            itemEl.dataset.inStock = ctx.is_in_stock ? 'true' : 'false';
            itemEl.dataset.outOfStock = ctx.is_out_of_stock ? 'true' : 'false';
            itemEl.dataset.lowStock = ctx.is_low_stock ? 'true' : 'false';
        } catch (e) { /* noop */ }

        // Update inline status pill if present
        const statusEl = itemEl.querySelector('[data-line-stock-display]');
        if (statusEl) {
            const cls = 'stock-' + (ctx.inventory_status || 'unknown');
            statusEl.classList.remove(
                'stock-in', 'stock-low', 'stock-out', 'stock-unknown',
                'stock-in_stock', 'stock-low_stock', 'stock-out_of_stock',
            );
            statusEl.classList.add(cls);
            const msg = statusEl.querySelector('[data-line-stock-message]');
            if (msg) msg.textContent = ctx.stock_message || '';
            else statusEl.textContent = ctx.stock_message || '';
        }
        // Update warehouse summary inline
        const whEl = itemEl.querySelector('[data-line-warehouse-summary]');
        if (whEl) {
            whEl.textContent = ctx.warehouse_summary || '';
            whEl.style.display = ctx.warehouse_summary ? '' : 'none';
        }
        // Update reservation block if present
        const resvEl = itemEl.querySelector('[data-line-reservation]');
        if (resvEl && responseData.reservation) {
            applyReservationBlock(resvEl, responseData.reservation);
        }
        // Disable add-to-cart if backend says so
        const addBtn = itemEl.querySelector('[data-cart-add]');
        if (addBtn) {
            const isPurchasable = ctx.is_in_stock || ctx.is_low_stock;
            if (isPurchasable) {
                addBtn.removeAttribute('disabled');
                addBtn.removeAttribute('aria-disabled');
            } else {
                addBtn.setAttribute('disabled', 'disabled');
                addBtn.setAttribute('aria-disabled', 'true');
            }
        }
        // Fire a custom event for external listeners
        try {
            itemEl.dispatchEvent(new CustomEvent('cart:inventory:updated', {
                bubbles: true,
                detail: { item: itemEl, inventory: ctx },
            }));
        } catch (e) { /* noop */ }
    }

    /**
     * Update reservation display from server-provided data. We do NOT
     * calculate expiry, status, or minutes - we only render what the
     * backend says.
     */
    function applyReservationBlock(resvEl, resvData) {
        if (!resvEl || !resvData || typeof resvData !== 'object') return;
        const status = resvData.status || '';
        resvEl.dataset.reservationStatus = status;
        const label = resvEl.querySelector('[data-line-reservation-label]');
        if (label) label.textContent = resvData.label || resvData.status_label || status;
        const token = resvEl.querySelector('[data-line-reservation-token]');
        if (token) {
            const t = resvData.token || '';
            token.textContent = t ? (t.length > 8 ? t.slice(0, 8) + '\u2026' : t) : '';
        }
        const expiry = resvEl.querySelector('[data-line-reservation-expiry]');
        if (expiry) {
            expiry.textContent = resvData.expires_at_human
                || (resvData.expires_at ? String(resvData.expires_at) : '');
        }
    }

    // =====================================================================
    // HEADER COUNT + MINI-CART REFRESH
    // =====================================================================

    function updateHeaderCount(value) {
        try {
            const n = (typeof value === 'number' && isFinite(value)) ? value : 0;
            const els = document.querySelectorAll(CONFIG.selectors.headerCartCount);
            for (let i = 0; i < els.length; i++) {
                els[i].textContent = String(n);
                if (n > 0) {
                    els[i].classList.remove('is-empty', 'is-hidden', 'hidden');
                    els[i].removeAttribute('hidden');
                } else {
                    els[i].classList.add('is-empty');
                }
            }
            // Broadcast so the rest of the storefront can react
            try {
                document.dispatchEvent(new CustomEvent('cart:count:updated', {
                    detail: { count: n },
                }));
            } catch (e) { /* noop */ }
        } catch (e) { safeLog('updateHeaderCount', e); }
    }

    /**
     * Refresh the mini-cart HTML fragment. Uses the user-supplied
     * endpoint, then injects the returned HTML into the mini-cart
     * container. Never touches inventory state directly.
     */
    function refreshMiniCart(options) {
        options = options || {};
        const endpoint = options.endpoint || null;
        if (!endpoint) return Promise.resolve();
        return sendRequest({
            url: endpoint,
            method: 'GET',
            scope: 'mini-cart',
            retry: true,
        }).then(function (response) {
            if (!response.ok) {
                safeWarn('refreshMiniCart', 'Mini cart refresh failed', response);
                return;
            }
            const data = response.data;
            const html = (data && (data.html || data.mini_cart_html || data.content))
                || (response.raw || '');
            const container = document.querySelector(CONFIG.selectors.miniCartContent);
            if (container && html) {
                container.innerHTML = html;
            }
            // Re-apply any inventory tokens to injected children
            try {
                const root = document.querySelector(CONFIG.selectors.miniCart);
                if (root) {
                    root.querySelectorAll('[data-cart-item-id], [data-cart-item]').forEach(function (el) {
                        const ctx = readItemInventoryContext(el);
                        applyItemInventoryFromResponse(el, ctx);
                    });
                }
            } catch (e) { /* noop */ }
            if (data && typeof data.cart_count === 'number') {
                updateHeaderCount(data.cart_count);
            } else if (data && typeof data.count === 'number') {
                updateHeaderCount(data.count);
            }
            try {
                document.dispatchEvent(new CustomEvent('cart:minicart:updated', {
                    detail: { data: data, html: html },
                }));
            } catch (e) { /* noop */ }
        });
    }

    // =====================================================================
    // API ENDPOINT RESOLUTION (CMS-driven, no hardcoding)
    // =====================================================================

    function resolveEndpoint(name, fallback) {
        // Priority: data-cart-endpoint on cart root, then
        // data-cart-endpoint on document root, then CONFIG.
        try {
            const root = document.querySelector(CONFIG.selectors.cartRoot);
            if (root && root.dataset) {
                const key = 'cart' + name.charAt(0).toUpperCase() + name.slice(1) + 'Url';
                if (root.dataset[key]) return root.dataset[key];
            }
            const doc = document.documentElement;
            if (doc && doc.dataset) {
                const key = 'cart' + name.charAt(0).toUpperCase() + name.slice(1) + 'Url';
                if (doc.dataset[key]) return doc.dataset[key];
            }
        } catch (e) { /* noop */ }
        return fallback;
    }

    function resolveItemEndpoint(itemEl, name, fallback) {
        try {
            if (itemEl && itemEl.dataset) {
                const key = 'cart' + name.charAt(0).toUpperCase() + name.slice(1) + 'Url';
                if (itemEl.dataset[key]) return itemEl.dataset[key];
            }
        } catch (e) { /* noop */ }
        return resolveEndpoint(name, fallback);
    }

    // =====================================================================
    // CORE OPERATIONS
    // =====================================================================

    /**
     * Update a single line item quantity. NEVER validates inventory
     * locally; sends the new quantity to the backend which returns the
     * authoritative post-operation payload.
     */
    function updateQuantity(itemEl, itemId, quantity, options) {
        options = options || {};
        if (!itemEl) return Promise.resolve();
        const previousQty = readCurrentQuantity(itemEl);
        setLoadingState(itemEl, true);
        setCartRootLoading(true);
        const endpoint = options.endpoint
            || resolveItemEndpoint(itemEl, 'update',
                (CONFIG.endpoints && CONFIG.endpoints.update) || null);
        if (!endpoint) {
            setLoadingState(itemEl, false);
            setCartRootLoading(false);
            showToast('Update URL is not configured. Please refresh the page.', 'error');
            return Promise.resolve();
        }
        const url = endpoint.replace('{id}', encodeURIComponent(String(itemId)));
        return sendRequest({
            url: url,
            method: 'POST',
            body: { quantity: quantity, item_id: itemId },
            scope: 'update',
            retry: false,
        }).then(function (response) {
            setLoadingState(itemEl, false);
            setCartRootLoading(false);
            handleApiResponse(response, {
                itemEl: itemEl,
                success: function (data) {
                    onQuantityUpdateSuccess(itemEl, data, quantity);
                },
                failure: function (data) {
                    onQuantityUpdateFailure(itemEl, data, previousQty);
                },
                error: function (err) {
                    onQuantityUpdateFailure(itemEl, { message: 'Network error' }, previousQty);
                },
            });
            return response;
        });
    }

    function readCurrentQuantity(itemEl) {
        const input = itemEl.querySelector(CONFIG.selectors.qtyInput);
        if (input) return normalizeQuantity(input.value, 1, 999);
        const ds = itemEl.dataset ? parseInt(itemEl.dataset.currentQuantity || '', 10) : 0;
        return isFinite(ds) && ds > 0 ? ds : 1;
    }

    function onQuantityUpdateSuccess(itemEl, data, requestedQty) {
        try {
            // Apply server-authoritative state
            if (data && data.item) {
                const itemPayload = data.item;
                if (itemEl && itemPayload.line_subtotal) {
                    const subtotalEl = itemEl.querySelector('[data-line-subtotal]');
                    if (subtotalEl) subtotalEl.textContent = itemPayload.line_subtotal;
                }
                if (itemEl && typeof itemPayload.quantity !== 'undefined') {
                    const input = itemEl.querySelector(CONFIG.selectors.qtyInput);
                    if (input) input.value = String(itemPayload.quantity);
                    itemEl.dataset.currentQuantity = String(itemPayload.quantity);
                }
            }
            if (data && data.inventory) {
                applyItemInventoryFromResponse(itemEl, data.inventory);
            } else {
                // Fall back to reading server-rendered context
                applyItemInventoryFromResponse(itemEl, readItemInventoryContext(itemEl));
            }
            // Update summary, header, mini-cart
            updateCartSummary(data);
            if (data && typeof data.cart_count === 'number') {
                updateHeaderCount(data.cart_count);
            }
            // Reservation refresh (server may have returned an updated
            // reservation alongside the quantity change)
            if (data && data.reservation) {
                const resvEl = itemEl.querySelector('[data-line-reservation]');
                if (resvEl) applyReservationBlock(resvEl, data.reservation);
            }
            // Refresh mini-cart asynchronously (fire-and-forget; never
            // blocks the main operation)
            refreshMiniCart({
                endpoint: resolveEndpoint('mini', (CONFIG.endpoints && CONFIG.endpoints.mini) || null),
            });
            // Announce for assistive tech
            const msg = (data && data.message) ||
                ('Quantity updated to ' + requestedQty + '.');
            announce(msg);
            if (data && data.message) showToast(data.message, 'success');
        } catch (e) { safeLog('onQuantityUpdateSuccess', e); }
    }

    function onQuantityUpdateFailure(itemEl, data, previousQty) {
        try {
            // Revert UI to the previous quantity
            if (itemEl) {
                const input = itemEl.querySelector(CONFIG.selectors.qtyInput);
                if (input && previousQty) input.value = String(previousQty);
            }
            const msg = (data && (data.message || data.error)) || 'Could not update quantity.';
            showToast(msg, 'error');
            announce(msg);
        } catch (e) { safeLog('onQuantityUpdateFailure', e); }
    }

    /**
     * Update the cart summary from server response. We render the
     * authoritative values returned by the backend. We do NOT compute
     * totals locally.
     */
    function updateCartSummary(data) {
        if (!data || typeof data !== 'object') return;
        try {
            const subtotalEl = document.querySelector('[data-cart-subtotal]');
            if (subtotalEl && (typeof data.subtotal !== 'undefined' || typeof data.subtotal_display !== 'undefined')) {
                subtotalEl.textContent = data.subtotal_display || data.subtotal || '';
            }
            const taxEl = document.querySelector('[data-cart-tax]');
            if (taxEl && (typeof data.tax !== 'undefined' || typeof data.tax_display !== 'undefined')) {
                taxEl.textContent = data.tax_display || data.tax || '';
            }
            const shipEl = document.querySelector('[data-cart-shipping]');
            if (shipEl && (typeof data.shipping !== 'undefined' || typeof data.shipping_display !== 'undefined')) {
                shipEl.textContent = data.shipping_display || data.shipping || '';
            }
            const discountEl = document.querySelector('[data-cart-discount]');
            if (discountEl && (typeof data.discount !== 'undefined' || typeof data.discount_display !== 'undefined')) {
                discountEl.textContent = data.discount_display || data.discount || '';
            }
            const grandEl = document.querySelector('[data-cart-grand-total]');
            if (grandEl && (typeof data.grand_total !== 'undefined' || typeof data.grand_total_display !== 'undefined')) {
                grandEl.textContent = data.grand_total_display || data.grand_total || '';
            }
            const countEl = document.querySelector('[data-cart-total-items]');
            if (countEl && (typeof data.total_items !== 'undefined' || typeof data.item_count !== 'undefined')) {
                countEl.textContent = String(data.total_items || data.item_count || 0);
            }
            const couponEl = document.querySelector('[data-cart-coupon-code]');
            if (couponEl && typeof data.coupon_code !== 'undefined') {
                couponEl.textContent = data.coupon_code || '';
            }
            // Stock messages (backend-supplied)
            const invMsg = document.querySelector('[data-cart-inventory-message]');
            if (invMsg && data.inventory && data.inventory.stock_message) {
                invMsg.textContent = data.inventory.stock_message;
            }
            const invStatus = document.querySelector('[data-cart-inventory-status]');
            if (invStatus && data.inventory && data.inventory.inventory_status) {
                invStatus.dataset.inventoryStatus = data.inventory.inventory_status;
                invStatus.className = 'cart-inventory-status status-' + data.inventory.inventory_status;
            }
            const warehouseEl = document.querySelector('[data-cart-warehouse-summary]');
            if (warehouseEl && data.inventory && data.inventory.warehouse_summary) {
                warehouseEl.textContent = data.inventory.warehouse_summary;
            }
            // Checkout CTA enable/disable
            const cta = document.querySelector(CONFIG.selectors.checkoutCta);
            if (cta && data && data.inventory) {
                if (data.inventory.is_out_of_stock) {
                    cta.setAttribute('disabled', 'disabled');
                    cta.setAttribute('aria-disabled', 'true');
                    if (data.checkout_blocked_message) cta.textContent = data.checkout_blocked_message;
                } else {
                    cta.removeAttribute('disabled');
                    cta.removeAttribute('aria-disabled');
                    if (data.checkout_cta_label) cta.textContent = data.checkout_cta_label;
                }
            }
            // Inventory block alert (issues)
            const alertEl = document.querySelector('[data-cart-inventory-alert]');
            if (alertEl) {
                if (data.issues && data.issues.length) {
                    alertEl.style.display = '';
                    const list = alertEl.querySelector('[data-cart-issues-list]');
                    if (list) {
                        list.innerHTML = data.issues.map(function (iss) {
                            return '<li class="cart-issue" data-cart-issue-code="' +
                                escapeAttr(iss.code || 'unknown') + '">' +
                                escapeHtml(iss.message || iss.code || 'Item requires review.') +
                                '</li>';
                        }).join('');
                    }
                } else {
                    alertEl.style.display = 'none';
                }
            }
            // Broadcast
            try {
                document.dispatchEvent(new CustomEvent('cart:summary:updated', {
                    detail: { data: data },
                }));
            } catch (e) { /* noop */ }
        } catch (e) { safeLog('updateCartSummary', e); }
    }

    /**
     * Handle a standardized API response. Translates the response into
     * one or more UI mutations, fired through callbacks for testability.
     */
    function handleApiResponse(response, callbacks) {
        callbacks = callbacks || {};
        const data = response && response.data;
        // Success
        if (response && response.ok && data && data.status !== 'error') {
            if (typeof callbacks.success === 'function') {
                try { callbacks.success(data); } catch (e) { safeLog('cb.success', e); }
            }
            return;
        }
        // API-level error
        if (typeof callbacks.failure === 'function') {
            try { callbacks.failure(data); } catch (e) { safeLog('cb.failure', e); }
            return;
        }
        // Network error
        if (typeof callbacks.error === 'function') {
            try { callbacks.error(response && response.error ? response : { error: 'unknown' }); } catch (e) { safeLog('cb.error', e); }
        }
    }

    /**
     * Remove a single line item. Delegates entirely to the backend.
     * The frontend NEVER validates inventory or pricing locally.
     */
    function removeItem(itemEl, itemId, options) {
        options = options || {};
        if (!itemEl) return Promise.resolve();
        setLoadingState(itemEl, true);
        setCartRootLoading(true);
        const endpoint = options.endpoint
            || resolveItemEndpoint(itemEl, 'remove',
                (CONFIG.endpoints && CONFIG.endpoints.remove) || null);
        if (!endpoint) {
            setLoadingState(itemEl, false);
            setCartRootLoading(false);
            showToast('Remove URL is not configured. Please refresh the page.', 'error');
            return Promise.resolve();
        }
        const url = endpoint.replace('{id}', encodeURIComponent(String(itemId)));
        return sendRequest({
            url: url,
            method: 'POST',
            body: { item_id: itemId },
            scope: 'remove',
            retry: false,
        }).then(function (response) {
            setLoadingState(itemEl, false);
            setCartRootLoading(false);
            handleApiResponse(response, {
                success: function (data) {
                    onRemoveSuccess(itemEl, data);
                },
                failure: function (data) {
                    showToast((data && (data.message || data.error)) || 'Could not remove item.', 'error');
                },
                error: function () {
                    showToast('Could not remove item. Please try again.', 'error');
                },
            });
        });
    }

    function onRemoveSuccess(itemEl, data) {
        try {
            if (!itemEl) return;
            // Animate out, then remove from DOM
            const animationMs = CONFIG.animation.rowExit;
            try {
                itemEl.style.transition = 'opacity ' + animationMs + 'ms ease, transform ' + animationMs + 'ms ease';
                itemEl.style.opacity = '0';
                itemEl.style.transform = 'translateX(20px)';
            } catch (e) { /* noop */ }
            setTimeout(function () {
                if (itemEl.parentNode) itemEl.parentNode.removeChild(itemEl);
                // If the cart is now empty, reload to surface empty state
                const root = document.querySelector(CONFIG.selectors.cartRoot);
                if (root) {
                    const remaining = root.querySelectorAll('[data-cart-item-id], [data-cart-item]').length;
                    if (remaining === 0) {
                        try { window.location.reload(); } catch (e) { /* noop */ }
                    }
                }
            }, animationMs);
            // Update summary
            updateCartSummary(data);
            if (data && typeof data.cart_count === 'number') {
                updateHeaderCount(data.cart_count);
            }
            // Refresh mini-cart
            refreshMiniCart({
                endpoint: resolveEndpoint('mini', (CONFIG.endpoints && CONFIG.endpoints.mini) || null),
            });
            const msg = (data && data.message) || 'Item removed from your bag.';
            announce(msg);
            if (data && data.message) showToast(data.message, 'success');
            else showToast(msg, 'success');
        } catch (e) { safeLog('onRemoveSuccess', e); }
    }

    /**
     * Save an item for later. Backend determines whether the
     * associated reservation is released.
     */
    function saveForLater(itemEl, itemId, options) {
        options = options || {};
        if (!itemEl) return Promise.resolve();
        setLoadingState(itemEl, true);
        setCartRootLoading(true);
        const endpoint = options.endpoint
            || resolveItemEndpoint(itemEl, 'save',
                (CONFIG.endpoints && CONFIG.endpoints.save) || null);
        if (!endpoint) {
            setLoadingState(itemEl, false);
            setCartRootLoading(false);
            showToast('Save URL is not configured. Please refresh the page.', 'error');
            return Promise.resolve();
        }
        const url = endpoint.replace('{id}', encodeURIComponent(String(itemId)));
        return sendRequest({
            url: url,
            method: 'POST',
            body: { item_id: itemId },
            scope: 'save',
            retry: false,
        }).then(function (response) {
            setLoadingState(itemEl, false);
            setCartRootLoading(false);
            handleApiResponse(response, {
                success: function (data) {
                    onSaveForLaterSuccess(itemEl, data);
                },
                failure: function (data) {
                    showToast((data && (data.message || data.error)) || 'Could not save item.', 'error');
                },
                error: function () {
                    showToast('Could not save item. Please try again.', 'error');
                },
            });
        });
    }

    function onSaveForLaterSuccess(itemEl, data) {
        try {
            if (!itemEl) return;
            const animationMs = CONFIG.animation.rowExit;
            try {
                itemEl.style.transition = 'opacity ' + animationMs + 'ms ease, transform ' + animationMs + 'ms ease';
                itemEl.style.opacity = '0';
                itemEl.style.transform = 'translateX(20px)';
            } catch (e) { /* noop */ }
            setTimeout(function () {
                if (itemEl.parentNode) itemEl.parentNode.removeChild(itemEl);
                const root = document.querySelector(CONFIG.selectors.cartRoot);
                if (root) {
                    const remaining = root.querySelectorAll('[data-cart-item-id], [data-cart-item]').length;
                    if (remaining === 0) {
                        try { window.location.reload(); } catch (e) { /* noop */ }
                    }
                }
            }, animationMs);
            updateCartSummary(data);
            if (data && typeof data.cart_count === 'number') {
                updateHeaderCount(data.cart_count);
            }
            refreshMiniCart({
                endpoint: resolveEndpoint('mini', (CONFIG.endpoints && CONFIG.endpoints.mini) || null),
            });
            const msg = (data && data.message) || 'Item saved for later.';
            announce(msg);
            if (data && data.message) showToast(data.message, 'success');
            else showToast(msg, 'success');
        } catch (e) { safeLog('onSaveForLaterSuccess', e); }
    }

    /**
     * Move a saved-for-later item back to the active cart. Backend
     * handles the reservation recreation.
     */
    function moveToCart(itemEl, itemId, options) {
        options = options || {};
        if (!itemEl) return Promise.resolve();
        setLoadingState(itemEl, true);
        setCartRootLoading(true);
        const endpoint = options.endpoint
            || resolveItemEndpoint(itemEl, 'moveToCart',
                (CONFIG.endpoints && CONFIG.endpoints.moveToCart) || null);
        if (!endpoint) {
            setLoadingState(itemEl, false);
            setCartRootLoading(false);
            showToast('Move URL is not configured. Please refresh the page.', 'error');
            return Promise.resolve();
        }
        const url = endpoint.replace('{id}', encodeURIComponent(String(itemId)));
        return sendRequest({
            url: url,
            method: 'POST',
            body: { item_id: itemId },
            scope: 'move-to-cart',
            retry: false,
        }).then(function (response) {
            setLoadingState(itemEl, false);
            setCartRootLoading(false);
            handleApiResponse(response, {
                success: function (data) {
                    onMoveToCartSuccess(itemEl, data);
                },
                failure: function (data) {
                    showToast((data && (data.message || data.error)) || 'Could not move item.', 'error');
                },
                error: function () {
                    showToast('Could not move item. Please try again.', 'error');
                },
            });
        });
    }

    function onMoveToCartSuccess(itemEl, data) {
        try {
            if (!itemEl) return;
            const animationMs = CONFIG.animation.rowExit;
            try {
                itemEl.style.transition = 'opacity ' + animationMs + 'ms ease, transform ' + animationMs + 'ms ease';
                itemEl.style.opacity = '0';
                itemEl.style.transform = 'translateX(20px)';
            } catch (e) { /* noop */ }
            setTimeout(function () {
                if (itemEl.parentNode) itemEl.parentNode.removeChild(itemEl);
            }, animationMs);
            updateCartSummary(data);
            if (data && typeof data.cart_count === 'number') {
                updateHeaderCount(data.cart_count);
            }
            refreshMiniCart({
                endpoint: resolveEndpoint('mini', (CONFIG.endpoints && CONFIG.endpoints.mini) || null),
            });
            const msg = (data && data.message) || 'Item moved back to your bag.';
            announce(msg);
            if (data && data.message) showToast(data.message, 'success');
            else showToast(msg, 'success');
        } catch (e) { safeLog('onMoveToCartSuccess', e); }
    }

    /**
     * Clear the entire cart. The frontend never asks for
     * confirmation of destructive actions; the user has already
     * confirmed via the UI's native dialog.
     */
    function clearCart(options) {
        options = options || {};
        setCartRootLoading(true);
        const endpoint = options.endpoint
            || resolveEndpoint('clear',
                (CONFIG.endpoints && CONFIG.endpoints.clear) || null);
        if (!endpoint) {
            setCartRootLoading(false);
            showToast('Clear URL is not configured. Please refresh the page.', 'error');
            return Promise.resolve();
        }
        return sendRequest({
            url: endpoint,
            method: 'POST',
            body: {},
            scope: 'clear',
            retry: false,
        }).then(function (response) {
            setCartRootLoading(false);
            handleApiResponse(response, {
                success: function (data) {
                    if (data && typeof data.cart_count === 'number') {
                        updateHeaderCount(data.cart_count);
                    }
                    showToast((data && data.message) || 'Cart cleared.', 'success');
                    announce((data && data.message) || 'Cart cleared.');
                    setTimeout(function () { try { window.location.reload(); } catch (e) {} }, 200);
                },
                failure: function (data) {
                    showToast((data && (data.message || data.error)) || 'Could not clear cart.', 'error');
                },
                error: function () {
                    showToast('Could not clear cart. Please try again.', 'error');
                },
            });
        });
    }

    /**
     * Apply a coupon code. Backend determines validity and discount.
     */
    function applyCoupon(code, options) {
        options = options || {};
        const form = document.querySelector(CONFIG.selectors.couponForm);
        const input = form ? form.querySelector(CONFIG.selectors.couponInput) : null;
        const codeValue = (code !== undefined && code !== null)
            ? String(code).trim() : (input ? String(input.value || '').trim() : '');
        if (!codeValue) {
            showToast('Please enter a coupon code.', 'error');
            return Promise.resolve();
        }
        setCartRootLoading(true);
        const endpoint = options.endpoint
            || resolveEndpoint('applyCoupon',
                (CONFIG.endpoints && CONFIG.endpoints.applyCoupon) || null);
        if (!endpoint) {
            setCartRootLoading(false);
            showToast('Coupon URL is not configured. Please refresh the page.', 'error');
            return Promise.resolve();
        }
        return sendRequest({
            url: endpoint,
            method: 'POST',
            body: { coupon_code: codeValue, code: codeValue },
            scope: 'coupon-apply',
            retry: false,
        }).then(function (response) {
            setCartRootLoading(false);
            handleApiResponse(response, {
                success: function (data) {
                    updateCartSummary(data);
                    refreshMiniCart({
                        endpoint: resolveEndpoint('mini', (CONFIG.endpoints && CONFIG.endpoints.mini) || null),
                    });
                    showToast((data && data.message) || 'Coupon applied.', 'success');
                    announce((data && data.message) || 'Coupon applied.');
                    // Reload to surface any coupon chip / discount in summary
                    setTimeout(function () { try { window.location.reload(); } catch (e) {} }, 600);
                },
                failure: function (data) {
                    showToast((data && (data.message || data.error)) || 'Invalid coupon code.', 'error');
                },
                error: function () {
                    showToast('Could not apply coupon. Please try again.', 'error');
                },
            });
        });
    }

    /**
     * Remove the currently applied coupon. Backend handles state.
     */
    function removeCoupon(options) {
        options = options || {};
        setCartRootLoading(true);
        const endpoint = options.endpoint
            || resolveEndpoint('removeCoupon',
                (CONFIG.endpoints && CONFIG.endpoints.removeCoupon) || null);
        if (!endpoint) {
            setCartRootLoading(false);
            showToast('Coupon removal URL is not configured. Please refresh the page.', 'error');
            return Promise.resolve();
        }
        return sendRequest({
            url: endpoint,
            method: 'POST' in {} ? 'POST' : 'POST',
            body: {},
            scope: 'coupon-remove',
            retry: false,
        }).then(function (response) {
            setCartRootLoading(false);
            handleApiResponse(response, {
                success: function (data) {
                    updateCartSummary(data);
                    refreshMiniCart({
                        endpoint: resolveEndpoint('mini', (CONFIG.endpoints && CONFIG.endpoints.mini) || null),
                    });
                    showToast((data && data.message) || 'Coupon removed.', 'success');
                    announce((data && data.message) || 'Coupon removed.');
                    setTimeout(function () { try { window.location.reload(); } catch (e) {} }, 500);
                },
                failure: function (data) {
                    showToast((data && (data.message || data.error)) || 'Could not remove coupon.', 'error');
                },
                error: function () {
                    showToast('Could not remove coupon. Please try again.', 'error');
                },
            });
        });
    }

    // =====================================================================
    // DEBOUNCED QTY HANDLERS
    // =====================================================================

    /**
     * Build a debounced per-input quantity change handler. We debounce
     * because the backend decides inventory validity and we want to
     * avoid hammering it while the user is typing.
     */
    const debouncedQtyByInput = new WeakMap();

    function getDebouncedQtyHandler(input) {
        let fn = debouncedQtyByInput.get(input);
        if (!fn) {
            fn = debounce(function () {
                handleDirectQuantityInput(input);
            }, CONFIG.timing.debounceQty);
            debouncedQtyByInput.set(input, fn);
        }
        return fn;
    }

    function handleDirectQuantityInput(input) {
        try {
            if (!input) return;
            const itemEl = closest(input, CONFIG.selectors.cartItem);
            if (!itemEl) return;
            const itemId = itemEl.dataset.cartItemId || itemEl.dataset.lineItemId || itemEl.dataset.cartItem;
            if (!itemId) return;
            const newQty = normalizeQuantity(input.value, 1, 999);
            if (newQty === normalizeQuantity(input.defaultValue || input.value, 1, 999)) {
                return; // no change
            }
            updateQuantity(itemEl, itemId, newQty, {});
        } catch (e) { safeLog('handleDirectQuantityInput', e); }
    }

    // =====================================================================
    // EVENT DELEGATION
    // =====================================================================
    // A single delegated listener handles every cart interaction. This
    // keeps memory usage flat regardless of how many items are on the
    // page. No per-card listeners. No memory leaks.

    function initializeEventDelegation() {
        if (typeof document === 'undefined') return;

        // ---- Click delegation (capture phase to beat legacy handlers) ----
        document.addEventListener('click', function (event) {
            try {
                const target = event.target;
                if (!target) return;

                // Quantity step buttons (+/-)
                const stepBtn = closest(target, CONFIG.selectors.qtyStep);
                if (stepBtn) {
                    event.preventDefault();
                    event.stopPropagation();
                    handleStepClick(stepBtn, event);
                    return;
                }

                // Remove form
                const removeForm = closest(target, CONFIG.selectors.removeForm);
                if (removeForm) {
                    event.preventDefault();
                    event.stopPropagation();
                    handleRemoveSubmit(removeForm, event);
                    return;
                }

                // Save for later
                const saveForm = closest(target, CONFIG.selectors.saveForm);
                if (saveForm) {
                    event.preventDefault();
                    event.stopPropagation();
                    handleSaveSubmit(saveForm, event);
                    return;
                }

                // Move to cart
                const moveForm = closest(target, CONFIG.selectors.moveForm);
                if (moveForm) {
                    event.preventDefault();
                    event.stopPropagation();
                    handleMoveSubmit(moveForm, event);
                    return;
                }

                // Clear cart link
                const clearLink = closest(target, CONFIG.selectors.clearLink);
                if (clearLink) {
                    event.preventDefault();
                    event.stopPropagation();
                    if (typeof window.confirm === 'function') {
                        if (!window.confirm('Clear all items from your cart?')) return;
                    }
                    clearCart({});
                    return;
                }

                // Remove coupon link
                const removeCouponLink = closest(target, '[data-cart-remove-coupon]');
                if (removeCouponLink) {
                    event.preventDefault();
                    event.stopPropagation();
                    removeCoupon({});
                    return;
                }
            } catch (e) { safeLog('click-delegation', e); }
        }, true);

        // ---- Change delegation (select / radio) ----
        document.addEventListener('change', function (event) {
            try {
                const target = event.target;
                if (!target || typeof target.matches !== 'function') return;

                // Variant change inside cart line
                if (target.matches('[data-line-variant-select]')) {
                    const itemEl = closest(target, CONFIG.selectors.cartItem);
                    if (!itemEl) return;
                    const itemId = itemEl.dataset.cartItemId || itemEl.dataset.lineItemId;
                    if (!itemId) return;
                    const variantId = target.value || '';
                    setLoadingState(itemEl, true);
                    const endpoint = resolveItemEndpoint(itemEl, 'variant',
                        (CONFIG.endpoints && CONFIG.endpoints.variant) || null);
                    if (!endpoint) {
                        setLoadingState(itemEl, false);
                        showToast('Variant URL is not configured. Please refresh the page.', 'error');
                        return;
                    }
                    const url = endpoint.replace('{id}', encodeURIComponent(String(itemId)));
                    sendRequest({
                        url: url,
                        method: 'POST',
                        body: { item_id: itemId, variant_id: variantId },
                        scope: 'variant',
                        retry: false,
                    }).then(function (response) {
                        setLoadingState(itemEl, false);
                        handleApiResponse(response, {
                            success: function (data) {
                                if (data && data.item) {
                                    const itemPayload = data.item;
                                    if (itemPayload.line_subtotal) {
                                        const sub = itemEl.querySelector('[data-line-subtotal]');
                                        if (sub) sub.textContent = itemPayload.line_subtotal;
                                    }
                                    if (itemPayload.quantity) {
                                        const input = itemEl.querySelector(CONFIG.selectors.qtyInput);
                                        if (input) input.value = String(itemPayload.quantity);
                                    }
                                }
                                if (data && data.inventory) {
                                    applyItemInventoryFromResponse(itemEl, data.inventory);
                                } else {
                                    applyItemInventoryFromResponse(itemEl, readItemInventoryContext(itemEl));
                                }
                                updateCartSummary(data);
                                if (data && typeof data.cart_count === 'number') {
                                    updateHeaderCount(data.cart_count);
                                }
                                refreshMiniCart({
                                    endpoint: resolveEndpoint('mini',
                                        (CONFIG.endpoints && CONFIG.endpoints.mini) || null),
                                });
                                showToast((data && data.message) || 'Variant updated.', 'success');
                            },
                            failure: function (data) {
                                showToast((data && (data.message || data.error)) || 'Could not update variant.', 'error');
                            },
                            error: function () {
                                showToast('Could not update variant. Please try again.', 'error');
                            },
                        });
                    });
                    return;
                }
            } catch (e) { safeLog('change-delegation', e); }
        }, true);

        // ---- Input delegation (debounced quantity typing) ----
        document.addEventListener('input', function (event) {
            try {
                const target = event.target;
                if (!target || typeof target.matches !== 'function') return;
                if (target.matches(CONFIG.selectors.qtyInput)) {
                    const handler = getDebouncedQtyHandler(target);
                    handler();
                }
            } catch (e) { safeLog('input-delegation', e); }
        }, true);

        // ---- Form submit delegation (coupon, fallback) ----
        document.addEventListener('submit', function (event) {
            try {
                const form = event.target;
                if (!form || typeof form.matches !== 'function') return;

                // Quantity form (fallback for non-JS submit)
                if (form.matches(CONFIG.selectors.qtyForm)) {
                    // We let the debounced input handler drive updates;
                    // prevent full-page reload if debounce is in flight.
                    const input = form.querySelector(CONFIG.selectors.qtyInput);
                    if (input) {
                        event.preventDefault();
                        const handler = getDebouncedQtyHandler(input);
                        handler.flush();
                    }
                    return;
                }

                // Coupon form
                if (form.matches(CONFIG.selectors.couponForm)) {
                    event.preventDefault();
                    const input = form.querySelector(CONFIG.selectors.couponInput);
                    const code = input ? input.value : '';
                    applyCoupon(code, {});
                    return;
                }
            } catch (e) { safeLog('submit-delegation', e); }
        }, true);
    }

    function handleStepClick(stepBtn, event) {
        try {
            const form = closest(stepBtn, CONFIG.selectors.qtyForm) || stepBtn.form;
            if (!form) return;
            const input = form.querySelector(CONFIG.selectors.qtyInput);
            if (!input) return;
            const direction = stepBtn.dataset.qtyAction
                || (stepBtn.textContent || '').trim()
                || 'increment';
            const currentQty = normalizeQuantity(input.value, 1, 999);
            let nextQty = currentQty;
            if (direction === 'increment' || direction === '+' || direction === 'inc') {
                nextQty = currentQty + 1;
            } else if (direction === 'decrement' || direction === '-' || direction === 'dec') {
                nextQty = Math.max(1, currentQty - 1);
            }
            // Clamp to backend limits
            const max = readElementConfig(input, 'maxQuantity', 999);
            const min = readElementConfig(input, 'minQuantity', 1);
            nextQty = Math.max(min, Math.min(max, nextQty));
            input.value = String(nextQty);
            const itemEl = closest(input, CONFIG.selectors.cartItem);
            const itemId = itemEl ? (itemEl.dataset.cartItemId || itemEl.dataset.lineItemId || itemEl.dataset.cartItem) : null;
            if (itemEl && itemId) {
                updateQuantity(itemEl, itemId, nextQty, {});
            }
        } catch (e) { safeLog('handleStepClick', e); }
    }

    function handleRemoveSubmit(form, event) {
        try {
            const itemEl = closest(form, CONFIG.selectors.cartItem);
            if (!itemEl) return;
            const itemId = itemEl.dataset.cartItemId || itemEl.dataset.lineItemId || itemEl.dataset.cartItem;
            if (!itemId) return;
            removeItem(itemEl, itemId, {});
        } catch (e) { safeLog('handleRemoveSubmit', e); }
    }

    function handleSaveSubmit(form, event) {
        try {
            const itemEl = closest(form, CONFIG.selectors.cartItem);
            if (!itemEl) return;
            const itemId = itemEl.dataset.cartItemId || itemEl.dataset.lineItemId || itemEl.dataset.cartItem;
            if (!itemId) return;
            saveForLater(itemEl, itemId, {});
        } catch (e) { safeLog('handleSaveSubmit', e); }
    }

    function handleMoveSubmit(form, event) {
        try {
            const itemEl = closest(form, CONFIG.selectors.cartItem);
            if (!itemEl) return;
            const itemId = itemEl.dataset.cartItemId || itemEl.dataset.lineItemId || itemEl.dataset.cartItem;
            if (!itemId) return;
            moveToCart(itemEl, itemId, {});
        } catch (e) { safeLog('handleMoveSubmit', e); }
    }

    // =====================================================================
    // MINI-CART INITIALIZATION
    // =====================================================================

    function initializeMiniCart() {
        const mini = document.querySelector(CONFIG.selectors.miniCart);
        if (!mini) return;
        const endpoint = resolveEndpoint('mini', (CONFIG.endpoints && CONFIG.endpoints.mini) || null);
        if (!endpoint) return;

        let refreshTimer = null;
        const throttledRefresh = throttle(function () {
            refreshMiniCart({ endpoint: endpoint });
        }, 200);

        // Refresh on hover
        mini.addEventListener('mouseenter', throttledRefresh, { passive: true });
        // Refresh when window regains focus
        window.addEventListener('focus', throttledRefresh, { passive: true });
        // Refresh on visibility change (e.g. user returns to tab)
        if (typeof document.visibilityState !== 'undefined') {
            document.addEventListener('visibilitychange', function () {
                if (document.visibilityState === 'visible') {
                    throttledRefresh();
                }
            });
        }
    }

    // =====================================================================
    // REFRESH MINI-CART (Public API)
    // =====================================================================

    function refreshFromResponse(data) {
        if (!data || typeof data !== 'object') return;
        try {
            if (typeof data.cart_count === 'number') {
                updateHeaderCount(data.cart_count);
            }
            updateCartSummary(data);
        } catch (e) { safeLog('refreshFromResponse', e); }
    }

    // =====================================================================
    // BOOTSTRAP / DYNAMIC CONTENT
    // =====================================================================

    function bootstrap() {
        try {
            initializeEventDelegation();
            initializeMiniCart();
            // Apply server-rendered inventory tokens to every item on
            // initial load. The frontend only renders - it never
            // calculates - so this is a safe, idempotent pass.
            try {
                const items = document.querySelectorAll(CONFIG.selectors.cartItem);
                for (let i = 0; i < items.length; i++) {
                    const ctx = readItemInventoryContext(items[i]);
                    applyItemInventoryFromResponse(items[i], ctx);
                }
            } catch (e) { /* noop */ }
        } catch (e) { safeLog('bootstrap', e); }
    }

    function observeDynamicContent() {
        if (typeof MutationObserver === 'undefined') return;
        try {
            const observer = new MutationObserver(function (mutations) {
                for (let i = 0; i < mutations.length; i++) {
                    const m = mutations[i];
                    for (let j = 0; j < m.addedNodes.length; j++) {
                        const node = m.addedNodes[j];
                        if (!node || node.nodeType !== 1) continue;
                        // Apply inventory context to any newly-added item
                        try {
                            if (node.matches && node.matches(CONFIG.selectors.cartItem)) {
                                const ctx = readItemInventoryContext(node);
                                applyItemInventoryFromResponse(node, ctx);
                            }
                            if (node.querySelectorAll) {
                                const items = node.querySelectorAll(CONFIG.selectors.cartItem);
                                for (let k = 0; k < items.length; k++) {
                                    const ctx = readItemInventoryContext(items[k]);
                                    applyItemInventoryFromResponse(items[k], ctx);
                                }
                            }
                        } catch (e) { /* noop */ }
                    }
                }
            });
            if (document.body) {
                observer.observe(document.body, { childList: true, subtree: true });
            } else {
                document.addEventListener('DOMContentLoaded', function () {
                    if (document.body) {
                        observer.observe(document.body, { childList: true, subtree: true });
                    }
                }, { once: true });
            }
        } catch (e) { safeLog('observeDynamicContent', e); }
    }

    // =====================================================================
    // PUBLIC API
    // =====================================================================

    function exposePublicAPI() {
        const api = Object.freeze({
            version: '4.0.0',
            config: CONFIG,
            // Operations
            updateQuantity: updateQuantity,
            removeItem: removeItem,
            saveForLater: saveForLater,
            moveToCart: moveToCart,
            clearCart: clearCart,
            applyCoupon: applyCoupon,
            removeCoupon: removeCoupon,
            refreshMiniCart: refreshMiniCart,
            refreshFromResponse: refreshFromResponse,
            // Rendering helpers
            updateCartSummary: updateCartSummary,
            updateHeaderCount: updateHeaderCount,
            applyItemInventoryFromResponse: applyItemInventoryFromResponse,
            // Utilities
            showToast: showToast,
            announce: announce,
            setLoadingState: setLoadingState,
            setCartRootLoading: setCartRootLoading,
            // Lifecycle
            bootstrap: bootstrap,
            // Test hooks (rarely used externally)
            _internal: {
                sendRequest: sendRequest,
                readItemInventoryContext: readItemInventoryContext,
                normalizeInventoryContext: normalizeInventoryContext,
            },
        });
        try {
            window.CartEngine = api;
        } catch (e) { /* noop */ }
    }

    // =====================================================================
    // BOOTSTRAP
    // =====================================================================

    function start() {
        bootstrap();
        observeDynamicContent();
        exposePublicAPI();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start, { once: true });
    } else {
        start();
    }
})();