from typing import Any, Dict

from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext_lazy as _
from django.views import View

from .forms import AuditFilterForm
from .selectors import get_audit_log_by_id, get_audit_logs

class StaffRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self) -> bool:
        return getattr(self.request.user, "is_staff", False) or getattr(self.request.user, "is_superuser", False)

class AuditLogListView(StaffRequiredMixin, View):
    """
    Renders administrative system audit log history.
    """
    template_name = "audit/audit_log.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = AuditFilterForm(request.GET)
        action = request.GET.get("action", "")
        severity = request.GET.get("severity", "")

        logs = get_audit_logs(action=action, severity=severity)[:100]

        return render(request, self.template_name, {
            "form": form,
            "logs": logs,
        })

class AuditLogDetailView(StaffRequiredMixin, View):
    """
    Detailed JSON change diff view for a single audit record.
    """
    template_name = "audit/audit_detail.html"

    def get(self, request: HttpRequest, pk: int, *args: Any, **kwargs: Any) -> HttpResponse:
        log_entry = get_audit_log_by_id(pk)
        if not log_entry:
            raise Http404(_("Audit log entry not found."))

        return render(request, self.template_name, {
            "log": log_entry,
        })