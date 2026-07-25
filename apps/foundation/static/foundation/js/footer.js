/**
 * Dynamic Footer Progressive Enhancements
 * Data structure and HTML layouts are dynamically rendered via Django CMS templates.
 */
document.addEventListener('DOMContentLoaded', () => {

    // 1. Dynamic Copyright Year & Brand Tag Interpolation
    function initializeCopyright() {
        const copyrightEl = document.getElementById('footer-copyright-text');
        if (copyrightEl) {
            const templateStr = copyrightEl.getAttribute('data-template') || '';
            const brandName = copyrightEl.getAttribute('data-brand') || 'Gobindas Handicrafts';
            const currentYear = new Date().getFullYear();
            
            if (templateStr) {
                copyrightEl.textContent = templateStr
                    .replace('{current_year}', currentYear)
                    .replace('{brand_name}', brandName);
            }
        }
    }

    // 2. Mobile Footer Column Accordion Toggle
    function bindMobileAccordions() {
        document.querySelectorAll('.footer-column h4').forEach(header => {
            header.addEventListener('click', () => {
                if (window.innerWidth <= 768) {
                    const parentBlock = header.parentElement;
                    const currentToggle = header.querySelector('.mobile-toggle');
                    if (!currentToggle) return;

                    const isOpen = currentToggle.getAttribute('aria-expanded') === 'true';

                    document.querySelectorAll('.footer-column').forEach(col => {
                        col.classList.remove('active');
                        const toggle = col.querySelector('.mobile-toggle');
                        if (toggle) toggle.setAttribute('aria-expanded', 'false');
                    });

                    if (!isOpen) {
                        parentBlock.classList.add('active');
                        currentToggle.setAttribute('aria-expanded', 'true');
                    }
                }
            });
        });
    }

    // 3. Frontend Intercepts & Custom CMS Event Triggers
    function bindActionTriggers() {
        document.addEventListener('footerAction', (e) => {
            const actionId = e.detail;
            console.log(`Footer CMS Action Triggered: ${actionId}`);
        });

        const newsletterForm = document.querySelector('.newsletter-form');
        if (newsletterForm) {
            newsletterForm.addEventListener('submit', (e) => {
                const action = newsletterForm.getAttribute('action');
                if (!action || action === '#') {
                    e.preventDefault();
                    console.warn('Newsletter submission intercepted: Endpoint missing in CMS settings.');
                }
            });
        }
    }

    initializeCopyright();
    bindMobileAccordions();
    bindActionTriggers();
});