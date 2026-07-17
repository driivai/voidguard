"""Tests that LOOK vacuous to a lazy scanner but genuinely verify.

None of these may flag — false-positive fixtures carry the same weight as
true-positive ones (scan TARGET, never run)."""

import unittest

import pytest

from helpers import roundtrip


def is_even(n):
    return n % 2 == 0


def check_positive(n):
    assert n > 0


def test_real_assert():
    assert 2 + 3 == 5


def test_helper_same_file():
    check_positive(5)


def test_imported_sibling_helper():
    roundtrip([1, 2])


def test_raises_zero_division():
    with pytest.raises(ZeroDivisionError):
        1 / 0  # noqa: B018


def test_boolean_named_truthiness():
    assert is_even(4)


class TestThings(unittest.TestCase):
    def test_equal(self):
        self.assertEqual(1 + 1, 2)


@pytest.fixture
def tracker():
    events = []
    yield events
    assert events  # the fixture verifies on the test's behalf, at teardown


def test_with_verifying_fixture(tracker):
    tracker.append("observed")


@pytest.mark.xfail(reason="known gap, tracked")
def test_known_gap():
    assert is_even(3)


# regression fixtures for the two FPs the first dogfood run produced:


def test_boolean_builtin_truthiness():
    values = [2, 4, 6]
    assert any(is_even(v) for v in values)
    assert all(is_even(v) for v in values)


def test_fail_on_flag_naming():
    # "fail" here names a flag, not a claimed error path; must not fire R5e
    modes = {"fail_on": "never"}
    assert modes["fail_on"] == "never"


def test_invalid_input_via_error_value():
    # error-path name, verified through a value instead of an exception —
    # mature suites do this constantly (status codes, error dicts); the name
    # heuristic must stay quiet when a real assertion is present
    assert chunk_error_message(0) == "size must be >= 1"


def chunk_error_message(size):
    if size < 1:
        return "size must be >= 1"
    return ""


class TestWithClassHelper(unittest.TestCase):
    # unittest suites assert through self-helpers constantly (the dateutil
    # pattern) — resolving them is the difference between clean and 245 FPs
    def _roundtrip_check(self, value):
        self.assertEqual(eval(repr(value)), value)  # noqa: S307

    def test_roundtrip_via_class_helper(self):
        self._roundtrip_check([1, 2, 3])


def test_stdlib_call_with_real_assert():
    from datetime import datetime
    stamp = datetime(2020, 1, 1)
    assert stamp.year == 2020


def shuffled(seed):
    return [seed, seed + 1]


def test_determinism_same_call_both_sides():
    # identical ASTs on both sides but each side is an invocation — a real
    # determinism check, not a tautology
    assert shuffled(7) == shuffled(7)
