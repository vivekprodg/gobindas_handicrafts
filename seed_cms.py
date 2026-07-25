import os
import django

# Initialize Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.foundation.models import (
    SiteSettings,
    HeaderBar,
    HeaderAnnouncement,
    HeaderCurrency,
    HeaderLanguage,
    HeaderUtilityLink,
    NavbarItem,
    NavbarMegaMenuColumn,
    NavbarMegaMenuLink,
    FooterSettings,
    FooterSection,
    FooterLink,
    FooterSocialLink,
    FooterPaymentMethod,
    FooterTrustBadge
)

def seed():
    import requests
    from django.core.files.base import ContentFile

    print("Seeding foundation CMS data...")

    # 1. SiteSettings
    # Get or create the singleton SiteSettings record.
    ss = SiteSettings.objects.first()
    if ss:
        ss.brand_title = "GOBINDAS"
        ss.brand_subtitle = "HANDICRAFTS"
        ss.brand_url = "https://www.gobindashandicraft.com"
        ss.logo_alt_text = "Gobindas Handicrafts Logo"
        ss.search_placeholder = "Find artisan rugs..."
        ss.search_button_label = "Search"
        ss.cart_button_label = "Shopping Bag"
        ss.cart_badge_count = 2
        ss.default_featured_title = "Meet the Artisans"
        ss.default_featured_text = "Explore the workshop lineages of Nepal."
        
        # Email CMS Configurations
        ss.company_notification_email = "admin@gobindashandicrafts.com"
        ss.sender_email_address = "noreply@gobindashandicrafts.com"
        ss.sender_display_name = "Gobindas Handicrafts"
        
        # Download and save default featured image if missing
        if not ss.default_featured_image:
            try:
                print("Downloading default mega menu featured image...")
                url = "https://images.unsplash.com/photo-1610701596007-11502861dcfa?ixlib=rb-4.0.3&auto=format&fit=crop&w=800&q=80"
                res = requests.get(url, timeout=15)
                if res.status_code == 200:
                    ss.default_featured_image.save("default_featured.jpg", ContentFile(res.content), save=False)
            except Exception as e:
                print(f"Failed to download default featured image: {e}")
                
        ss.save()
        print("-> Updated existing SiteSettings singleton with search/cart configs, email CMS settings, and fallback media.")
    else:
        print("-> WARNING: No SiteSettings record found. Please upload a logo through the admin panel first.")

    # 2. HeaderBar
    HeaderBar.objects.all().delete()
    hb = HeaderBar.objects.create(
        currency_label="NPR",
        language_label="EN",
        announcement_messages=[
            "Celebrating World Fair Trade Day: Free shipping on hand-woven textiles.",
            "New Arrivals: View the latest ethically sourced ceramics.",
            "Join the Artisan Club for 15% off your first handcrafted order."
        ],
        rotator_interval_ms=4000,
        left_utilities=[],
        right_utilities=[
            {"label": "Store Locator", "url": "/foundation/store-locator/"},
            {"label": "Track Order", "url": "/foundation/track-order/"}
        ]
    )

    # Seed Announcements
    HeaderAnnouncement.objects.create(header_bar=hb, text="Celebrating World Fair Trade Day: Free shipping on hand-woven textiles.", position=1, is_visible=True)
    HeaderAnnouncement.objects.create(header_bar=hb, text="New Arrivals: View the latest ethically sourced ceramics.", position=2, is_visible=True)
    HeaderAnnouncement.objects.create(header_bar=hb, text="Join the Artisan Club for 15% off your first handcrafted order.", position=3, is_visible=True)

    # Seed Currencies
    HeaderCurrency.objects.create(header_bar=hb, label="Nepalese Rupee", code="NPR", symbol="NPR", position=1, is_visible=True)
    HeaderCurrency.objects.create(header_bar=hb, label="US Dollar", code="USD", symbol="$", position=2, is_visible=True)
    HeaderCurrency.objects.create(header_bar=hb, label="Euro", code="EUR", symbol="€", position=3, is_visible=True)

    # Seed Languages
    HeaderLanguage.objects.create(header_bar=hb, label="English", code="EN", position=1, is_visible=True)
    HeaderLanguage.objects.create(header_bar=hb, label="Nepali", code="NE", position=2, is_visible=True)

    # Seed Utility Links
    HeaderUtilityLink.objects.create(header_bar=hb, label="Store Locator", link_url="/foundation/store-locator/", side="right", position=1, is_visible=True)
    HeaderUtilityLink.objects.create(header_bar=hb, label="Track Order", link_url="/foundation/track-order/", side="right", position=2, is_visible=True)

    print("-> Created HeaderBar singleton and dynamic related lists.")

    # 3. NavbarItems
    NavbarItem.objects.all().delete()
    
    # Shop Craft (Mega Menu parent)
    shop_craft = NavbarItem.objects.create(
        label="Shop Craft",
        menu_type=NavbarItem.MenuType.MEGA_MENU,
        position=10,
        visibility_scope=NavbarItem.VisibilityScope.ALL,
        featured_title="Meet the Artisans",
        featured_text="Explore the workshop lineages of Nepal."
    )

    # Seed Mega Menu Columns and Links
    col1 = NavbarMegaMenuColumn.objects.create(parent_item=shop_craft, heading="Raw Materials", position=1)
    NavbarMegaMenuLink.objects.create(parent_column=col1, label="Organic Cotton", link_url="/material/cotton/", position=1)
    NavbarMegaMenuLink.objects.create(parent_column=col1, label="Reclaimed Wood", link_url="/material/wood/", position=2)
    NavbarMegaMenuLink.objects.create(parent_column=col1, label="Glazed Ceramics", link_url="/material/ceramics/", position=3)
    NavbarMegaMenuLink.objects.create(parent_column=col1, label="Natural Fibers", link_url="/material/fibers/", position=4)

    col2 = NavbarMegaMenuColumn.objects.create(parent_item=shop_craft, heading="By Space", position=2)
    NavbarMegaMenuLink.objects.create(parent_column=col2, label="Living Room", link_url="#", position=1)
    NavbarMegaMenuLink.objects.create(parent_column=col2, label="Sanctuary & Bath", link_url="#", position=2)
    NavbarMegaMenuLink.objects.create(parent_column=col2, label="Kitchen & Dining", link_url="#", position=3)
    
    # Artisans (Simple link)
    NavbarItem.objects.create(
        label="Artisans",
        menu_type=NavbarItem.MenuType.LINK,
        link_url="/artisans/",
        position=20,
        visibility_scope=NavbarItem.VisibilityScope.ALL
    )
    
    # Sustainability (Link with badge)
    NavbarItem.objects.create(
        label="Sustainability",
        menu_type=NavbarItem.MenuType.LINK,
        link_url="/traceability/",
        position=30,
        badge_text="Eco-Friendly",
        visibility_scope=NavbarItem.VisibilityScope.ALL
    )
    print("-> Created NavbarItems, Columns, and Links.")

    # 4. FooterSettings
    fs = FooterSettings.objects.first()
    if not fs:
        fs = FooterSettings()
    fs.brand_name = "Gobindas Handicrafts"
    fs.fair_trade_statement = "Dedicated to preserving ancestral handicraft lineages. Every piece is ethically created, supporting master artisans across Nepal."
    fs.newsletter_heading = "Join the Artisan Circle"
    fs.newsletter_subtext = "Receive curated stories of heritage crafts and exclusive access to limited runs."
    fs.newsletter_endpoint = "#"
    fs.newsletter_placeholder = "Enter your email address"
    fs.copyright_template = "© {current_year} {brand_name}. All rights reserved."
    fs.save()
    print("-> Created/Updated FooterSettings singleton.")

    # 5. FooterSections and FooterLinks
    FooterSection.objects.all().delete()
    FooterLink.objects.all().delete()

    # Section 1
    sec1 = FooterSection.objects.create(title="Shop Craft", position=1)
    FooterLink.objects.create(section=sec1, label="Ceramics & Pottery", route="/category/ceramics/", link_type="internal_route", position=1)
    FooterLink.objects.create(section=sec1, label="Handwoven Textiles", route="/category/textiles/", link_type="internal_route", position=2)
    FooterLink.objects.create(section=sec1, label="Reclaimed Wood", route="/category/wood/", link_type="internal_route", position=3)
    FooterLink.objects.create(section=sec1, label="Artisan Jewelry", route="/category/jewelry/", link_type="internal_route", position=4)

    # Section 2
    sec2 = FooterSection.objects.create(title="Care & Heritage", position=2)
    FooterLink.objects.create(section=sec2, label="Material Care Guides", route="/care-guides/", link_type="internal_route", position=1)
    FooterLink.objects.create(section=sec2, label="Meet the Artisans", route="/artisans/", link_type="internal_route", position=2)
    FooterLink.objects.create(section=sec2, label="Traceability Reports", route="/traceability/", link_type="internal_route", position=3)

    # Section 3
    sec3 = FooterSection.objects.create(title="Client Care", position=3)
    FooterLink.objects.create(section=sec3, label="Shipping & Origins", route="/policies/shipping/", link_type="internal_route", position=1)
    FooterLink.objects.create(section=sec3, label="Bespoke Orders", route="/custom-orders/", link_type="internal_route", position=2)
    FooterLink.objects.create(section=sec3, label="Contact Us", route="/contact/", link_type="internal_route", position=3)
    print("-> Created FooterSections and FooterLinks.")

    # 6. FooterSocialLinks
    FooterSocialLink.objects.all().delete()
    FooterSocialLink.objects.create(platform="instagram", url="https://instagram.com", icon_key="instagram", position=1)
    FooterSocialLink.objects.create(platform="pinterest", url="https://pinterest.com", icon_key="pinterest", position=2)
    print("-> Created FooterSocialLinks.")

    # 7. FooterPaymentMethods
    FooterPaymentMethod.objects.all().delete()
    FooterPaymentMethod.objects.create(method_name="visa", icon_key="visa", position=1)
    FooterPaymentMethod.objects.create(method_name="mastercard", icon_key="mastercard", position=2)
    print("-> Created FooterPaymentMethods.")

    # 8. FooterTrustBadges
    FooterTrustBadge.objects.all().delete()
    FooterTrustBadge.objects.create(badge_name="fair trade", icon_key="fair-trade", position=1)
    FooterTrustBadge.objects.create(badge_name="climate neutral", icon_key="climate-neutral", position=2)
    print("-> Created FooterTrustBadges.")

    # Clean cache
    from apps.foundation.services import invalidate_foundation_cms_cache
    invalidate_foundation_cms_cache()
    print("-> Cleared foundation CMS caches.")
    print("Seeding finished successfully!")

if __name__ == '__main__':
    seed()