from pathlib import Path
import os

try:
    import dj_database_url  # type: ignore[reportMissingImports]
except Exception:
    dj_database_url = None

# ==========================================
# BASE DIRECTORY
# ==========================================
BASE_DIR = Path(__file__).resolve().parent.parent


def _load_local_env_file():
    env_path = BASE_DIR / ".env.local"
    if not env_path.exists():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass


_load_local_env_file()

# ==========================================
# SECURITY
# ==========================================
SECRET_KEY = 'django-insecure-pradhan-form-portal-secret-key-change-in-production'

# Local dev default True rakho; production me env DEBUG=False set karo.
DEBUG = os.getenv('DEBUG', 'True').lower() == 'true'

ALLOWED_HOSTS = [h.strip() for h in os.getenv('ALLOWED_HOSTS', '*').split(',') if h.strip()]
CSRF_TRUSTED_ORIGINS = [u.strip() for u in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if u.strip()]

# ==========================================
# INSTALLED APPS
# ==========================================
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Aapke apps
    'accounts',
    'vacancies',
    'core',
    # Cloudinary apps
    'cloudinary_storage',
    'cloudinary',
]

# ==========================================
# MIDDLEWARE
# ==========================================
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'accounts.middleware.AdminAccessMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# ==========================================
# URLs & WSGI
# ==========================================
ROOT_URLCONF = 'portal_main.urls'
WSGI_APPLICATION = 'portal_main.wsgi.application'

# ==========================================
# TEMPLATES
# ==========================================
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
                'core.context_processors.breadcrumbs',
            ],
        },
    },
]

# ==========================================
# DATABASE
# ==========================================
_db_url = os.getenv('DATABASE_URL')
if _db_url and dj_database_url:
    DATABASES = {
        'default': dj_database_url.parse(_db_url, conn_max_age=600, ssl_require=True),
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

# ==========================================
# STATIC & MEDIA FILES
# ==========================================
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# ==========================================
# STORAGE (Media)
# ==========================================
# Cloudinary ko production me enable karne ke liye environment variables set karo:
# - CLOUDINARY_URL  (recommended)  OR
# - CLOUDINARY_CLOUD_NAME + CLOUDINARY_API_KEY + CLOUDINARY_API_SECRET
# Local dev / offline me Cloudinary fail hone se 500 aa jata hai (uploads), isliye default filesystem rakha hai.
_cloudinary_url = os.getenv("CLOUDINARY_URL", "").strip()
_cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
_cloud_key = os.getenv("CLOUDINARY_API_KEY", "").strip()
_cloud_secret = os.getenv("CLOUDINARY_API_SECRET", "").strip()
USE_CLOUDINARY = bool(_cloudinary_url or (_cloud_name and _cloud_key and _cloud_secret))

if USE_CLOUDINARY:
    CLOUDINARY_STORAGE = {
        "CLOUD_NAME": _cloud_name,
        "API_KEY": _cloud_key,
        "API_SECRET": _cloud_secret,
    }
    _cloudinary_proxy = os.getenv("CLOUDINARY_API_PROXY", "").strip()
    if _cloudinary_proxy:
        CLOUDINARY_STORAGE["API_PROXY"] = _cloudinary_proxy

    DEFAULT_FILE_STORAGE = "cloudinary_storage.storage.MediaCloudinaryStorage"
    STORAGES = {
        "default": {"BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
else:
    DEFAULT_FILE_STORAGE = "django.core.files.storage.FileSystemStorage"
    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": MEDIA_ROOT, "base_url": MEDIA_URL},
        },
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }

# ==========================================
# CASHFREE PAYMENT
# ==========================================
CASHFREE_CLIENT_ID = os.getenv("CASHFREE_CLIENT_ID", "").strip()
CASHFREE_CLIENT_SECRET = os.getenv("CASHFREE_CLIENT_SECRET", "").strip()
CASHFREE_MODE = os.getenv("CASHFREE_MODE", "sandbox").strip().lower() or "sandbox"
CASHFREE_API_VERSION = os.getenv("CASHFREE_API_VERSION", "2025-01-01").strip() or "2025-01-01"
CASHFREE_ENABLED = bool(CASHFREE_CLIENT_ID and CASHFREE_CLIENT_SECRET)
CASHFREE_API_BASE = (
    "https://sandbox.cashfree.com/pg"
    if CASHFREE_MODE != "production"
    else "https://api.cashfree.com/pg"
)

# ==========================================
# LOGIN & SESSIONS
# ==========================================
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/accounts/login/'

SESSION_COOKIE_AGE = 86400 # 24 Hours
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_SAVE_EVERY_REQUEST = False

# ==========================================
# LANGUAGE & TIMEZONE
# ==========================================
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
