# Project: Handicraft E-commerce

A Django-based e-commerce platform dedicated to showcasing and selling artisanal handicraft products.

## Project Overview

- **Framework:** Django 5.1.4
- **Architecture:** Modular architecture with three primary applications:
    - `apps.foundation`: Core utilities, site settings (branding, header/footer configuration), and common base models.
    - `apps.homepage`: CMS-driven dynamic homepage components (Hero, Trust Bar, Discovery sections, Carousel, Social Proof).
    - `apps.catalog`: Catalog management (Categories, Artisans, Products, Ethical Standards).
- **Database:** PostgreSQL (production), SQLite3 (development).
- **Key Dependencies:** Celery (background tasks), Redis (caching), Pillow (image processing), Django REST Framework (API), Pytest (testing).

## Building and Running

Ensure you have created and activated your virtual environment, then install dependencies:

```bash
pip install -r requirements/development.txt
```

### Development
- **Run Server:** `python manage.py runserver`
- **Run Tests:** `pytest`
- **Seed CMS Data:** Run the provided seed scripts:
    - `python seed_cms.py`
    - `python seed_homepage_cms.py`

## Development Conventions

- **CMS Structure:** Use `SingletonCMSModel` for global configurations and `CMSBaseModel` for entities requiring `created_at` and `updated_at` timestamps.
- **Image Handling:** All image fields should be processed using the project's service layer (e.g., `apps.foundation.services.optimize_uploaded_image`) to ensure optimized sizing/compression.
- **Testing:** All new features must include tests using `pytest` and `pytest-django`.
- **Styling:** The project utilizes a custom CSS approach within Django templates (`{% block extra_css %}`).
