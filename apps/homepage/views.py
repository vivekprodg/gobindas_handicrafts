from __future__ import annotations

import json
from django.views.generic import TemplateView
from .services import get_homepage_payload

class HomepageView(TemplateView):
    template_name = "homepage/homepage.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # 1. Fetch the compiled & cached CMS payload from Service Layer
        homepage_payload = get_homepage_payload(use_cache=True)

        # 2. Extract homepage categories directly from the payload (avoids duplicate DB query)
        homepage_categories = []
        for module in homepage_payload.get("modules", []):
            if module.get("type") == "visual_discovery":
                homepage_categories = module.get("parameters", {}).get("categories", [])
                break

        context["homepage_payload"] = homepage_payload
        context["homepage_categories"] = homepage_categories
        context["homepage_payload_json"] = json.dumps(homepage_payload)

        return context