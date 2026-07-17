"""Deliberately hollow tests — every one must be flagged (scan TARGET, never run)."""

from unittest.mock import Mock

from mypkg.core import run  # dotted, product-style import
from helpers_missing import roundtrip  # bare module, nowhere in this tree


def mul(a, b):
    return a * b


def test_no_assert_at_all():
    result = mul(3, 4)  # noqa: F841  -- computed, never checked


def test_product_call_no_assert():
    run()


def test_tautology_only():
    x = mul(2, 2)
    assert True
    assert x == x


def test_truthy_only():
    result = mul(2, 3)
    assert result


def test_mock_interaction_only():
    m = Mock()
    m(5)
    m.assert_called_once_with(5)


def test_raises_on_empty():
    # the name claims an error path; the body neither takes it nor asserts
    mul(0, 0)


def test_unresolved_helper():
    roundtrip(1)
