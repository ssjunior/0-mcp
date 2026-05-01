SECRET_KEY = 'test-secret-key'
DEBUG = True
ALLOWED_HOSTS = ['*']
USE_TZ = True
TIME_ZONE = 'UTC'

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'tests.testapp',
]

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

MCP = {
    'COOKIE_ID': 'sid',
    'API_SESSION_TTL': 300,
}
