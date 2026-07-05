from __future__ import annotations

from django import forms
from django.urls import reverse_lazy
from django.views.generic import TemplateView, FormView, CreateView
from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.views import LoginView, LogoutView, PasswordResetView, PasswordResetConfirmView
from django.shortcuts import render
from django.http import HttpResponse
from django.template.exceptions import TemplateDoesNotExist

from .services import get_foundation_cms_payload

class HomePageView(TemplateView):
    template_name = "home.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cms_payload = get_foundation_cms_payload(use_cache=True)
        context.update(
            {
                "site_settings": cms_payload["site_settings"],
                "header_bar": cms_payload["header_bar"],
                "navbar_items": cms_payload["navbar_items"],
                "page_name": "home",
                "footer_data": cms_payload["footer"],
                "footer_logo": cms_payload["footer"]["brand"]["logo_url"] if cms_payload["footer"] and cms_payload["footer"].get("brand") else None,
            }
        )
        return context

class StoreLocatorView(TemplateView):
    template_name = "foundation/store_locator.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cms_payload = get_foundation_cms_payload(use_cache=True)
        context.update({
            "page_title": "Find a Store | Gobindas Handicrafts",
            "meta_description": "Locate our showrooms and authorized retailers worldwide.",
            "site_settings": cms_payload["site_settings"],
            "header_bar": cms_payload["header_bar"],
            "navbar_items": cms_payload["navbar_items"],
            "footer_data": cms_payload["footer"],
            "footer_logo": cms_payload["footer"]["brand"]["logo_url"] if cms_payload["footer"] and cms_payload["footer"].get("brand") else None,
            "stores": [
                {
                    "id": 1,
                    "name": "Jaipur Heritage Showroom",
                    "address": "12, Amer Road, Jaipur, Rajasthan 302002",
                    "phone": "+91 141 263 1234",
                    "hours": "Mon - Sun: 10:00 AM - 8:00 PM",
                },
                {
                    "id": 2,
                    "name": "New Delhi Flagship Store",
                    "address": "Block E, Connaught Place, New Delhi 110001",
                    "phone": "+91 11 4151 5678",
                    "hours": "Mon - Sat: 11:00 AM - 9:00 PM",
                },
                {
                    "id": 3,
                    "name": "Udaipur Lakefront Gallery",
                    "address": "45, Lal Ghat Road, Udaipur, Rajasthan 313001",
                    "phone": "+91 294 242 9876",
                    "hours": "Mon - Sun: 10:00 AM - 7:00 PM",
                }
            ]
        })
        return context

class TrackOrderForm(forms.Form):
    order_number = forms.CharField(
        max_length=50,
        label="Order Number",
        widget=forms.TextInput(attrs={"placeholder": "e.g. GH-123456", "class": "form-input"}),
    )
    email_or_phone = forms.CharField(
        max_length=100,
        label="Email or Phone Number",
        widget=forms.TextInput(attrs={"placeholder": "e.g. email@example.com", "class": "form-input"}),
    )

class TrackOrderView(FormView):
    template_name = "foundation/track_order.html"
    form_class = TrackOrderForm
    success_url = reverse_lazy("foundation:track_order")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cms_payload = get_foundation_cms_payload(use_cache=True)
        context.update({
            "page_title": "Track Your Order | Gobindas Handicrafts",
            "meta_description": "Track the shipping status of your premium handcrafted products.",
            "site_settings": cms_payload["site_settings"],
            "header_bar": cms_payload["header_bar"],
            "navbar_items": cms_payload["navbar_items"],
            "footer_data": cms_payload["footer"],
            "footer_logo": cms_payload["footer"]["brand"]["logo_url"] if cms_payload["footer"] and cms_payload["footer"].get("brand") else None,
        })
        return context

    def form_valid(self, form):
        context = self.get_context_data(form=form)
        order_number = form.cleaned_data["order_number"]
        email_or_phone = form.cleaned_data["email_or_phone"]
        
        # Placeholder mock results easily connected to backend models/services later
        context["tracking_result"] = {
            "order_number": order_number,
            "status": "In Transit (Out for Delivery)",
            "estimated_delivery": "June 28, 2026",
            "carrier": "Artisan Express",
            "origin": "Workshop Lineages of Jaipur, Rajasthan",
            "destination": email_or_phone,
            "steps": [
                {"date": "2026-06-25", "title": "Shipped from Jaipur Hub", "done": True},
                {"date": "2026-06-26", "title": "Arrived at Local Sorting Center", "done": True},
                {"date": "2026-06-27", "title": "Out for Delivery", "done": False},
            ]
        }
        return self.render_to_response(context)

class CustomLoginView(LoginView):
    template_name = "registration/login.html"
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cms_payload = get_foundation_cms_payload(use_cache=True)
        context.update({
            "page_title": "Sign In | Gobindas Handicrafts",
            "site_settings": cms_payload["site_settings"],
            "header_bar": cms_payload["header_bar"],
            "navbar_items": cms_payload["navbar_items"],
            "footer_data": cms_payload["footer"],
            "footer_logo": cms_payload["footer"]["brand"]["logo_url"] if cms_payload["footer"] and cms_payload["footer"].get("brand") else None,
        })
        return context

class CustomLogoutView(LogoutView):
    next_page = reverse_lazy("foundation:home")
    http_method_names = ["get", "post", "options"]

class RegisterView(CreateView):
    template_name = "registration/register.html"
    form_class = UserCreationForm
    success_url = reverse_lazy("foundation:home")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cms_payload = get_foundation_cms_payload(use_cache=True)
        context.update({
            "page_title": "Create Account | Gobindas Handicrafts",
            "site_settings": cms_payload["site_settings"],
            "header_bar": cms_payload["header_bar"],
            "navbar_items": cms_payload["navbar_items"],
            "footer_data": cms_payload["footer"],
            "footer_logo": cms_payload["footer"]["brand"]["logo_url"] if cms_payload["footer"] and cms_payload["footer"].get("brand") else None,
        })
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        login(self.request, self.object)
        return response

class CustomPasswordResetView(PasswordResetView):
    template_name = "registration/password_reset_form.html"
    email_template_name = "registration/password_reset_email.html"
    success_url = reverse_lazy("foundation:password_reset_done")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cms_payload = get_foundation_cms_payload(use_cache=True)
        context.update({
            "page_title": "Reset Password | Gobindas Handicrafts",
            "site_settings": cms_payload["site_settings"],
            "header_bar": cms_payload["header_bar"],
            "navbar_items": cms_payload["navbar_items"],
            "footer_data": cms_payload["footer"],
            "footer_logo": cms_payload["footer"]["brand"]["logo_url"] if cms_payload["footer"] and cms_payload["footer"].get("brand") else None,
        })
        return context

class CustomPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = "registration/password_reset_confirm.html"
    success_url = reverse_lazy("foundation:password_reset_complete")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cms_payload = get_foundation_cms_payload(use_cache=True)
        context.update({
            "page_title": "Confirm Password Reset | Gobindas Handicrafts",
            "site_settings": cms_payload["site_settings"],
            "header_bar": cms_payload["header_bar"],
            "navbar_items": cms_payload["navbar_items"],
            "footer_data": cms_payload["footer"],
            "footer_logo": cms_payload["footer"]["brand"]["logo_url"] if cms_payload["footer"] and cms_payload["footer"].get("brand") else None,
        })
        return context

class CareGuidesPlaceholderView(TemplateView):
    template_name = "foundation/placeholder.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            "title": "Material Care Guides",
            "description": "Learn how to preserve the quality and heritage of your handcrafted wood, ceramics, and textiles. Our care guides are coming soon."
        })
        return context

class TraceabilityPlaceholderView(TemplateView):
    template_name = "foundation/placeholder.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            "title": "Traceability Reports",
            "description": "We are compiling full origin and material journey reports for all of our collections. Discover transparency in craft soon."
        })
        return context

class PoliciesShippingPlaceholderView(TemplateView):
    template_name = "foundation/placeholder.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            "title": "Shipping & Origins",
            "description": "Details regarding our international shipping policies, direct-to-artisan payouts, and carbon-neutral distribution are being updated."
        })
        return context

class CustomOrdersPlaceholderView(TemplateView):
    template_name = "foundation/placeholder.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            "title": "Bespoke Orders",
            "description": "Interested in custom-sized rugs or tailored wood furniture? Our bespoke ordering request flow is currently in development."
        })
        return context

class ContactForm(forms.Form):
    name = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={"placeholder": "Your Name", "class": "form-input"}),
    )
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={"placeholder": "Your Email Address", "class": "form-input"}),
    )
    subject = forms.CharField(
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Subject", "class": "form-input"}),
    )
    message = forms.CharField(
        widget=forms.Textarea(attrs={"placeholder": "How can we help you?", "rows": 5, "class": "form-textarea"}),
        required=True,
    )

class ContactPageView(FormView):
    template_name = "foundation/contact.html"
    form_class = ContactForm
    success_url = reverse_lazy("foundation:contact")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cms_payload = get_foundation_cms_payload(use_cache=True)
        contact_data = cms_payload.get("contact_page") or {}

        page_title = contact_data.get("seo_meta_title") or "Contact Us | Gobindas Handicrafts"
        meta_description = contact_data.get("seo_meta_description") or "Reach our client care team regarding artisan lineages, custom orders, and other support."
        meta_keywords = contact_data.get("seo_meta_keywords") or "contact, support, artisan, handicrafts"

        context.update({
            "page_title": page_title,
            "meta_description": meta_description,
            "meta_keywords": meta_keywords,
            "site_settings": cms_payload["site_settings"],
            "header_bar": cms_payload["header_bar"],
            "navbar_items": cms_payload["navbar_items"],
            "footer_data": cms_payload["footer"],
            "footer_logo": cms_payload["footer"]["brand"]["logo_url"] if cms_payload["footer"] and cms_payload["footer"].get("brand") else None,
            "contact_data": contact_data,
        })
        return context

    def form_valid(self, form):
        context = self.get_context_data(form=form)
        context["form_submitted"] = True
        return self.render_to_response(context)

# Alias mapping for backward compatibility with urls.py references
class ContactPlaceholderView(ContactPageView):
    pass

# =================================================
# CUSTOM ERROR HANDLERS
# =================================================
def bad_request_view(request, exception):
    """Handler for 400 Bad Request."""
    try:
        return render(request, "foundation/errors/400.html", status=400)
    except TemplateDoesNotExist:
        return HttpResponse(
            "<h1>400 Bad Request</h1><p>The request could not be understood by the server.</p>", 
            status=400
        )

def permission_denied_view(request, exception):
    """Handler for 403 Forbidden."""
    try:
        return render(request, "foundation/errors/403.html", status=403)
    except TemplateDoesNotExist:
        return HttpResponse(
            "<h1>403 Forbidden</h1><p>You do not have permission to access this resource.</p>", 
            status=403
        )

def page_not_found_view(request, exception):
    """Handler for 404 Not Found."""
    try:
        return render(request, "foundation/errors/404.html", status=404)
    except TemplateDoesNotExist:
        return HttpResponse(
            "<h1>404 Not Found</h1><p>The requested resource was not found on this server.</p>", 
            status=404
        )

def server_error_view(request):
    """Handler for 500 Internal Server Error."""
    try:
        return render(request, "foundation/errors/500.html", status=500)
    except TemplateDoesNotExist:
        return HttpResponse(
            "<h1>500 Internal Server Error</h1><p>The server encountered an unexpected condition.</p>", 
            status=500
        )