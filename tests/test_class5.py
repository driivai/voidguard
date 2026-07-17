"""Class 5 (EXPERIMENTAL) fixture suite — both directions, same as every rule.

The live fixtures are the contract: a detector for "your tests verify nothing"
is an accusation, so its false-positive fixtures carry the same weight as the
true-positive ones.
"""

from __future__ import annotations

from pathlib import Path

from voidguard import engine, rules_class5
from voidguard.model import UNKNOWN, VOID, WARN

FIXTURES = Path(__file__).parent / "fixtures" / "r5_vacuous"


def scan(sub: str):
    return rules_class5.scan_path(str(FIXTURES / sub))


def guards(result):
    return {(f.guard.split("::")[1], f.rule): f for f in result.findings}


# -- void side: every hollow shape is flagged with the right verdict ---------------


def test_void_side_flags_every_shape():
    result, stats = scan("void")
    by = guards(result)

    assert by[("test_no_assert_at_all", "R5a")].verdict == VOID
    assert by[("test_product_call_no_assert", "R5a")].verdict == VOID
    assert by[("test_tautology_only", "R5b")].verdict == VOID
    assert by[("test_truthy_only", "R5c")].verdict == WARN
    assert by[("test_mock_interaction_only", "R5d")].verdict == UNKNOWN
    # the error-claiming name fires R5e AND the assert-free body fires R5a
    assert by[("test_raises_on_empty", "R5e")].verdict == UNKNOWN
    assert by[("test_raises_on_empty", "R5a")].verdict == VOID
    assert by[("test_unresolved_helper", "R5a")].verdict == UNKNOWN

    assert stats["test_functions"] == 7


def test_void_side_unresolved_is_never_void():
    result, _ = scan("void")
    unresolved = [f for f in result.findings
                  if "unresolved" in f.mechanism or "unresolved" in f.evidence.summary]
    assert unresolved and all(f.verdict == UNKNOWN for f in unresolved)


# -- live side: zero findings, full stop --------------------------------------------


def test_live_side_never_flags():
    result, stats = scan("live")
    assert result.findings == [], [f"{f.id} {f.guard}" for f in result.findings]
    # and the denominator proves the tests were actually seen, not skipped
    assert stats["test_functions"] >= 8


# -- contract: evidence on every finding, and NOT wired into the default scan -------


def test_every_class5_finding_carries_evidence():
    result, _ = scan("void")
    for f in result.findings:
        assert f.evidence.summary, f.id
        assert f.evidence.searched, f.id
        assert f.question and f.fix, f.id
        assert f.id.startswith("VG-5-")


def test_class5_is_not_in_the_default_scan():
    result = engine.scan(str(FIXTURES / "void"))
    assert not [f for f in result.findings if f.vg_class == 5]
