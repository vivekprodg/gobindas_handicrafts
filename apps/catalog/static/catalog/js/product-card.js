/**
 * Gobindas Handicrafts - Enterprise Product Card Discovery & Interaction Engine
 * ============================================================================
 * Powers dynamic product actions, wishlist toggles, cart dispatching,
 * generic multi-attribute variant resolution, and contextual pricing matrices.
 * 
 * @module GobindasProductCardEngine
 * @version 3.0.0
 */

(function () {
    'use strict';

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
            statusInStock: 'in_stock',
            statusLowStock: 'low_stock',
            statusOutOfStock: 'out_of_stock',
            statusUnknown: 'unknown',
            classInStock: 'is-in-stock',
            classLowStock: 'is-low-stock',
            classOutOfStock: 'is-out-of-stock',
            classUnknown: 'is-unknown',
            ariaLivePoliteness: 'polite'
        },
        endpoints: {
            inventoryLookup: null,
            inventoryStream: null,
            cartSync: null,
            wishlistToggle: null
        },
        network: {
            defaultHeaders: { 'X-Requested-With': 'XMLHttpRequest' },
            credentials: 'same-origin'
        },
        behaviour: {
            debounceMs: 120,
            liveAnnounceInventoryChanges: true,
            autoFetchInventoryOnHover: false,
            autoFetchInventoryOnIntersection: true
        }
    });

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
        } catch (e) {}
        return merged;
    }

    const CONFIG = resolveConfiguration();

    function cloneObject(obj) {
        if (obj === null || typeof obj !== 'object') return obj;
        if (Array.isArray(obj)) return obj.map(cloneObject);
        const out = {};
        for (const key of Object.keys(obj)) {
            out[key] = cloneObject(obj[key]);
        }
        return out;
    }

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
                continue;
            } else {
                out[key] = cloneObject(sourceVal);
            }
        }
        return out;
    }

    function safeJsonParse(text, fallback) {
        if (text === null || text === undefined || text === '') return fallback;
        try {
            return JSON.parse(text);
        } catch (e) {
            return fallback;
        }
    }

    function escapeHTML(value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function sanitizeURL(url) {
        if (typeof url !== 'string' || url.length === 0) return '';
        const trimmed = url.trim();
        if (/^javascript:/i.test(trimmed)) return '';
        if (/^data:/i.test(trimmed) && !/^data:image\//i.test(trimmed)) return '';
        if (/^vbscript:/i.test(trimmed)) return '';
        return trimmed;
    }

    function getCSRFToken() {
        try {
            const meta = document.querySelector('meta[name="csrf-token"]');
            if (meta && meta.content) return meta.content;
            const input = document.querySelector('input[name="csrfmiddlewaretoken"]');
            if (input && input.value) return input.value;
        } catch (e) {}
        return '';
    }

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

    function logError(scope, err) {
        try {
            const message = (err && (err.message || String(err))) || 'Unknown error';
            if (typeof console !== 'undefined' && console.error) {
                console.error('[GobindasProductCardEngine]', scope, message);
            }
        } catch (e) {}
    }

    function getClosestCard(element) {
        if (!element || typeof element.closest !== 'function') return null;
        try {
            return element.closest(CONFIG.selectors.card);
        } catch (e) {
            return null;
        }
    }

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
        const status = String(merged.inventory_status || CONFIG.inventory.statusUnknown).toLowerCase();
        merged.inventory_status = status;
        merged.is_in_stock = status === CONFIG.inventory.statusInStock;
        merged.is_low_stock = status === CONFIG.inventory.statusLowStock;
        merged.is_out_of_stock = status === CONFIG.inventory.statusOutOfStock;
        return merged;
    }

    function readCardInventoryContext(card) {
        if (!card || !card.dataset) return normalizeInventoryContext(null);
        const raw = card.dataset.inventory;
        if (raw) {
            const parsed = safeJsonParse(raw, null);
            if (parsed && typeof parsed === 'object') return normalizeInventoryContext(parsed);
        }
        if (card.dataset.inventoryStatus) {
            return normalizeInventoryContext({ inventory_status: card.dataset.inventoryStatus });
        }
        return normalizeInventoryContext(null);
    }

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

    function getStockStatusClass(status) {
        switch (status) {
            case CONFIG.inventory.statusInStock: return CONFIG.inventory.classInStock;
            case CONFIG.inventory.statusLowStock: return CONFIG.inventory.classLowStock;
            case CONFIG.inventory.statusOutOfStock: return CONFIG.inventory.classOutOfStock;
            default: return CONFIG.inventory.classUnknown;
        }
    }

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

        const messageEl = stockEl.matches('[data-stock-message]')
            ? stockEl
            : stockEl.querySelector(CONFIG.selectors.stockMessage);
        if (messageEl) {
            messageEl.textContent = ctx.stock_message || 'Stock status unavailable';
        } else {
            const iconEl = stockEl.querySelector(CONFIG.selectors.stockIcon);
            stockEl.textContent = '';
            if (iconEl) stockEl.appendChild(iconEl);
            const span = document.createElement('span');
            span.textContent = ctx.stock_message || 'Stock status unavailable';
            stockEl.appendChild(span);
        }

        if (CONFIG.behaviour.liveAnnounceInventoryChanges) {
            announceInventoryChange(ctx);
        }
    }

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

        card.dispatchEvent(new CustomEvent('gobindas:inventory:updated', {
            bubbles: true,
            detail: { card: card, inventory: normalized }
        }));
    }

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
        } catch (e) {}
    }

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

    async function handleWishlistToggle(btn) {
        if (!btn || btn.disabled) return;
        if (btn.dataset.wishlistLoading === 'true') return;
        const card = getClosestCard(btn);
        const productId = card ? (card.dataset.productId || card.dataset.id) : null;

        const wasWishlisted = btn.classList.contains(CONFIG.wishlist.activeClass);
        const targetState = !wasWishlisted;

        btn.dispatchEvent(new CustomEvent('gobindas:wishlist:toggle', {
            bubbles: true,
            detail: { productId: productId, isWishlisted: targetState, element: btn }
        }));

        btn.dataset.wishlistLoading = 'true';
        btn.setAttribute('aria-busy', 'true');
        setWishlistVisuals(btn, targetState);

        const url = btn.dataset.url
            || resolveCardConfig(card, 'endpoints.wishlistToggle')
            || CONFIG.endpoints.wishlistToggle;

        if (!url) {
            btn.dataset.wishlistLoading = 'false';
            btn.removeAttribute('aria-busy');
            btn.dispatchEvent(new CustomEvent('gobindas:wishlist:changed', {
                bubbles: true,
                detail: { productId: productId, isWishlisted: targetState, optimistic: true }
            }));
            return;
        }

        const requiresAuth = (card && card.dataset.authenticated === 'true')
            || document.body.dataset.authenticated === 'true';
        if (!requiresAuth) {
            const loginUrl = btn.dataset.loginUrl || '/login';
            const returnUrl = encodeURIComponent(window.location.pathname + window.location.search);
            setWishlistVisuals(btn, wasWishlisted);
            btn.dataset.wishlistLoading = 'false';
            btn.removeAttribute('aria-busy');
            window.location.href = loginUrl + '?next=' + returnUrl;
            return;
        }

        try {
            const data = await fetchJSON(url, {
                method: 'POST',
                scope: 'wishlist',
                body: { product_id: productId, action: targetState ? 'add' : 'remove' }
            });
            if (!data || data.__error) {
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

        btn.classList.add('pc-adding');
        const originalHTML = btn.innerHTML;
        btn.innerHTML = CONFIG.cart.successText;
        btn.style.background = CONFIG.cart.successBg;
        btn.style.borderColor = CONFIG.cart.successBorder;
        btn.style.color = CONFIG.cart.successColor;

        btn.dispatchEvent(new CustomEvent('gobindas:cart:add', { bubbles: true, detail: payload }));
        btn.dispatchEvent(new CustomEvent('gobindas:cart:updated', { bubbles: true, detail: payload }));
        if (cartAction === 'add') {
            btn.dispatchEvent(new CustomEvent('gobindas:cart:item-added', { bubbles: true, detail: payload }));
        }

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

        setTimeout(() => {
            btn.innerHTML = originalHTML;
            btn.style.background = '';
            btn.style.borderColor = '';
            btn.style.color = '';
            btn.classList.remove('pc-adding');
        }, CONFIG.cart.timeoutDuration);
    }

    function getVariantData(card) {
        if (!card) return null;
        const raw = card.dataset.productVariants || card.getAttribute('data-variants');
        if (!raw) return null;
        const parsed = safeJsonParse(raw, null);
        if (!parsed || !Array.isArray(parsed)) return null;
        return parsed;
    }

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

    function applyVariantPresentation(card, variant) {
        if (!card) return;
        if (!variant || typeof variant !== 'object') {
            card.removeAttribute('data-selected-variant-id');
            return;
        }
        if (variant.id || variant.sku) {
            card.dataset.selectedVariantId = String(variant.id || variant.sku || '');
        }
        const priceEl = card.querySelector(CONFIG.selectors.priceDisplay);
        if (priceEl && variant.price !== undefined && variant.price !== null) {
            const currency = priceEl.dataset.currency || variant.currency || 'NPR';
            priceEl.textContent = currency + ' ' + String(variant.price);
        }
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
        const primaryImg = card.querySelector(CONFIG.selectors.imagePrimary);
        if (primaryImg && (variant.image || variant.featured_image)) {
            const safeSrc = sanitizeURL(variant.image || variant.featured_image);
            if (safeSrc) primaryImg.src = safeSrc;
        }
    }

    async function applyVariantInventory(card, variant) {
        if (!card) return;
        const live = await fetchCardInventoryContext(card);
        if (live) {
            applyInventoryContext(card, live);
            return;
        }
        if (variant && variant.embedded_inventory && typeof variant.embedded_inventory === 'object') {
            applyInventoryContext(card, variant.embedded_inventory);
            return;
        }
        applyInventoryContext(card, normalizeInventoryContext(null));
    }

    async function handleVariantAction(targetElement) {
        const card = getClosestCard(targetElement);
        if (!card) return;
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

    function handleQuickView(btn) {
        const card = getClosestCard(btn);
        if (!card) return;
        const productId = card.dataset.productId || card.dataset.id;
        const slug = card.dataset.productSlug;
        const url = btn.dataset.url
            || (slug ? ('/quick-view/' + encodeURIComponent(slug) + '/') : null);
        if (!url) return;

        if (typeof window.GobindasQuickView !== 'undefined'
            && typeof window.GobindasQuickView.open === 'function') {
            window.GobindasQuickView.open(url, { productId: productId, slug: slug });
        } else {
            window.location.href = url;
        }
    }

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
                    setTimeout(function () { _pendingRefresh.delete(card); }, 30000);
                });
            }
        }, { rootMargin: '200px' });
        return _intersectionObserver;
    }

    async function refreshCardInventory(card) {
        if (!card) return null;
        const live = await fetchCardInventoryContext(card);
        if (live) {
            applyInventoryContext(card, live);
            return live;
        }
        return null;
    }

    function bootstrapCard(card) {
        if (!card || card.dataset.pcBootstrapped === 'true') return;
        card.dataset.pcBootstrapped = 'true';

        const initialCtx = readCardInventoryContext(card);
        applyInventoryContext(card, initialCtx);

        try {
            const initialVariant = resolveActiveVariant(card);
            if (initialVariant) {
                applyVariantPresentation(card, initialVariant);
            }
        } catch (e) {
            logError('bootstrap-variant', e);
        }

        if (CONFIG.behaviour.autoFetchInventoryOnIntersection) {
            const observer = ensureIntersectionObserver();
            if (observer) observer.observe(card);
        }
    }

    function bootstrapAllCards(root) {
        const scope = root || document;
        if (!scope || typeof scope.querySelectorAll !== 'function') return;
        const cards = scope.querySelectorAll(CONFIG.selectors.card);
        for (let i = 0; i < cards.length; i++) {
            bootstrapCard(cards[i]);
        }
    }

    function initializeEventDelegation() {
        document.addEventListener('click', function (event) {
            const target = event.target;
            if (!target) return;

            const wishlistBtn = target.closest && target.closest(CONFIG.selectors.wishlistBtn);
            if (wishlistBtn) {
                event.preventDefault();
                event.stopPropagation();
                handleWishlistToggle(wishlistBtn);
                return;
            }

            const cartBtn = target.closest && target.closest(CONFIG.selectors.cartBtn);
            if (cartBtn) {
                event.preventDefault();
                event.stopPropagation();
                handleAddToCart(cartBtn);
                return;
            }

            const quickviewBtn = target.closest && target.closest(CONFIG.selectors.quickviewTrigger);
            if (quickviewBtn) {
                event.preventDefault();
                event.stopPropagation();
                handleQuickView(quickviewBtn);
                return;
            }

            const variantOpt = target.closest && target.closest(CONFIG.selectors.variantOption);
            if (variantOpt) {
                event.preventDefault();
                handleVariantAction(variantOpt);
                return;
            }
        }, true);

        document.addEventListener('change', function (event) {
            const target = event.target;
            if (!target || !target.matches) return;
            if (target.matches(CONFIG.selectors.variantInput)) {
                handleVariantAction(target);
            }
        }, true);
    }

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
        } catch (e) {}
    }

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
})();