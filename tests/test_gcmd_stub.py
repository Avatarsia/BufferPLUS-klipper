"""Shared GCmd parsing must preserve optional values and enforce bounds."""

import pytest

from helpers import FakeGCmd


@pytest.mark.parametrize('getter', ['get_int', 'get_float'])
def test_unset_optional_numeric_parameter_stays_none(getter):
    assert getattr(FakeGCmd(), getter)('MISSING', None) is None


@pytest.mark.parametrize('getter', ['get_int', 'get_float'])
@pytest.mark.parametrize('bound,value', [
    ({'minval': 2}, 1),
    ({'maxval': 2}, 3),
    ({'above': 2}, 2),
    ({'below': 2}, 2),
])
def test_explicit_numeric_parameter_rejects_out_of_bounds(getter, bound, value):
    with pytest.raises(ValueError):
        getattr(FakeGCmd({'VALUE': str(value)}), getter)('value', **bound)


@pytest.mark.parametrize('getter', ['get_int', 'get_float'])
def test_numeric_bounds_allow_inclusive_endpoints(getter):
    assert getattr(FakeGCmd({'value': '2'}), getter)(
        'VALUE', minval=2, maxval=2, above=1, below=3) == 2


def test_config_boolean_zero_remains_false():
    from fakes_klipper import FakeConfig

    assert FakeConfig(values={'enabled': '0'}).getboolean('enabled') is False
