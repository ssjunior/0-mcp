from .tenant import db_state


class DBRouter:
    def db_for_read(self, model, **hints):
        if model._meta.app_config.label == 'master':
            account_db = 'default'
        elif model._meta.app_config.label == 'queue':
            account_db = 'queue'
        else:
            account_db = db_state.get()

        return account_db

    def db_for_write(self, model, **hints):
        if model._meta.app_config.label == 'master':
            account_db = 'default'
        elif model._meta.app_config.label == 'queue':
            account_db = 'queue'
        else:
            account_db = db_state.get()

        return account_db

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        should_migrate = False

        if app_label == 'master':
            should_migrate = db == 'default'

        elif app_label == 'queue':
            should_migrate = db == 'queue'

        elif app_label not in ['queue', 'master'] and db not in ['queue', 'default']:
            should_migrate = True

        return should_migrate
