"""Public export surface — script-only helpers must not leak to top level."""
import zeromcp


def test_set_default_not_exported_at_top_level():
    assert not hasattr(zeromcp, 'set_default'), (
        'set_default mutates the global default DB connection — keep it '
        'out of the top-level export so callers reach for the safer '
        'aset_tenant by default.'
    )


def test_unset_default_not_exported_at_top_level():
    assert not hasattr(zeromcp, 'unset_default')


def test_script_only_helpers_still_importable_explicitly():
    # the helpers exist — they just must not be on `zeromcp` top level
    from zeromcp.tenant.tenant import set_default, unset_default  # noqa: F401


def test_validate_token_async_exported():
    assert hasattr(zeromcp, 'validate_token_async')


def test_per_request_tenant_helpers_remain_exported():
    for name in ('aset_tenant', 'set_tenant', 'get_tenant'):
        assert hasattr(zeromcp, name), f'{name} should remain a top-level export'
