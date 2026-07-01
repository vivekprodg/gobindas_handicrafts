/**
 * Dynamic Footer Behaviors
 * * Note: Data structures and HTML layout are now exclusively driven by the Django CMS backend.
 * This file is solely responsible for client-side progressive enhancements.
 */
document.addEventListener('DOMContentLoaded', () => {

    // ==========================================
    // 1. Dynamic Copyright Year & Brand Parsing
    // ==========================================
    function initializeCopyright() {
        const copyrightEl = document.getElementById('footer-copyright-text');
        
        if (copyrightEl) {
            const templateStr = copyrightEl.getAttribute('data-template') || '';
            const brandName = copyrightEl.getAttribute('data-brand') || '';
            const currentYear = new Date().getFullYear();
            
            if (templateStr) {
                // Replace CMS template tags with real-time browser data
                copyrightEl.textContent = templateStr
                    .replace('{current_year}', currentYear)
                    .replace('{brand_name}', brandName);
            }
        }
    }

    // ==========================================
    // 2. Mobile Accordion Toggle
    // ==========================================
    function bindMobileAccordions() {
        document.querySelectorAll('.footer-column h4').forEach(header => {
            header.addEventListener('click', () => {
                // Only trigger accordion behavior on mobile views
                if (window.innerWidth <= 768) {
                    const parentBlock = header.parentElement;
                    const currentToggle = header.querySelector('.mobile-toggle');
                    
                    if (!currentToggle) return;

                    const isOpen = currentToggle.getAttribute('aria-expanded') === 'true';

                    // Close all active columns
                    document.querySelectorAll('.footer-column').forEach(col => {
                        col.classList.remove('active');
                        const toggle = col.querySelector('.mobile-toggle');
                        if (toggle) toggle.setAttribute('aria-expanded', 'false');
                    });

                    // Open the clicked column if it was previously closed
                    if (!isOpen) {
                        parentBlock.classList.add('active');
                        currentToggle.setAttribute('aria-expanded', 'true');
                    }
                }
            });
        });
    }

    // ==========================================
    // 3. Frontend Enhancements & Action Intercepts
    // ==========================================
    function bindActionTriggers() {
        // Intercept action links (e.g. "open_return_modal") configured in the CMS
        document.addEventListener('footerAction', (e) => {
            const actionId = e.detail;
            console.log(`Footer CMS Action Triggered: ${actionId}`);
            
            // Example hooks for future integration:
            // if (actionId === 'open_return_modal') { returnModal.open(); }
            // if (actionId === 'open_support_chat') { chatWidget.show(); }
        });

        // Graceful fallback if newsletter form is submitted without a CMS endpoint
        const newsletterForm = document.querySelector('.newsletter-form');
        if (newsletterForm) {
            newsletterForm.addEventListener('submit', (e) => {
                if (newsletterForm.getAttribute('action') === '#') {
                    e.preventDefault();
                    console.warn('Newsletter submission intercepted: No endpoint configured in CMS.');
                }
            });
        }
    }

    // Initialize all modules
    initializeCopyright();
    bindMobileAccordions();
    bindActionTriggers();
});