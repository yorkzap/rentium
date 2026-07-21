# ruff: noqa
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include
from django.urls import path
from django.views import defaults as default_views
from django.views.generic import TemplateView
from drf_spectacular.views import SpectacularAPIView
from drf_spectacular.views import SpectacularSwaggerView

from rentium.appointments.api import urls as appointments_urls
from rentium.comms.api import urls as comms_urls
from rentium.messaging.api import urls as messaging_urls
from rentium.showcase.api import urls as showcase_urls
from rentium.users.api.views import CustomObtainAuthToken
from rentium.users.api.views import password_reset_confirm_view
from rentium.users.api.views import password_reset_request_view
from rentium.users.api.views import resend_verification_email
from rentium.users.api.views import verify_email_confirm

urlpatterns = [
    path("", TemplateView.as_view(template_name="pages/home.html"), name="home"),
    path(
        "about/",
        TemplateView.as_view(template_name="pages/about.html"),
        name="about",
    ),
    # Django Admin, use {% url 'admin:index' %}
    path(settings.ADMIN_URL, admin.site.urls),
    # User management
    path("users/", include("rentium.users.urls", namespace="users")),
    path("accounts/", include("allauth.urls")),
    # Media files
    *static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT),
]

if settings.DEBUG:
    urlpatterns += staticfiles_urlpatterns()

# API URLS
urlpatterns += [
    path("api/users/", include("rentium.users.api.urls")),
    # Auth helpers under /api/users/<action>/ MUST be registered before the
    # DefaultRouter users viewset below — otherwise DRF treats the action name
    # as a user pk (e.g. password-reset → /api/users/<pk>/) and returns 403.
    path(
        "api/users/verify-email/confirm/",
        verify_email_confirm,
        name="verify-email-confirm",
    ),
    path(
        "api/users/resend-verification/",
        resend_verification_email,
        name="resend-verification",
    ),
    path(
        "api/users/password-reset/",
        password_reset_request_view,
        name="password-reset",
    ),
    path(
        "api/users/password-reset/confirm/",
        password_reset_confirm_view,
        name="password-reset-confirm",
    ),
    path("api/leases/", include("rentium.leases.api.urls", namespace="leases_api")),
    path(
        "api/properties/",
        include("rentium.properties.api.urls", namespace="properties_api"),
    ),
    # Root-level cross-app resources ONLY (users/landlords/tenants). Do not add
    # domain viewsets (leases, properties, etc.) here — see config/api_router.py
    # for why: registering the same viewset in two places causes silent URL
    # shadowing.
    path("api/", include("config.api_router")),
    # Notifications feed. The events router registers "notifications", so this
    # include yields /api/notifications/, /unread_count/, etc. It must be a
    # separate include because the events router was never part of
    # config.api_router.
    path("api/", include("rentium.events.api.urls")),
    # DRF auth token
    path("api/auth-token/", CustomObtainAuthToken.as_view(), name="auth-token"),
    path("api/schema/", SpectacularAPIView.as_view(), name="api-schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="api-schema"),
        name="api-docs",
    ),
    path("api/ledger/", include("rentium.ledger.api.urls")),
    path("api/attention/", include("rentium.attention.urls")),
    path("api/rama/", include("rentium.rama.urls")),
    path(
        "api/comms/",
        include((comms_urls.urlpatterns, "comms"), namespace="comms_api"),
    ),
    path("api/maintenance/", include("rentium.maintenance.api.urls")),
    path("api/messaging/", include("rentium.messaging.api.urls")),
    path("api/agenda/", include("rentium.agenda.api.urls")),
    path(
        "api/appointments/",
        include(
            (appointments_urls.urlpatterns, "appointments"),
            namespace="appointments_api",
        ),
    ),
    # =======================================================================
    # PUBLIC, UNAUTHENTICATED SURFACE
    #
    # Every route below /api/public/ is reachable by anyone on the internet with
    # no token. They are mounted here, together, deliberately: the whole public
    # surface of this application is these eight lines, and a reviewer can see
    # all of it without opening a single viewset.
    #
    # DEFAULT_PERMISSION_CLASSES is IsAuthenticated, so each of these views has to
    # declare AllowAny explicitly to work at all. You cannot accidentally publish
    # an endpoint in this codebase — publishing one requires writing the word
    # AllowAny, and that shows up in a diff.
    #
    # Two includes share the prefix. They don't collide: appointments owns
    # properties/ and viewing-requests/; showcase owns cities/, l/, listings/,
    # inquiries/ and sitemap-data/. Keeping them as separate lists (rather than
    # merging them) is what makes the auth boundary legible from this file.
    # =======================================================================
    path(
        "api/public/",
        include(
            (appointments_urls.public_urlpatterns, "appointments_public"),
            namespace="appointments_public",
        ),
    ),
    path(
        "api/public/",
        include(
            (showcase_urls.public_urlpatterns, "showcase_public"),
            namespace="showcase_public",
        ),
    ),
    # Telegram's inbound webhook: auth is a secret header (checked in the
    # view), never a session — see comms/api/views.py:telegram_webhook.
    path(
        "api/public/",
        include(
            (comms_urls.public_urlpatterns, "comms_public"),
            namespace="comms_public",
        ),
    ),
    # The prospect's tokenized chat thread: auth is the per-conversation
    # access_token in the URL, PII-minimized payload — see
    # messaging/api/public_views.py.
    path(
        "api/public/",
        include(
            (messaging_urls.public_urlpatterns, "messaging_public"),
            namespace="messaging_public",
        ),
    ),
    # Landlord-authenticated showcase surface: the opt-in settings page, the
    # inquiry inbox, and address autocomplete (which proxies Geoapify so its key
    # never reaches a browser).
    path(
        "api/showcase/",
        include((showcase_urls.urlpatterns, "showcase"), namespace="showcase"),
    ),
]

if settings.DEBUG:
    urlpatterns += [
        path(
            "400/",
            default_views.bad_request,
            kwargs={"exception": Exception("Bad Request!")},
        ),
        path(
            "403/",
            default_views.permission_denied,
            kwargs={"exception": Exception("Permission Denied")},
        ),
        path(
            "404/",
            default_views.page_not_found,
            kwargs={"exception": Exception("Page not Found")},
        ),
        path("500/", default_views.server_error),
    ]

    if "debug_toolbar" in settings.INSTALLED_APPS:
        import debug_toolbar

        urlpatterns = [path("__debug__/", include(debug_toolbar.urls))] + urlpatterns
