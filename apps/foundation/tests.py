from django.test import TestCase
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from apps.foundation.models import SiteSettings
from apps.foundation.services import resolve_navigation_url
from apps.catalog.models import Category, Artisan

class NavigationURLResolutionTests(TestCase):
    def test_external_and_anchor_urls_returned_as_is(self):
        # External URLs
        self.assertEqual(resolve_navigation_url("http://example.com"), "http://example.com")
        self.assertEqual(resolve_navigation_url("https://bcorporation.net"), "https://bcorporation.net")
        # Mailto / Tel
        self.assertEqual(resolve_navigation_url("mailto:info@gobindas.com"), "mailto:info@gobindas.com")
        self.assertEqual(resolve_navigation_url("tel:+1234567890"), "tel:+1234567890")
        # Anchors
        self.assertEqual(resolve_navigation_url("#"), "#")
        self.assertEqual(resolve_navigation_url("#section"), "#section")
        # Double slash
        self.assertEqual(resolve_navigation_url("//cdn.example.com"), "//cdn.example.com")

    def test_named_django_urls_resolved_successfully(self):
        # Named URL in foundation
        self.assertEqual(resolve_navigation_url("foundation:store_locator"), "/foundation/store-locator/")
        self.assertEqual(resolve_navigation_url("foundation:track_order"), "/foundation/track-order/")
        # Named URL in catalog
        self.assertEqual(resolve_navigation_url("catalog:ceramics"), "/ceramics/")
        self.assertEqual(resolve_navigation_url("catalog:category_ceramics"), "/category/ceramics/")
        # Root namespace URL
        self.assertEqual(resolve_navigation_url("homepage:homepage"), "/")

    def test_internal_paths_normalized_successfully(self):
        # Paths with leading slash but no trailing slash
        self.assertEqual(resolve_navigation_url("/ceramics"), "/ceramics/")
        self.assertEqual(resolve_navigation_url("/category/ceramics"), "/category/ceramics/")
        # Paths with leading slash and trailing slash
        self.assertEqual(resolve_navigation_url("/ceramics/"), "/ceramics/")
        # Paths without leading or trailing slash
        self.assertEqual(resolve_navigation_url("ceramics"), "/ceramics/")
        self.assertEqual(resolve_navigation_url("care-guides"), "/care-guides/")
        # Paths to files (should not append slash)
        self.assertEqual(resolve_navigation_url("/static/favicon.ico"), "/static/favicon.ico")

    def test_invalid_or_none_falls_back_to_anchor(self):
        self.assertEqual(resolve_navigation_url(None), "#")
        self.assertEqual(resolve_navigation_url(""), "#")


class PlaceholderPagesTests(TestCase):
    def setUp(self):
        # Create a mock logo image (1x1 pixel transparent gif)
        small_gif = (
            b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x00\x00\x00\x21\xf9'
            b'\x04\x01\x0a\x00\x01\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00'
            b'\x00\x02\x02\x4c\x01\x00\x3b'
        )
        logo = SimpleUploadedFile("logo.gif", small_gif, content_type="image/gif")
        SiteSettings.objects.create(
            logo=logo,
            brand_title="GOBINDAS",
            brand_subtitle="HANDICRAFTS"
        )

        # Create placeholder Category records required by the catalog listing view tests
        Category.objects.create(
            name="Ceramics",
            slug="ceramics",
            description="Glazed Ceramics",
            is_active=True
        )
        Category.objects.create(
            name="Woven Textiles",
            slug="textiles",
            description="Woven Textiles",
            is_active=True
        )
        Category.objects.create(
            name="Handcarved Wood",
            slug="wood",
            description="Handcarved Wood",
            is_active=True
        )
        Category.objects.create(
            name="Artisan Jewelry",
            slug="jewelry",
            description="Artisan Jewelry",
            is_active=True
        )

        # Create placeholder Artisan record required by the artisan detail view test
        Artisan.objects.create(
            name="Rajendra",
            slug="rajendra",
            bio="Master Rajendra",
            quote="Every chisel mark is a word in the story of my ancestors.",
            is_active=True
        )

    def test_catalog_category_placeholders_return_200(self):
        endpoints = [
            "/ceramics/",
            "/category/ceramics/",
            "/textiles/",
            "/category/textiles/",
            "/wood/",
            "/category/wood/",
            "/jewelry/",
            "/category/jewelry/",
        ]
        for url in endpoints:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, f"URL {url} failed with {response.status_code}")
            self.assertContains(response, "Refine Collection")

    def test_catalog_artisan_placeholders_return_200(self):
        # List view
        response = self.client.get("/artisans/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Meet the Makers")
        
        # Detail view
        response = self.client.get("/artisans/rajendra/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Master Rajendra")

    def test_foundation_general_placeholders_return_200(self):
        endpoints = [
            "/care-guides/",
            "/traceability/",
            "/policies/shipping/",
            "/custom-orders/",
            "/contact/",
        ]
        for url in endpoints:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, f"URL {url} failed with {response.status_code}")
            self.assertContains(response, "Return to Gallery")