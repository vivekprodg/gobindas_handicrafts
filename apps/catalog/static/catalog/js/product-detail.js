/**
 * Gobindas Handicrafts - Enterprise Product Detail Page (PDP) Interactive Engine
 * ============================================================================
 * Handles gallery image switching, lightbox triggers, quantity adjustments,
 * tab switching, variant selection, and recently viewed storage.
 */

(function () {
    'use strict';

    function escapeHtml(str) {
        if (str === null || str === undefined) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function setStockBadgeClass(element, isInStock, isLowStock, isOutOfStock) {
        if (!element) return;
        element.classList.remove('stock-in', 'stock-low', 'stock-out', 'stock-unknown');
        if (isOutOfStock) {
            element.classList.add('stock-out');
        } else if (isLowStock) {
            element.classList.add('stock-low');
        } else if (isInStock) {
            element.classList.add('stock-in');
        } else {
            element.classList.add('stock-unknown');
        }
    }

    function updateStockMessage(status, freeStock) {
        var labelEl = document.querySelector('[data-inventory-region="main"] .stock-label span:last-child');
        if (!labelEl) return;
        var message = 'Stock status unavailable';
        var numFree = parseFloat(freeStock);
        if (status === 'out_of_stock') {
            message = 'Temporarily Out of Stock';
        } else if (status === 'low_stock') {
            if (!isNaN(numFree) && numFree > 0) {
                message = 'Only ' + Math.floor(numFree) + ' left in stock';
            } else {
                message = 'Low stock - order soon';
            }
        } else if (status === 'in_stock') {
            message = 'In Stock & Ready to Ship';
        }
        labelEl.textContent = message;
    }

    window.pdpGallerySetImage = function (imgUrl, thumbElement) {
        var mainImage = document.getElementById('pdpMainImage');
        if (!mainImage || !imgUrl) return;
        mainImage.src = imgUrl;
        mainImage.setAttribute('data-zoom-url', imgUrl);
        var allThumbs = document.querySelectorAll('.gallery-vertical-thumbs .thumb-item');
        for (var i = 0; i < allThumbs.length; i++) {
            allThumbs[i].classList.remove('active');
            allThumbs[i].setAttribute('aria-selected', 'false');
        }
        if (thumbElement) {
            thumbElement.classList.add('active');
            thumbElement.setAttribute('aria-selected', 'true');
        }
    };

    window.pdpToggleAccordion = function (headerBtn) {
        if (!headerBtn) return;
        var parentNode = headerBtn.closest('.luxury-accordion-node');
        if (!parentNode) return;
        var panel = parentNode.querySelector('.luxury-accordion-panel-content');
        if (!panel) return;
        var isCurrentlyExpanded = headerBtn.getAttribute('aria-expanded') === 'true';

        var system = parentNode.closest('.luxury-accordion-system');
        if (system) {
            var allNodes = system.querySelectorAll('.luxury-accordion-node');
            for (var i = 0; i < allNodes.length; i++) {
                var node = allNodes[i];
                if (node !== parentNode) {
                    node.classList.remove('active');
                    var hdr = node.querySelector('.luxury-accordion-header');
                    var pnl = node.querySelector('.luxury-accordion-panel-content');
                    if (hdr) hdr.setAttribute('aria-expanded', 'false');
                    if (pnl) pnl.style.maxHeight = null;
                }
            }
        }

        if (isCurrentlyExpanded) {
            parentNode.classList.remove('active');
            headerBtn.setAttribute('aria-expanded', 'false');
            panel.style.maxHeight = null;
        } else {
            parentNode.classList.add('active');
            headerBtn.setAttribute('aria-expanded', 'true');
            panel.style.maxHeight = panel.scrollHeight + 'px';
        }
    };

    window.pdpAdjustQty = function (amount) {
        var input = document.getElementById('qtyInput');
        if (!input) return;
        var currentVal = parseInt(input.value, 10) || 1;
        var newVal = currentVal + amount;
        var max = parseInt(input.getAttribute('max'), 10) || 99;
        var min = parseInt(input.getAttribute('min'), 10) || 1;
        if (newVal < min) newVal = min;
        if (newVal > max) newVal = max;
        input.value = newVal;
    };

    window.pdpTriggerBuyNow = function () {
        var qtyInput = document.getElementById('qtyInput');
        var quantity = qtyInput ? parseInt(qtyInput.value, 10) || 1 : 1;
        var container = document.querySelector('.product-detail-container');
        if (!container) return;
        var productId = container.dataset.productId;
        if (!productId) return;
        window.location.href = '/cart/express-checkout/?product_id=' + encodeURIComponent(productId) + '&quantity=' + encodeURIComponent(quantity);
    };

    window.pdpOpenLightbox = function (frame) {
        if (!frame) return;
        var img = frame.querySelector('img');
        if (img && img.dataset.zoomUrl) {
            window.dispatchEvent(new CustomEvent('gobindas:gallery:open-lightbox', {
                bubbles: true,
                detail: { imageUrl: img.dataset.zoomUrl, alt: img.alt || '' }
            }));
        }
    };

    function initTabs() {
        var tabButtons = document.querySelectorAll('.pdp-tab-button');
        var tabPanes = document.querySelectorAll('.pdp-tab-pane');
        if (!tabButtons.length) return;

        function activateTab(tabName) {
            for (var i = 0; i < tabButtons.length; i++) {
                var btn = tabButtons[i];
                if (btn.dataset.tab === tabName) {
                    btn.classList.add('active');
                    btn.setAttribute('aria-selected', 'true');
                    btn.setAttribute('tabindex', '0');
                } else {
                    btn.classList.remove('active');
                    btn.setAttribute('aria-selected', 'false');
                    btn.setAttribute('tabindex', '-1');
                }
            }
            for (var j = 0; j < tabPanes.length; j++) {
                var pane = tabPanes[j];
                if (pane.id === 'pane-' + tabName) {
                    pane.classList.add('active');
                    pane.removeAttribute('hidden');
                } else {
                    pane.classList.remove('active');
                    pane.setAttribute('hidden', '');
                }
            }
        }

        for (var k = 0; k < tabButtons.length; k++) {
            (function (btn) {
                btn.addEventListener('click', function () {
                    activateTab(btn.dataset.tab);
                });
                btn.addEventListener('keydown', function (e) {
                    var allTabs = Array.prototype.slice.call(tabButtons);
                    var idx = allTabs.indexOf(btn);
                    if (e.key === 'ArrowRight') {
                        e.preventDefault();
                        var next = allTabs[(idx + 1) % allTabs.length];
                        next.focus();
                        activateTab(next.dataset.tab);
                    } else if (e.key === 'ArrowLeft') {
                        e.preventDefault();
                        var prev = allTabs[(idx - 1 + allTabs.length) % allTabs.length];
                        prev.focus();
                        activateTab(prev.dataset.tab);
                    }
                });
            })(tabButtons[k]);
        }
    }

    function initVariantSelection() {
        var variantPills = document.querySelectorAll('.variant-pill');
        if (!variantPills.length) return;
        var container = document.querySelector('.product-detail-container');
        var inventoryRegion = document.querySelector('[data-inventory-region="main"] .stock-label');

        for (var i = 0; i < variantPills.length; i++) {
            (function (pill) {
                pill.addEventListener('click', function () {
                    if (pill.disabled) return;
                    var group = pill.closest('.variant-options');
                    if (!group) return;
                    var allPills = group.querySelectorAll('.variant-pill');
                    for (var j = 0; j < allPills.length; j++) {
                        allPills[j].classList.remove('active');
                        allPills[j].setAttribute('aria-checked', 'false');
                    }
                    pill.classList.add('active');
                    pill.setAttribute('aria-checked', 'true');

                    var isInStock = pill.dataset.variantIsInStock === 'true';
                    var inventoryStatus = pill.dataset.variantInventoryStatus || 'unknown';
                    var freeStock = pill.dataset.variantFreeStock || '0.00';

                    if (inventoryRegion) {
                        setStockBadgeClass(
                            inventoryRegion,
                            isInStock,
                            inventoryStatus === 'low_stock',
                            inventoryStatus === 'out_of_stock'
                        );
                    }
                    updateStockMessage(inventoryStatus, freeStock);

                    if (container) {
                        container.dataset.inventoryStatus = inventoryStatus;
                    }

                    var buyNowBtn = document.getElementById('buyNowBtn');
                    if (buyNowBtn) {
                        buyNowBtn.disabled = !isInStock;
                    }
                    var mobileBuyBtn = document.querySelector('.btn-sticky-mobile-buy');
                    if (mobileBuyBtn) {
                        mobileBuyBtn.disabled = !isInStock;
                    }

                    document.dispatchEvent(new CustomEvent('gobindas:variant:change', {
                        bubbles: true,
                        detail: {
                            attributeId: pill.dataset.attributeId,
                            optionId: pill.dataset.optionId,
                            variantId: pill.dataset.variantId,
                            isInStock: isInStock
                        }
                    }));
                });
            })(variantPills[i]);
        }
    }

    function initCart() {
        var bagBtn = document.getElementById('addToBagBtn');
        if (!bagBtn) return;
        bagBtn.addEventListener('click', function () {
            var originalText = this.innerHTML;
            this.innerHTML = 'ADDED TO COLLECTION';
            this.style.background = '#4A6B53';
            this.style.borderColor = '#4A6B53';
            this.style.color = '#FFFFFF';

            var qtyInput = document.getElementById('qtyInput');
            var container = document.querySelector('.product-detail-container');
            var productId = container ? container.dataset.productId : null;
            var quantity = qtyInput ? parseInt(qtyInput.value, 10) || 1 : 1;

            this.dispatchEvent(new CustomEvent('gobindas:cart:detail-add', {
                bubbles: true,
                detail: {
                    productId: productId,
                    quantity: quantity,
                    action: this.dataset.cartAction || 'add'
                }
            }));

            setTimeout(function () {
                this.innerHTML = originalText;
                this.style.background = '#1A1512';
                this.style.borderColor = '#1A1512';
                this.style.color = '#FFFFFF';
            }.bind(this), 2000);
        });
    }

    function initRecentlyViewed() {
        try {
            var container = document.querySelector('.product-detail-container');
            if (!container) return;
            var productId = container.dataset.productId;
            var productSlug = container.dataset.productSlug;
            if (!productId || !productSlug) return;

            var titleEl = document.querySelector('.product-title-row h1');
            var priceEl = document.querySelector('.price-current-large');
            var imgEl = document.getElementById('pdpMainImage');
            var recentItems = [];
            try {
                recentItems = JSON.parse(localStorage.getItem('gobindas_recent_masterpieces') || '[]');
                if (!Array.isArray(recentItems)) recentItems = [];
            } catch (e) {
                recentItems = [];
            }
            recentItems = recentItems.filter(function (item) { return item && item.id !== productId; });
            recentItems.unshift({
                id: productId,
                slug: productSlug,
                title: titleEl ? titleEl.textContent.trim() : '',
                price: priceEl ? priceEl.textContent.trim() : '',
                image: imgEl ? imgEl.src : ''
            });
            try {
                localStorage.setItem('gobindas_recent_masterpieces', JSON.stringify(recentItems.slice(0, 4)));
            } catch (e) {}

            var filteredRecent = recentItems.filter(function (item) { return item && item.id !== productId; });
            var recentGrid = document.getElementById('pdpRecentlyViewedGrid');
            var recentSection = document.getElementById('pdpRecentlyViewedBox');

            if (filteredRecent.length > 0 && recentGrid && recentSection) {
                recentSection.style.display = 'block';
                recentGrid.innerHTML = filteredRecent.map(function (item) {
                    return '<article class="gobindas-product-card" data-product-id="' + escapeHtml(item.id) + '" data-product-slug="' + escapeHtml(item.slug) + '">' +
                        '<div class="product-card-media">' +
                            '<a href="/product/' + escapeHtml(item.slug) + '/" class="product-card-media-link">' +
                                '<img src="' + escapeHtml(item.image) + '" alt="' + escapeHtml(item.title) + '" loading="lazy">' +
                            '</a>' +
                        '</div>' +
                        '<div class="product-card-details">' +
                            '<a href="/product/' + escapeHtml(item.slug) + '/" class="product-title-link" style="font-family: var(--font-brand); font-size:1rem; font-weight:600;">' + escapeHtml(item.title) + '</a>' +
                            '<div class="product-bottom-meta" style="margin-top:auto; padding-top:0.5rem; display:flex; justify-content:space-between; align-items:center;">' +
                                '<span class="price-current" style="font-family: var(--font-brand); font-weight:700;">' + escapeHtml(item.price) + '</span>' +
                            '</div>' +
                        '</div>' +
                    '</article>';
                }).join('');
            }
        } catch (e) {}
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            initTabs();
            initVariantSelection();
            initCart();
            initRecentlyViewed();
        });
    } else {
        initTabs();
        initVariantSelection();
        initCart();
        initRecentlyViewed();
    }
})();