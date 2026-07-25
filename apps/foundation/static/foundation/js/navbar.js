/**
 * Global Dynamic Navigation Bar Controller
 */
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
        const dataset = topBar.dataset.announcements || topBar.getAttribute("data-announcements");
        const parsed = safeJsonParse(dataset);

        if (Array.isArray(parsed)) {
            return parsed.filter(Boolean).map(String);
        }
        return [];
    };

    const setupTopHeaderRotator = () => {
        if (!topBar || !rotatorEl) return;

        const messages = getAnnouncementMessages();
        const rotatorSpeedRaw = topBar.dataset.rotatorInterval || topBar.getAttribute("data-rotator-interval");
        const parsedSpeed = Number.parseInt(rotatorSpeedRaw, 10);
        const rotatorSpeed = Number.isFinite(parsedSpeed) && parsedSpeed > 0 ? parsedSpeed : DEFAULT_ROTATOR_SPEED;

        if (!messages.length) {
            rotatorEl.style.opacity = "1";
            return;
        }

        rotatorEl.textContent = messages[0];
        rotatorEl.style.opacity = "1";
        rotatorEl.setAttribute("aria-live", "polite");

        if (messages.length === 1) return;

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
            if (document.hidden) stopRotation();
            else startRotation();
        });

        topBar.addEventListener("mouseenter", stopRotation);
        topBar.addEventListener("mouseleave", startRotation);
        topBar.addEventListener("focusin", stopRotation);
        topBar.addEventListener("focusout", startRotation);
    };

    const getDirectTrigger = (item) => {
        if (!item) return null;
        for (const child of item.children) {
            if (child.matches("a, span, button")) return child;
        }
        return null;
    };

    const getMegaMenu = (item) => item ? item.querySelector(":scope > .mega-menu") : null;

    const closeAllDesktopStates = () => {
        navItems.forEach((item) => {
            if (getMegaMenu(item)) {
                item.setAttribute("aria-expanded", "false");
            }
        });
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
            megaMenu.style.display = "";
        });
    };

    const openMobileMenu = (item) => {
        const megaMenu = getMegaMenu(item);
        if (!megaMenu) return;

        closeAllMobileMenus();

        item.dataset.open = "true";
        item.setAttribute("aria-expanded", "true");
        megaMenu.style.display = "flex";
        megaMenu.style.position = "static";
        megaMenu.style.width = "100%";
        megaMenu.style.opacity = "1";
        megaMenu.style.visibility = "visible";
        megaMenu.style.transform = "translateY(0)";
        megaMenu.style.pointerEvents = "auto";
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
        navLinks.style.padding = "1rem 0 0";
        document.body.style.overflow = "hidden";
    };

    const syncResponsiveState = () => {
        if (isMobileViewport()) {
            if (hamburger && hamburger.getAttribute("aria-expanded") === "true") {
                if (navLinks) navLinks.style.display = "flex";
                document.body.style.overflow = "hidden";
            } else {
                if (navLinks) navLinks.style.display = "none";
                document.body.style.overflow = "";
            }
        } else {
            if (navLinks) navLinks.style.display = "";
            if (hamburger) hamburger.setAttribute("aria-expanded", "false");
            document.body.style.overflow = "";
            closeAllMobileMenus();
            closeAllDesktopStates();
        }
    };

    setupTopHeaderRotator();

    if (hamburger) {
        hamburger.setAttribute("role", "button");
        hamburger.setAttribute("tabindex", "0");
        hamburger.setAttribute("aria-expanded", "false");
        
        hamburger.addEventListener("click", (e) => {
            e.preventDefault();
            toggleMobileNav();
        });

        hamburger.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
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
            if (!isMobileViewport()) item.setAttribute("aria-expanded", "true");
        };

        const closeDesktop = () => {
            if (!isMobileViewport()) item.setAttribute("aria-expanded", "false");
        };

        item.addEventListener("mouseenter", openDesktop);
        item.addEventListener("mouseleave", closeDesktop);
        item.addEventListener("focusin", openDesktop);
        item.addEventListener("focusout", (e) => {
            if (!item.contains(e.relatedTarget)) closeDesktop();
        });

        if (trigger) {
            trigger.addEventListener("click", (e) => {
                if (!isMobileViewport()) return;
                if (trigger.tagName.toLowerCase() === "a") e.preventDefault();

                if (item.dataset.open === "true") closeAllMobileMenus();
                else openMobileMenu(item);
            });
        }
    });

    document.addEventListener("click", (e) => {
        if (nav && !nav.contains(e.target)) {
            closeAllDesktopStates();
            if (isMobileViewport()) {
                closeAllMobileMenus();
                if (hamburger) hamburger.setAttribute("aria-expanded", "false");
                if (navLinks) {
                    navLinks.dataset.open = "false";
                    navLinks.style.display = "none";
                }
                document.body.style.overflow = "";
            }
        }
    });

    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
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

    window.addEventListener("resize", debounce(syncResponsiveState, 150));
    syncResponsiveState();
});