from typing import Any
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import (
    LoginView, 
    LogoutView, 
    PasswordChangeView, 
    PasswordResetConfirmView, 
    PasswordResetView
)
from django.db import transaction
from django.db.models import Count, QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import (
    CreateView, 
    DeleteView, 
    ListView, 
    TemplateView, 
    UpdateView, 
    View
)

from apps.customers.forms import (
    CustomerAddressForm, 
    CustomerLoginForm, 
    CustomerPasswordChangeForm, 
    CustomerProfileForm, 
    CustomerRegistrationForm
)
from apps.customers.models import CustomerAddress, CustomerProfile, SavedCart, Wishlist

# Lazy imports for order integration safely decoupled from hard structural dependencies
try:
    from apps.orders.models import Order
except ImportError:
    Order = None

try:
    from apps.cart.services import get_or_create_cart, add_item_to_cart
except ImportError:
    # Safe fallback if cart app architecture differs
    def get_or_create_cart(request): return None
    def add_item_to_cart(cart, product, variant, quantity): pass

logger = logging.getLogger(__name__)


# ==============================================================================
# 1. CORE AUTHENTICATION WORKFLOWS
# ==============================================================================

class CustomerRegistrationView(CreateView):
    """
    Manages end-to-end customer registration workflows.
    Ensures safe transaction encapsulation alongside automated login upon success.
    """
    template_name = 'registration/register.html'
    form_class = CustomerRegistrationForm
    success_url = reverse_lazy('foundation:home')

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect('customers:dashboard')
        return super().get(request, *args, **kwargs)

    @transaction.atomic
    def form_valid(self, form: CustomerRegistrationForm) -> HttpResponse:
        user = form.save()
        # Explicit login dispatch after successful payload execution
        login(self.request, user)
        messages.success(self.request, _("Welcome! Your account has been successfully created."))
        return HttpResponseRedirect(self.get_success_url())


class CustomerLoginView(LoginView):
    """
    Advanced login integration enabling seamless email or username entry.
    """
    template_name = 'registration/login.html'
    form_class = CustomerLoginForm
    redirect_authenticated_user = True

    def get_success_url(self) -> str:
        return self.request.GET.get('next') or reverse_lazy('customers:dashboard')

    def form_valid(self, form: CustomerLoginForm) -> HttpResponse:
        messages.success(self.request, _("Welcome back! You have successfully logged in."))
        return super().form_valid(form)


class CustomerLogoutView(LogoutView):
    """
    Standard secure logout handler with status messaging.
    """
    next_page = reverse_lazy('foundation:home')

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        messages.info(request, _("You have been successfully logged out."))
        return super().dispatch(request, *args, **kwargs)


class CustomerPasswordResetView(PasswordResetView):
    template_name = 'registration/password_reset_form.html'
    email_template_name = 'registration/password_reset_email.html'
    success_url = reverse_lazy('foundation:password_reset_done')


class CustomerPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = 'registration/password_reset_confirm.html'
    success_url = reverse_lazy('foundation:password_reset_complete')


class CustomerPasswordChangeView(LoginRequiredMixin, PasswordChangeView):
    template_name = 'customers/password_change.html'
    form_class = CustomerPasswordChangeForm
    success_url = reverse_lazy('customers:dashboard')

    def form_valid(self, form: CustomerPasswordChangeForm) -> HttpResponse:
        messages.success(self.request, _("Your password has been successfully updated."))
        return super().form_valid(form)


# ==============================================================================
# 2. CUSTOMER DASHBOARD & PROFILE MANAGEMENT
# ==============================================================================

class CustomerDashboardView(LoginRequiredMixin, TemplateView):
    """
    Primary landing hub for authenticated customer sessions. 
    Protects database stability by resolving analytics parameters through optimized subqueries.
    """
    template_name = 'customers/dashboard.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = self.request.user

        profile = get_object_or_404(
            CustomerProfile.objects.annotate(
                addr_count=Count('addresses', distinct=True),
                wish_count=Count('wishlist_items', distinct=True),
                cart_count=Count('saved_carts', distinct=True)
            ),
            user=user
        )

        recent_orders = []
        if Order:
            recent_orders = Order.objects.filter(user=user).order_by('-created_at')[:3]

        context.update({
            'profile': profile,
            'address_count': profile.addr_count,
            'wishlist_count': profile.wish_count,
            'saved_cart_count': profile.cart_count,
            'recent_orders': recent_orders,
        })
        return context


class CustomerProfileUpdateView(LoginRequiredMixin, UpdateView):
    """
    Safely controls unified form mutation vectors spanning across both User and Profile entities.
    """
    template_name = 'customers/profile_edit.html'
    form_class = CustomerProfileForm
    success_url = reverse_lazy('customers:dashboard')

    def get_object(self, queryset: QuerySet | None = None) -> CustomerProfile:
        return get_object_or_404(CustomerProfile.objects.select_related('user'), user=self.request.user)

    @transaction.atomic
    def form_valid(self, form: CustomerProfileForm) -> HttpResponse:
        messages.success(self.request, _("Your profile information has been successfully updated."))
        return super().form_valid(form)


# ==============================================================================
# 3. ADDRESS BOOK MANAGEMENT
# ==============================================================================

class AddressListView(LoginRequiredMixin, ListView):
    """
    Maintains user isolation to list only operational delivery nodes assigned 
    to the active customer profile.
    """
    template_name = 'customers/address_list.html'
    context_object_name = 'addresses'

    def get_queryset(self) -> QuerySet[CustomerAddress]:
        profile = get_object_or_404(CustomerProfile, user=self.request.user)
        return CustomerAddress.objects.filter(customer=profile, is_active=True).order_by('-is_default', '-updated_at')


class AddressCreateView(LoginRequiredMixin, CreateView):
    """
    Provisions new customer address rows and enforces profile relationship mapping.
    """
    template_name = 'customers/address_form.html'
    form_class = CustomerAddressForm
    success_url = reverse_lazy('customers:address_list')

    def form_valid(self, form: CustomerAddressForm) -> HttpResponse:
        profile = get_object_or_404(CustomerProfile, user=self.request.user)
        form.instance.customer = profile
        messages.success(self.request, _("New address successfully added to your address book."))
        return super().form_valid(form)


class AddressUpdateView(LoginRequiredMixin, UpdateView):
    """
    Safeguards mutation vectors to prevent arbitrary lateral assignment manipulation.
    """
    template_name = 'customers/address_form.html'
    form_class = CustomerAddressForm
    success_url = reverse_lazy('customers:address_list')

    def get_queryset(self) -> QuerySet[CustomerAddress]:
        profile = get_object_or_404(CustomerProfile, user=self.request.user)
        return CustomerAddress.objects.filter(customer=profile, is_active=True)

    def form_valid(self, form: CustomerAddressForm) -> HttpResponse:
        messages.success(self.request, _("Address details have been successfully updated."))
        return super().form_valid(form)


class AddressDeleteView(LoginRequiredMixin, DeleteView):
    """
    Implements secure contextual soft-deletion of physical nodes to retain historical invoice integrity.
    """
    template_name = 'customers/address_confirm_delete.html'
    success_url = reverse_lazy('customers:address_list')

    def get_queryset(self) -> QuerySet[CustomerAddress]:
        profile = get_object_or_404(CustomerProfile, user=self.request.user)
        return CustomerAddress.objects.filter(customer=profile, is_active=True)

    def form_valid(self, form: Any) -> HttpResponse:
        address = self.get_object()
        # Soft delete mechanism
        address.is_active = False
        address.is_default = False
        address.save()
        messages.success(self.request, _("Address has been successfully removed from your active list."))
        return HttpResponseRedirect(self.success_url)


# ==============================================================================
# 4. WISHLIST MANAGEMENT
# ==============================================================================

class WishlistView(LoginRequiredMixin, ListView):
    """
    Assembles lazy product object references isolated by active session identity.
    """
    template_name = 'customers/wishlist.html'
    context_object_name = 'wishlist_items'
    paginate_by = 12

    def get_queryset(self) -> QuerySet[Wishlist]:
        profile = get_object_or_404(CustomerProfile, user=self.request.user)
        return Wishlist.objects.filter(customer=profile).select_related('product', 'product__category', 'product__primary_image')


class WishlistAddView(LoginRequiredMixin, View):
    """
    Dynamically orchestrates product favoriting flows securely supporting AJAX methodologies.
    """
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        product_id = request.POST.get('product_id')
        if not product_id:
            return JsonResponse({'status': 'error', 'message': _("Invalid product identifier.")}, status=400)

        profile = get_object_or_404(CustomerProfile, user=request.user)
        
        try:
            from apps.catalog.models import Product
            product = get_object_or_404(Product, pk=product_id)
            wishlist_item, created = Wishlist.objects.get_or_create(customer=profile, product=product)
            
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'status': 'success', 'created': created})
                
            msg = _("Added to your wishlist.") if created else _("Product is already in your wishlist.")
            messages.success(request, msg)
            return redirect(request.META.get('HTTP_REFERER', 'customers:wishlist'))
            
        except Exception as e:
            logger.error("Wishlist insertion failed: %s", str(e))
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'status': 'error', 'message': str(e)}, status=500)
            messages.error(request, _("An error occurred while managing your wishlist."))
            return redirect('customers:wishlist')


class WishlistRemoveView(LoginRequiredMixin, View):
    """
    Tears down wishlist entries conditionally matching session identity boundaries.
    """
    def post(self, request: HttpRequest, pk: int, *args: Any, **kwargs: Any) -> HttpResponse:
        profile = get_object_or_404(CustomerProfile, user=request.user)
        wishlist_item = get_object_or_404(Wishlist, pk=pk, customer=profile)
        wishlist_item.delete()
        
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'status': 'success', 'message': _("Removed from wishlist.")})
            
        messages.success(request, _("Product successfully removed from your wishlist."))
        return redirect('customers:wishlist')


# ==============================================================================
# 5. SAVED CART WORKFLOWS
# ==============================================================================

class SavedCartListView(LoginRequiredMixin, ListView):
    """
    Renders historical persistent cart vectors isolating ownership parameters.
    """
    template_name = 'customers/saved_carts.html'
    context_object_name = 'saved_carts'

    def get_queryset(self) -> QuerySet[SavedCart]:
        profile = get_object_or_404(CustomerProfile, user=self.request.user)
        return SavedCart.objects.filter(customer=profile).prefetch_related('items', 'items__product')


class SavedCartDeleteView(LoginRequiredMixin, DeleteView):
    """
    Secures absolute deletion of persistent operational carts by cross-matching profile boundaries.
    """
    template_name = 'customers/saved_cart_confirm_delete.html'
    success_url = reverse_lazy('customers:saved_cart_list')

    def get_queryset(self) -> QuerySet[SavedCart]:
        profile = get_object_or_404(CustomerProfile, user=self.request.user)
        return SavedCart.objects.filter(customer=profile)

    def form_valid(self, form: Any) -> HttpResponse:
        messages.success(self.request, _("Saved cart has been permanently removed."))
        return super().form_valid(form)