import environ
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(DEBUG=(bool, False))
environ.Env.read_env(BASE_DIR / '.env')

SECRET_KEY = env('SECRET_KEY')
DEBUG = env('DEBUG')
ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['localhost'])

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'rest_framework',
    'channels',          # Django Channels — WebSocket support
    'core',
    'django_celery_beat',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'bulkmed.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'bulkmed.wsgi.application'

import os
# Use DATABASE_URL from .env if set, otherwise fall back to SQLite for local dev
_db_url = os.environ.get('DATABASE_URL', '')
if _db_url and not _db_url.startswith('postgres://user:password'):
    DATABASES = {'default': env.db('DATABASE_URL')}
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

# Password validators disabled for dev/demo — only confirmation match is enforced.
AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
}

LOGIN_REDIRECT_URL = '/go/'   # smart redirect — routes store→/dashboard/, factory→/factory/
LOGOUT_REDIRECT_URL = '/'    # back to the public landing page
LOGIN_URL = '/accounts/login/'

# Email (console backend for dev — swap for SMTP in production)
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
EMAIL_HOST = 'smtp.gmail.com'
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = env('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = env('EMAIL_HOST_PASSWORD', default='')
DEFAULT_FROM_EMAIL = 'BulkMed <noreply@bulkmed.in>'

# Commission rate (5%)
PLATFORM_COMMISSION_RATE = 0.05

# Razorpay payment gateway
RAZORPAY_KEY_ID     = env('RAZORPAY_KEY_ID',     default='rzp_test_XXXXXXXXXXXXXXXX')
RAZORPAY_KEY_SECRET = env('RAZORPAY_KEY_SECRET',  default='XXXXXXXXXXXXXXXXXXXXXXXX')

# ── 3PL Logistics ─────────────────────────────────────────────────────────────
# Set LOGISTICS_PROVIDER to 'delhivery', 'shadowfax', or 'mock' (default).
# 'mock' returns realistic fake data without any real API calls — safe for dev.
LOGISTICS_PROVIDER = env('LOGISTICS_PROVIDER', default='mock')

# Delhivery (required when LOGISTICS_PROVIDER='delhivery')
DELHIVERY_API_TOKEN = env('DELHIVERY_API_TOKEN', default='')
DELHIVERY_WAREHOUSE = env('DELHIVERY_WAREHOUSE', default='BulkMed-WH1')

# Shadowfax (required when LOGISTICS_PROVIDER='shadowfax')
SHADOWFAX_CLIENT_ID     = env('SHADOWFAX_CLIENT_ID',     default='')
SHADOWFAX_CLIENT_SECRET = env('SHADOWFAX_CLIENT_SECRET', default='')

# Webhook token — providers POST to /webhooks/logistics/?token=<this>
# Generate a strong random string: python -c "import secrets; print(secrets.token_hex(32))"
LOGISTICS_WEBHOOK_TOKEN = env('LOGISTICS_WEBHOOK_TOKEN', default='')

# Order pool settings
POOL_WAIT_HOURS = 48
PREDICTION_ALERT_DAYS = 15

# ── Celery ────────────────────────────────────────────────────────────────────
CELERY_BROKER_URL = env('REDIS_URL', default='redis://localhost:6379/0')
CELERY_RESULT_BACKEND = env('REDIS_URL', default='redis://localhost:6379/0')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE

# Celery Beat — periodic task schedule
from celery.schedules import crontab

CELERY_BEAT_SCHEDULE = {
    # Lock expired pools, assign nearest factory, create deliveries — every hour
    'lock-expired-pools-hourly': {
        'task': 'core.tasks.task_lock_expired_pools',
        'schedule': crontab(minute=0),
    },
    # AI seasonal demand predictions — daily at midnight
    'update-predictions-daily': {
        'task': 'core.tasks.task_update_seasonal_predictions',
        'schedule': crontab(hour=0, minute=0),
    },
    # Auto-release pending payouts after 48h dispute-free window — every 6 hours
    'auto-release-payouts': {
        'task': 'core.tasks.task_auto_release_payouts',
        'schedule': crontab(minute=0, hour='*/6'),  # 00:00, 06:00, 12:00, 18:00
    },
    # Low-stock push notifications — every 4 hours
    'low-stock-alerts': {
        'task': 'core.tasks.task_low_stock_alerts',
        'schedule': crontab(minute=0, hour='*/4'),  # 00:00, 04:00, 08:00, 12:00, 16:00, 20:00
    },
    # Auto-disable products near expiry — daily at 1 AM
    'disable-expiring-products': {
        'task': 'core.tasks.task_disable_expiring_products',
        'schedule': crontab(hour=1, minute=0),
    },
    # Minimum Active Pool Guarantee — every 2 hours
    # Creates a new open pool for any product that has stock but no live pool.
    # Prevents the storefront from showing "No open pools" after expiry.
    'ensure-pool-coverage': {
        'task': 'core.tasks.task_ensure_pool_coverage',
        'schedule': crontab(minute=30),   # :30 past every hour, offset from lock task
    },
}

# Allow Razorpay test popups (mock bank/OTP pages) to communicate back
# to the parent window. Without this, Django's default 'same-origin' COOP
# header blocks the cross-origin popup and causes the about:blank hang.
SECURE_CROSS_ORIGIN_OPENER_POLICY = 'same-origin-allow-popups'
# ASGI application — replaces WSGI_APPLICATION for WebSocket support
ASGI_APPLICATION = 'bulkmed.asgi.application'

# Channel layers — Redis backend so messages survive across multiple workers
# and can be sent from any Django view/task, not just the consumer process.
CHANNEL_LAYERS = {
    'default': {
        # Use in-memory channel layer when Redis is not available (dev only).
        # Switch to RedisChannelLayer in production or when running Daphne.
        'BACKEND': 'channels.layers.InMemoryChannelLayer',
    },
}

# Uncomment below and comment above when Redis is running:
# CHANNEL_LAYERS = {
#     'default': {
#         'BACKEND': 'channels_redis.core.RedisChannelLayer',
#         'CONFIG': {
#             'hosts': [env('REDIS_URL', default='redis://localhost:6379/0')],
#         },
#     },
# }
