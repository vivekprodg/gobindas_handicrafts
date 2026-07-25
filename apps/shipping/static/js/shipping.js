/**
 * Dynamic Frontend Shipping Calculation Interceptor
 */
document.addEventListener('DOMContentLoaded', () => {
    const countrySelect = document.querySelector('[data-shipping-country]');
    if (!countrySelect) return;

    const updateShippingRates = async () => {
        const country = countrySelect.value || 'NP';
        const stateInput = document.querySelector('[data-shipping-state]');

        try {
            const response = await fetch('/shipping/api/rates/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                    'X-CSRFToken': document.querySelector('input[name="csrfmiddlewaretoken"]')?.value || '',
                },
                body: JSON.stringify({
                    country_code: country,
                    state_or_province: stateInput ? stateInput.value : '',
                })
            });

            const data = await response.json();
            if (data.success && data.selected_method) {
                const shippingDisplay = document.querySelector('[data-cart-shipping]');
                if (shippingDisplay) {
                    shippingDisplay.textContent = `NPR ${data.shipping_fee}`;
                }
            }
        } catch (err) {
            console.error('Failed to update shipping rates:', err);
        }
    };

    countrySelect.addEventListener('change', updateShippingRates);
});