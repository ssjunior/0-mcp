"""MCP bridge must wrap the view in async-capable project middleware,
skip sync-only middleware, and cache the chain by view class."""
from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.http import JsonResponse
from django.test import RequestFactory, override_settings

from zeromcp.mcp import bridge


class AsyncTagger:
    """Async-capable middleware that tags the response."""
    async_capable = True
    sync_capable = False

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(self.get_response):
            markcoroutinefunction(self)

    async def __call__(self, request):
        response = await self.get_response(request)
        response['X-Async-Tag'] = '1'
        return response


class SyncOnlyTagger:
    """Sync-only middleware — bridge must skip it."""
    async_capable = False
    sync_capable = True

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response['X-Sync-Tag'] = '1'
        return response


class _FakeView:
    """Stand-in for ``view_cls.as_view()`` — returns a fresh callable each call."""
    @classmethod
    def as_view(cls):
        async def view(request):
            return JsonResponse({'ok': True})
        return view


def setup_function(_fn):
    bridge._chain_cache.clear()


@override_settings(MIDDLEWARE=[
    'tests.test_mcp_middleware_chain.AsyncTagger',
    'tests.test_mcp_middleware_chain.SyncOnlyTagger',
])
async def test_async_middleware_runs_sync_only_skipped():
    handler = bridge._build_middleware_chain(_FakeView)
    response = await handler(RequestFactory().get('/'))
    assert response['X-Async-Tag'] == '1'
    assert 'X-Sync-Tag' not in response


@override_settings(MIDDLEWARE=[
    'tests.test_mcp_middleware_chain.AsyncTagger',
])
async def test_chain_cached_by_view_cls():
    first = bridge._build_middleware_chain(_FakeView)
    second = bridge._build_middleware_chain(_FakeView)
    assert first is second


@override_settings(MIDDLEWARE=[
    'tests.test_mcp_middleware_chain.AsyncTagger',
])
async def test_distinct_view_classes_get_distinct_chains():
    class _OtherView:
        @classmethod
        def as_view(cls):
            async def view(request):
                return JsonResponse({})
            return view

    a = bridge._build_middleware_chain(_FakeView)
    b = bridge._build_middleware_chain(_OtherView)
    assert a is not b


@override_settings(MIDDLEWARE=[
    'tests.test_mcp_middleware_chain.AsyncTagger',
    'zeromcp.middleware.ExceptionMiddleware',
])
async def test_unimportable_or_sync_only_does_not_crash_chain():
    # ExceptionMiddleware has no async_capable flag — it should be skipped,
    # AsyncTagger should still run.
    handler = bridge._build_middleware_chain(_FakeView)
    response = await handler(RequestFactory().get('/'))
    assert response['X-Async-Tag'] == '1'
