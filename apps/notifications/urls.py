from django.urls import path
from .views import CustomerNotificationListView, NotificationPreferenceView

app_name = "notifications"

urlpatterns = [
    path("history/", CustomerNotificationListView.as_view(), name="list"),
    path("preferences/", NotificationPreferenceView.as_view(), name="preferences"),
]