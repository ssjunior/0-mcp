"""Generated docs follow the installed framework version.

Hardcoded ``'1.0.0'`` defaults made every project's ``/docs`` page read
"v1.0.0" forever, even after upgrading the framework. Default to
``zeromcp.__version__`` instead so the version on the page tracks the
package metadata."""
import zeromcp
from zeromcp.openapi import build_spec


def test_version_attribute_exposed():
    assert zeromcp.__version__
    # Either real metadata or the editable-checkout sentinel.
    assert zeromcp.__version__ != '1.0.0'


def test_build_spec_defaults_to_package_version():
    spec = build_spec({})
    assert spec['info']['version'] == zeromcp.__version__


def test_build_spec_explicit_version_wins():
    spec = build_spec({}, version='2.5.0')
    assert spec['info']['version'] == '2.5.0'
