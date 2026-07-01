import os
import django
import requests
from django.core.files.base import ContentFile

# Set environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.homepage.models import (
    HomepageSettings,
    HeroSection,
    HeroCTA,
    TrustBarSection,
    TrustBarItem,
    CategorySection,
    CategoryItem,
    TrendingSection,
    TrendingProduct,
    ArtisanStorySection,
    SocialProofSection,
    SocialProofImage,
)

def download_image(url):
    print(f"Downloading image from: {url}")
    try:
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            return ContentFile(response.content)
    except Exception as e:
        print(f"Failed to download image: {e}")
    return None

def seed_homepage():
    print("Starting Homepage CMS seeding...")

    # 1. Global Settings
    settings, created = HomepageSettings.objects.get_or_create()
    settings.page_title = "Gobindas Handicrafts - Enterprise Homepage"
    settings.is_active = True
    settings.save()
    print("-> Seeded HomepageSettings")

    # 2. Hero Section
    HeroCTA.objects.all().delete()
    HeroSection.objects.all().delete()
    
    hero = HeroSection.objects.create(
        subtitle="Heritage & Craft",
        title="Preserving the Lineages of Indian Artisans"
    )
    
    hero_bg_url = "https://images.unsplash.com/photo-1610701596007-11502861dcfa?ixlib=rb-4.0.3&auto=format&fit=crop&w=1920&q=80"
    img_file = download_image(hero_bg_url)
    if img_file:
        hero.background_media.save("hero_bg.jpg", img_file, save=True)
    else:
        hero.save()
        
    HeroCTA.objects.create(
        hero_section=hero,
        label="Shop Ceramics",
        url="/ceramics",
        style="btn-primary",
        position=10
    )
    HeroCTA.objects.create(
        hero_section=hero,
        label="Meet the Makers",
        url="/artisans",
        style="btn-outline",
        position=20
    )
    print("-> Seeded HeroSection & CTAs")

    # 3. Trust Bar Section
    TrustBarItem.objects.all().delete()
    TrustBarSection.objects.all().delete()
    
    trust_bar = TrustBarSection.objects.create(is_active=True)
    
    signals = [
        {"icon": "fair-trade", "text": "Fair Trade Certified", "position": 10},
        {"icon": "shipping", "text": "Free Global Shipping Over $500", "position": 20},
        {"icon": "artisan", "text": "100% Direct to Artisan", "position": 30}
    ]
    for sig in signals:
        TrustBarItem.objects.create(
            trust_bar=trust_bar,
            icon=sig["icon"],
            text=sig["text"],
            position=sig["position"]
        )
    print("-> Seeded TrustBarSection")

    # 4. Category Section
    CategoryItem.objects.all().delete()
    CategorySection.objects.all().delete()
    
    cat_sec = CategorySection.objects.create(heading="Shop by Craft Type")
    
    categories = [
        {
            "name": "Handcarved Wood",
            "url": "https://images.unsplash.com/photo-1578749556568-bc2c40e68b61?ixlib=rb-4.0.3&auto=format&fit=crop&w=800&q=80",
            "link": "/wood",
            "position": 10
        },
        {
            "name": "Woven Textiles",
            "url": "https://images.unsplash.com/photo-1606722590583-6951b5ea92a3?ixlib=rb-4.0.3&auto=format&fit=crop&w=800&q=80",
            "link": "/textiles",
            "position": 20
        },
        {
            "name": "Glazed Ceramics",
            "url": "https://images.unsplash.com/photo-1610701596045-5606d289cb83?ixlib=rb-4.0.3&auto=format&fit=crop&w=800&q=80",
            "link": "/ceramics",
            "position": 30
        }
    ]
    for index, cat in enumerate(categories):
        item = CategoryItem.objects.create(
            category_section=cat_sec,
            name=cat["name"],
            link=cat["link"],
            position=cat["position"]
        )
        img_file = download_image(cat["url"])
        if img_file:
            item.image.save(f"category_{index}.jpg", img_file, save=True)
        else:
            item.save()
    print("-> Seeded CategorySection")

    # 5. Merchandising Carousel Section
    TrendingProduct.objects.all().delete()
    TrendingSection.objects.all().delete()
    
    trend_sec = TrendingSection.objects.create(heading="Trending Artifacts")
    
    products = [
        {
            "title": "Jaipur Terracotta Vase",
            "price": "$120.00",
            "url": "https://images.unsplash.com/photo-1578749556568-bc2c40e68b61?ixlib=rb-4.0.3&auto=format&fit=crop&w=600&q=80",
            "badge": "Only 1 Left",
            "position": 10
        },
        {
            "title": "Handloomed Silk Throw",
            "price": "$240.00",
            "url": "https://images.unsplash.com/photo-1606722590583-6951b5ea92a3?ixlib=rb-4.0.3&auto=format&fit=crop&w=600&q=80",
            "badge": None,
            "position": 20
        },
        {
            "title": "Carved Teak Root Bowl",
            "price": "$185.00",
            "url": "https://images.unsplash.com/photo-1610701596045-5606d289cb83?ixlib=rb-4.0.3&auto=format&fit=crop&w=600&q=80",
            "badge": "New Kiln Run",
            "position": 30
        },
        {
            "title": "Bronze Ganesha Murti",
            "price": "$450.00",
            "url": "https://images.unsplash.com/photo-1544974246-88d40733575c?ixlib=rb-4.0.3&auto=format&fit=crop&w=600&q=80",
            "badge": "One of a Kind",
            "position": 40
        }
    ]
    for index, prod in enumerate(products):
        item = TrendingProduct.objects.create(
            trending_section=trend_sec,
            title=prod["title"],
            price=prod["price"],
            badge=prod["badge"],
            position=prod["position"]
        )
        img_file = download_image(prod["url"])
        if img_file:
            item.image.save(f"product_{index}.jpg", img_file, save=True)
        else:
            item.save()
    print("-> Seeded TrendingSection")

    # 6. Artisan Story Section
    ArtisanStorySection.objects.all().delete()
    
    story = ArtisanStorySection.objects.create(
        artisan_name="Master Rajendra",
        quote='"Every chisel mark is a word in the story of my ancestors."',
        bio="Operating out of a 200-year-old workshop in Patan, Rajendra represents the seventh generation of woodcarvers in his lineage. His work ensures ancient motifs survive into the modern era.",
        button_text="Discover His Collection",
        target_url="/artisans/rajendra"
    )
    story_img_url = "https://images.unsplash.com/photo-1544974246-88d40733575c?ixlib=rb-4.0.3&auto=format&fit=crop&w=1000&q=80"
    img_file = download_image(story_img_url)
    if img_file:
        story.image.save("artisan_story.jpg", img_file, save=True)
    else:
        story.save()
    print("-> Seeded ArtisanStorySection")

    # 7. Social Proof Section
    SocialProofImage.objects.all().delete()
    SocialProofSection.objects.all().delete()
    
    social_sec = SocialProofSection.objects.create(heading="Gobindas In Your Home")
    
    social_images = [
        "https://images.unsplash.com/photo-1610701596007-11502861dcfa?ixlib=rb-4.0.3&w=400&q=80",
        "https://images.unsplash.com/photo-1606722590583-6951b5ea92a3?ixlib=rb-4.0.3&w=400&q=80",
        "https://images.unsplash.com/photo-1578749556568-bc2c40e68b61?ixlib=rb-4.0.3&w=400&q=80",
        "https://images.unsplash.com/photo-1610701596045-5606d289cb83?ixlib=rb-4.0.3&w=400&q=80"
    ]
    for index, img_url in enumerate(social_images):
        item = SocialProofImage.objects.create(
            social_proof_section=social_sec,
            position=(index + 1) * 10
        )
        img_file = download_image(img_url)
        if img_file:
            item.image.save(f"social_{index}.jpg", img_file, save=True)
        else:
            item.save()
    print("-> Seeded SocialProofSection")

    # Clean homepage cache
    from apps.homepage.services import invalidate_homepage_cache
    invalidate_homepage_cache()
    print("-> Cleared Homepage CMS cache.")
    print("Homepage CMS seeding finished successfully!")

if __name__ == '__main__':
    seed_homepage()