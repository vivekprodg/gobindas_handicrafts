/**
 * Gobindas Handicrafts - Advanced Product Discovery & Discovery Filtering Engine
 * Powers real-time product discovery, progressive AJAX filtering, deep URL state mapping, 
 * accessible accordions, and responsive catalog layouts.
 */

(function () {
    'use strict';

    // Core Discovery Configurations
    const CONFIG = {
        selectors: {
            form: '#catalog-filter-form',
            priceSlider: '#priceRangeInput',
            priceLabel: '#maxPriceLabel',
            accordionTrigger: '.filter-node-trigger',
            accordionNode: '.filter-accordion-node',
            productGallery: '#main-product-gallery, .product-showcase-matrix',
            paginationNav: '.catalog-pagination-nav',
            metricsCount: '.product-metrics-count',
            sortSelect: '#catalog-sort-select'
        },
        classes: {
            active: 'active',
            loading: 'catalog-loading',
            disabled: 'disabled'
        },
        locale: 'en-NP',
        currencySymbol: 'NPR'
    };

    class ProductDiscoveryEngine {
        constructor() {
            this.form = document.querySelector(CONFIG.selectors.form);
            this.slider = document.getElementById('priceRangeInput');
            this.label = document.getElementById('maxPriceLabel');
            this.ajaxEnabled = true; // Progressive enhancement flag
            this.isFetching = false;

            this.init();
        }

        init() {
            this.initializeAccordions();
            this.initializePriceSlider();
            this.initializeFormInterception();
            this.initializeHistoryMapping();
            this.initializeAccessibilityAttributes();
        }

        /**
         * Dynamic state management for accessible UI filters accordions
         */
        initializeAccordions() {
            document.addEventListener('click', (e) => {
                const trigger = e.target.closest(CONFIG.selectors.accordionTrigger);
                if (!trigger) return;

                e.preventDefault();
                const parentNode = trigger.closest(CONFIG.selectors.accordionNode);
                if (!parentNode) return;

                const isCurrentlyActive = parentNode.classList.contains(CONFIG.classes.active);
                
                if (isCurrentlyActive) {
                    parentNode.classList.remove(CONFIG.classes.active);
                    trigger.setAttribute('aria-expanded', 'false');
                } else {
                    parentNode.classList.add(CONFIG.classes.active);
                    trigger.setAttribute('aria-expanded', 'true');
                }
            });
        }

        /**
         * Realtime interactive formatting for local pricing range vectors
         */
        initializePriceSlider() {
            if (!this.slider || !this.label) return;

            this.slider.addEventListener('input', (e) => {
                const liveValue = Number(e.target.value);
                const formattedVal = liveValue.toLocaleString(CONFIG.locale);
                this.label.innerText = `Max: ${CONFIG.currencySymbol} ${formattedVal}`;
            });
        }

        /**
         * Intercepts standard and programmatic form actions to inject AJAX state streaming
         */
        initializeFormInterception() {
            if (!this.form) return;

            // Cache natural native browser submission behavior for runtime failover execution
            this.form._nativeSubmit = this.form.submit;

            // Override programmatic form.submit() invocations to leverage progressive enhancement pipeline
            this.form.submit = () => {
                if (this.ajaxEnabled) {
                    this.applyFiltersAjax();
                } else {
                    this.form._nativeSubmit();
                }
            };

            // Capture structural native form submit event vectors
            this.form.addEventListener('submit', (e) => {
                if (this.ajaxEnabled) {
                    e.preventDefault();
                    this.applyFiltersAjax();
                }
            });

            // Handle AJAX updates when sort options change
            const sortSelect = document.querySelector(CONFIG.selectors.sortSelect);
            if (sortSelect) {
                sortSelect.addEventListener('change', () => {
                    if (this.ajaxEnabled) {
                        this.applyFiltersAjax();
                    }
                });
            }

            // Capture asynchronous pagination click actions via delegate layout patterns
            document.addEventListener('click', (e) => {
                const paginationLink = e.target.closest(`${CONFIG.selectors.paginationNav} a:not(.${CONFIG.classes.disabled})`);
                if (!paginationLink) return;

                const targetUrl = paginationLink.getAttribute('href');
                if (targetUrl && targetUrl !== '#' && this.ajaxEnabled) {
                    e.preventDefault();
                    this.applyFiltersAjax(targetUrl);
                }
            });
        }

        /**
         * Synchronization mechanism for native push-state browser history events
         */
        initializeHistoryMapping() {
            window.addEventListener('popstate', () => {
                if (this.ajaxEnabled) {
                    this.applyFiltersAjax(window.location.href, false);
                }
            });
        }

        /**
         * Auto-injects rich WAI-ARIA tokens to improve accessibility compliance parameters
         */
        initializeAccessibilityAttributes() {
            document.querySelectorAll(CONFIG.selectors.accordionNode).forEach(node => {
                const trigger = node.querySelector(CONFIG.selectors.accordionTrigger);
                if (!trigger) return;

                const isActive = node.classList.contains(CONFIG.classes.active);
                trigger.setAttribute('aria-expanded', isActive ? 'true' : 'false');
                trigger.setAttribute('role', 'button');
            });
        }

        /**
         * Synchronizes URL query payload states safely without full-page reloads
         */
        generateTargetUrl() {
            if (!this.form) return window.location.href;

            const formData = new FormData(this.form);
            const params = new URLSearchParams();

            // Retain search query elements from window location contexts
            const currentUrlParams = new URLSearchParams(window.location.search);
            const searchTerms = ['search', 'q', 'category'];
            searchTerms.forEach(term => {
                if (currentUrlParams.has(term)) {
                    params.set(term, currentUrlParams.get(term));
                }
            });

            // Map checkbox multi-values and radio items safely onto parameter arrays
            for (const [key, value] of formData.entries()) {
                if (!value) continue;
                
                // Do not duplicate search terms mapped from parent context
                if (searchTerms.includes(key) && params.has(key)) continue;

                // Append multi-select parameters elegantly
                if (params.has(key)) {
                    const existingVal = params.get(key);
                    // Prevent pushing duplicate scalar parameters
                    if (!existingVal.split(',').includes(value)) {
                        params.set(key, `${existingVal},${value}`);
                    }
                } else {
                    params.set(key, value);
                }
            }

            // Sync sort logic inside custom select wrappers if decoupled from form hierarchy
            const sortSelect = document.querySelector(CONFIG.selectors.sortSelect);
            if (sortSelect && !params.has('sort')) {
                params.set('sort', sortSelect.value);
            }

            return `${window.location.pathname}?${params.toString()}`;
        }

        /**
         * Primary Asynchronous Processing Module for dynamic layout segment swapping
         * @param {string} specificUrl - Explicit override destination target URL string
         * @param {boolean} pushState - Instructs ecosystem whether to record history vectors
         */
        async applyFiltersAjax(specificUrl = null, pushState = true) {
            if (this.isFetching) return;

            const targetUrl = specificUrl || this.generateTargetUrl();
            this.setLoadingState(true);

            try {
                const response = await fetch(targetUrl, {
                    method: 'GET',
                    headers: {
                        'X-Requested-With': 'XMLHttpRequest',
                        'Accept': 'text/html'
                    }
                });

                if (!response.ok) throw new Error(`Discovery fetch aborted with status code: ${response.status}`);

                const htmlString = await response.text();
                this.renderDynamicPayload(htmlString);

                if (pushState) {
                    history.pushState({ url: targetUrl }, '', targetUrl);
                }

                // Announce pagination state mutation for assistive technologies
                this.announceStateChange('Discovery collection view refreshed successfully.');

            } catch (error) {
                console.error('Asynchronous discovery pipeline failure, invoking safe browser fallback:', error);
                // Graceful complete failover mitigation logic
                if (!specificUrl && this.form && typeof this.form._nativeSubmit === 'function') {
                    this.form._nativeSubmit();
                } else {
                    window.location.href = targetUrl;
                }
            } finally {
                this.setLoadingState(false);
            }
        }

        /**
         * Sets visual processing parameters across operational view surfaces
         */
        setLoadingState(isLoading) {
            this.isFetching = isLoading;
            const gallery = document.querySelector(CONFIG.selectors.productGallery);
            const metrics = document.querySelector(CONFIG.selectors.metricsCount);

            if (gallery) {
                if (isLoading) {
                    gallery.classList.add(CONFIG.classes.loading);
                    gallery.style.opacity = '0.5';
                    gallery.style.transition = 'opacity 0.2s ease-in-out';
                } else {
                    gallery.classList.remove(CONFIG.classes.loading);
                    gallery.style.opacity = '1';
                }
            }

            if (metrics && isLoading) {
                metrics.setAttribute('aria-busy', 'true');
            } else if (metrics) {
                metrics.removeAttribute('aria-busy');
            }
        }

        /**
         * Sanitizes and parses raw HTML text payloads to safely mutate targeted DOM nodes
         */
        renderDynamicPayload(htmlString) {
            const parser = new DOMParser();
            const doc = parser.parseFromString(htmlString, 'text/html');

            const targets = [
                CONFIG.selectors.productGallery,
                CONFIG.selectors.paginationNav,
                CONFIG.selectors.metricsCount
            ];

            targets.forEach(selector => {
                const sourceElement = doc.querySelector(selector);
                const localElement = document.querySelector(selector);

                if (sourceElement && localElement) {
                    localElement.innerHTML = sourceElement.innerHTML;
                    
                    // Retain primary CSS container classes for downstream script alignment
                    if (sourceElement.className) {
                        localElement.className = sourceElement.className;
                    }
                } else if (localElement) {
                    // Safe empty state representation rendering fallback
                    localElement.innerHTML = '';
                }
            });

            // Broadcast structural layout adjustments to dependent downstream scripts (e.g. Card Engines)
            if (window.GobindasProductCardEngine && typeof window.GobindasProductCardEngine.refreshAllCards === 'function') {
                window.GobindasProductCardEngine.refreshAllCards();
            }

            // Scroll catalog view up to minimize orientation fragmentation for end consumers
            const galleryNode = document.querySelector(CONFIG.selectors.productGallery);
            if (galleryNode) {
                galleryNode.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
        }

        /**
         * Injects operational accessibility logging frames to update screen readers on dynamic state mutations
         */
        announceStateChange(message) {
            let liveRegion = document.getElementById('plp-accessibility-announcer');
            if (!liveRegion) {
                liveRegion = document.createElement('div');
                liveRegion.id = 'plp-accessibility-announcer';
                liveRegion.setAttribute('aria-live', 'polite');
                liveRegion.setAttribute('aria-atomic', 'true');
                liveRegion.style.position = 'absolute';
                liveRegion.style.width = '1px';
                liveRegion.style.height = '1px';
                liveRegion.style.overflow = 'hidden';
                liveRegion.style.clip = 'rect(1px, 1px, 1px, 1px)';
                document.body.appendChild(liveRegion);
            }
            liveRegion.innerText = message;
        }
    }

    // Launch discovery execution workflows
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => {
            window.GobindasPLPEngine = new ProductDiscoveryEngine();
        });
    } else {
        window.GobindasPLPEngine = new ProductDiscoveryEngine();
    }

})();