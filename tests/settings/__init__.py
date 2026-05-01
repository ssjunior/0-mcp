DEFAULT_DATABASE = {
    'ENGINE': 'django.db.backends.sqlite3',
    'NAME': ':memory:',
}
TENANT_ACCOUNT_MODEL = 'testapp.Account'
TENANT_USER_MODEL = 'testapp.User'
TENANT_USER_API_MODEL = 'testapp.UserApi'
TENANT_DB_PREFIX = 'test'
HASH_LENGTH = 32
