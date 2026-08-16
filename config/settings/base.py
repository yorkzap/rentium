# ruff: noqa: ERA001, E501
"""Base settings to build other settings files upon."""

import ssl
from pathlib import Path

import environ
from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve(strict=True).parent.parent.parent
# rentium/
APPS_DIR = BASE_DIR / "rentium"
env = environ.Env()

READ_DOT_ENV_FILE = env.bool("DJANGO_READ_DOT_ENV_FILE", default=False)
if READ_DOT_ENV_FILE:
    # OS environment variables take precedence over variables from .env
    env.read_env(str(BASE_DIR / ".env"))

# GENERAL
# ------------------------------------------------------------------------------
DEBUG = env.bool("DJANGO_DEBUG", False)
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en-us"
SITE_ID = 1
USE_I18N = True
USE_TZ = True
LOCALE_PATHS = [str(BASE_DIR / "locale")]

# DATABASES
# ------------------------------------------------------------------------------
DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["ATOMIC_REQUESTS"] = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# URLS
# ------------------------------------------------------------------------------
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

# APPS
# ------------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.sites",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",  # Handy template tags
    "django.contrib.admin",
    "django.forms",
]
THIRD_PARTY_APPS = [
    "crispy_forms",
    "crispy_bootstrap5",
    "allauth",
    "allauth.account",
    "allauth.mfa",
    "allauth.socialaccount",
    "django_celery_beat",
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    "drf_spectacular",
    "webpack_loader",
    "django_filters",
]

LOCAL_APPS = [
    "rentium.users",
    # Your stuff: custom apps go here
    "rentium.properties",
    "rentium.leases",
    "rentium.events",
    "rentium.ledger",
    "rentium.maintenance",
    "rentium.messaging",
    "rentium.agenda",
    "rentium.appointments",
    # The public, logged-out layer: landlord showcase pages, city pages, property
    # pages, inquiries. Deliberately its own app so its serializers can never
    # accidentally inherit an internal one and leak a street address.
    "rentium.showcase",
    # RAMA: the reasoning layer over the service functions above. The model
    # orchestrates, the app computes — see docs/rama-architecture.md in the
    # frontend repo.
    "rentium.rama",
    # Communication channels (Telegram now; email/WhatsApp behind the same
    # abstraction later) — where RAMA reaches the landlord outside the app.
    "rentium.comms",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# MIGRATIONS
# ------------------------------------------------------------------------------
MIGRATION_MODULES = {"sites": "rentium.contrib.sites.migrations"}

# AUTHENTICATION
# ------------------------------------------------------------------------------
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
AUTH_USER_MODEL = "users.User"
LOGIN_REDIRECT_URL = "users:redirect"
LOGIN_URL = "account_login"

# PASSWORDS
# ------------------------------------------------------------------------------
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# MIDDLEWARE
# ------------------------------------------------------------------------------
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]

# STATIC
# ------------------------------------------------------------------------------
STATIC_ROOT = str(BASE_DIR / "staticfiles")
STATIC_URL = "/static/"
STATICFILES_DIRS = [str(APPS_DIR / "static")]
STATICFILES_FINDERS = [
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
]

# MEDIA
# ------------------------------------------------------------------------------
MEDIA_ROOT = str(APPS_DIR / "media")
MEDIA_URL = "/media/"

# CACHE
# ------------------------------------------------------------------------------
# Redis is already here for Celery. The address-autocomplete proxy caches results
# (rentium/core/geo.py) — a street address does not move, and the Geoapify free
# tier is 3,000 requests a day, so caching a repeated lookup is the difference
# between "fine forever" and "broken by Tuesday".
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env("REDIS_URL", default="redis://redis:6379/0"),
    }
}

# TEMPLATES
# ------------------------------------------------------------------------------
# NOTE: the ONLY server-rendered templates in this project are outbound EMAIL
# bodies (rentium/templates/emails/*). Every user-facing surface is the Next.js
# app talking to this API. An email body is a string handed to an SMTP server —
# there's no browser involved, so the frontend cannot produce it.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [str(APPS_DIR / "templates")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.template.context_processors.i18n",
                "django.template.context_processors.media",
                "django.template.context_processors.static",
                "django.template.context_processors.tz",
                "django.contrib.messages.context_processors.messages",
                "rentium.users.context_processors.allauth_settings",
            ],
        },
    },
]

FORM_RENDERER = "django.forms.renderers.TemplatesSetting"
CRISPY_TEMPLATE_PACK = "bootstrap5"
CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"

# FIXTURES
# ------------------------------------------------------------------------------
FIXTURE_DIRS = (str(APPS_DIR / "fixtures"),)

# SECURITY
# ------------------------------------------------------------------------------
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"

# EMAIL
# ------------------------------------------------------------------------------
# DEV: cookiecutter ships Mailpit. Nothing to sign up for — it listens on
# mailpit:1025, swallows every outbound message, and you read them at
# http://localhost:8025. In your local .env:
#     DJANGO_EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
#     EMAIL_HOST=mailpit
#     EMAIL_PORT=1025
#
# PROD: sign up with a transactional provider (Postmark, Mailgun, SendGrid, SES)
# and paste their SMTP credentials into these same env vars — the config is
# provider-agnostic on purpose. You ALSO need SPF + DKIM DNS records for
# rentium.ca at your registrar, or lease invites will land in spam.
EMAIL_BACKEND = env(
    "DJANGO_EMAIL_BACKEND",
    default="django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = env("EMAIL_HOST", default="mailpit")
EMAIL_PORT = env.int("EMAIL_PORT", default=1025)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=False)
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = env(
    "DJANGO_DEFAULT_FROM_EMAIL", default="Rentium <no-reply@rentium.ca>"
)
SERVER_EMAIL = env("DJANGO_SERVER_EMAIL", default=DEFAULT_FROM_EMAIL)

# ADMIN
# ------------------------------------------------------------------------------
ADMIN_URL = "admin/"
ADMINS = [("""Raj Singh""", "raj-singh@rentium.ca")]
MANAGERS = ADMINS
DJANGO_ADMIN_FORCE_ALLAUTH = env.bool("DJANGO_ADMIN_FORCE_ALLAUTH", default=False)

# LOGGING
# ------------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "%(levelname)s %(asctime)s %(module)s %(process)d %(thread)d %(message)s",
        },
    },
    "handlers": {
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {"level": "INFO", "handlers": ["console"]},
}

REDIS_URL = env("REDIS_URL", default="redis://redis:6379/0")
REDIS_SSL = REDIS_URL.startswith("rediss://")

# Celery
# ------------------------------------------------------------------------------
if USE_TZ:
    CELERY_TIMEZONE = TIME_ZONE
CELERY_BROKER_URL = REDIS_URL
CELERY_BROKER_USE_SSL = {"ssl_cert_reqs": ssl.CERT_NONE} if REDIS_SSL else None
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_REDIS_BACKEND_USE_SSL = CELERY_BROKER_USE_SSL
CELERY_RESULT_EXTENDED = True
CELERY_RESULT_BACKEND_ALWAYS_RETRY = True
CELERY_RESULT_BACKEND_MAX_RETRIES = 10
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TASK_TIME_LIMIT = 5 * 60
CELERY_TASK_SOFT_TIME_LIMIT = 60
# A RAMA turn is an interactive model loop, not an ordinary job: it runs several
# provider round-trips and is meant to stop itself politely at
# RAMA_TURN_BUDGET_SECONDS. The default 60s soft limit sat BELOW that budget, so
# Celery killed the task before the graceful stop could ever fire and the
# landlord got "something broke" instead of a partial answer. Turn-running tasks
# opt into this larger limit; run_turn clamps its own budget to whatever the
# caller actually grants, so the two can never disagree again.
RAMA_TURN_BUDGET_SECONDS = 90
# Room for the final model round that produces the answer, plus persistence and
# the outbound send, after the loop stops accepting new rounds.
RAMA_TURN_TASK_HEADROOM_SECONDS = 30
RAMA_TURN_TASK_SOFT_TIME_LIMIT = (
    RAMA_TURN_BUDGET_SECONDS + RAMA_TURN_TASK_HEADROOM_SECONDS
)
RAMA_TURN_TASK_TIME_LIMIT = RAMA_TURN_TASK_SOFT_TIME_LIMIT + 60
# Batch tasks (morning briefings, weekly deliberation) run one whole turn PER
# LANDLORD, so they need many turns' worth. They are off the interactive path,
# so the ceiling is generous.
RAMA_TURN_BATCH_SOFT_TIME_LIMIT = 15 * 60
RAMA_TURN_BATCH_TIME_LIMIT = RAMA_TURN_BATCH_SOFT_TIME_LIMIT + 5 * 60
# The SAME drift, one layer down: the providers hardcoded 25s while a turn had
# 90. Asked "what rooms have we actually recorded", RAMA fetched the portfolio
# snapshot — 66KB, ~16k tokens — and the next call took longer than 25s, so the
# landlord got "Could not reach the openai API" while 65 seconds of budget went
# unused. A single call may take most of a turn, but never all of it: leaving
# headroom is what lets the loop stop gracefully and answer with what it has.
RAMA_PROVIDER_TIMEOUT_SECONDS = RAMA_TURN_BUDGET_SECONDS - 20
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_WORKER_SEND_TASK_EVENTS = True
CELERY_TASK_SEND_SENT_EVENT = True
CELERY_WORKER_HIJACK_ROOT_LOGGER = False

# Beat schedule
# ------------------------------------------------------------------------------
# THIS WAS MISSING ENTIRELY. CELERY_BEAT_SCHEDULER was configured but no schedule
# was ever defined, which means nothing in rentium/ledger/daily.py has ever run:
# rent charges silently stop generating once the 45-day horizon lapses (around
# month two of a quiet lease, "Expected This Month" just drops to $0), leases
# never flip to EXPIRED, tenants never get "rent due soon" nudges, and
# maintenance SLA breaches are never detected. Every one of those jobs was
# written, wired, and then never scheduled.
#
# Requires BOTH a worker and beat to be running:
#   celeryworker:  celery -A config.celery_app worker -l info
#   celerybeat:    celery -A config.celery_app beat -l info
#
# Every task below is idempotent — a double-run posts nothing twice.
CELERY_BEAT_SCHEDULE = {
    # expire ended leases -> generate rent charges -> due-soon reminders ->
    # inspection delivery deadlines -> SLA check. The order matters and is
    # enforced inside daily.run_all().
    "rentium-daily-housekeeping": {
        "task": "rentium.ledger.tasks.run_daily_housekeeping",
        "schedule": crontab(hour=6, minute=15),
    },
    # SLA breaches are the one time-sensitive job — an RTA emergency work order
    # blowing its deadline at 9am shouldn't wait until tomorrow to alert anyone.
    "rentium-sla-breach-check": {
        "task": "rentium.ledger.tasks.check_sla_breaches",
        "schedule": crontab(minute=5),  # hourly at :05
    },
    # Safety net: re-dispatch DomainEvents published but never processed (worker
    # down, broker hiccup). Without it, a dropped event is a notification that
    # silently never arrives and nobody ever finds out it didn't.
    "rentium-replay-unprocessed-events": {
        "task": "rentium.events.tasks.replay_unprocessed_events",
        "schedule": crontab(minute="*/10"),
    },
    # Backfill net for properties that got an address without going through the
    # autocomplete (admin, import, hand-typed). The normal path already has
    # coordinates before this ever runs.
    "rentium-geocode-pending": {
        "task": "rentium.showcase.tasks.geocode_pending",
        "schedule": crontab(minute=25),  # hourly at :25
    },
    # Sergeants ($0-LLM watchers): min balances, deposit-return deadlines,
    # late-payment patterns, expense anomalies, cash surplus. After daily
    # housekeeping so the ledger/lease state it reads is already current.
    "rama-sergeants-daily": {
        "task": "rentium.rama.tasks.run_sergeants",
        "schedule": crontab(hour=6, minute=45),
    },
    # Morning briefing: deterministic digest to every channel that opted in
    # (ChannelAccount.prefs.briefing). After Sergeants so it can include
    # today's fresh insights.
    "rama-morning-briefing": {
        "task": "rentium.comms.tasks.send_morning_briefings",
        "schedule": crontab(hour=7, minute=0),
    },
    # One analysis a week, not one a day: a background agent that produces
    # something every morning trains people to stop reading it.
    "rama-treasurer-weekly": {
        "task": "rentium.rama.tasks.run_weekly_deliberation",
        "schedule": crontab(day_of_week=1, hour=7, minute=30),
    },
    # Let stale viewing negotiations die so they stop nagging the landlord and
    # free the per-property open-request cap. Overnight, off-peak.
    "appointments-expire-stale-viewings": {
        "task": "rentium.appointments.tasks.expire_stale_viewing_requests",
        "schedule": crontab(hour=3, minute=30),
    },
}

# django-allauth
# ------------------------------------------------------------------------------
ACCOUNT_ALLOW_REGISTRATION = env.bool("DJANGO_ACCOUNT_ALLOW_REGISTRATION", True)
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_EMAIL_REQUIRED = True
ACCOUNT_USERNAME_REQUIRED = False
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_ADAPTER = "rentium.users.adapters.AccountAdapter"
ACCOUNT_FORMS = {"signup": "rentium.users.forms.UserSignupForm"}
SOCIALACCOUNT_ADAPTER = "rentium.users.adapters.SocialAccountAdapter"
SOCIALACCOUNT_FORMS = {"signup": "rentium.users.forms.UserSocialSignupForm"}

# django-rest-framework
# -------------------------------------------------------------------------------
# The default permission is IsAuthenticated. Every public endpoint in
# rentium.showcase therefore has to declare AllowAny *explicitly* — which is
# exactly the property we want: you cannot accidentally publish an endpoint.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # Throttles are opt-in per view (no global default class), but the RATES all
    # live here so there's one place to look. appointments/public_views.py had a
    # comment literally asking for this and it was never added.
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_THROTTLE_RATES": {
        "public_read": "120/min",  # city / property / showcase page reads
        "inquiry": "5/hour",  # contact form: generous for a human, brutal for a bot
        "viewing_request": "5/hour",
        # Prospect replying in their tokenized chat thread — human-paced, but a
        # leaked token shouldn't let a bot flood the landlord's inbox.
        "public_chat_read": "120/min",
        "public_chat_send": "20/hour",
        # Address autocomplete proxies a METERED api (Geoapify, 3k/day free). One
        # landlord typing an address fires maybe 8 requests. A bot could burn the
        # daily quota in ninety seconds, so this is a real limit, not a formality.
        "address_search": "60/min",
        # Telegram's own retry/backoff means this only needs to absorb bursts,
        # not sustained traffic — one bot, one landlord base.
        "telegram_webhook": "60/min",
        "whatsapp_webhook": "60/min",
        # Password-reset emails can be abused as a free spam vector; keep tight.
        "password_reset": "5/hour",
    },
}

# django-cors-headers - https://github.com/adamchainz/django-cors-headers#setup
CORS_URLS_REGEX = r"^/api/.*$"

SPECTACULAR_SETTINGS = {
    "TITLE": "Rentium API",
    "DESCRIPTION": "Documentation of API endpoints of Rentium",
    "VERSION": "1.0.0",
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAdminUser"],
    "SCHEMA_PATH_PREFIX": "/api/",
}

# django-webpack-loader
# ------------------------------------------------------------------------------
WEBPACK_LOADER = {
    "DEFAULT": {
        "CACHE": not DEBUG,
        "STATS_FILE": BASE_DIR / "webpack-stats.json",
        "POLL_INTERVAL": 0.1,
        "IGNORE": [r".+\.hot-update.js", r".+\.map"],
    },
}

# Your stuff...
# ------------------------------------------------------------------------------
# Localhost origins are dev-only — gate them behind DEBUG so the DEFAULT never
# leaks a permissive CORS policy into production. (An explicit
# CORS_ALLOWED_ORIGINS env var still overrides this entirely.)
_cors_origin_defaults = ["https://rentium.ca", "https://www.rentium.ca"]
_cors_regex_defaults = [r"^https://[a-z0-9-]+\.rentium\.ca$"]
if DEBUG:
    _cors_origin_defaults += [
        "http://localhost:3000",  # Next.js development server
        "http://localhost:8081",  # Ignite Mobile development server
    ]
    _cors_regex_defaults += [r"^http://[a-z0-9-]+\.localhost:3000$"]

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=_cors_origin_defaults)
# Landlord vanity subdomains (raj.rentium.ca) are a wildcard — a fixed list
# can't enumerate them. Any browser call from a showcase page (e.g. a contact
# form) is same-scheme https on a *.rentium.ca host.
CORS_ALLOWED_ORIGIN_REGEXES = env.list(
    "CORS_ALLOWED_ORIGIN_REGEXES", default=_cors_regex_defaults
)
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "dnt",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
]

# The logged-in app (invite links, dashboard deep links).
FRONTEND_URL = env("FRONTEND_URL", default="http://localhost:3000")
# URLs RAMA hands to people must be stable production-facing links. FRONTEND_URL
# can be localhost (development) or the legacy app.rentium.ca redirect, so it is
# intentionally not the canonical link origin.
CANONICAL_FRONTEND_ORIGIN = env(
    "CANONICAL_FRONTEND_ORIGIN",
    default="https://www.rentium.ca",
)

# The PUBLIC, indexable site — canonical origin for /l/<slug> and
# /<province>/<city>. Same host as FRONTEND_URL today; split them the day the
# public site moves to its own domain, without touching any other code.
PUBLIC_SITE_URL = env("PUBLIC_SITE_URL", default=FRONTEND_URL)

# GEOAPIFY — address autocomplete AND geocoding, on one key.
# Free tier: 3,000 requests/day, no card. https://myprojects.geoapify.com
#
# Deliberately NOT exposed to the browser (no NEXT_PUBLIC_ twin): the frontend
# calls OUR endpoint (/api/showcase/address-search/) and Django proxies. A key in
# the browser is a key strangers spend, and 3k/day is a budget a bot burns in an
# afternoon.
#
# NOT used for map TILES. A single map view loads 150-250 tiles, so roughly
# fifteen visitors to one city page would exhaust the entire daily quota and the
# map would go blank for everyone, silently. Tiles come from Carto/OSM — free,
# unlimited, no key. See src/components/public/ListingMap.tsx.
GEOAPIFY_KEY = env("GEOAPIFY_KEY", default="")

# Email confirmation settings
ACCOUNT_EMAIL_CONFIRMATION_AUTHENTICATED_REDIRECT_URL = f"{FRONTEND_URL}/dashboard"
ACCOUNT_EMAIL_CONFIRMATION_ANONYMOUS_REDIRECT_URL = f"{FRONTEND_URL}/auth/login"

# RAMA
# ------------------------------------------------------------------------------
# Provider and model are configuration, not code: any provider with function
# calling slots in behind rentium/rama/providers/. Defaults are xAI Grok
# (switch to Anthropic/OpenAI in Django admin → RAMA configuration without
# redeploying). Env vars are bootstrap/fallback; the admin singleton wins
# when set (see rentium.rama.runtime.get_active_config). Staff can still
# override provider/model per chat request; whatever ran is stamped on
# every RamaAudit row.
# Web research for the Treasurer. `none` (the default) means it simply does
# not research — an unconfigured provider is a safe no-op, never an error.
# `fake` serves fixtures so evals are deterministic and CI needs no network.
RAMA_RESEARCH_BACKEND = env("RAMA_RESEARCH_BACKEND", default="none")
FIRECRAWL_API_KEY = env("FIRECRAWL_API_KEY", default="")

RAMA_ENABLED = env.bool("RAMA_ENABLED", default=True)
RAMA_PROVIDER = env("RAMA_PROVIDER", default="xai")
# grok-4-1-fast-* were retired by xAI on 2026-05-15; grok-4.3 is the current
# fast/cheap tier with strong tool calling.
RAMA_MODEL = env("RAMA_MODEL", default="grok-4.3")
# Sampling/limits for all providers. RAMA routes and phrases over tool
# results — temperature 0 for determinism; 4096 tokens so plan previews and
# full set listings never truncate mid-answer.
RAMA_TEMPERATURE = env.float("RAMA_TEMPERATURE", default=0.0)
RAMA_MAX_TOKENS = env.int("RAMA_MAX_TOKENS", default=4096)
# Semantic interpretation (rama/interpret.py): where a deterministic path needs
# to know what the landlord MEANT, the model classifies into a closed set the
# caller lists, and Python validates and executes. There is no finite list of
# ways to say "it's left the bank", and enumerating phrasings was losing turns.
# Off falls every call site back to its pattern matching — less smart, still
# correct — which is also what happens when no key is configured.
RAMA_SEMANTIC_INTERPRETATION = env.bool(
    "RAMA_SEMANTIC_INTERPRETATION", default=True
)
# Command-engine v2 retrieves a compact, request-relevant capability set
# instead of sending the full 100+ operation surface to every model turn.
RAMA_COMMAND_ENGINE_V2 = env.bool("RAMA_COMMAND_ENGINE_V2", default=True)
RAMA_TOOL_RETRIEVAL_LIMIT = env.int("RAMA_TOOL_RETRIEVAL_LIMIT", default=12)
# Per-role platform defaults for the agent hierarchy (General decides, FSA
# analyzes, Corporals execute on the landlord's cheap chat model). Empty =
# fall back to the landlord's chat provider with the role's default tier
# (see rama.runtime.get_role_config).
RAMA_GENERAL_PROVIDER = env("RAMA_GENERAL_PROVIDER", default="")
RAMA_GENERAL_MODEL = env("RAMA_GENERAL_MODEL", default="")
RAMA_FSA_PROVIDER = env("RAMA_FSA_PROVIDER", default="")
RAMA_FSA_MODEL = env("RAMA_FSA_MODEL", default="")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")
OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
XAI_API_KEY = env("XAI_API_KEY", default="")
GEMINI_API_KEY = env("GEMINI_API_KEY", default="")
MISTRAL_API_KEY = env("MISTRAL_API_KEY", default="")

# ------------------------------------------------------------------------------
# Comms: Telegram bot config. TELEGRAM_WEBHOOK_SECRET is set on the bot via
# setWebhook(secret_token=...) and must match what comms/api/views.py checks —
# it is the ONLY authentication on that public endpoint.
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_WEBHOOK_SECRET = env("TELEGRAM_WEBHOOK_SECRET", default="")
TELEGRAM_BOT_USERNAME = env("TELEGRAM_BOT_USERNAME", default="")

# Comms: WhatsApp (Meta Cloud API by default — pluggable in comms/whatsapp.py).
# All blank until a provider is wired, which keeps the transport a safe no-op
# and the webhook a 403. WHATSAPP_VERIFY_TOKEN is echoed on Meta's GET
# handshake; WHATSAPP_APP_SECRET (optional) verifies the POST HMAC signature.
WHATSAPP_TOKEN = env("WHATSAPP_TOKEN", default="")
WHATSAPP_PHONE_NUMBER_ID = env("WHATSAPP_PHONE_NUMBER_ID", default="")
WHATSAPP_VERIFY_TOKEN = env("WHATSAPP_VERIFY_TOKEN", default="")
WHATSAPP_APP_SECRET = env("WHATSAPP_APP_SECRET", default="")
WHATSAPP_API_VERSION = env("WHATSAPP_API_VERSION", default="v21.0")
