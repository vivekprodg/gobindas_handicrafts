import logging
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import (
    LoginView,
    LogoutView,
    PasswordChangeView,
    PasswordResetConfirmView,
    PasswordResetView,
)
from django.db import models, transaction
from django.db.models import Count, QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import (
    CreateView,
    DeleteView,
    ListView,
    TemplateView,
    UpdateView,
    View,
)

from apps.customers.forms import (
    CustomerAddressForm,
    CustomerLoginForm,
    CustomerPasswordChangeForm,
    CustomerPasswordResetForm,
    CustomerProfileForm,
    CustomerRegistrationForm,
)
from apps.customers.models import CustomerAddress, CustomerProfile, SavedCart, Wishlist
from apps.notifications.services import EmailNotificationService

# Lazy order integration safely decoupled from hard model dependencies
try:
    from apps.orders.models import Order
except ImportError:
    Order = None

logger = logging.getLogger(__name__)

# ==============================================================================
# 1. CORE AUTHENTICATION WORKFLOWS
# ==============================================================================
class CustomerRegistrationView(CreateView):
    """
    Manages customer registration supporting both Individual personal accounts and
    Wholesale / Organization / Bulk Supplier business registrations.
    Triggers automated welcome email to user and registration alert email to company admin.
    """
    template_name = "customers/register.html"
    form_class = CustomerRegistrationForm
    success_url = reverse_lazy("customers:dashboard")

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect("customers:dashboard")
        return super().get(request, *args, **kwargs)

    @transaction.atomic
    def form_valid(self, form: CustomerRegistrationForm) -> HttpResponse:
        user = form.save()
        login(self.request, user, backend="django.contrib.auth.backends.ModelBackend")

        profile = getattr(user, "customer_profile", None)

        # Trigger registration emails on transaction commit after user, profile, and address exist in DB
        transaction.on_commit(
            lambda: EmailNotificationService.send_user_registration_emails(user=user, request=self.request)
        )

        if profile and profile.is_business_account:
            messages.success(
                self.request,
                _("Welcome! Your wholesale/organization account has been created. You can now manage bulk orders.")
            )
        else:
            messages.success(self.request, _("Welcome! Your account has been successfully created."))

        return HttpResponseRedirect(self.get_success_url())

class CustomerLoginView(LoginView):
    """
    Handles user login using either username or email address.
    """
    template_name = "customers/login.html"
    form_class = CustomerLoginForm
    redirect_authenticated_user = True

    def get_success_url(self) -> str:
        return self.request.GET.get("next") or reverse_lazy("customers:dashboard")

    def form_valid(self, form: CustomerLoginForm) -> HttpResponse:
        messages.success(self.request, _("Welcome back! You have successfully signed in."))
        return super().form_valid(form)

class CustomerLogoutView(LogoutView):
    """
    Standard secure logout view handler supporting both GET and POST requests.
    Only queues notification messages when an authentic user logs out.
    """
    next_page = reverse_lazy("foundation:home")
    http_method_names = ["get", "post", "options"]

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self.post(request, *args, **kwargs)

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if request.user.is_authenticated:
            messages.info(request, _("You have been successfully logged out."))
        return super().dispatch(request, *args, **kwargs)

class CustomerPasswordResetView(PasswordResetView):
    """
    Dispatches password reset link emails to the customer's email address.
    """
    template_name = "registration/password_reset_form.html"
    email_template_name = "registration/password_reset_email.html"
    subject_template_name = "registration/password_reset_subject.txt"
    form_class = CustomerPasswordResetForm
    success_url = reverse_lazy("customers:password_reset_done")

    def form_valid(self, form: CustomerPasswordResetForm) -> HttpResponse:
        messages.success(
            self.request,
            _("Password reset instructions have been emailed to your address. Please check your inbox.")
        )
        return super().form_valid(form)

class CustomerPasswordResetConfirmView(PasswordResetConfirmView):
    """
    Validates token and uidb64 to update the user password.
    """
    template_name = "registration/password_reset_confirm.html"
    success_url = reverse_lazy("customers:password_reset_complete")

    def form_valid(self, form: Any) -> HttpResponse:
        messages.success(
            self.request,
            _("Your password has been reset successfully! You may now sign in with your new credentials.")
        )
        return super().form_valid(form)

class CustomerPasswordChangeView(LoginRequiredMixin, PasswordChangeView):
    template_name = "customers/password_change.html"
    form_class = CustomerPasswordChangeForm
    success_url = reverse_lazy("customers:dashboard")

    def form_valid(self, form: CustomerPasswordChangeForm) -> HttpResponse:
        messages.success(self.request, _("Your password has been successfully updated."))
        return super().form_valid(form)

# ==============================================================================
# 2. DASHBOARD & PROFILE MANAGEMENT
# ==============================================================================
class CustomerDashboardView(LoginRequiredMixin, TemplateView):
    """
    Main user dashboard displaying account metrics, organization classification, and order history.
    """
    template_name = "customers/dashboard.html"

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = self.request.user

        profile, created = CustomerProfile.objects.get_or_create(user=user)

        counts = CustomerProfile.objects.filter(pk=profile.pk).aggregate(
            addr_count=Count("addresses", filter=models.Q(addresses__is_active=True), distinct=True),
            wish_count=Count("wishlist_items", distinct=True),
            cart_count=Count("saved_carts", distinct=True)
        )

        recent_orders = []
        if Order is not None:
            recent_orders = Order.objects.filter(customer=user).order_by("-created_at")[:3]

        context.update({
            "profile": profile,
            "address_count": counts.get("addr_count", 0),
            "wishlist_count": counts.get("wish_count", 0),
            "saved_cart_count": counts.get("cart_count", 0),
            "recent_orders": recent_orders,
        })
        return context

class CustomerProfileUpdateView(LoginRequiredMixin, UpdateView):
    """
    Updates CustomerProfile demographic details, organization parameters, and linked user records.
    """
    template_name = "customers/profile.html"
    form_class = CustomerProfileForm
    success_url = reverse_lazy("customers:dashboard")

    def get_object(self, queryset: Optional[QuerySet] = None) -> CustomerProfile:
        profile, created = CustomerProfile.objects.get_or_create(user=self.request.user)
        return profile

    @transaction.atomic
    def form_valid(self, form: CustomerProfileForm) -> HttpResponse:
        messages.success(self.request, _("Your profile details have been successfully updated."))
        return super().form_valid(form)

# ==============================================================================
# 3. ADDRESS BOOK MANAGEMENT
# ==============================================================================
class AddressListView(LoginRequiredMixin, ListView):
    template_name = "customers/address-list.html"
    context_object_name = "addresses"

    def get_queryset(self) -> QuerySet[CustomerAddress]:
        profile, created = CustomerProfile.objects.get_or_create(user=self.request.user)
        return CustomerAddress.objects.filter(customer=profile, is_active=True).order_by("-is_default", "-updated_at")

class AddressCreateView(LoginRequiredMixin, CreateView):
    template_name = "customers/address-form.html"
    form_class = CustomerAddressForm
    success_url = reverse_lazy("customers:address_list")

    @transaction.atomic
    def form_valid(self, form: CustomerAddressForm) -> HttpResponse:
        profile, created = CustomerProfile.objects.get_or_create(user=self.request.user)
        form.instance.customer = profile

        if getattr(form.instance, "is_default", False):
            CustomerAddress.objects.filter(customer=profile, is_default=True).update(is_default=False)

        messages.success(self.request, _("New address successfully saved to your address book."))
        return super().form_valid(form)

class AddressUpdateView(LoginRequiredMixin, UpdateView):
    template_name = "customers/address-form.html"
    form_class = CustomerAddressForm
    success_url = reverse_lazy("customers:address_list")

    def get_queryset(self) -> QuerySet[CustomerAddress]:
        profile, created = CustomerProfile.objects.get_or_create(user=self.request.user)
        return CustomerAddress.objects.filter(customer=profile, is_active=True)

    @transaction.atomic
    def form_valid(self, form: CustomerAddressForm) -> HttpResponse:
        profile, created = CustomerProfile.objects.get_or_create(user=self.request.user)
        if getattr(form.instance, "is_default", False):
            CustomerAddress.objects.filter(
                customer=profile,
                is_default=True
            ).exclude(pk=form.instance.pk).update(is_default=False)

        messages.success(self.request, _("Address details have been successfully updated."))
        return super().form_valid(form)

class AddressDeleteView(LoginRequiredMixin, View):
    def post(self, request: HttpRequest, pk: int, *args: Any, **kwargs: Any) -> HttpResponse:
        profile, created = CustomerProfile.objects.get_or_create(user=request.user)
        address = get_object_or_404(CustomerAddress, pk=pk, customer=profile, is_active=True)
        address.is_active = False
        address.is_default = False
        address.save()
        messages.success(request, _("Address successfully deleted."))
        return redirect("customers:address_list")

class AddressSetDefaultShippingView(LoginRequiredMixin, View):
    def post(self, request: HttpRequest, pk: int, *args: Any, **kwargs: Any) -> HttpResponse:
        profile, created = CustomerProfile.objects.get_or_create(user=request.user)
        address = get_object_or_404(CustomerAddress, pk=pk, customer=profile, is_active=True)
        address.address_type = CustomerAddress.AddressType.SHIPPING
        address.is_default = True
        address.save()
        messages.success(request, _("Default shipping address updated."))
        return redirect("customers:address_list")

class AddressSetDefaultBillingView(LoginRequiredMixin, View):
    def post(self, request: HttpRequest, pk: int, *args: Any, **kwargs: Any) -> HttpResponse:
        profile, created = CustomerProfile.objects.get_or_create(user=request.user)
        address = get_object_or_404(CustomerAddress, pk=pk, customer=profile, is_active=True)
        address.address_type = CustomerAddress.AddressType.BILLING
        address.is_default = True
        address.save()
        messages.success(request, _("Default billing address updated."))
        return redirect("customers:address_list")

# ==============================================================================
# 4. WISHLIST MANAGEMENT
# ==============================================================================
class WishlistView(LoginRequiredMixin, ListView):
    template_name = "customers/wishlist.html"
    context_object_name = "wishlist_items"
    paginate_by = 12

    def get_queryset(self) -> QuerySet[Wishlist]:
        profile, created = CustomerProfile.objects.get_or_create(user=self.request.user)
        return Wishlist.objects.filter(customer=profile).select_related("product", "product__category", "product__artisan")

class WishlistAddView(LoginRequiredMixin, View):
    def post(self, request: HttpRequest, product_id: Optional[int] = None, *args: Any, **kwargs: Any) -> HttpResponse:
        target_id = product_id or request.POST.get("product_id")
        if not target_id:
            return JsonResponse({"status": "error", "message": _("Invalid product ID.")}, status=400)

        profile, created = CustomerProfile.objects.get_or_create(user=request.user)
        try:
            from apps.catalog.models import Product
            product = get_object_or_404(Product, pk=target_id)
            _, item_created = Wishlist.objects.get_or_create(customer=profile, product=product)

            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"status": "success", "created": item_created})

            msg = _("Added to your wishlist.") if item_created else _("Item is already in your wishlist.")
            messages.success(request, msg)
            return redirect(request.META.get("HTTP_REFERER", "customers:wishlist"))
        except Exception as e:
            logger.error("Wishlist operation failed: %s", str(e))
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"status": "error", "message": str(e)}, status=500)
            messages.error(request, _("Unable to update wishlist."))
            return redirect("customers:wishlist")

class WishlistRemoveView(LoginRequiredMixin, View):
    def post(self, request: HttpRequest, pk: int, *args: Any, **kwargs: Any) -> HttpResponse:
        profile, created = CustomerProfile.objects.get_or_create(user=request.user)
        wishlist_item = get_object_or_404(Wishlist, pk=pk, customer=profile)
        wishlist_item.delete()

        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"status": "success", "message": _("Item removed from wishlist.")})

        messages.success(request, _("Item removed from your wishlist."))
        return redirect("customers:wishlist")

# ==============================================================================
# 5. SAVED CARTS
# ==============================================================================
class SavedCartListView(LoginRequiredMixin, ListView):
    template_name = "customers/saved-carts.html"
    context_object_name = "saved_carts"

    def get_queryset(self) -> QuerySet[SavedCart]:
        profile, created = CustomerProfile.objects.get_or_create(user=self.request.user)
        return SavedCart.objects.filter(customer=profile).prefetch_related("items", "items__product")

class SavedCartDeleteView(LoginRequiredMixin, DeleteView):
    template_name = "customers/saved_cart_confirm_delete.html"
    success_url = reverse_lazy("customers:saved_cart_list")

    def get_queryset(self) -> QuerySet[SavedCart]:
        profile, created = CustomerProfile.objects.get_or_create(user=self.request.user)
        return SavedCart.objects.filter(customer=profile)

    def form_valid(self, form: Any) -> HttpResponse:
        messages.success(self.request, _("Saved cart removed."))
        return super().form_valid(form)

class SavedCartLoadView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest, pk: int, *args: Any, **kwargs: Any) -> HttpResponse:
        profile, created = CustomerProfile.objects.get_or_create(user=request.user)
        saved_cart = get_object_or_404(SavedCart, pk=pk, customer=profile)

        messages.success(request, _("Cart '%s' loaded into active session.") % saved_cart.name)
        return redirect("customers:saved_cart_list")