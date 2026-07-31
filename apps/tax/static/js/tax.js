/**
 * Frontend dynamic tax estimation controller for cart and checkout addresses.
 */
document.addEventListener('DOMContentLoaded', () => {
    const addressSelector = document.querySelector('[data-tax-country-input]');
    if (!addressSelector) return;

    const recalculateTax = async () => {
        const country = addressSelector.value || 'NP';
        const stateInput = document.querySelector('[data-tax-state-input]');
        const zipInput = document.querySelector('[data-tax-zip-input]');

        try {
            const response = await fetch('/tax/api/estimate/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                    'X-CSRFToken': document.querySelector('input[name="csrfmiddlewaretoken"]')?.value || '',
                },
                body: JSON.stringify({
                    country_code: country,
                    state_or_province: stateInput ? stateInput.value : '',
                    postal_code: zipInput ? zipInput.value : '',
                })
            });

            const data = await response.json();
            if (data.success) {
                const taxDisplay = document.querySelector('[data-cart-tax]');
                if (taxDisplay) {
                    taxDisplay.textContent = `US$ ${data.tax_total}`;
                }
            }
        } catch (err) {
            console.error('Tax estimation error:', err);
        }
    };

    addressSelector.addEventListener('change', recalculateTax);
});