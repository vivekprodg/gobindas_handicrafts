/**
 * Gobindas Handicrafts - Enterprise Product Card discovery & interaction engine
 * Powers dynamic product actions, wishlist toggles, cart dispatching, 
 * generic multi-attribute variant resolution, and contextual pricing matrices.
 * * Refactored to support robust AJAX persistence, database-backed wishlists,
 * save-cart integrations, and optimistic UI synchronization.
 */

(function () {
    'use strict';

    // Global configurations and design token defaults
    const CONFIG = {
        selectors: {
            card: '.gobindas-product-card, .product-card-wrapper, [data-product-card]',
            wishlistBtn: '.toggle-wishlist',
            wishlistCounter: '.wishlist-count, .wishlist-badge-count, [data-wishlist-count]',
            cartBtn: '.btn-card-add-cart',
            variantInput: '[data-variant-input], .variant-selector, .swatch-input, select[name="variant-option"]',
            priceDisplay: '.product-price, .current-price, .price-display, [data-price-display]',
            comparePriceDisplay: '.compare-price, .original-price, [data-compare-price]',
            stockDisplay: '.stock-status, .availability-badge, [data-stock-display]',
            badgeContainer: '.product-card-badges, .badge-frame, [data-badge-container]'
        },
        wishlist: {
            activeClass: 'wishlisted',
            activeColor: '#C0392B',
            inactiveColor: 'none',
            strokeColor: 'currentColor'
        },
        cart: {
            successText: 'Added to Bag',
            successBg: '#4A6B53',
            successBorder: '#4A6B53',
            successColor: '#FFFFFF',
            timeoutDuration: 2000
        }
    };

    /**
     * Safe HTML/Text Escaper to mitigate XSS in dynamic injections
     */
    function escapeHTML(str) {
        if (!str) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * Finds the closest contextual product card container
     */
    function getClosestCard(element) {
        return element.closest(CONFIG.selectors.card);
    }

    /**
     * Retrieves the CSRF token for secure backend communications
     */
    function getCSRFToken() {
        const meta = document.querySelector('meta[name="csrf-token"], input[name="csrfmiddlewaretoken"]');
        return meta ? (meta.content || meta.value) : '';
    }

    /**
     * Centralized UI updating for wishlist visual tokens (supports optimistic UI & rollback)
     */
    function setWishlistVisuals(btn, isWishlisted) {
        const svg = btn.querySelector('svg');
        
        if (isWishlisted) {
            btn.classList.add(CONFIG.wishlist.activeClass);
            btn.setAttribute('aria-pressed', 'true');
            if (svg) {
                svg.setAttribute('fill', CONFIG.wishlist.activeColor);
                svg.setAttribute('stroke', CONFIG.wishlist.activeColor);
            }
        } else {
            btn.classList.remove(CONFIG.wishlist.activeClass);
            btn.setAttribute('aria-pressed', 'false');
            if (svg) {
                svg.setAttribute('fill', CONFIG.wishlist.inactiveColor);
                svg.setAttribute('stroke', CONFIG.wishlist.strokeColor);
            }
        }
    }

    /**
     * Updates all synchronized wishlist counters in the DOM safely
     */
    function updateWishlistCounters(count) {
        if (count === undefined || count === null) return;
        
        document.querySelectorAll(CONFIG.selectors.wishlistCounter).forEach(counter => {
            counter.innerText = count;
            // Optionally toggle visibility based on count presence
            if (count > 0) {
                counter.classList.remove('hidden', 'd-none', 'visually-hidden');
            } else {
                counter.classList.add('hidden', 'd-none', 'visually-hidden');
            }
        });
    }

    /**
     * Handles Wishlist Toggle Action with database persistence, auth checking, and optimistic UI updates
     */
    async function handleWishlistToggle(btn) {
        if (!btn || btn.disabled || btn.dataset.loading === 'true') return;

        const card = getClosestCard(btn);
        const productId = card ? (card.dataset.productId || card.dataset.id) : null;
        
        // Define initial truth state based on current UI
        const isCurrentlyWishlisted = btn.classList.contains(CONFIG.wishlist.activeClass);
        const targetState = !isCurrentlyWishlisted;

        // 1. Dispatch legacy custom event for backward compatibility (Analytics, GA4, etc.)
        btn.dispatchEvent(new CustomEvent('gobindas:wishlist:toggle', {
            bubbles: true,
            detail: { productId, isWishlisted: targetState, element: btn }
        }));

        const url = btn.dataset.url || (card ? card.dataset.wishlistUrl : null);

        // If no persistence endpoint is defined, fallback to pure UI toggle (Backward compatibility)
        if (!url) {
            setWishlistVisuals(btn, targetState);
            return;
        }

        // 2. Authentication Requirement Check
        const requiresAuth = btn.dataset.authenticated === 'true' || (card && card.dataset.authenticated === 'true');
        const loginUrl = btn.dataset.loginUrl || '/login';
        const isGlobalAuth = document.body.dataset.authenticated === 'true';

        if (requiresAuth && !isGlobalAuth) {
            // Save state context in URL so server can redirect back efficiently
            const returnUrl = encodeURIComponent(window.location.pathname + window.location.search);
            window.location.href = `${loginUrl}?next=${returnUrl}`;
            return;
        }

        // 3. Apply Optimistic UI and lock button
        btn.dataset.loading = 'true';
        btn.style.opacity = '0.6';
        btn.style.pointerEvents = 'none';
        btn.setAttribute('aria-busy', 'true');
        setWishlistVisuals(btn, targetState);

        try {
            // 4. Database Persistence Request
            const response = await fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCSRFToken(),
                    'X-Requested-With': 'XMLHttpRequest'
                },
                body: JSON.stringify({
                    product_id: productId,
                    action: targetState ? 'add' : 'remove'
                })
            });

            if (!response.ok) {
                // Catch backend-enforced auth redirects
                if (response.status === 401 || response.status === 403) {
                    const returnUrl = encodeURIComponent(window.location.pathname + window.location.search);
                    window.location.href = `${loginUrl}?next=${returnUrl}`;
                    return;
                }
                throw new Error(`Server returned status: ${response.status}`);
            }

            const data = await response.json();

            // 5. Synchronize precise state from server response
            const serverState = data.is_wishlisted !== undefined ? data.is_wishlisted : targetState;
            setWishlistVisuals(btn, serverState);

            if (data.wishlist_count !== undefined) {
                updateWishlistCounters(data.wishlist_count);
            }

            // Dispatch extended interaction hooks
            const eventName = serverState ? 'gobindas:wishlist:added' : 'gobindas:wishlist:removed';
            btn.dispatchEvent(new CustomEvent(eventName, { bubbles: true, detail: { productId, data, element: btn } }));
            btn.dispatchEvent(new CustomEvent('gobindas:wishlist:changed', { bubbles: true, detail: { productId, isWishlisted: serverState, data, element: btn } }));

        } catch (error) {
            console.error('Wishlist database synchronization failed:', error);
            
            // Graceful failure rollback
            setWishlistVisuals(btn, isCurrentlyWishlisted);
            
            btn.dispatchEvent(new CustomEvent('gobindas:wishlist:error', {
                bubbles: true,
                detail: { productId, error, element: btn }
            }));
        } finally {
            // Release execution lock safely
            btn.dataset.loading = 'false';
            btn.style.opacity = '';
            btn.style.pointerEvents = '';
            btn.removeAttribute('aria-busy');
        }
    }

    /**
     * Handles Add to Cart UX lifecycle, state cache retrieval, and cart persistence workflows
     */
    function handleAddToCart(btn) {
        if (!btn || btn.disabled || btn.classList.contains('adding')) return;

        btn.classList.add('adding');
        const originalText = btn.innerText;
        const originalBg = btn.style.background;
        const originalBorder = btn.style.borderColor;
        const originalColor = btn.style.color;

        // Apply visual success tokens matching strict brand standards
        btn.innerText = CONFIG.cart.successText;
        btn.style.background = CONFIG.cart.successBg;
        btn.style.borderColor = CONFIG.cart.successBorder;
        btn.style.color = CONFIG.cart.successColor;

        const card = getClosestCard(btn);
        const productId = card ? (card.dataset.productId || card.dataset.id) : null;
        const variantId = card ? card.dataset.selectedVariantId : null;
        
        // Cart Contextual Interactions (supports standard add, save-for-later, merge workflows)
        const cartAction = btn.dataset.cartAction || 'add';
        const payload = { productId, variantId, quantity: 1, action: cartAction, element: btn };

        // 1. Fire original analytics payload
        btn.dispatchEvent(new CustomEvent('gobindas:cart:add', {
            bubbles: true,
            detail: payload
        }));

        // 2. Fire extended persistence and multi-cart module hooks
        btn.dispatchEvent(new CustomEvent('gobindas:cart:updated', { bubbles: true, detail: payload }));
        if (cartAction === 'add') {
            btn.dispatchEvent(new CustomEvent('gobindas:cart:item-added', { bubbles: true, detail: payload }));
        }

        // 3. Optional direct AJAX cart persistence hook (if endpoint configured)
        const syncUrl = btn.dataset.syncUrl || (card ? card.dataset.cartSyncUrl : null);
        if (syncUrl) {
            fetch(syncUrl, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCSRFToken(),
                    'X-Requested-With': 'XMLHttpRequest'
                },
                body: JSON.stringify(payload)
            })
            .then(res => res.ok ? res.json() : Promise.reject(res))
            .then(data => {
                btn.dispatchEvent(new CustomEvent('gobindas:cart:sync-success', { bubbles: true, detail: { ...payload, response: data } }));
            })
            .catch(error => {
                console.error('Cart background sync failed:', error);
                btn.dispatchEvent(new CustomEvent('gobindas:cart:sync-error', { bubbles: true, detail: { ...payload, error } }));
            });
        }

        // Graceful state rollback execution context
        setTimeout(() => {
            btn.innerText = originalText;
            btn.style.background = originalBg;
            btn.style.borderColor = originalBorder;
            btn.style.color = originalColor;
            btn.classList.remove('adding');
        }, CONFIG.cart.timeoutDuration);
    }

    /**
     * Parses the variant repository matrix stored on the card markup safely
     */
    function getVariantData(card) {
        if (!card) return null;
        const rawData = card.dataset.productVariants || card.getAttribute('data-variants');
        if (!rawData) return null;

        try {
            return JSON.parse(rawData);
        } catch (e) {
            console.error('Malformed variant collection payload on product card:', e);
            return null;
        }
    }

    /**
     * Resolves the matching variant configuration object based on chosen UI combinations
     */
    function resolveActiveVariant(card) {
        const variants = getVariantData(card);
        if (!variants || !Array.isArray(variants)) return null;

        // Query all active inputs or selectors representing dynamic parameters within this card boundaries
        const selectors = card.querySelectorAll(CONFIG.selectors.variantInput);
        const chosenAttributes = {};

        selectors.forEach(selector => {
            let name = selector.dataset.attributeName || selector.name;
            let value = '';

            if (selector.tagName.toLowerCase() === 'select') {
                value = selector.value;
            } else if (selector.type === 'radio' || selector.type === 'checkbox') {
                if (selector.checked) {
                    value = selector.value;
                } else {
                    return; // Skip unchecked options
                }
            } else if (selector.classList.contains('active') || selector.dataset.selected === 'true') {
                value = selector.dataset.attributeValue || selector.innerText.trim();
            } else {
                // Generalized fallback check for active class triggers on swatches
                const activeSibling = selector.parentElement.querySelector('.active, .selected');
                if (activeSibling) {
                    value = activeSibling.dataset.attributeValue || activeSibling.value;
                }
            }

            if (name && value) {
                // Keep naming standard sanitized
                name = name.toLowerCase().replace('variant-', '');
                chosenAttributes[name] = value.toLowerCase();
            }
        });

        // Match against variant inventory dictionary
        return variants.find(variant => {
            const variantAttrs = variant.attributes || variant.options || {};
            return Object.keys(chosenAttributes).every(key => {
                if (!variantAttrs[key]) return false;
                return String(variantAttrs[key]).toLowerCase() === String(chosenAttributes[key]).toLowerCase();
            });
        });
    }

    /**
     * Orchestrates dynamic card UI visual updates whenever configuration matches
     */
    function updateCardUiState(card, variant) {
        if (!card) return;

        const priceEl = card.querySelector(CONFIG.selectors.priceDisplay);
        const compareEl = card.querySelector(CONFIG.selectors.comparePriceDisplay);
        const stockEl = card.querySelector(CONFIG.selectors.stockDisplay);
        const cartBtn = card.querySelector(CONFIG.selectors.cartBtn);
        const badgeContainer = card.querySelector(CONFIG.selectors.badgeContainer);

        if (variant) {
            // Track the active variant identity globally on the DOM element for cart operations
            card.dataset.selectedVariantId = variant.id || variant.sku || '';

            // Update Dynamic Prices using project formatting policies or fallback defaults
            if (priceEl && variant.price) {
                const currencyPrefix = priceEl.dataset.currency || 'NPR ';
                priceEl.innerText = `${currencyPrefix}${variant.price}`;
            }

            if (compareEl) {
                if (variant.compare_at_price || variant.compare_price) {
                    const currencyPrefix = compareEl.dataset.currency || 'NPR ';
                    const priceVal = variant.compare_at_price || variant.compare_price;
                    compareEl.innerText = `${currencyPrefix}${priceVal}`;
                    compareEl.style.display = '';
                } else {
                    compareEl.style.display = 'none';
                }
            }

            // Perform inventory state handling and dynamic availability checks
            const isAvailable = variant.available !== undefined ? variant.available : (parseInt(variant.stock || 0) > 0);
            
            if (stockEl) {
                if (isAvailable) {
                    stockEl.innerText = variant.stock_message || 'In Stock';
                    stockEl.className = stockEl.className.replace(/out-of-stock/g, 'in-stock');
                } else {
                    stockEl.innerText = variant.out_of_stock_message || 'Out of Stock';
                    stockEl.className = stockEl.className.replace(/in-stock/g, 'out-of-stock');
                }
            }

            if (cartBtn) {
                if (isAvailable) {
                    cartBtn.disabled = false;
                    cartBtn.removeAttribute('aria-disabled');
                    if (cartBtn.classList.contains('disabled')) cartBtn.classList.remove('disabled');
                } else {
                    cartBtn.disabled = true;
                    cartBtn.setAttribute('aria-disabled', 'true');
                    cartBtn.classList.add('disabled');
                }
            }

            // Update Dynamic Feature Badging Framework
            if (badgeContainer && variant.is_featured !== undefined) {
                let featuredBadge = badgeContainer.querySelector('.badge-featured');
                if (variant.is_featured) {
                    if (!featuredBadge) {
                        featuredBadge = document.createElement('span');
                        featuredBadge.className = 'product-badge badge-featured';
                        featuredBadge.innerText = 'Masterpiece';
                        badgeContainer.appendChild(featuredBadge);
                    }
                } else if (featuredBadge) {
                    featuredBadge.remove();
                }
            }

            // Update variant image representation contextually if present
            const imgEl = card.querySelector('.product-card-media img, .product-image img');
            if (imgEl && (variant.image || variant.featured_image)) {
                imgEl.src = variant.image || variant.featured_image;
            }

        } else {
            // Reset fallback or mark as unavailable options combination
            card.removeAttribute('data-selected-variantId');
            if (cartBtn) {
                cartBtn.disabled = true;
                cartBtn.setAttribute('aria-disabled', 'true');
                cartBtn.classList.add('disabled');
            }
            if (stockEl) {
                stockEl.innerText = 'Unavailable Combination';
            }
        }

        // Notify global window subsystem for modular AJAX listening and extensible features
        card.dispatchEvent(new CustomEvent('gobindas:variant:change', {
            bubbles: true,
            detail: { card, variant }
        }));
    }

    /**
     * Initializes Variant Selector node changes inside components
     */
    function handleVariantAction(targetElement) {
        const card = getClosestCard(targetElement);
        if (!card) return;

        // If the variant selector clicked is a custom button swatch, toggle visual classes manually
        if (targetElement.tagName.toLowerCase() !== 'select' && targetElement.type !== 'radio' && targetElement.type !== 'checkbox') {
            const parentGroup = targetElement.parentElement;
            if (parentGroup) {
                parentGroup.querySelectorAll('.active, .selected, .swatch-active').forEach(el => {
                    el.classList.remove('active', 'selected', 'swatch-active');
                    el.removeAttribute('data-selected');
                });
            }
            targetElement.classList.add('active');
            targetElement.setAttribute('data-selected', 'true');
        }

        const activeVariant = resolveActiveVariant(card);
        updateCardUiState(card, activeVariant);
    }

    /**
     * High Performance Centralized Global Event Delegation Framework
     */
    function initializeEventDelegation() {
        // Intercept click event vectors safely on the main layout frame
        document.addEventListener('click', event => {
            const target = event.target;

            // 1. Wishlist Selector Trap
            const wishlistBtn = target.closest(CONFIG.selectors.wishlistBtn);
            if (wishlistBtn) {
                event.preventDefault();
                event.stopPropagation();
                handleWishlistToggle(wishlistBtn);
                return;
            }

            // 2. Add to Cart Selector Trap
            const cartBtn = target.closest(CONFIG.selectors.cartBtn);
            if (cartBtn) {
                event.preventDefault();
                event.stopPropagation();
                handleAddToCart(cartBtn);
                return;
            }

            // 3. Custom Button Swatch Variant Trap
            const variantInput = target.closest(CONFIG.selectors.variantInput);
            if (variantInput && variantInput.tagName.toLowerCase() !== 'select' && variantInput.type !== 'radio' && variantInput.type !== 'checkbox') {
                handleVariantAction(variantInput);
                return;
            }
        }, true);

        // Intercept form change standard vectors safely for selects/radios
        document.addEventListener('change', event => {
            const target = event.target;
            if (target.matches(CONFIG.selectors.variantInput)) {
                handleVariantAction(target);
            }
        }, true);
    }

    /**
     * Optional Direct Node Bootstrap Loop (Ensures maximum backward compatibility 
     * with existing layout scripts calling individual initializer code blocks)
     */
    function bootstrapCurrentDoms() {
        document.querySelectorAll(CONFIG.selectors.card).forEach(card => {
            const initialVariant = resolveActiveVariant(card);
            if (initialVariant) {
                updateCardUiState(card, initialVariant);
            }
        });
    }

    // Execution entrypoint sequence
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => {
            initializeEventDelegation();
            bootstrapCurrentDoms();
        });
    } else {
        initializeEventDelegation();
        bootstrapCurrentDoms();
    }

    // Expose clean, enterprise-grade architecture API to global scope safely for customization
    window.GobindasProductCardEngine = {
        forceUpdateCard: (cardElement) => {
            const variant = resolveActiveVariant(cardElement);
            updateCardUiState(cardElement, variant);
        },
        toggleWishlistState: handleWishlistToggle,
        dispatchAddToCart: handleAddToCart,
        refreshAllCards: bootstrapCurrentDoms
    };

})();