/**
 * Cart Interaction Engine
 * Handles AJAX quantity updates, item deletions, coupon application, and mini-cart sync.
 */
(function () {
    'use strict';

    function readCsrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        if (meta && meta.content) return meta.content;
        const input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        return input ? input.value : '';
    }

    function sendRequest(url, method, body) {
        const headers = {
            'X-Requested-With': 'XMLHttpRequest',
            'Accept': 'application/json',
        };
        const csrf = readCsrfToken();
        if (csrf && method !== 'GET') {
            headers['X-CSRFToken'] = csrf;
        }

        let payload = body;
        if (body && typeof body === 'object' && !(body instanceof FormData)) {
            headers['Content-Type'] = 'application/json';
            payload = JSON.stringify(body);
        }

        return fetch(url, {
            method: method,
            headers: headers,
            body: method === 'GET' ? undefined : payload,
        }).then(res => res.json());
    }

    function updateBadgeCount(count) {
        document.querySelectorAll('[data-cart-count]').forEach(el => {
            el.textContent = String(count || 0);
        });
    }

    function refreshMiniCart() {
        sendRequest('/cart/mini/', 'GET').then(data => {
            const container = document.querySelector('[data-mini-cart-content]');
            if (container && data.html) {
                container.innerHTML = data.html;
            }
        }).catch(() => {});
    }

    function initDelegation() {
        document.addEventListener('click', function (e) {
            const stepBtn = e.target.closest('[data-qty-action]');
            if (stepBtn) {
                e.preventDefault();
                const form = stepBtn.closest('form');
                const input = form.querySelector('[data-cart-qty-input]');
                if (!input) return;

                let current = parseInt(input.value, 10) || 1;
                const action = stepBtn.dataset.qtyAction;
                if (action === 'increment') current += 1;
                if (action === 'decrement') current = Math.max(1, current - 1);

                input.value = String(current);

                const itemRow = stepBtn.closest('[data-cart-item-id]');
                if (itemRow) {
                    const itemId = itemRow.dataset.cartItemId;
                    sendRequest(`/cart/items/${itemId}/update/`, 'POST', { quantity: current }).then(res => {
                        if (res.success) {
                            window.location.reload();
                        }
                    });
                }
                return;
            }

            const clearBtn = e.target.closest('[data-cart-clear]');
            if (clearBtn) {
                e.preventDefault();
                if (confirm('Are you sure you want to clear your cart?')) {
                    sendRequest('/cart/clear/', 'POST', {}).then(() => {
                        window.location.reload();
                    });
                }
            }
        });

        document.addEventListener('submit', function (e) {
            const couponForm = e.target.closest('[data-cart-coupon-form]');
            if (couponForm) {
                e.preventDefault();
                const input = couponForm.querySelector('[data-cart-coupon-input]');
                const code = input ? input.value.trim() : '';
                if (code) {
                    sendRequest('/cart/coupon/apply/', 'POST', { coupon_code: code }).then(res => {
                        window.location.reload();
                    });
                }
            }
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        initDelegation();
    });

    window.CartEngine = {
        refreshMiniCart: refreshMiniCart,
        updateBadgeCount: updateBadgeCount,
    };
})();