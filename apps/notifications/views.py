from typing import Any, Dict

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext_lazy as _
from django.views import View

from .forms import NotificationPreferenceForm
from .selectors import get_user_notification_logs, get_user_preferences

class CustomerNotificationListView(LoginRequiredMixin, View):
    """
    Renders customer notification audit history.
    """
    template_name = "notifications/list.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        logs = get_user_notification_logs(request.user)
        return render(request, self.template_name, {
            "logs": logs,
        })

class NotificationPreferenceView(LoginRequiredMixin, View):
    """
    Allows customers to update their notification opt-in preferences.
    """
    template_name = "notifications/preferences.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        pref = get_user_preferences(request.user)
        form = NotificationPreferenceForm(instance=pref)
        return render(request, self.template_name, {"form": form})

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        pref = get_user_preferences(request.user)
        form = NotificationPreferenceForm(request.POST, instance=pref)
        if form.is_valid():
            form.save()
            messages.success(request, _("Notification preferences updated successfully."))
            return redirect("notifications:preferences")

        return render(request, self.template_name, {"form": form})