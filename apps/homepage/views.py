from __future__ import annotations

import json
from django.views.generic import TemplateView

from apps.catalog.models import Category
from .services import get_homepage_payload


class HomepageView(TemplateView):
    """
    Enterprise Class-Based View for the Dynamic CMS Homepage.
    Separates business logic by entirely relying on the Service layer to build the payload,
    while directly querying dynamic CMS elements like homepage categories.
    """
    # Note: Ensure you map this to the exact template file location of your homepage.html
    template_name = "homepage/homepage.html" 

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # 1. Fetch the compiled, cached CMS payload from the Service Layer
        homepage_payload = get_homepage_payload(use_cache=True)

        # 2. Dynamically fetch categories flagged for the homepage
        homepage_categories = (
            Category.objects
            .filter(
                show_on_homepage=True,
                is_active=True
            )
            .order_by(
                "sort_order",
                "name"
            )
        )

        # 3. Pass as a raw dictionary for standard Django template rendering if needed
        context["homepage_payload"] = homepage_payload
        
        # 4. Pass the dynamic categories to the template
        context["homepage_categories"] = homepage_categories

        # 5. Pass as a serialized JSON string to exactly clone the frontend architecture
        # This allows you to replace `const cmsPayload = { ... }` in your HTML with:
        # const cmsPayload = {{ homepage_payload_json|safe }};
        context["homepage_payload_json"] = json.dumps(homepage_payload)

        return context