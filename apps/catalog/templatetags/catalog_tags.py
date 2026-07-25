"""
Custom Django Template Tags and Filters for the Catalog application.
Provides utility tags for dynamic URL parameter manipulation, star rating rendering,
and facet count formatting.
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Dict, Optional, Union

from django import template
from django.utils.safestring import mark_safe

register = template.Library()

@register.simple_tag(takes_context=True)
def url_replace(context: Dict[str, Any], **kwargs: Any) -> str:
    """
    Modifies or appends query string parameters while preserving existing filters and page state.
    Usage in templates:
        <a href="?{% url_replace page=2 %}">Page 2</a>
        <a href="?{% url_replace material=None %}">Remove Material Filter</a>
    """
    request = context.get("request")
    if not request:
        return ""

    query_dict = request.GET.copy()

    for key, value in kwargs.items():
        if value is None or value == "":
            query_dict.pop(key, None)
        elif isinstance(value, (list, tuple)):
            query_dict.setlist(key, [str(v) for v in value if v is not None])
        else:
            query_dict[key] = str(value)

    return query_dict.urlencode()

@register.simple_tag(takes_context=True)
def remove_filter_param(context: Dict[str, Any], param: str, value: Optional[str] = None) -> str:
    """
    Removes a specific value from a multi-select GET parameter array or deletes the parameter if single-value.
    Usage:
        <a href="?{% remove_filter_param 'material' 'Wood' %}">Remove Wood</a>
    """
    request = context.get("request")
    if not request:
        return ""

    query_dict = request.GET.copy()
    if not value:
        query_dict.pop(param, None)
    else:
        values = query_dict.getlist(param)
        updated_values = [v for v in values if str(v) != str(value)]
        if updated_values:
            query_dict.setlist(param, updated_values)
        else:
            query_dict.pop(param, None)

    return query_dict.urlencode()

@register.filter(name="render_star_rating")
def render_star_rating(value: Optional[Union[int, float, str]]) -> str:
    """
    Converts a numeric rating (1-5) into star character strings.
    Usage:
        {{ product.rating|render_star_rating }}
    """
    try:
        rating = int(float(value or 5))
    except (ValueError, TypeError):
        rating = 5

    rating = max(1, min(5, rating))

    stars = {
        5: "★★★★★",
        4: "★★★★☆",
        3: "★★★☆☆",
        2: "★★☆☆☆",
        1: "★☆☆☆☆",
    }
    return stars.get(rating, "★★★★★")

@register.filter(name="format_facet_count")
def format_facet_count(count: Optional[Union[int, str]]) -> str:
    """
    Formats count numbers into clean badge strings.
    Usage:
        {{ option.count|format_facet_count }} -> "(12)"
    """
    try:
        num = int(count or 0)
        return f"({num:,})"
    except (ValueError, TypeError):
        return "(0)"

@register.filter(name="has_active_filter")
def has_active_filter(request_get: Any, param_name: str) -> bool:
    """
    Checks if a given GET parameter is active in the current request.
    """
    if not request_get:
        return False
    return param_name in request_get and bool(request_get.get(param_name))