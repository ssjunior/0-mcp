"""``return_result`` raises a clear TypeError when an override returned
an HttpResponse instead of the dict the framework expects.

Regression: a project override of ``update_obj`` returning ``JsonResponse``
crashed with ``AttributeError: 'JsonResponse' object has no attribute 'pop'``
because ``return_result`` iterates the result and pops disallowed keys.
The contract is "return a dict (or raise HTTPException) and let the
framework serialise" — fail loudly instead of pretending to work."""
import pytest
from django.http import HttpResponse, JsonResponse

from zeromcp.base import BaseResource
from tests.testapp.models import Space


class _Resource(BaseResource):
    model = Space
    authenticated = False


@pytest.mark.asyncio
async def test_jsonresponse_from_override_raises_typeerror():
    res = _Resource()
    with pytest.raises(TypeError) as exc:
        await res.return_result(JsonResponse({'success': False}))
    msg = str(exc.value)
    assert 'HttpResponse' in msg
    assert 'must return a dict' in msg


@pytest.mark.asyncio
async def test_plain_httpresponse_from_override_raises_typeerror():
    res = _Resource()
    with pytest.raises(TypeError):
        await res.return_result(HttpResponse('boom'))


@pytest.mark.asyncio
async def test_dict_passes_through_and_pops_unknown_keys():
    res = _Resource()
    res.edit_fields = ['id', 'name']
    res.edit_related_fields = {}
    res.related_models = {}
    res.m2m_fields = []
    res.edit_exclude_fields = []
    res.normalize_obj = False
    out = await res.return_result({'id': 1, 'name': 'x', 'secret': 'no'})
    assert out == {'id': 1, 'name': 'x'}
