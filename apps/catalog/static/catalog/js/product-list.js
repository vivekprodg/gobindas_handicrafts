/**
 * Gobindas Handicrafts - Advanced Product Discovery & Filtering Engine
 * Powers real-time product discovery, multi-value array parameter serialization,
 * progressive AJAX filtering, URL state mapping, accessible accordions, and active chip management.
 * 
 * @module GobindasPLPEngine
 * @version 3.1.0
 */

(function () {
    'use strict';

    const CONFIG = {
        selectors: {
            form: '#catalog-filter-form',
            priceMinInput: '#minPriceInput, #searchMinPrice',
            priceMaxInput: '#maxPriceInput, #searchMaxPrice',
            accordionTrigger: '.filter-node-trigger',
            accordionNode: '.filter-accordion-node',
            productGallery: '#main-product-gallery, .product-showcase-matrix',
            paginationNav: '.catalog-pagination-nav',
            metricsCount: '.product-metrics-count',
            sortSelect: '#catalog-sort-select',
            activeChipsWorkspace: '.active-filters-workspace',
            chipRemoveBtn: '.chip-remove-btn'
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
            this.ajaxEnabled = true;
            this.isFetching = false;

            this.init();
        }

        init() {
            this.initializeAccordions();
            this.initializeFormInterception();
            this.initializeHistoryMapping();
            this.initializeChipHandlers();
            this.initializeAccessibilityAttributes();
        }

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

        initializeChipHandlers() {
            document.addEventListener('click', (e) => {
                const removeBtn = e.target.closest(CONFIG.selectors.chipRemoveBtn);
                if (!removeBtn) return;

                e.preventDefault();
                const targetUrl = removeBtn.getAttribute('href');
                if (targetUrl && targetUrl !== '#' && this.ajaxEnabled) {
                    this.applyFiltersAjax(targetUrl);
                }
            });
        }

        initializeFormInterception() {
            if (!this.form) return;

            this.form._nativeSubmit = this.form.submit;

            this.form.submit = () => {
                if (this.ajaxEnabled) {
                    this.applyFiltersAjax();
                } else {
                    this.form._nativeSubmit();
                }
            };

            this.form.addEventListener('submit', (e) => {
                if (this.ajaxEnabled) {
                    e.preventDefault();
                    this.applyFiltersAjax();
                }
            });

            const sortSelect = document.querySelector(CONFIG.selectors.sortSelect);
            if (sortSelect) {
                sortSelect.addEventListener('change', () => {
                    if (this.ajaxEnabled) {
                        this.applyFiltersAjax();
                    }
                });
            }

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

        initializeHistoryMapping() {
            window.addEventListener('popstate', () => {
                if (this.ajaxEnabled) {
                    this.applyFiltersAjax(window.location.href, false);
                }
            });
        }

        initializeAccessibilityAttributes() {
            document.querySelectorAll(CONFIG.selectors.accordionNode).forEach(node => {
                const trigger = node.querySelector(CONFIG.selectors.accordionTrigger);
                if (!trigger) return;

                const isActive = node.classList.contains(CONFIG.classes.active);
                trigger.setAttribute('aria-expanded', isActive ? 'true' : 'false');
                trigger.setAttribute('role', 'button');
            });
        }

        generateTargetUrl() {
            if (!this.form) return window.location.href;

            const formData = new FormData(this.form);
            const params = new URLSearchParams();

            // Preserve search query if present
            const currentUrlParams = new URLSearchParams(window.location.search);
            if (currentUrlParams.has('q')) {
                params.set('q', currentUrlParams.get('q'));
            }

            for (const [key, value] of formData.entries()) {
                if (!value || value === '') continue;

                // Handle multi-value fields cleanly
                if (params.has(key)) {
                    params.append(key, value);
                } else {
                    params.set(key, value);
                }
            }

            const sortSelect = document.querySelector(CONFIG.selectors.sortSelect);
            if (sortSelect && sortSelect.value && !params.has('sort')) {
                params.set('sort', sortSelect.value);
            }

            return `${window.location.pathname}?${params.toString()}`;
        }

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

                this.announceStateChange('Discovery collection view refreshed successfully.');

            } catch (error) {
                console.error('Asynchronous discovery pipeline failure:', error);
                if (!specificUrl && this.form && typeof this.form._nativeSubmit === 'function') {
                    this.form._nativeSubmit();
                } else {
                    window.location.href = targetUrl;
                }
            } finally {
                this.setLoadingState(false);
            }
        }

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

        renderDynamicPayload(htmlString) {
            const parser = new DOMParser();
            const doc = parser.parseFromString(htmlString, 'text/html');

            const targets = [
                CONFIG.selectors.productGallery,
                CONFIG.selectors.paginationNav,
                CONFIG.selectors.metricsCount,
                CONFIG.selectors.activeChipsWorkspace,
                CONFIG.selectors.form
            ];

            targets.forEach(selector => {
                const sourceElement = doc.querySelector(selector);
                const localElement = document.querySelector(selector);

                if (sourceElement && localElement) {
                    localElement.innerHTML = sourceElement.innerHTML;
                    if (sourceElement.className) {
                        localElement.className = sourceElement.className;
                    }
                } else if (localElement) {
                    localElement.innerHTML = '';
                }
            });

            // Re-bind form pointer if form was replaced
            this.form = document.querySelector(CONFIG.selectors.form);

            if (window.GobindasProductCardEngine && typeof window.GobindasProductCardEngine.refreshAllCards === 'function') {
                window.GobindasProductCardEngine.refreshAllCards();
            }

            const galleryNode = document.querySelector(CONFIG.selectors.productGallery);
            if (galleryNode) {
                galleryNode.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
        }

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

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => {
            window.GobindasPLPEngine = new ProductDiscoveryEngine();
        });
    } else {
        window.GobindasPLPEngine = new ProductDiscoveryEngine();
    }
})();