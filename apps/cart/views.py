"""
Main views package entry point.
Page views live in `apps.cart.views.pages` and API endpoints live in `apps.cart.views.api`.
This module is kept clean to avoid circular imports.
"""

from .views.api import *  # noqa: F401, F403
from .views.pages import *  # noqa: F401, F403