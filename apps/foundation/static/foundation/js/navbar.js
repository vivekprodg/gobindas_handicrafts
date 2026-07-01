document.addEventListener("DOMContentLoaded", () => {
    const topBar = document.getElementById("top-bar-node");
    const rotatorEl = document.getElementById("campaign-rotator");
    const nav = document.getElementById("global-nav");
    
    const hamburger = nav ? nav.querySelector(".hamburger") : null;
    const navLinks = nav ? nav.querySelector(".nav-links") : null;
    const navItems = nav ? nav.querySelectorAll(".nav-item") : [];

    const MOBILE_BREAKPOINT = 1024;
    const ROTATION_FADE_MS = 400;
    const DEFAULT_ROTATOR_SPEED = 4000;

    const isMobileViewport = () => window.innerWidth <= MOBILE_BREAKPOINT;

    const safeJsonParse = (value) => {
        if (!value || typeof value !== "string") return null;
        try {
            return JSON.parse(value);
        } catch {
            return null;
        }
    };

    const debounce = (func, wait) => {
        let timeout;
        return function executedFunction(...args) {
            const later = () => {
                clearTimeout(timeout);
                func(...args);
            };
            clearTimeout(timeout);
            timeout = setTimeout(later, wait);
        };
    };

    const getAnnouncementMessages = () => {
        if (!topBar) return [];

        const dataset =
            topBar.dataset.announcements ||
            topBar.dataset.announcementsJson ||
            topBar.getAttribute("data-announcements") ||
            topBar.getAttribute("data-announcements-json");

        const parsed = safeJsonParse(dataset);

        if (Array.isArray(parsed)) {
            return parsed.filter(Boolean).map(String);
        }

        if (parsed && typeof parsed === "object") {
            if (parsed.enabled === false || parsed.is_active === false || parsed.visible === false) {
                return [];
            }

            if (Array.isArray(parsed.campaigns)) {
                return parsed.campaigns.filter(Boolean).map(String);
            }

            if (Array.isArray(parsed.announcement_messages)) {
                return parsed.announcement_messages.filter(Boolean).map(String);
            }
        }

        return [];
    };

    const setupTopHeaderRotator = () => {
        if (!topBar || !rotatorEl) return;

        const messages = getAnnouncementMessages();
        const rotatorSpeedRaw =
            topBar.dataset.rotatorInterval ||
            topBar.getAttribute("data-rotator-interval") ||
            topBar.dataset.rotatorSpeed ||
            topBar.getAttribute("data-rotator-speed");

        const parsedSpeed = Number.parseInt(rotatorSpeedRaw, 10);
        const rotatorSpeed = Number.isFinite(parsedSpeed) && parsedSpeed > 0 
            ? parsedSpeed 
            : DEFAULT_ROTATOR_SPEED;

        if (!messages.length) {
            rotatorEl.style.opacity = "1";
            return;
        }

        rotatorEl.textContent = messages[0];
        rotatorEl.style.opacity = "1";
        rotatorEl.setAttribute("aria-live", "polite");

        if (messages.length === 1) {
            return;
        }

        let currentIndex = 0;
        let timerId = null;

        const rotate = () => {
            rotatorEl.style.opacity = "0";

            window.setTimeout(() => {
                currentIndex = (currentIndex + 1) % messages.length;
                rotatorEl.textContent = messages[currentIndex];
                rotatorEl.style.opacity = "1";
            }, ROTATION_FADE_MS);
        };

        const startRotation = () => {
            if (timerId === null) {
                timerId = window.setInterval(rotate, rotatorSpeed);
            }
        };

        const stopRotation = () => {
            if (timerId !== null) {
                window.clearInterval(timerId);
                timerId = null;
            }
        };

        startRotation();

        document.addEventListener("visibilitychange", () => {
            if (document.hidden) {
                stopRotation();
            } else {
                startRotation();
            }
        });

        topBar.addEventListener("mouseenter", stopRotation);
        topBar.addEventListener("mouseleave", startRotation);
        topBar.addEventListener("focusin", stopRotation);
        topBar.addEventListener("focusout", startRotation);
    };

    const getDirectTrigger = (item) => {
        if (!item) return null;
        for (const child of item.children) {
            if (child.matches("a, span, button")) {
                return child;
            }
        }
        return null;
    };

    const getMegaMenu = (item) => item ? item.querySelector(":scope > .mega-menu") : null;

    const closeAllDesktopStates = () => {
        navItems.forEach((item) => {
            const megaMenu = getMegaMenu(item);
            if (!megaMenu) return;
            item.setAttribute("aria-expanded", "false");
        });
    };

    const resetMobileMenuStyles = () => {
        if (!navLinks) return;
        navLinks.style.display = "";
        navLinks.style.flexDirection = "";
        navLinks.style.gap = "";
        navLinks.style.width = "";
        navLinks.style.height = "";
        navLinks.style.padding = "";
        navLinks.style.alignItems = "";
        navLinks.style.justifyContent = "";
    };

    const closeAllMobileMenus = () => {
        navItems.forEach((item) => {
            const megaMenu = getMegaMenu(item);
            if (!megaMenu) return;

            item.dataset.open = "false";
            item.setAttribute("aria-expanded", "false");
            megaMenu.style.opacity = "";
            megaMenu.style.visibility = "";
            megaMenu.style.transform = "";
            megaMenu.style.pointerEvents = "";
            megaMenu.style.position = "";
            megaMenu.style.left = "";
            megaMenu.style.top = "";
            megaMenu.style.width = "";
            megaMenu.style.zIndex = "";
            megaMenu.style.display = "";
        });
    };

    const openMobileMenu = (item) => {
        const megaMenu = getMegaMenu(item);
        if (!megaMenu) return;

        navItems.forEach((otherItem) => {
            if (otherItem !== item) {
                const otherMenu = getMegaMenu(otherItem);
                if (!otherMenu) return;
                otherItem.dataset.open = "false";
                otherItem.setAttribute("aria-expanded", "false");
                otherMenu.style.opacity = "";
                otherMenu.style.visibility = "";
                otherMenu.style.transform = "";
                otherMenu.style.pointerEvents = "";
                otherMenu.style.position = "";
                otherMenu.style.left = "";
                otherMenu.style.top = "";
                otherMenu.style.width = "";
                otherMenu.style.zIndex = "";
                otherMenu.style.display = "";
            }
        });

        item.dataset.open = "true";
        item.setAttribute("aria-expanded", "true");

        megaMenu.style.display = "flex";
        megaMenu.style.position = "static";
        megaMenu.style.left = "auto";
        megaMenu.style.top = "auto";
        megaMenu.style.width = "100%";
        megaMenu.style.opacity = "1";
        megaMenu.style.visibility = "visible";
        megaMenu.style.transform = "translateY(0)";
        megaMenu.style.pointerEvents = "auto";
        megaMenu.style.zIndex = "1";
    };

    const toggleMobileNav = () => {
        if (!hamburger || !navLinks) return;

        const isOpen = hamburger.getAttribute("aria-expanded") === "true";

        if (isOpen) {
            hamburger.setAttribute("aria-expanded", "false");
            navLinks.dataset.open = "false";
            navLinks.style.display = "none";
            closeAllMobileMenus();
            document.body.style.overflow = "";
            return;
        }

        hamburger.setAttribute("aria-expanded", "true");
        navLinks.dataset.open = "true";
        navLinks.style.display = "flex";
        navLinks.style.flexDirection = "column";
        navLinks.style.gap = "1rem";
        navLinks.style.width = "100%";
        navLinks.style.height = "auto";
        navLinks.style.padding = "1rem 0 0";
        navLinks.style.alignItems = "stretch";
        navLinks.style.justifyContent = "flex-start";
        document.body.style.overflow = "hidden";
    };

    const syncResponsiveState = () => {
        if (isMobileViewport()) {
            if (hamburger && hamburger.getAttribute("aria-expanded") === "true") {
                if (navLinks) {
                    navLinks.style.display = "flex";
                }
                document.body.style.overflow = "hidden";
            } else {
                if (navLinks) {
                    navLinks.style.display = "none";
                }
                document.body.style.overflow = "";
            }
        } else {
            if (navLinks) {
                navLinks.style.display = "";
            }
            if (hamburger) {
                hamburger.setAttribute("aria-expanded", "false");
            }
            document.body.style.overflow = "";
            closeAllMobileMenus();
            closeAllDesktopStates();
            resetMobileMenuStyles();
        }
    };

    setupTopHeaderRotator();

    if (hamburger) {
        if (!hamburger.hasAttribute("role")) {
            hamburger.setAttribute("role", "button");
            hamburger.setAttribute("tabindex", "0");
        }
        hamburger.setAttribute("aria-expanded", "false");
        hamburger.setAttribute("aria-controls", "global-nav");
        
        hamburger.addEventListener("click", (event) => {
            event.preventDefault();
            toggleMobileNav();
        });

        hamburger.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                toggleMobileNav();
            }
        });
    }

    navItems.forEach((item) => {
        const megaMenu = getMegaMenu(item);
        if (!megaMenu) return;

        const trigger = getDirectTrigger(item);
        item.setAttribute("aria-haspopup", "true");
        item.setAttribute("aria-expanded", "false");
        item.dataset.open = "false";

        const openDesktop = () => {
            if (isMobileViewport()) return;
            item.setAttribute("aria-expanded", "true");
        };

        const closeDesktop = () => {
            if (isMobileViewport()) return;
            item.setAttribute("aria-expanded", "false");
        };

        item.addEventListener("mouseenter", openDesktop);
        item.addEventListener("mouseleave", closeDesktop);
        item.addEventListener("focusin", openDesktop);
        item.addEventListener("focusout", (event) => {
            if (!item.contains(event.relatedTarget)) {
                closeDesktop();
            }
        });

        if (trigger) {
            trigger.addEventListener("click", (event) => {
                if (!isMobileViewport()) {
                    return;
                }

                if (trigger.tagName.toLowerCase() === "a") {
                    event.preventDefault();
                }

                const isOpen = item.dataset.open === "true";
                if (isOpen) {
                    item.dataset.open = "false";
                    item.setAttribute("aria-expanded", "false");
                    megaMenu.style.opacity = "";
                    megaMenu.style.visibility = "";
                    megaMenu.style.transform = "";
                    megaMenu.style.pointerEvents = "";
                    megaMenu.style.position = "";
                    megaMenu.style.left = "";
                    megaMenu.style.top = "";
                    megaMenu.style.width = "";
                    megaMenu.style.zIndex = "";
                    megaMenu.style.display = "";
                } else {
                    openMobileMenu(item);
                }
            });

            trigger.addEventListener("keydown", (event) => {
                if (!isMobileViewport()) return;
                if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    trigger.click();
                }
            });
        }

        megaMenu.addEventListener("click", (event) => {
            if (!isMobileViewport()) return;
            const target = event.target;
            if (target && (target.matches("a") || target.closest("a"))) {
                closeAllMobileMenus();
                if (hamburger) {
                    hamburger.setAttribute("aria-expanded", "false");
                }
                if (navLinks) {
                    navLinks.dataset.open = "false";
                    navLinks.style.display = "none";
                }
                document.body.style.overflow = "";
            }
        });
    });

    document.addEventListener("click", (event) => {
        if (!nav) return;
        if (!nav.contains(event.target)) {
            closeAllDesktopStates();
            if (isMobileViewport()) {
                closeAllMobileMenus();
                if (hamburger && hamburger.getAttribute("aria-expanded") === "true") {
                    hamburger.setAttribute("aria-expanded", "false");
                }
                if (navLinks && navLinks.dataset.open === "true") {
                    navLinks.dataset.open = "false";
                    navLinks.style.display = "none";
                }
                document.body.style.overflow = "";
            }
        }
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            closeAllDesktopStates();
            closeAllMobileMenus();
            if (hamburger && hamburger.getAttribute("aria-expanded") === "true") {
                hamburger.setAttribute("aria-expanded", "false");
                hamburger.focus();
            }
            if (navLinks && isMobileViewport()) {
                navLinks.dataset.open = "false";
                navLinks.style.display = "none";
            }
            document.body.style.overflow = "";
        }
    });

    window.addEventListener("resize", debounce(() => {
        syncResponsiveState();
    }, 150));

    syncResponsiveState();
});