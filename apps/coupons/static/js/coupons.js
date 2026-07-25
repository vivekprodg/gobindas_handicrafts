/**
 * Ultra-Premium ES6 Progressive Controller for Coupons & Promotional Vouchers.
 */
document.addEventListener("DOMContentLoaded", () => {
    const widget = document.querySelector("[data-coupons-widget]");
    if (!widget) return;

    const applyForm = widget.querySelector("[data-coupon-apply-form]");
    const input = widget.querySelector("[data-coupon-input]");
    const submitBtn = widget.querySelector("[data-coupon-submit-btn]");
    const feedback = widget.querySelector("[data-coupon-feedback]");
    const removeForm = widget.querySelector("[data-remove-coupon-form]");
    const drawerHeader = widget.querySelector("[data-drawer-toggle]");
    const drawer = widget.querySelector("[data-public-coupons-drawer]");

    // Extract CSRF Token cleanly from DOM
    const getCsrfToken = () => {
        const csrfInput = widget.querySelector("input[name='csrfmiddlewaretoken']");
        return csrfInput ? csrfInput.value : "";
    };

    // Toggle Public Coupons Drawer
    if (drawerHeader && drawer) {
        drawerHeader.addEventListener("click", () => {
            drawer.classList.toggle("expanded");
            const list = drawer.querySelector("[data-coupons-list]");
            if (list) {
                list.style.display = drawer.classList.contains("expanded") ? "flex" : "none";
            }
        });
    }

    // Show Feedback Message
    const showFeedback = (message, isSuccess = true) => {
        if (!feedback) return;
        feedback.textContent = message;
        feedback.className = `coupon-feedback-message ${isSuccess ? "success" : "error"}`;
        feedback.style.display = "block";
    };

    // Set Submit Button Loading State
    const setLoading = (isLoading) => {
        if (!submitBtn) return;
        const text = submitBtn.querySelector(".btn-text");
        const spinner = submitBtn.querySelector(".btn-spinner");

        if (isLoading) {
            submitBtn.disabled = true;
            if (text) text.style.display = "none";
            if (spinner) spinner.style.display = "inline-block";
        } else {
            submitBtn.disabled = false;
            if (text) text.style.display = "inline";
            if (spinner) spinner.style.display = "none";
        }
    };

    // Handle AJAX Apply Coupon
    if (applyForm) {
        applyForm.addEventListener("submit", async (e) => {
            e.preventDefault();
            const code = input ? input.value.trim() : "";
            if (!code) {
                showFeedback("Please enter a valid coupon code.", false);
                return;
            }

            setLoading(true);
            try {
                const response = await fetch(applyForm.action, {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/x-www-form-urlencoded",
                        "X-Requested-With": "XMLHttpRequest",
                        "X-CSRFToken": getCsrfToken(),
                    },
                    body: new URLSearchParams({ coupon_code: code }),
                });

                const data = await response.json();
                if (data.success) {
                    showFeedback(data.message || "Coupon applied successfully!", true);
                    setTimeout(() => window.location.reload(), 600);
                } else {
                    showFeedback(data.message || "Invalid or ineligible coupon code.", false);
                }
            } catch (err) {
                showFeedback("Connection error. Please try again.", false);
            } finally {
                setLoading(false);
            }
        });
    }

    // Quick Apply from Public Coupon List
    const quickApplyButtons = widget.querySelectorAll("[data-apply-code]");
    quickApplyButtons.forEach((btn) => {
        btn.addEventListener("click", () => {
            const code = btn.getAttribute("data-apply-code");
            if (input) {
                input.value = code;
                if (applyForm) {
                    applyForm.dispatchEvent(new Event("submit", { cancelable: true }));
                }
            }
        });
    });

    // Handle AJAX Remove Coupon
    if (removeForm) {
        removeForm.addEventListener("submit", async (e) => {
            e.preventDefault();
            try {
                const response = await fetch(removeForm.action, {
                    method: "POST",
                    headers: {
                        "X-Requested-With": "XMLHttpRequest",
                        "X-CSRFToken": getCsrfToken(),
                    },
                });

                const data = await response.json();
                if (data.success) {
                    window.location.reload();
                }
            } catch (err) {
                console.error("Failed to remove coupon:", err);
            }
        });
    }
});