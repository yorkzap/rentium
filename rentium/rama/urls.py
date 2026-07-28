from django.urls import path

from . import views

app_name = "rama"

urlpatterns = [
    path("chat/", views.chat_view, name="chat"),
    path("upload/", views.upload_view, name="upload"),
    path("documents/", views.documents_view, name="documents"),
    path(
        "documents/<uuid:document_id>/",
        views.document_detail_view,
        name="document-detail",
    ),
    path(
        "documents/<uuid:document_id>/download/",
        views.document_download_view,
        name="document-download",
    ),
    path("general/chat/", views.general_chat_view, name="general-chat"),
    path("treasurer/chat/", views.treasurer_chat_view, name="treasurer-chat"),
    path("constitution/", views.constitution_view, name="constitution"),
    path("auto-actions/", views.auto_actions_view, name="auto-actions"),
    path(
        "auto-actions/<uuid:action_id>/undo/",
        views.auto_action_undo_view,
        name="auto-action-undo",
    ),
    path("treasurer/", views.treasurer_view, name="treasurer"),
    path("memory/", views.memory_view, name="memory"),
    path(
        "memory/<uuid:memory_id>/",
        views.memory_delete_view,
        name="memory-delete",
    ),
    path("insights/", views.insights_view, name="insights"),
    path(
        "capability-gaps/",
        views.capability_gaps_view,
        name="capability-gaps",
    ),
    path("insights/<int:insight_id>/", views.insight_detail_view, name="insight-detail"),
    path("holdings/", views.holdings_view, name="holdings"),
    path("bank-balances/", views.bank_balances_view, name="bank-balances"),
    path("config/", views.config_view, name="config"),
    path("portfolios/", views.portfolios_view, name="portfolios"),
    path("settings/", views.settings_view, name="settings"),
    path("state-of-the-union/", views.union_view, name="union"),
]
