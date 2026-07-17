"""Test utility with a NON-verifier name that nonetheless asserts.

The hard false-positive case: a test calling roundtrip() looks assert-free
unless the scanner resolves this sibling module.
"""


def roundtrip(value):
    encoded = repr(value)
    assert eval(encoded) == value  # noqa: S307  -- fixture, never executed
