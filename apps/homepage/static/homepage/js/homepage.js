/**
 * Gobindas Handicrafts - Auto-Sliding & Interactive Navigation Merchandising Carousel
 */
document.addEventListener('DOMContentLoaded', () => {
    const carousel = document.querySelector('.merch-carousel');
    if (!carousel) return;

    const prevBtn = document.querySelector('.carousel-nav-btn.prev-btn');
    const nextBtn = document.querySelector('.carousel-nav-btn.next-btn');

    let autoSlideTimer = null;
    let isHovered = false;
    let isDown = false;
    let startX, scrollLeft;

    const cardWidth = 320;      // Distance to scroll per click or auto-slide
    const slideInterval = 3500; // Auto-slide timer interval (3.5 seconds)

    // 1. FORWARD / REWIND SLIDE FUNCTIONS
    const slideNext = () => {
        const maxScrollLeft = carousel.scrollWidth - carousel.clientWidth;
        if (carousel.scrollLeft >= maxScrollLeft - 10) {
            // Loop back to start smoothly when reaching the end
            carousel.scrollTo({ left: 0, behavior: 'smooth' });
        } else {
            carousel.scrollBy({ left: cardWidth, behavior: 'smooth' });
        }
    };

    const slidePrev = () => {
        const maxScrollLeft = carousel.scrollWidth - carousel.clientWidth;
        if (carousel.scrollLeft <= 10) {
            // Rewind to the far right if clicking previous at the start
            carousel.scrollTo({ left: maxScrollLeft, behavior: 'smooth' });
        } else {
            carousel.scrollBy({ left: -cardWidth, behavior: 'smooth' });
        }
    };

    // 2. AUTO-SLIDE TIMER CONTROLLER
    const startAutoSlide = () => {
        if (autoSlideTimer) clearInterval(autoSlideTimer);
        autoSlideTimer = setInterval(() => {
            if (isHovered || isDown) return;
            slideNext();
        }, slideInterval);
    };

    const stopAutoSlide = () => {
        if (autoSlideTimer) {
            clearInterval(autoSlideTimer);
            autoSlideTimer = null;
        }
    };

    // 3. ARROW BUTTON CLICK HANDLERS
    if (prevBtn) {
        prevBtn.addEventListener('click', (e) => {
            e.preventDefault();
            stopAutoSlide();
            slidePrev();
            if (!isHovered) startAutoSlide();
        });
    }

    if (nextBtn) {
        nextBtn.addEventListener('click', (e) => {
            e.preventDefault();
            stopAutoSlide();
            slideNext();
            if (!isHovered) startAutoSlide();
        });
    }

    // 4. PAUSE AUTO-SLIDE ON MOUSE HOVER
    const wrapper = carousel.closest('.merch-carousel-wrapper') || carousel;
    wrapper.addEventListener('mouseenter', () => {
        isHovered = true;
        stopAutoSlide();
    });

    wrapper.addEventListener('mouseleave', () => {
        isHovered = false;
        startAutoSlide();
    });

    // 5. MOUSE DRAG / TOUCH SWIPE SUPPORT
    carousel.addEventListener('mousedown', (e) => {
        isDown = true;
        stopAutoSlide();
        startX = e.pageX - carousel.offsetLeft;
        scrollLeft = carousel.scrollLeft;
    });

    carousel.addEventListener('mouseup', () => {
        isDown = false;
        if (!isHovered) startAutoSlide();
    });

    carousel.addEventListener('mousemove', (e) => {
        if (!isDown) return;
        e.preventDefault();
        const x = e.pageX - carousel.offsetLeft;
        const walk = (x - startX) * 2;
        carousel.scrollLeft = scrollLeft - walk;
    });

    // Start auto-sliding on page load
    startAutoSlide();
});