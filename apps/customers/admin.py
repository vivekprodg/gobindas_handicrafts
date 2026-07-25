from django.contrib import admin
from django.db import models
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.customers.models import (
    CustomerAddress,
    CustomerProfile,
    SavedCart,
    SavedCartItem,
    SocialAccountMetadata,
    Wishlist,
)

class CustomerAddressInline(admin.TabularInline):
    model = CustomerAddress
    extra = 0
    fields = ['full_name', 'phone_number', 'address_type', 'city', 'country', 'is_default', 'is_active']
    readonly_fields = ['created_at', 'updated_at']
    can_delete = True
    show_change_link = True

class WishlistInline(admin.TabularInline):
    model = Wishlist
    extra = 0
    fields = ['product', 'created_at']
    readonly_fields = ['created_at']
    raw_id_fields = ['product']
    can_delete = True

class SavedCartInline(admin.TabularInline):
    model = SavedCart
    extra = 0
    fields = ['name', 'created_at', 'updated_at']
    readonly_fields = ['created_at', 'updated_at']
    show_change_link = True
    can_delete = True

class SocialAccountInline(admin.TabularInline):
    model = SocialAccountMetadata
    extra = 0
    fields = ['provider', 'provider_uid', 'linked_at', 'last_synced_at']
    readonly_fields = ['linked_at', 'last_synced_at']
    can_delete = True

class SavedCartItemInline(admin.TabularInline):
    model = SavedCartItem
    extra = 0
    fields = ['product', 'variant', 'quantity', 'created_at', 'updated_at']
    readonly_fields = ['created_at', 'updated_at']
    raw_id_fields = ['product', 'variant']
    can_delete = True

@admin.register(CustomerProfile)
class CustomerProfileAdmin(admin.ModelAdmin):
    list_display = [
        'full_name',
        'account_type_badge',
        'company_name_display',
        'get_username',
        'get_email',
        'phone_number',
        'tax_id_number',
        'country_of_incorporation',
        'b2b_approval_badge',
        'address_count',
        'wishlist_count',
        'created_at',
    ]
    list_filter = [
        'account_type',
        'is_approved_b2b',
        'country_of_incorporation',
        'business_type',
        'gender',
        'newsletter_subscribed',
        'created_at',
    ]
    search_fields = [
        'company_name',
        'tax_id_number',
        'business_registration_number',
        'user__first_name',
        'user__last_name',
        'user__username',
        'user__email',
        'phone_number',
    ]
    raw_id_fields = ['user']
    list_select_related = ['user']
    list_per_page = 50
    date_hierarchy = 'created_at'
    ordering = ['-created_at']
    readonly_fields = ['created_at', 'updated_at']
    inlines = [CustomerAddressInline, WishlistInline, SavedCartInline, SocialAccountInline]
    actions = ['approve_selected_b2b_accounts', 'revoke_selected_b2b_accounts']

    fieldsets = (
        (_("Account Classification"), {
            'fields': ('user', 'account_type', 'is_approved_b2b', 'phone_number', 'avatar')
        }),
        (_("Organization & B2B Wholesaler Profile"), {
            'fields': (
                'company_name',
                'business_type',
                'tax_id_number',
                'business_registration_number',
                'country_of_incorporation',
                'business_website',
            ),
            'classes': ('collapse',),
            'description': _("Company legal and tax identification details for Wholesalers, Bulk Suppliers, and Organizations.")
        }),
        (_("Personal Demographics"), {
            'fields': ('date_of_birth', 'gender', 'preferred_language', 'newsletter_subscribed')
        }),
        (_("Metadata"), {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('user').annotate(
            _address_count=models.Count('addresses', filter=models.Q(addresses__is_active=True), distinct=True),
            _wishlist_count=models.Count('wishlist_items', distinct=True),
            _saved_cart_count=models.Count('saved_carts', distinct=True),
        )

    @admin.display(description=_("Full Name"), ordering='user__first_name')
    def full_name(self, obj: CustomerProfile) -> str:
        return f"{obj.user.first_name} {obj.user.last_name}".strip() or obj.user.username

    @admin.display(description=_("Account Type"), ordering='account_type')
    def account_type_badge(self, obj: CustomerProfile) -> str:
        colors = {
            CustomerProfile.AccountType.INDIVIDUAL: ("#2C2520", "#F5F1ED"),
            CustomerProfile.AccountType.WHOLESALE: ("#1B5E20", "#E8F5E9"),
            CustomerProfile.AccountType.ORGANIZATION: ("#0D47A1", "#E3F2FD"),
        }
        fg, bg = colors.get(obj.account_type, ("#2C2520", "#F5F1ED"))
        return format_html(
            '<span style="background:{}; color:{}; padding:3px 10px; border-radius:12px; font-weight:700; font-size:11px; text-transform:uppercase;">{}</span>',
            bg, fg, obj.get_account_type_display()
        )

    @admin.display(description=_("Company / Organization"), ordering='company_name')
    def company_name_display(self, obj: CustomerProfile) -> str:
        return obj.company_name if obj.company_name else "—"

    @admin.display(description=_("B2B Status"), ordering='is_approved_b2b')
    def b2b_approval_badge(self, obj: CustomerProfile) -> str:
        if not obj.is_business_account:
            return format_html('<span style="color:#888;">N/A</span>')
        if obj.is_approved_b2b:
            return format_html('<span style="color:#2E7D32; font-weight:bold;">Approved</span>')
        return format_html('<span style="color:#C62828; font-weight:bold;">Pending Approval</span>')

    @admin.display(description=_("Username"), ordering='user__username')
    def get_username(self, obj: CustomerProfile) -> str:
        return obj.user.username

    @admin.display(description=_("Email"), ordering='user__email')
    def get_email(self, obj: CustomerProfile) -> str:
        return obj.user.email

    @admin.display(description=_("Addresses"), ordering='_address_count')
    def address_count(self, obj: CustomerProfile) -> int:
        return getattr(obj, '_address_count', 0)

    @admin.display(description=_("Wishlist"), ordering='_wishlist_count')
    def wishlist_count(self, obj: CustomerProfile) -> int:
        return getattr(obj, '_wishlist_count', 0)

    @admin.action(description=_("Approve selected B2B / Wholesale accounts"))
    def approve_selected_b2b_accounts(self, request, queryset):
        updated = queryset.filter(account_type__in=[
            CustomerProfile.AccountType.WHOLESALE, CustomerProfile.AccountType.ORGANIZATION
        ]).update(is_approved_b2b=True)
        self.message_user(request, _(f"Successfully approved {updated} B2B / Organization account(s)."))

    @admin.action(description=_("Revoke B2B approval for selected accounts"))
    def revoke_selected_b2b_accounts(self, request, queryset):
        updated = queryset.update(is_approved_b2b=False)
        self.message_user(request, _(f"Revoked B2B status for {updated} account(s)."))

@admin.register(CustomerAddress)
class CustomerAddressAdmin(admin.ModelAdmin):
    list_display = [
        'customer',
        'full_name',
        'phone_number',
        'address_type',
        'city',
        'country',
        'is_default',
        'is_active',
        'created_at',
    ]
    list_filter = ['address_type', 'is_default', 'is_active', 'country', 'created_at']
    search_fields = ['full_name', 'phone_number', 'address_line_1', 'city', 'state_or_province', 'postal_code', 'country']
    raw_id_fields = ['customer']
    list_select_related = ['customer', 'customer__user']
    list_per_page = 50
    ordering = ['-created_at']
    readonly_fields = ['created_at', 'updated_at']

@admin.register(Wishlist)
class WishlistAdmin(admin.ModelAdmin):
    list_display = ['customer', 'product', 'created_at']
    search_fields = ['customer__user__username', 'customer__user__email', 'product__title']
    raw_id_fields = ['customer', 'product']
    list_select_related = ['customer', 'customer__user', 'product']

@admin.register(SavedCart)
class SavedCartAdmin(admin.ModelAdmin):
    list_display = ['customer', 'name', 'created_at', 'updated_at']
    search_fields = ['customer__user__username', 'name']
    raw_id_fields = ['customer']
    inlines = [SavedCartItemInline]

@admin.register(SocialAccountMetadata)
class SocialAccountMetadataAdmin(admin.ModelAdmin):
    list_display = ['customer', 'provider', 'provider_uid', 'linked_at']
    list_filter = ['provider', 'linked_at']
    raw_id_fields = ['customer']