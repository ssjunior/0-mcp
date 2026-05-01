"""Bootstrap Django for standalone scripts.

Importing this module calls ``django.setup()`` so a script can use the
ORM (models, queries) without going through ``manage.py``. Inside a
fully-initialised Django process the call is a no-op, so it's safe to
import from modules that may run in either context.

Configuration:
- If ``DJANGO_SETTINGS_MODULE`` is already set, it is used as-is.
- Otherwise, the module checked is ``ZEROMCP_DEFAULT_SETTINGS`` (env).
- Otherwise, it falls back to ``settings.settings`` — the layout used
  by the projects this framework grew up with.

Set ``ZEROMCP_DEFAULT_SETTINGS=myproject.settings`` in your environment
(or ``.env``) when your project does not follow the ``settings/settings.py``
package layout.
"""
import os

import django

if not os.getenv('DJANGO_SETTINGS_MODULE'):
    os.environ['DJANGO_SETTINGS_MODULE'] = os.getenv(
        'ZEROMCP_DEFAULT_SETTINGS', 'settings.settings',
    )

django.setup()
