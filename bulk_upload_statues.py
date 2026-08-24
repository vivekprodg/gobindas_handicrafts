import os
import re
from pathlib import Path
import django
from django.core.files import File
from django.utils.text import slugify

# Setup Django Environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.catalog.models import Product, ProductImage, Category, Artisan
from apps.homepage.models import TrendingSection, TrendingProduct
from apps.homepage.services import invalidate_homepage_cache
from apps.catalog.services import invalidate_catalog_cache

BULK_DIR = "bulk_images"

def format_title_from_folder(folder_name: str) -> str:
    """Converts hyphens, underscores, and camelCase into clean title string."""
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1 \2', folder_name)
    s2 = re.sub('([a-z0-9])([A-Z])', r'\1 \2', s1)
    clean = re.sub(r'[-_]+', ' ', s2)
    return " ".join(clean.split()).title()

def run_bulk_upload():
    if not os.path.exists(BULK_DIR):
        print(f"❌ Folder '{BULK_DIR}' not found.")
        return

    subfolders = sorted([
        f for f in os.listdir(BULK_DIR) 
        if os.path.isdir(os.path.join(BULK_DIR, f))
    ])

    if not subfolders:
        print("❌ No subfolders found in bulk_images.")
        return

    print("=" * 80)
    print(f"  GOBINDAS HANDICRAFTS: MULTI-ANGLE STATUE UPLOADER ({len(subfolders)} Folders)")
    print("=" * 80)

    # 1. Resolve Category & Default Artisan
    statues_category = Category.objects.filter(slug__in=['statues', 'statues-sculptures']).first()
    if not statues_category:
        statues_category = Category.objects.create(
            name="Statues",
            slug="statues",
            description="Handcrafted sacred Buddhist and Hindu statues sculpted by master Newar artisans in Nepal.",
            show_on_homepage=True,
            show_in_menu=True,
            is_active=True
        )

    default_artisan = Artisan.objects.filter(is_active=True).first()

    # 2. Setup Homepage Carousel Section
    trending_section, _ = TrendingSection.objects.get_or_create(
        id=1,
        defaults={'heading': 'Featured Masterpieces'}
    )

    uploaded_count = 0
    trending_pos = 10

    for index, folder_name in enumerate(subfolders, start=1):
        folder_path = os.path.join(BULK_DIR, folder_name)
        
        # Get all image files inside folder sorted naturally
        image_files = sorted([
            f for f in os.listdir(folder_path)
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))
        ])

        if not image_files:
            print(f"⚠️ [SKIP - NO IMAGES] Folder: {folder_name}")
            continue

        title = format_title_from_folder(folder_name)
        sku = f"GH-STAT-{index:03d}"
        
        # Clean unique slug
        base_slug = slugify(folder_name) or f"statue-{index}"
        slug = base_slug
        slug_count = 1
        while Product.objects.filter(slug=slug).exists():
            slug = f"{base_slug}-{slug_count}"
            slug_count += 1

        is_featured = (index <= 16)  # Feature top 16 statues in the homepage carousel

        # Create Product
        product = Product.objects.create(
            title=title,
            slug=slug,
            sku=sku,
            category=statues_category,
            artisan=default_artisan,
            currency="USD",
            short_description=f"Exquisite hand-carved {title.lower()} crafted by master artisans in Patan, Nepal.",
            description=f"Authentic {title.lower()} embodying traditional iconography, hand-cast using the ancestral lost-wax method and finished with sacred artisanal detailing.",
            status=Product.ProductStatus.PUBLISHED,
            is_active=True,
            is_featured=is_featured,
            badge_text="Masterpiece" if is_featured else None
        )

        # 3. Attach Primary Image (Image 1)
        primary_filename = image_files[0]
        primary_path = os.path.join(folder_path, primary_filename)
        with open(primary_path, 'rb') as f:
            product.primary_image.save(f"{slug}-01-front{Path(primary_filename).suffix}", File(f), save=True)

        # 4. Attach Hover Image (Image 2 if available, else Image 1)
        hover_filename = image_files[1] if len(image_files) > 1 else image_files[0]
        hover_path = os.path.join(folder_path, hover_filename)
        with open(hover_path, 'rb') as f:
            product.hover_image.save(f"{slug}-02-angle{Path(hover_filename).suffix}", File(f), save=True)

        # 5. Attach ALL images to Product Gallery (ProductImage)
        for img_idx, img_file in enumerate(image_files, start=1):
            img_path = os.path.join(folder_path, img_file)
            with open(img_path, 'rb') as f:
                p_image = ProductImage(
                    product=product,
                    title=f"{title} - View {img_idx}",
                    alt_text=f"{title} - Detailed Angle {img_idx}",
                    position=img_idx,
                    is_primary=(img_idx == 1),
                    is_active=True
                )
                p_image.image.save(f"{slug}-{img_idx:02d}{Path(img_file).suffix}", File(f), save=True)

        # 6. Attach to Homepage Trending Carousel if featured
        if is_featured:
            TrendingProduct.objects.create(
                trending_section=trending_section,
                product=product,
                title=title,
                price="",
                badge="Featured",
                position=trending_pos
            )
            trending_pos += 10

        # Set category cover image if empty
        if not statues_category.image and product.primary_image:
            statues_category.image = product.primary_image
            statues_category.save()

        uploaded_count += 1
        print(f"✅ [{uploaded_count}/{len(subfolders)}] '{title}' -> {len(image_files)} images attached | SKU: {sku}")

    # 7. Invalidate Caches
    invalidate_catalog_cache()
    invalidate_homepage_cache()

    print("\n" + "=" * 80)
    print(f"🎉 SUCCESS: {uploaded_count} products uploaded with full multi-angle galleries!")
    print("=" * 80)

if __name__ == '__main__':
    run_bulk_upload()
