/**
 * Gobindas Handicrafts - Enterprise Product Card Discovery & Interaction Engine
 * ============================================================================
 * 
 * Powers dynamic product actions, wishlist toggles, cart dispatching,
 * generic multi-attribute variant resolution, and contextual pricing
 * matrices for the entire storefront.
 * 
 * ARCHITECTURE PRINCIPLES
 * ----------------------
 * 1. CATALOG OWNS PRESENTATION ONLY.
 *    The catalog never calculates, stores, or trusts inventory values.
 *    Every inventory display, stock badge, availability message, and
 *    warehouse summary is consumed from the Inventory layer.
 * 
 * 2. INVENTORY IS THE SINGLE SOURCE OF TRUTH.
 *    All stock / availability / warehouse information is read from
 *    the standardized inventory context rendered by the server, or
 *    fetched live from the Inventory API endpoints. Client-side
 *    inventory caching is deliberately avoided to prevent stale
 *    stock representations.
 * 
 * 3. PROGRESSIVE ENHANCEMENT.
 *    If JavaScript fails to load, the server-rendered HTML still
 *    functions correctly. All interactive enhancements are layered
 *    on top of working markup.
 * 
 * 4. ZERO TRUST FOR CLIENT-SIDE INVENTORY.
 *    Inventory values are never used for business-critical decisions
 *    on the client. The server is the only authority for stock.
 * 
 * 5. EVENT-DRIVEN, MODULAR, CMS-DRIVEN.
 *    Every configurable string, selector, and behavior is read from
 *    data attributes or a runtime configuration object so that the
 *    CMS (or theme team) can change labels, URLs, and behavior
 *    without modifying JavaScript source code.
 * 
 * 6. SECURITY-FIRST.
 *    All dynamic content is escaped before DOM insertion. User
 *    input is never evaluated. URLs are sanitized.
 * 
 * 7. PERFORMANCE-FIRST.
 *    A single delegated click/change listener handles every product
 *    card on the page. Heavy work is debounced. No per-card
 *    listeners. No memory leaks. Lazy initialization of observers.
 * 
 * @module GobindasProductCardEngine
 * @version 3.0.0
 * @author Gobindas Handicrafts Engineering Team
 */

(function () {
    'use strict';

    // =====================================================================
    // CONFIGURATION (CMS-DRIVEN / DATA-ATTRIBUTE OVERRIDABLE)
    // =====================================================================
    // Every label, selector, URL, and behavior below can be overridden
    // at runtime by either:
    //   (a) a global `window.GOBINDAS_PC_CONFIG` object defined before
    //       this script loads, or
    //   (b) a `data-pc-config='{"key":"value"}'` attribute on the
    //       <html> root element.
    // This is the canonical extension surface used by the CMS.
    // =====================================================================
    const DEFAULT_CONFIG = Object.freeze({
        selectors: {
            card: '.gobindas-product-card, .product-card-wrapper, [data-product-card]',
            wishlistBtn: '.toggle-wishlist',
            wishlistCounter: '.wishlist-count, .wishlist-badge-count, [data-wishlist-count]',
            cartBtn: '.btn-card-add-cart, .card-add-to-cart, [data-card-add-to-cart]',
            variantInput: '[data-variant-input], .variant-selector, .swatch-input, select[name="variant-option"]',
            priceDisplay: '.price-current, .current-price, [data-price-display]',
            comparePriceDisplay: '.price-original, .compare-price, [data-compare-price]',
            stockDisplay: '.stock-indicator, .stock-status, [data-stock-display]',
            stockMessage: '[data-stock-message]',
            stockIcon: '[data-stock-icon]',
            warehouseDisplay: '.warehouse-indicator, [data-warehouse-display]',
            warehouseSummary: '[data-warehouse-summary]',
            badgeContainer: '.product-card-badges, .badge-frame, [data-badge-container]',
            inventoryRegion: '[data-inventory-region]',
            imagePrimary: '.product-card-image-primary, .product-card-media-link img:first-of-type',
            imageHover: '.product-card-image-hover',
            quickviewTrigger: '.trigger-quickview, [data-trigger-quickview]',
            variantGroup: '[data-variant-group]',
            variantOption: '[data-variant-option]',
            addToBagLabelTarget: '[data-add-to-bag-label]',
            outOfStockLabel: '[data-out-of-stock-label]',
            inStockLabel: '[data-in-stock-label]',
            lowStockLabel: '[data-low-stock-label]'
        },
        wishlist: {
            activeClass: 'wishlisted',
            activeColor: '#C0392B',
            inactiveColor: 'none',
            strokeColor: 'currentColor',
            networkTimeoutMs: 8000
        },
        cart: {
            successText: 'Added to Bag',
            successBg: '#4A6B53',
            successBorder: '#4A6B53',
            successColor: '#FFFFFF',
            timeoutDuration: 2000,
            networkTimeoutMs: 8000
        },
        inventory: {
            // Status states are derived exclusively from the Inventory layer.
            // These string tokens are the only ones the client ever renders.
            statusInStock: 'in_stock',
            statusLowStock: 'low_stock',
            statusOutOfStock: 'out_of_stock',
            statusUnknown: 'unknown',
            // The container CSS class on the stock indicator, per state.
            classInStock: 'is-in-stock',
            classLowStock: 'is-low-stock',
            classOutOfStock: 'is-out-of-stock',
            classUnknown: 'is-unknown',
            // ARIA live region politeness.
            ariaLivePoliteness: 'polite'
        },
        // Endpoints consumed by the engine. All are optional and
        // resolved at runtime from the closest card's data attributes,
        // then from this default config.
        endpoints: {
            inventoryLookup: null,    // e.g. '/api/inventory/lookup/'
            inventoryStream: null,    // e.g. '/api/inventory/stream/'
            cartSync: null,
            wishlistToggle: null
        },
        // Network defaults.
        network: {
            defaultHeaders: { 'X-Requested-With': 'XMLHttpRequest' },
            credentials: 'same-origin'
        },
        // Behavioural flags.
        behaviour: {
            debounceMs: 120,
            liveAnnounceInventoryChanges: true,
            autoFetchInventoryOnHover: false,
            autoFetchInventoryOnIntersection: true
        }
    });

    /**
     * Deeply merge override configuration over the DEFAULT_CONFIG.
     * Supports three layers of override (later wins):
     *   1. DEFAULT_CONFIG (hard-coded safe defaults)
     *   2. window.GOBINDAS_PC_CONFIG (runtime configuration)
     *   3. <html data-pc-config="..."> attribute (per-page configuration)
     */
    function resolveConfiguration() {
        let merged = cloneObject(DEFAULT_CONFIG);
        if (typeof window !== 'undefined' && window.GOBINDAS_PC_CONFIG && typeof window.GOBINDAS_PC_CONFIG === 'object') {
            merged = deepMerge(merged, window.GOBINDAS_PC_CONFIG);
        }
        try {
            const htmlEl = document.documentElement;
            if (htmlEl && htmlEl.dataset && htmlEl.dataset.pcConfig) {
                const domConfig = safeJsonParse(htmlEl.dataset.pcConfig, null);
                if (domConfig && typeof domConfig === 'object') {
                    merged = deepMerge(merged, domConfig);
                }
            }
        } catch (e) {
            // DOM attribute is best-effort; ignore malformed config.
        }
        return merged;
    }

    const CONFIG = resolveConfiguration();

    // =====================================================================
    // UTILITIES
    // =====================================================================

    /**
     * Shallow clone an object. Avoids the structured-clone ceremony
     * for simple POJOs while remaining safe for the config merge.
     */
    function cloneObject(obj) {
        if (obj === null || typeof obj !== 'object') return obj;
        if (Array.isArray(obj)) return obj.map(cloneObject);
        const out = {};
        for (const key of Object.keys(obj)) {
            out[key] = cloneObject(obj[key]);
        }
        return out;
    }

    /**
     * Recursive object merge. Nested plain objects are deep-merged.
     * Arrays and primitives are replaced wholesale.
     */
    function deepMerge(target, source) {
        if (target === null || typeof target !== 'object') return source;
        if (source === null || typeof source !== 'object') return source;
        if (Array.isArray(source)) return cloneObject(source);
        const out = Array.isArray(target) ? cloneObject(target) : cloneObject(target);
        for (const key of Object.keys(source)) {
            const targetVal = out[key];
            const sourceVal = source[key];
            if (
                targetVal && typeof targetVal === 'object' && !Array.isArray(targetVal) &&
                sourceVal && typeof sourceVal === 'object' && !Array.isArray(sourceVal)
            ) {
                out[key] = deepMerge(targetVal, sourceVal);
            } else if (sourceVal === undefined) {
                // Skip undefined source values (preserve target).
                continue;
            } else {
                out[key] = cloneObject(sourceVal);
            }
        }
        return out;
    }

    /**
     * Safe JSON parse with fallback. Never throws.
     */
    function safeJsonParse(text, fallback) {
        if (text === null || text === undefined || text === '') return fallback;
        try {
            return JSON.parse(text);
        } catch (e) {
            return fallback;
        }
    }

    /**
     * Escape a value for safe insertion into HTML contexts.
     */
    function escapeHTML(value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * Sanitize a URL for safe assignment to href / src attributes.
     * Rejects javascript:, data:, and vbscript: schemes.
     */
    function sanitizeURL(url) {
        if (typeof url !== 'string' || url.length === 0) return '';
        const trimmed = url.trim();
        if (/^javascript:/i.test(trimmed)) return '';
        if (/^data:/i.test(trimmed) && !/^data:image\//i.test(trimmed)) return '';
        if (/^vbscript:/i.test(trimmed)) return '';
        return trimmed;
    }

    /**
     * Read a CSRF token from the document. Returns empty string
     * if no token is present (server will reject the request).
     */
    function getCSRFToken() {
        try {
            const meta = document.querySelector('meta[name="csrf-token"]');
            if (meta && meta.content) return meta.content;
            const input = document.querySelector('input[name="csrfmiddlewaretoken"]');
            if (input && input.value) return input.value;
        } catch (e) {
            // Defensive: query may fail in detached documents.
        }
        return '';
    }

    /**
     * Debounce wrapper. Returns a debounced version of the function.
     */
    function debounce(fn, wait) {
        let timer = null;
        const debounced = function (...args) {
            if (timer !== null) clearTimeout(timer);
            timer = setTimeout(() => {
                timer = null;
                try { fn.apply(this, args); } catch (e) { logError('debounced-fn', e); }
            }, wait);
        };
        debounced.cancel = function () {
            if (timer !== null) { clearTimeout(timer); timer = null; }
        };
        return debounced;
    }

    /**
     * Centralized error logger. Filters sensitive details from
     * reaching the console in production (CMS may override).
     */
    function logError(scope, err) {
        try {
            const message = (err && (err.message || String(err))) || 'Unknown error';
            // eslint-disable-next-line no-console
            if (typeof console !== 'undefined' && console.error) {
                console.error('[GobindasProductCardEngine]', scope, message);
            }
        } catch (e) {
            // Never let logging crash the engine.
        }
    }

    /**
     * Walk up the DOM looking for the closest product card root.
     */
    function getClosestCard(element) {
        if (!element || typeof element.closest !== 'function') return null;
        try {
            return element.closest(CONFIG.selectors.card);
        } catch (e) {
            return null;
        }
    }

    /**
     * Resolve a per-card configuration value from data attributes,
     * falling back to global config. Used for endpoints, labels, and
     * per-card feature toggles.
     */
    function resolveCardConfig(card, path) {
        if (!card || !path) return undefined;
        const segments = path.split('.');
        let cursor = CONFIG;
        for (const segment of segments) {
            if (cursor && typeof cursor === 'object' && segment in cursor) {
                cursor = cursor[segment];
            } else {
                cursor = undefined;
                break;
            }
        }
        // Allow per-card data attribute override (e.g. data-cart-endpoint).
        if (card.dataset) {
            const dataKey = 'pc' + segments
                .map(s => s.charAt(0).toUpperCase() + s.slice(1))
                .join('');
            const dataValue = card.dataset[dataKey];
            if (dataValue !== undefined && dataValue !== '') {
                return dataValue;
            }
        }
        return cursor;
    }

    /**
     * Fetch JSON with timeout and AbortController. Never throws.
     */
    async function fetchJSON(url, options) {
        if (!url || typeof url !== 'string') return null;
        const opts = options || {};
        const timeoutMs = (opts.timeoutMs) || (
            (opts.scope === 'wishlist') ? CONFIG.wishlist.networkTimeoutMs :
            (opts.scope === 'cart') ? CONFIG.cart.networkTimeoutMs :
            8000
        );
        const controller = new AbortController();
        const timer = setTimeout(() => { try { controller.abort(); } catch (e) {} }, timeoutMs);
        try {
            const headers = Object.assign(
                {},
                CONFIG.network.defaultHeaders,
                opts.headers || {},
                { 'Content-Type': 'application/json' }
            );
            const csrf = getCSRFToken();
            if (csrf && !headers['X-CSRFToken']) headers['X-CSRFToken'] = csrf;

            const fetchOptions = {
                method: opts.method || 'GET',
                credentials: CONFIG.network.credentials,
                headers: headers,
                signal: controller.signal
            };
            if (opts.body !== undefined) {
                fetchOptions.body = typeof opts.body === 'string' ? opts.body : JSON.stringify(opts.body);
            }
            const response = await fetch(url, fetchOptions);
            clearTimeout(timer);
            if (!response.ok) {
                return { __error: true, status: response.status, statusText: response.statusText };
            }
            const text = await response.text();
            return safeJsonParse(text, { __error: true, status: response.status, raw: text });
        } catch (e) {
            clearTimeout(timer);
            return { __error: true, exception: (e && e.name) || 'NetworkError', message: (e && e.message) || 'Network error' };
        }
    }

    // =====================================================================
    // INVENTORY CONTEXT NORMALIZATION
    // =====================================================================
    // The Inventory layer (server-rendered context or live API) returns
    // data in a canonical shape. The renderer ALWAYS consumes from this
    // normalized structure and never branches on Product / Variant
    // attributes.
    // =====================================================================

    /**
     * Build a normalized inventory context object from any input.
     * Returns safe defaults for all keys, with values that were
     * explicitly provided by the server.
     */
    function normalizeInventoryContext(raw) {
        const fallback = {
            exists: false,
            inventory_status: CONFIG.inventory.statusUnknown,
            is_in_stock: false,
            is_low_stock: false,
            is_out_of_stock: true,
            available_quantity: '0.00',
            reserved_quantity: '0.00',
            free_stock: '0.00',
            incoming_quantity: '0.00',
            warehouse_count: 0,
            warehouse_summary: '',
            stock_message: 'Stock status unavailable',
            variant_count: 0,
            in_stock_variants: 0,
            low_stock_variants: 0,
            out_of_stock_variants: 0
        };
        if (!raw || typeof raw !== 'object') return fallback;
        const merged = Object.assign({}, fallback, raw);
        // Derive booleans defensively from the canonical status token.
        const status = String(merged.inventory_status || CONFIG.inventory.statusUnknown).toLowerCase();
        merged.inventory_status = status;
        merged.is_in_stock = status === CONFIG.inventory.statusInStock;
        merged.is_low_stock = status === CONFIG.inventory.statusLowStock;
        merged.is_out_of_stock = status === CONFIG.inventory.statusOutOfStock;
        return merged;
    }

    /**
     * Read the inventory context rendered into a card by the server.
     * Sources, in priority order:
     *   1. data-inventory attribute (JSON)
     *   2. data-inventory-status attribute (status-only fallback)
     *   3. Empty normalized context
     */
    function readCardInventoryContext(card) {
        if (!card || !card.dataset) return normalizeInventoryContext(null);
        const raw = card.dataset.inventory;
        if (raw) {
            const parsed = safeJsonParse(raw, null);
            if (parsed && typeof parsed === 'object') return normalizeInventoryContext(parsed);
        }
        // Status-only fallback.
        if (card.dataset.inventoryStatus) {
            return normalizeInventoryContext({ inventory_status: card.dataset.inventoryStatus });
        }
        return normalizeInventoryContext(null);
    }

    /**
     * Live-fetch inventory for a card from the Inventory API.
     * Returns a normalized context or null on failure.
     */
    async function fetchCardInventoryContext(card) {
        if (!card) return null;
        const productId = card.dataset.productId || card.dataset.id;
        if (!productId) return null;
        const variantId = card.dataset.selectedVariantId || '';
        const endpoint = resolveCardConfig(card, 'endpoints.inventoryLookup')
            || CONFIG.endpoints.inventoryLookup;
        if (!endpoint) return null;
        const url = endpoint
            + (endpoint.indexOf('?') === -1 ? '?' : '&')
            + 'product_id=' + encodeURIComponent(productId)
            + (variantId ? '&variant_id=' + encodeURIComponent(variantId) : '');
        const data = await fetchJSON(url, { scope: 'inventory' });
        if (!data || data.__error) return null;
        return normalizeInventoryContext(data);
    }

    // =====================================================================
    // INVENTORY RENDERING
    // =====================================================================

    /**
     * Map inventory_status to the configured CSS class for the
     * stock indicator.
     */
    function getStockStatusClass(status) {
        switch (status) {
            case CONFIG.inventory.statusInStock: return CONFIG.inventory.classInStock;
            case CONFIG.inventory.statusLowStock: return CONFIG.inventory.classLowStock;
            case CONFIG.inventory.statusOutOfStock: return CONFIG.inventory.classOutOfStock;
            default: return CONFIG.inventory.classUnknown;
        }
    }

    /**
     * Update the visual state of the stock indicator element to
     * reflect the supplied inventory context. Always preserves
     * accessibility semantics (role="status", aria-live).
     */
    function renderInventoryToStockElement(stockEl, ctx) {
        if (!stockEl || !ctx) return;
        const statusClass = getStockStatusClass(ctx.inventory_status);
        const allClasses = [
            CONFIG.inventory.classInStock,
            CONFIG.inventory.classLowStock,
            CONFIG.inventory.classOutOfStock,
            CONFIG.inventory.classUnknown
        ];
        allClasses.forEach(cls => { if (cls) stockEl.classList.remove(cls); });
        if (statusClass) stockEl.classList.add(statusClass);

        // Update message text
        const messageEl = stockEl.matches('[data-stock-message]')
            ? stockEl
            : stockEl.querySelector(CONFIG.selectors.stockMessage);
        if (messageEl) {
            messageEl.textContent = ctx.stock_message || 'Stock status unavailable';
        } else {
            // Fall back: replace the text content of the stock element
            // while preserving any nested icon.
            const iconEl = stockEl.querySelector(CONFIG.selectors.stockIcon);
            stockEl.textContent = '';
            if (iconEl) stockEl.appendChild(iconEl);
            const span = document.createElement('span');
            span.textContent = ctx.stock_message || 'Stock status unavailable';
            stockEl.appendChild(span);
        }

        // Announce for assistive technology
        if (CONFIG.behaviour.liveAnnounceInventoryChanges) {
            announceInventoryChange(ctx);
        }
    }

    /**
     * Update the add-to-cart button's enabled state and label based
     * on the inventory context. Does not assume the existence of
     * specific CSS classes.
     */
    function renderInventoryToCartButton(btn, ctx) {
        if (!btn || !ctx) return;
        const isPurchasable = ctx.is_in_stock || ctx.is_low_stock;
        if (isPurchasable) {
            btn.disabled = false;
            btn.removeAttribute('aria-disabled');
            btn.removeAttribute('data-disabled-by-inventory');
        } else {
            btn.disabled = true;
            btn.setAttribute('aria-disabled', 'true');
            btn.setAttribute('data-disabled-by-inventory', 'true');
        }
        // Allow CMS-driven label overrides via data attributes on the
        // button itself. If the button carries a label template, use it.
        const inStockLabel = btn.dataset.inStockLabel
            || btn.querySelector(CONFIG.selectors.inStockLabel)?.textContent
            || null;
        const outOfStockLabel = btn.dataset.outOfStockLabel
            || btn.querySelector(CONFIG.selectors.outOfStockLabel)?.textContent
            || null;
        if (ctx.is_out_of_stock && outOfStockLabel) {
            btn.textContent = outOfStockLabel;
        } else if (inStockLabel) {
            btn.textContent = inStockLabel;
        }
    }

    /**
     * Render warehouse summary to the warehouse display element.
     */
    function renderInventoryToWarehouseElement(warehouseEl, ctx) {
        if (!warehouseEl || !ctx) return;
        const summaryEl = warehouseEl.matches('[data-warehouse-summary]')
            ? warehouseEl
            : warehouseEl.querySelector(CONFIG.selectors.warehouseSummary);
        if (summaryEl) {
            summaryEl.textContent = ctx.warehouse_summary || '';
        }
        if (!ctx.warehouse_summary) {
            warehouseEl.style.display = 'none';
        } else {
            warehouseEl.style.display = '';
        }
    }

    /**
     * Apply a normalized inventory context to every inventory-related
     * element on the card. Does not touch price, images, or other
     * non-inventory DOM.
     */
    function applyInventoryContext(card, ctx) {
        if (!card) return;
        const normalized = normalizeInventoryContext(ctx);
        card.dataset.inventoryStatus = normalized.inventory_status;
        card.dataset.inStock = normalized.is_in_stock ? 'true' : 'false';
        card.dataset.outOfStock = normalized.is_out_of_stock ? 'true' : 'false';
        card.dataset.lowStock = normalized.is_low_stock ? 'true' : 'false';

        const stockEl = card.querySelector(CONFIG.selectors.stockDisplay);
        if (stockEl) renderInventoryToStockElement(stockEl, normalized);

        const cartBtn = card.querySelector(CONFIG.selectors.cartBtn);
        if (cartBtn) renderInventoryToCartButton(cartBtn, normalized);

        const warehouseEl = card.querySelector(CONFIG.selectors.warehouseDisplay);
        if (warehouseEl) renderInventoryToWarehouseElement(warehouseEl, normalized);

        // Fire a custom event so the rest of the storefront (and
        // extension points) can react to the inventory change.
        card.dispatchEvent(new CustomEvent('gobindas:inventory:updated', {
            bubbles: true,
            detail: { card: card, inventory: normalized }
        }));
    }

    /**
     * Announce inventory changes to assistive technology via an
     * aria-live region. The region is created lazily on first use.
     */
    let _liveRegion = null;
    function announceInventoryChange(ctx) {
        try {
            if (!_liveRegion) {
                _liveRegion = document.createElement('div');
                _liveRegion.setAttribute('role', 'status');
                _liveRegion.setAttribute('aria-live', CONFIG.inventory.ariaLivePoliteness);
                _liveRegion.setAttribute('aria-atomic', 'true');
                _liveRegion.className = 'pc-visually-hidden-announcer';
                Object.assign(_liveRegion.style, {
                    position: 'absolute',
                    width: '1px',
                    height: '1px',
                    padding: '0',
                    margin: '-1px',
                    overflow: 'hidden',
                    clip: 'rect(0,0,0,0)',
                    whiteSpace: 'nowrap',
                    border: '0'
                });
                document.body.appendChild(_liveRegion);
            }
            _liveRegion.textContent = ctx.stock_message || '';
        } catch (e) {
            // Live region creation is best-effort.
        }
    }

    // =====================================================================
    // WISHLIST HANDLING
    // =====================================================================

    /**
     * Update wishlist button visual tokens. Supports optimistic UI
     * with rollback on persistence failure.
     */
    function setWishlistVisuals(btn, isWishlisted) {
        if (!btn) return;
        if (isWishlisted) {
            btn.classList.add(CONFIG.wishlist.activeClass);
            btn.setAttribute('aria-pressed', 'true');
        } else {
            btn.classList.remove(CONFIG.wishlist.activeClass);
            btn.setAttribute('aria-pressed', 'false');
        }
        const svg = btn.querySelector('svg');
        if (svg) {
            svg.setAttribute('fill', isWishlisted ? CONFIG.wishlist.activeColor : CONFIG.wishlist.inactiveColor);
            svg.setAttribute('stroke', isWishlisted ? CONFIG.wishlist.activeColor : CONFIG.wishlist.strokeColor);
            svg.setAttribute('aria-hidden', 'true');
        }
    }

    /**
     * Synchronize all wishlist counter elements in the DOM.
     */
    function updateWishlistCounters(count) {
        if (count === undefined || count === null) return;
        const counters = document.querySelectorAll(CONFIG.selectors.wishlistCounter);
        for (let i = 0; i < counters.length; i++) {
            counters[i].textContent = String(count);
            if (Number(count) > 0) {
                counters[i].classList.remove('hidden', 'd-none', 'visually-hidden', 'invisible');
            } else {
                counters[i].classList.add('hidden', 'd-none', 'visually-hidden', 'invisible');
            }
        }
    }

    /**
     * Handle wishlist toggle click. Always performs an optimistic UI
     * update, then persists to the server with rollback on failure.
     */
    async function handleWishlistToggle(btn) {
        if (!btn || btn.disabled) return;
        if (btn.dataset.wishlistLoading === 'true') return;
        const card = getClosestCard(btn);
        const productId = card ? (card.dataset.productId || card.dataset.id) : null;

        const wasWishlisted = btn.classList.contains(CONFIG.wishlist.activeClass);
        const targetState = !wasWishlisted;

        // 1. Fire legacy compatibility event for analytics hooks.
        btn.dispatchEvent(new CustomEvent('gobindas:wishlist:toggle', {
            bubbles: true,
            detail: { productId: productId, isWishlisted: targetState, element: btn }
        }));

        // 2. Apply optimistic UI.
        btn.dataset.wishlistLoading = 'true';
        btn.setAttribute('aria-busy', 'true');
        setWishlistVisuals(btn, targetState);

        const url = btn.dataset.url
            || resolveCardConfig(card, 'endpoints.wishlistToggle')
            || CONFIG.endpoints.wishlistToggle;

        // 3. Pure UI mode (no persistence endpoint) - keep optimistic state.
        if (!url) {
            btn.dataset.wishlistLoading = 'false';
            btn.removeAttribute('aria-busy');
            btn.dispatchEvent(new CustomEvent('gobindas:wishlist:changed', {
                bubbles: true,
                detail: { productId: productId, isWishlisted: targetState, optimistic: true }
            }));
            return;
        }

        // 4. Auth gate.
        const requiresAuth = (card && card.dataset.authenticated === 'true')
            || document.body.dataset.authenticated === 'true';
        if (!requiresAuth) {
            const loginUrl = btn.dataset.loginUrl || '/login';
            const returnUrl = encodeURIComponent(window.location.pathname + window.location.search);
            // Roll back before navigating.
            setWishlistVisuals(btn, wasWishlisted);
            btn.dataset.wishlistLoading = 'false';
            btn.removeAttribute('aria-busy');
            window.location.href = loginUrl + '?next=' + returnUrl;
            return;
        }

        // 5. Persist to server.
        try {
            const data = await fetchJSON(url, {
                method: 'POST',
                scope: 'wishlist',
                body: { product_id: productId, action: targetState ? 'add' : 'remove' }
            });
            if (!data || data.__error) {
                // Server rejected - roll back.
                setWishlistVisuals(btn, wasWishlisted);
                if (data && (data.status === 401 || data.status === 403)) {
                    const loginUrl = btn.dataset.loginUrl || '/login';
                    const returnUrl = encodeURIComponent(window.location.pathname + window.location.search);
                    window.location.href = loginUrl + '?next=' + returnUrl;
                    return;
                }
                btn.dispatchEvent(new CustomEvent('gobindas:wishlist:error', {
                    bubbles: true,
                    detail: { productId: productId, error: data }
                }));
                return;
            }
            // 6. Reconcile UI with server-authoritative state.
            const serverState = (data && typeof data === 'object' && 'is_wishlisted' in data)
                ? Boolean(data.is_wishlisted)
                : targetState;
            setWishlistVisuals(btn, serverState);
            if (data && 'wishlist_count' in data) {
                updateWishlistCounters(data.wishlist_count);
            }
            btn.dispatchEvent(new CustomEvent(
                serverState ? 'gobindas:wishlist:added' : 'gobindas:wishlist:removed',
                { bubbles: true, detail: { productId: productId, data: data, element: btn } }
            ));
            btn.dispatchEvent(new CustomEvent('gobindas:wishlist:changed', {
                bubbles: true,
                detail: { productId: productId, isWishlisted: serverState, data: data, element: btn }
            }));
        } catch (e) {
            setWishlistVisuals(btn, wasWishlisted);
            logError('wishlist-toggle', e);
            btn.dispatchEvent(new CustomEvent('gobindas:wishlist:error', {
                bubbles: true,
                detail: { productId: productId, error: e }
            }));
        } finally {
            btn.dataset.wishlistLoading = 'false';
            btn.removeAttribute('aria-busy');
        }
    }

    // =====================================================================
    // CART HANDLING
    // =====================================================================

    /**
     * Handle add-to-cart click. Optimistically updates the button,
     * fires analytics events, and optionally persists to the server.
     */
    function handleAddToCart(btn) {
        if (!btn || btn.disabled) return;
        if (btn.classList.contains('pc-adding')) return;

        const card = getClosestCard(btn);
        const productId = card ? (card.dataset.productId || card.dataset.id) : null;
        const variantId = card ? card.dataset.selectedVariantId : null;
        const cartAction = btn.dataset.cartAction || 'add';

        const payload = {
            productId: productId,
            variantId: variantId,
            quantity: 1,
            action: cartAction,
            element: btn,
            card: card
        };

        // Visual success state.
        btn.classList.add('pc-adding');
        const originalHTML = btn.innerHTML;
        btn.innerHTML = CONFIG.cart.successText;
        btn.style.background = CONFIG.cart.successBg;
        btn.style.borderColor = CONFIG.cart.successBorder;
        btn.style.color = CONFIG.cart.successColor;

        // Analytics hooks.
        btn.dispatchEvent(new CustomEvent('gobindas:cart:add', { bubbles: true, detail: payload }));
        btn.dispatchEvent(new CustomEvent('gobindas:cart:updated', { bubbles: true, detail: payload }));
        if (cartAction === 'add') {
            btn.dispatchEvent(new CustomEvent('gobindas:cart:item-added', { bubbles: true, detail: payload }));
        }

        // Optional server persistence.
        const syncUrl = btn.dataset.syncUrl
            || resolveCardConfig(card, 'endpoints.cartSync')
            || CONFIG.endpoints.cartSync;
        if (syncUrl) {
            fetchJSON(syncUrl, {
                method: 'POST',
                scope: 'cart',
                body: payload
            }).then(data => {
                if (!data || data.__error) {
                    btn.dispatchEvent(new CustomEvent('gobindas:cart:sync-error', {
                        bubbles: true,
                        detail: Object.assign({}, payload, { error: data })
                    }));
                    return;
                }
                btn.dispatchEvent(new CustomEvent('gobindas:cart:sync-success', {
                    bubbles: true,
                    detail: Object.assign({}, payload, { response: data })
                }));
            }).catch(err => {
                logError('cart-sync', err);
                btn.dispatchEvent(new CustomEvent('gobindas:cart:sync-error', {
                    bubbles: true,
                    detail: Object.assign({}, payload, { error: err })
                }));
            });
        }

        // Visual rollback.
        setTimeout(() => {
            btn.innerHTML = originalHTML;
            btn.style.background = '';
            btn.style.borderColor = '';
            btn.style.color = '';
            btn.classList.remove('pc-adding');
        }, CONFIG.cart.timeoutDuration);
    }

    // =====================================================================
    // VARIANT RESOLUTION
    // =====================================================================

    /**
     * Parse the variant repository embedded on the card markup.
     * Returns a normalized array of variant objects.
     */
    function getVariantData(card) {
        if (!card) return null;
        const raw = card.dataset.productVariants || card.getAttribute('data-variants');
        if (!raw) return null;
        const parsed = safeJsonParse(raw, null);
        if (!parsed || !Array.isArray(parsed)) return null;
        return parsed;
    }

    /**
     * Find the variant whose selected attribute set matches the
     * currently chosen UI inputs.
     */
    function resolveActiveVariant(card) {
        const variants = getVariantData(card);
        if (!variants) return null;
        const chosen = {};
        try {
            const inputs = card.querySelectorAll(CONFIG.selectors.variantInput);
            for (let i = 0; i < inputs.length; i++) {
                const el = inputs[i];
                if (!el) continue;
                const tag = (el.tagName || '').toLowerCase();
                const type = (el.type || '').toLowerCase();
                let name = el.dataset.attributeName || el.name || '';
                let value = '';
                if (tag === 'select') {
                    value = el.value || '';
                } else if (type === 'radio' || type === 'checkbox') {
                    if (!el.checked) continue;
                    value = el.value || '';
                } else {
                    // Custom swatch / button
                    const isActive = el.classList.contains('active')
                        || el.classList.contains('selected')
                        || el.dataset.selected === 'true'
                        || el.getAttribute('aria-checked') === 'true';
                    if (!isActive) continue;
                    value = el.dataset.attributeValue
                        || el.dataset.value
                        || el.value
                        || (el.textContent || '').trim();
                }
                if (!name || value === '' || value === undefined) continue;
                name = String(name).toLowerCase().replace(/^variant[-_]?/, '');
                chosen[name] = String(value).toLowerCase();
            }
        } catch (e) {
            logError('variant-resolve', e);
            return null;
        }
        if (Object.keys(chosen).length === 0) return null;
        for (let i = 0; i < variants.length; i++) {
            const variant = variants[i];
            if (!variant || typeof variant !== 'object') continue;
            const attrs = variant.attributes || variant.options || variant;
            if (!attrs || typeof attrs !== 'object') continue;
            let match = true;
            for (const key of Object.keys(chosen)) {
                const candidateKeys = [key, key.replace(/-/g, '_'), key.replace(/_/g, '-')];
                let matched = false;
                for (const k of candidateKeys) {
                    if (k in attrs && String(attrs[k]).toLowerCase() === chosen[key]) {
                        matched = true;
                        break;
                    }
                }
                if (!matched) { match = false; break; }
            }
            if (match) return variant;
        }
        return null;
    }

    /**
     * Apply the matched variant's non-inventory presentation data
     * (price, image, sku, etc.) to the card. Inventory is fetched
     * separately so that it is always the most current value.
     */
    function applyVariantPresentation(card, variant) {
        if (!card) return;
        if (!variant || typeof variant !== 'object') {
            card.removeAttribute('data-selected-variant-id');
            return;
        }
        if (variant.id || variant.sku) {
            card.dataset.selectedVariantId = String(variant.id || variant.sku || '');
        }
        // Price
        const priceEl = card.querySelector(CONFIG.selectors.priceDisplay);
        if (priceEl && variant.price !== undefined && variant.price !== null) {
            const currency = priceEl.dataset.currency || variant.currency || 'NPR';
            priceEl.textContent = currency + ' ' + String(variant.price);
        }
        // Compare-at price
        const compareEl = card.querySelector(CONFIG.selectors.comparePriceDisplay);
        if (compareEl) {
            const compareVal = variant.compare_at_price || variant.compare_price;
            if (compareVal !== undefined && compareVal !== null && compareVal !== '') {
                const currency = compareEl.dataset.currency || variant.currency || 'NPR';
                compareEl.textContent = currency + ' ' + String(compareVal);
                compareEl.style.display = '';
            } else {
                compareEl.style.display = 'none';
            }
        }
        // Image swap
        const primaryImg = card.querySelector(CONFIG.selectors.imagePrimary);
        if (primaryImg && (variant.image || variant.featured_image)) {
            const safeSrc = sanitizeURL(variant.image || variant.featured_image);
            if (safeSrc) primaryImg.src = safeSrc;
        }
        // Variant identity badge
        const badgeContainer = card.querySelector(CONFIG.selectors.badgeContainer);
        if (badgeContainer && variant.is_featured !== undefined) {
            let featuredBadge = badgeContainer.querySelector('.badge-featured');
            if (variant.is_featured && !featuredBadge) {
                featuredBadge = document.createElement('span');
                featuredBadge.className = 'product-badge badge-featured';
                featuredBadge.textContent = 'Masterpiece';
                badgeContainer.appendChild(featuredBadge);
            } else if (!variant.is_featured && featuredBadge) {
                featuredBadge.remove();
            }
        }
    }

    /**
     * Update inventory on the card by fetching live data from the
     * Inventory API. Falls back to the variant's embedded inventory
     * context if present.
     */
    async function applyVariantInventory(card, variant) {
        if (!card) return;
        // Prefer live fetch for freshest data.
        const live = await fetchCardInventoryContext(card);
        if (live) {
            applyInventoryContext(card, live);
            return;
        }
        // Fallback: variant.embedded_inventory if the server provided it.
        if (variant && variant.embedded_inventory && typeof variant.embedded_inventory === 'object') {
            applyInventoryContext(card, variant.embedded_inventory);
            return;
        }
        // Last-resort fallback: derive an inventory context from
        // the legacy `variant.stock` and `variant.available` hints
        // that some legacy serializers still attach. Treat them
        // strictly as advisory display data.
        if (variant && (variant.available !== undefined || variant.stock !== undefined)) {
            const isAvail = variant.available !== undefined
                ? Boolean(variant.available)
                : (Number(variant.stock) > 0);
            const ctx = normalizeInventoryContext({
                inventory_status: isAvail ? CONFIG.inventory.statusInStock : CONFIG.inventory.statusOutOfStock,
                stock_message: isAvail
                    ? (variant.stock_message || 'In stock')
                    : (variant.out_of_stock_message || 'Out of stock'),
                available_quantity: variant.stock || 0,
                warehouse_summary: variant.warehouse_summary || ''
            });
            applyInventoryContext(card, ctx);
            return;
        }
        // No signal at all - mark as unknown but do not crash.
        applyInventoryContext(card, normalizeInventoryContext(null));
    }

    /**
     * Handle any variant input change.
     */
    async function handleVariantAction(targetElement) {
        const card = getClosestCard(targetElement);
        if (!card) return;
        // For custom swatch buttons, toggle active state.
        const tag = (targetElement.tagName || '').toLowerCase();
        const type = (targetElement.type || '').toLowerCase();
        if (tag !== 'select' && type !== 'radio' && type !== 'checkbox' && type !== 'hidden') {
            const group = targetElement.closest(CONFIG.selectors.variantGroup)
                || targetElement.parentElement;
            if (group) {
                const siblings = group.querySelectorAll(
                    CONFIG.selectors.variantOption + ', ' + CONFIG.selectors.variantInput
                );
                for (let i = 0; i < siblings.length; i++) {
                    const sib = siblings[i];
                    if (!sib || sib === targetElement) continue;
                    sib.classList.remove('active', 'selected', 'pc-variant-active');
                    sib.removeAttribute('data-selected');
                    sib.setAttribute('aria-checked', 'false');
                }
            }
            targetElement.classList.add('active', 'pc-variant-active');
            targetElement.setAttribute('data-selected', 'true');
            targetElement.setAttribute('aria-checked', 'true');
        }
        const variant = resolveActiveVariant(card);
        applyVariantPresentation(card, variant);
        try {
            await applyVariantInventory(card, variant);
        } catch (e) {
            logError('variant-inventory', e);
        }
        card.dispatchEvent(new CustomEvent('gobindas:variant:change', {
            bubbles: true,
            detail: { card: card, variant: variant }
        }));
    }

    // =====================================================================
    // QUICK VIEW
    // =====================================================================

    function handleQuickView(btn) {
        const card = getClosestCard(btn);
        if (!card) return;
        const productId = card.dataset.productId || card.dataset.id;
        const slug = card.dataset.productSlug;
        const url = btn.dataset.url
            || (slug ? ('/product/' + encodeURIComponent(slug) + '/?quickview=1') : null);
        if (!url) {
            card.dispatchEvent(new CustomEvent('gobindas:quickview:open', {
                bubbles: true,
                detail: { productId: productId, slug: slug, card: card }
            }));
            return;
        }
        // Open the quick view in a modal if the storefront provides a
        // quick-view router on the window; otherwise navigate.
        if (typeof window.GobindasQuickView !== 'undefined'
            && typeof window.GobindasQuickView.open === 'function') {
            window.GobindasQuickView.open(url, { productId: productId, slug: slug });
        } else {
            window.location.href = url;
        }
    }

    // =====================================================================
    // INTERSECTION OBSERVER (LAZY INVENTORY REFRESH)
    // =====================================================================

    let _intersectionObserver = null;
    const _pendingRefresh = new WeakSet();

    function ensureIntersectionObserver() {
        if (_intersectionObserver || typeof IntersectionObserver === 'undefined') return null;
        if (!CONFIG.behaviour.autoFetchInventoryOnIntersection) return null;
        _intersectionObserver = new IntersectionObserver(function (entries) {
            for (let i = 0; i < entries.length; i++) {
                const entry = entries[i];
                if (!entry.isIntersecting) continue;
                const card = entry.target;
                if (_pendingRefresh.has(card)) continue;
                _pendingRefresh.add(card);
                refreshCardInventory(card).finally(function () {
                    // Allow re-refresh after a short delay.
                    setTimeout(function () { _pendingRefresh.delete(card); }, 30000);
                });
            }
        }, { rootMargin: '200px' });
        return _intersectionObserver;
    }

    /**
     * Refresh the inventory of a single card by hitting the
     * Inventory API. Always safe to call.
     */
    async function refreshCardInventory(card) {
        if (!card) return null;
        const live = await fetchCardInventoryContext(card);
        if (live) {
            applyInventoryContext(card, live);
            return live;
        }
        return null;
    }

    // =====================================================================
    // DEBOUNCED VARIANT REFRESH
    // =====================================================================

    const _debouncedRefreshByCard = new WeakMap();
    function debouncedRefreshCard(card) {
        if (!card) return;
        let fn = _debouncedRefreshByCard.get(card);
        if (!fn) {
            fn = debounce(function () { refreshCardInventory(card); }, CONFIG.behaviour.debounceMs);
            _debouncedRefreshByCard.set(card, fn);
        }
        fn();
    }

    // =====================================================================
    // INITIALIZATION
    // =====================================================================

    /**
     * Bootstrap a single card: apply server-rendered inventory, attach
     * lazy observers, mark for live refresh on intersection.
     */
    function bootstrapCard(card) {
        if (!card || card.dataset.pcBootstrapped === 'true') return;
        card.dataset.pcBootstrapped = 'true';

        // 1. Apply the server-rendered inventory context immediately.
        const initialCtx = readCardInventoryContext(card);
        applyInventoryContext(card, initialCtx);

        // 2. Apply variant presentation if a variant is already active.
        try {
            const initialVariant = resolveActiveVariant(card);
            if (initialVariant) {
                applyVariantPresentation(card, initialVariant);
            }
        } catch (e) {
            logError('bootstrap-variant', e);
        }

        // 3. Attach to intersection observer for live refresh.
        if (CONFIG.behaviour.autoFetchInventoryOnIntersection) {
            const observer = ensureIntersectionObserver();
            if (observer) observer.observe(card);
        }
    }

    /**
     * Bootstrap every card on the page.
     */
    function bootstrapAllCards(root) {
        const scope = root || document;
        if (!scope || typeof scope.querySelectorAll !== 'function') return;
        const cards = scope.querySelectorAll(CONFIG.selectors.card);
        for (let i = 0; i < cards.length; i++) {
            bootstrapCard(cards[i]);
        }
    }

    /**
     * Centralized delegated event handling. Replaces the need for
     * per-card listeners and keeps memory usage flat regardless of
     * how many cards are on the page.
     */
    function initializeEventDelegation() {
        // Capture-phase listener to ensure we beat any per-card
        // listeners that legacy scripts may have attached.
        document.addEventListener('click', function (event) {
            const target = event.target;
            if (!target) return;

            // 1. Wishlist toggle
            const wishlistBtn = target.closest && target.closest(CONFIG.selectors.wishlistBtn);
            if (wishlistBtn) {
                event.preventDefault();
                event.stopPropagation();
                handleWishlistToggle(wishlistBtn);
                return;
            }

            // 2. Add to cart
            const cartBtn = target.closest && target.closest(CONFIG.selectors.cartBtn);
            if (cartBtn) {
                event.preventDefault();
                event.stopPropagation();
                handleAddToCart(cartBtn);
                return;
            }

            // 3. Quick view
            const quickviewBtn = target.closest && target.closest(CONFIG.selectors.quickviewTrigger);
            if (quickviewBtn) {
                event.preventDefault();
                event.stopPropagation();
                handleQuickView(quickviewBtn);
                return;
            }

            // 4. Variant option (custom swatch / button)
            const variantOpt = target.closest && target.closest(CONFIG.selectors.variantOption);
            if (variantOpt) {
                event.preventDefault();
                handleVariantAction(variantOpt);
                return;
            }
        }, true);

        // 5. Variant change events (select / radio / checkbox)
        document.addEventListener('change', function (event) {
            const target = event.target;
            if (!target || !target.matches) return;
            if (target.matches(CONFIG.selectors.variantInput)) {
                handleVariantAction(target);
            }
        }, true);
    }

    /**
     * Public API exposed on the window for programmatic use.
     */
    function exposePublicAPI() {
        const api = Object.freeze({
            version: '3.0.0',
            config: CONFIG,
            refreshCard: refreshCardInventory,
            refreshAll: function () { bootstrapAllCards(); },
            toggleWishlist: handleWishlistToggle,
            addToCart: handleAddToCart,
            applyInventory: applyInventoryContext,
            resolveVariant: resolveActiveVariant,
            normalizeInventory: normalizeInventoryContext,
            fetchInventory: fetchCardInventoryContext,
            bootstrapCard: bootstrapCard
        });
        try {
            window.GobindasProductCardEngine = api;
        } catch (e) {
            // Window is read-only; the engine still functions internally.
        }
    }

    // =====================================================================
    // BOOTSTRAP
    // =====================================================================

    function start() {
        initializeEventDelegation();
        bootstrapAllCards();
        exposePublicAPI();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start, { once: true });
    } else {
        start();
    }

    // Re-bootstrap any cards added dynamically (SPA navigation, AJAX grids).
    if (typeof MutationObserver !== 'undefined') {
        const dynamicObserver = new MutationObserver(function (mutations) {
            for (let i = 0; i < mutations.length; i++) {
                const mutation = mutations[i];
                for (let j = 0; j < mutation.addedNodes.length; j++) {
                    const node = mutation.addedNodes[j];
                    if (!node || node.nodeType !== 1) continue;
                    if (node.matches && node.matches(CONFIG.selectors.card)) {
                        bootstrapCard(node);
                    }
                    if (node.querySelectorAll) {
                        const nested = node.querySelectorAll(CONFIG.selectors.card);
                        for (let k = 0; k < nested.length; k++) {
                            bootstrapCard(nested[k]);
                        }
                    }
                }
            }
        });
        if (document.body) {
            dynamicObserver.observe(document.body, { childList: true, subtree: true });
        } else {
            document.addEventListener('DOMContentLoaded', function () {
                if (document.body) {
                    dynamicObserver.observe(document.body, { childList: true, subtree: true });
                }
            }, { once: true });
        }
    }
})();