import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings.settings')
os.environ.setdefault('REDIS_SERVER', 'localhost')
os.environ.setdefault('REDIS_DB', '0')

import django  # noqa: E402

django.setup()

import fakeredis.aioredis  # noqa: E402
import pytest  # noqa: E402

from zeromcp import redis_config  # noqa: E402


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_config, '_redis_client', client)
    yield client


@pytest.fixture
def redis(fake_redis):
    return fake_redis
