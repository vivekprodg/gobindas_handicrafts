/**
 * Payment Gateway Selection Controller
 */
document.addEventListener('DOMContentLoaded', () => {
    const cards = document.querySelectorAll('.gateway-card-option');
    cards.forEach(card => {
        card.addEventListener('click', () => {
            cards.forEach(c => c.classList.remove('selected'));
            card.classList.add('selected');
            const radio = card.querySelector('input[type="radio"]');
            if (radio) radio.checked = true;
        });
    });
});