from django.urls import path

from . import views

app_name = "rama"

urlpatterns = [
    path("chat/", views.chat_view, name="chat"),
    path("upload/", views.upload_view, name="upload"),
    path("general/chat/", views.general_chat_view, name="general-chat"),
    path("constitution/", views.constitution_view, name="constitution"),
    path("insights/", views.insights_view, name="insights"),
    path("insights/<int:insight_id>/", views.insight_detail_view, name="insight-detail"),
    path("holdings/", views.holdings_view, name="holdings"),
    path("bank-balances/", views.bank_balances_view, name="bank-balances"),
    path("config/", views.config_view, name="config"),
    path("portfolios/", views.portfolios_view, name="portfolios"),
    path("settings/", views.settings_view, name="settings"),
    path("state-of-the-union/", views.union_view, name="union"),
]
