/**
 * Dynamic Floating WhatsApp Support Widget Controller
 * Reads phone numbers and default prompt messages from data attributes.
 */
document.addEventListener('DOMContentLoaded', () => {
    const whatsappBtn = document.getElementById('whatsapp-widget');
    if (!whatsappBtn) return;

    whatsappBtn.addEventListener('click', (e) => {
        const phone = whatsappBtn.getAttribute('data-phone');
        const defaultMsg = whatsappBtn.getAttribute('data-message') || "Hello, I am interested in your artisan products.";
        if (!phone) return;

        const cleanPhone = phone.replace(/[^\d+]/g, '');
        const encodedMsg = encodeURIComponent(defaultMsg);
        whatsappBtn.href = `https://wa.me/${cleanPhone}?text=${encodedMsg}`;
    });
});