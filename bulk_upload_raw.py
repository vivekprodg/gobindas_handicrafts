import os
import re
import random
from decimal import Decimal
import django
from django.core.files import File
from django.utils.text import slugify

# Setup Django Environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.catalog.models import Product, Category, Artisan, ProductCollection
from apps.homepage.models import TrendingSection, TrendingProduct, CategorySection
from apps.homepage.services import invalidate_homepage_cache
from apps.catalog.services import invalidate_catalog_cache

BULK_DIR = "bulk_images"

# Default Categories to support "Explore Collection" (Visual Discovery)
DEFAULT_CATEGORIES = [
    {"name": "Statues & Sculptures", "slug": "statues-sculptures", "desc": "Hand-carved sacred statues and heritage sculptures."},
    {"name": "Wood Carvings", "slug": "wood-carvings", "desc": "Artisanal architectural carvings and wooden artifacts."},
    {"name": "Metal & Bronze Masterpieces", "slug": "metal-bronze", "desc": "Traditional lost-wax bronze and brass cast artifacts."},
    {"name": "Ritual Artifacts", "slug": "ritual-artifacts", "desc": "Sacral bells, singing bowls, and ceremonial objects."}
]

def ensure_homepage_categories():
    """Ensures homepage categories exist and are set to show_on_homepage=True."""
    categories = []
    for cat_data in DEFAULT_CATEGORIES:
        category, created = Category.objects.get_or_create(
            slug=cat_data["slug"],
            defaults={
                "name": cat_data["name"],
                "description": cat_data["desc"],
                "show_on_homepage": True,
                "show_in_menu": True,
                "is_active": True,
            }
        )
        if not category.show_on_homepage or not category.is_active:
            category.show_on_homepage = True
            category.is_active = True
            category.save()
        categories.append(category)
    return categories

def cleanup_existing_prices():
    """Wipes prices from existing Product and TrendingProduct records."""
    p_updated = Product.objects.all().update(price=None, original_price=None, cost_price=None)
    t_updated = TrendingProduct.objects.all().update(price="")
    print(f"🧹 Cleaned existing prices: {p_updated} products & {t_updated} trending products updated.")

def process_exact_camera_images():
    cleanup_existing_prices()

    if not os.path.exists(BULK_DIR):
        print(f"❌ Error: Folder '{BULK_DIR}' does not exist.")
        return

    files = sorted([f for f in os.listdir(BULK_DIR) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))])
    
    if not files:
        print(f"❌ No images found in '{BULK_DIR}'.")
        return

    print("=" * 75)
    print(f"  GOBINDAS HANDICRAFTS BULK UPLOADER WITH DUPLICATE SCANNER ({len(files)} files)")
    print("=" * 75)

    prefix = input("Enter a product title prefix (e.g. 'Handcrafted Statue' or 'Statue')\n[Press Enter for 'Handcrafted Statue']: ").strip()
    if not prefix:
        prefix = "Handcrafted Statue"

    # Setup Homepage Categories for Explore Collection
    categories = ensure_homepage_categories()
    default_artisan = Artisan.objects.filter(is_active=True).first()

    # Get or Create Homepage Sections
    trending_section, _ = TrendingSection.objects.get_or_create(
        id=1, 
        defaults={'heading': 'Featured Artifacts'}
    )

    category_section, _ = CategorySection.objects.get_or_create(
        id=1,
        defaults={'heading': 'Explore Collection', 'is_active': True}
    )

    print(f"\nProcessing {len(files)} photos with prefix '{prefix}'...")
    print(f"🔍 Scanning database for duplicate SKUs...")
    print(f"📂 Distributing new products across {len(categories)} 'Explore Collection' categories!\n")

    uploaded_count = 0
    skipped_count = 0
    trending_pos = 10

    # Determine featured indices among new/unprocessed files
    featured_count = min(20, len(files))
    featured_indices = set(random.sample(range(len(files)), featured_count))

    for index, filename in enumerate(files):
        filepath = os.path.join(BULK_DIR, filename)
        filename_no_ext = os.path.splitext(filename)[0]

        # Product Title & Base SKU
        title = f"{prefix} {filename_no_ext}"
        base_sku = f"GH-{filename_no_ext}"

        # ----------------------------------------------------------------------
        # DUPLICATE SCANNER: Check if product already exists
        # ----------------------------------------------------------------------
        existing_product = Product.objects.filter(sku__iexact=base_sku).first()
        if existing_product:
            skipped_count += 1
            print(f"⏭️  [SKIPPED - DUPLICATE] '{title}' (SKU: {base_sku}) is already in database.")
            
            # Ensure category has a cover image if missing
            if existing_product.category and not existing_product.category.image and existing_product.primary_image:
                existing_product.category.image = existing_product.primary_image
                existing_product.category.save()
            continue

        # Generate unique slug
        base_slug = slugify(f"{prefix}-{filename_no_ext}") or f"item-{index+1}"
        slug = base_slug
        slug_counter = 1
        while Product.objects.filter(slug=slug).exists():
            slug = f"{base_slug}-{slug_counter}"
            slug_counter += 1

        is_featured_item = index in featured_indices

        # Assign to a category cyclically
        assigned_category = categories[uploaded_count % len(categories)]

        # Create new product without price
        product = Product.objects.create(
            title=title,
            slug=slug,
            sku=base_sku,
            price=None,
            original_price=None,
            currency="USD",
            short_description=f"Exquisite {prefix.lower()} created by master artisans.",
            description=f"Ethically produced {prefix.lower()} using traditional techniques preserved through generations.",
            category=assigned_category,
            artisan=default_artisan,
            status=Product.ProductStatus.PUBLISHED,
            is_active=True,
            is_featured=is_featured_item,
        )

        # Upload & attach image
        with open(filepath, 'rb') as f:
            django_file = File(f)
            product.primary_image.save(filename, django_file, save=True)

            # Assign cover image to Category for Explore Collection
            if not assigned_category.image:
                f.seek(0)
                assigned_category.image.save(f"cat_{assigned_category.slug}.jpg", File(f), save=True)
                print(f"🖼️ Set category cover image for '{assigned_category.name}'")

        # Attach to Homepage Carousel if featured
        if is_featured_item:
            TrendingProduct.objects.get_or_create(
                trending_section=trending_section,
                product=product,
                defaults={
                    'title': title,
                    'price': "",
                    'badge': "Featured",
                    'position': trending_pos
                }
            )
            trending_pos += 10

        uploaded_count += 1
        status_tag = "⭐ [FEATURED CAROUSEL]" if is_featured_item else "✅ [NEW PRODUCT UPLOADED]"
        print(f"{status_tag} [{index+1}/{len(files)}] '{title}' | Category: {assigned_category.name}")

    # Invalidate Caches so changes reflect immediately
    invalidate_homepage_cache()
    invalidate_catalog_cache()

    print("\n" + "=" * 75)
    print(f"🎉 EXECUTION SUMMARY:")
    print(f"   • Total Photos Scanned : {len(files)}")
    print(f"   • New Products Uploaded: {uploaded_count}")
    print(f"   • Duplicates Skipped   : {skipped_count}")
    print(f"   • 'Explore Collection' Categories Populated & Updated.")
    print("=" * 75)

if __name__ == "__main__":
    process_exact_camera_images()