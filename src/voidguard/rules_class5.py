"""Class 5 — vacuous assertions (EXPERIMENTAL — see docs/class5-spec.md).

A test that runs, passes, and verifies nothing beyond "the code did not raise".
NOT registered in the engine and NOT reachable from `voidguard scan`: run it
explicitly with

    python -m voidguard.rules_class5 <path> [--json FILE]

The verdict discipline is stricter than usual because the false-positive risk
is the whole game: VOID requires the assertion layer to be provably absent or
provably unable to fail AFTER resolving same-file helpers, sibling-module
helpers, and verifying fixtures. Anything unresolved is UNKNOWN, never VOID.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

from . import report
from .model import UNKNOWN, VOID, WARN, Evidence, Finding, ScanResult
from .repo import Repo

_Q5 = ("does this test verify any behavior beyond 'the code did not raise'?")

#: Called names treated as verification without needing resolution. Covers
#: numpy.testing.assert_*, pandas tm.assert_frame_equal, custom check_/verify_
#: helpers, snapshot/approval frameworks.
_VERIFIER_NAME = re.compile(
    r"^(assert|check|verify|validate|expect|ensure|confirm|snapshot|approve|matches)",
    re.IGNORECASE)

#: Bare truthiness on a call whose name promises a boolean is a real check.
_BOOLEAN_NAME = re.compile(r"^(is|has|can|should|was|are|will)_")

#: Boolean predicates by convention: `assert any(...)`, `assert re.match(...)`,
#: `assert path.exists()` are real assertions, not weak truthiness. Both FP
#: shapes here were caught by the first dogfood run on a real suite.
_BOOLEAN_BUILTINS = {
    "any", "all", "isinstance", "issubclass", "callable", "bool", "hasattr",
    "startswith", "endswith", "match", "search", "fullmatch", "exists",
    "is_file", "is_dir", "issuperset", "issubset", "isdisjoint", "isclose",
    "isfinite", "isnan", "isinf", "isdigit", "isalpha", "isalnum",
    "isnumeric", "isspace", "isidentifier", "isupper", "islower", "istitle",
    "is_integer", "samefile",
}

#: unittest assertion surface (anything self.assert*/self.fail*).
_UNITTEST_ASSERT = re.compile(r"^(assert|fail)")

#: Mock interaction assertions — pin THAT something was called, never a value.
_MOCK_INTERACTION = re.compile(
    r"^assert_(called|any_call|not_called|has_calls|awaited|has_awaits|"
    r"called_once|called_with|called_once_with|awaited_once|awaited_with)")

#: S6 vocabulary: names that claim an error-path behavior. `fails?` was in the
#: first draft and immediately false-positived on a real test named after a
#: --fail-on CLI flag — too polysemous (fail_on, failover, failfast), dropped.
_NAME_CLAIMS_ERROR = re.compile(r"(?:^|_)(rejects?|raises?|errors?|invalid)(?:_|$)")

_RAISES_CTX = {"raises", "warns", "deprecated_call"}
_TEST_FILE = re.compile(r"(^test_.*\.py$|_test\.py$)")


def _is_test_func(node: ast.AST) -> bool:
    return (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test"))


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    out = []
    for d in node.decorator_list:
        t = d.func if isinstance(d, ast.Call) else d
        out.append(ast.unparse(t) if hasattr(ast, "unparse") else "")
    return out


def _body_has_assert(fn: ast.AST) -> bool:
    """Does a helper/fixture body contain any failure mechanism at all?"""
    for n in ast.walk(fn):
        if isinstance(n, ast.Assert):
            return True
        if isinstance(n, ast.Raise):
            return True
        if isinstance(n, ast.Call):
            name = _call_name(n)
            if name and (_VERIFIER_NAME.match(name.split(".")[-1])
                         or name.split(".")[-1] == "fail"):
                return True
    return False


def _fixture_verifies(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A yield-fixture that asserts after yield verifies on behalf of its users."""
    seen_yield = False
    for n in ast.walk(fn):
        if isinstance(n, (ast.Yield, ast.YieldFrom)):
            seen_yield = True
    if not seen_yield:
        return _body_has_assert(fn)
    # positional: any assert anywhere in a yield-fixture counts (teardown checks)
    return _body_has_assert(fn)


def _call_name(call: ast.Call) -> str:
    f = call.func
    parts: list[str] = []
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
    return ".".join(reversed(parts))


def _is_tautology(test: ast.expr) -> bool:
    if isinstance(test, ast.Constant):
        return bool(test.value)  # assert True / assert 1 / assert "x"
    if isinstance(test, ast.Compare) and len(test.comparators) == 1:
        if isinstance(test.ops[0], (ast.Eq, ast.Is)):
            # f(x) == f(x) is NOT a tautology — two invocations can differ
            # (that shape is how determinism tests are written; flagging it
            # false-positived on five independent AI-written suites at once)
            if any(isinstance(n, ast.Call) for n in ast.walk(test)):
                return False
            return ast.dump(test.left) == ast.dump(test.comparators[0])
    return False


#: Bare imports of these are product/platform code, not test utilities.
_STDLIB = set(getattr(sys, "stdlib_module_names", ()))


class _FileFacts:
    """Everything resolvable about one test file (plus its conftest/siblings)."""

    def __init__(self) -> None:
        self.asserting_helpers: set[str] = set()
        self.silent_helpers: set[str] = set()
        self.verifying_fixtures: set[str] = set()
        #: non-test METHODS pooled across every class in the file — unittest
        #: suites assert through self._helper(); a mixin/base defined in the
        #: same file resolves here (one level, name-based; recorded in spec)
        self.class_asserting: set[str] = set()
        self.class_silent: set[str] = set()
        self.import_module: dict[str, str] = {}  # local name -> module it came from
        self.resolved_sources: list[str] = []    # for the evidence search set


def _called_names(fn: ast.AST) -> set[str]:
    """Simple names this body calls: foo() and self.foo() both yield 'foo'."""
    out: set[str] = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                out.add(n.func.id)
            elif (isinstance(n.func, ast.Attribute)
                  and isinstance(n.func.value, ast.Name)
                  and n.func.value.id == "self"):
                out.add(n.func.attr)
    return out


def _collect_module_facts(tree: ast.Module, facts: _FileFacts) -> None:
    module_pool: dict[str, ast.AST] = {}
    class_pool: dict[str, ast.AST] = {}

    def take(node, pool: dict[str, ast.AST]) -> None:
        decos = " ".join(_decorator_names(node))
        if "fixture" in decos:
            if _fixture_verifies(node):
                facts.verifying_fixtures.add(node.name)
        else:
            # test-named functions join the pool too: suites reuse tests as
            # helpers (test_x() calling test_base(check=...)) and a call to an
            # asserting test is a real assertion
            pool[node.name] = node

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            take(node, module_pool)
        elif isinstance(node, ast.ClassDef):
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    take(m, class_pool)
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * node.level) + (node.module or "")
            for alias in node.names:
                facts.import_module[alias.asname or alias.name] = mod
        elif isinstance(node, ast.Import):
            for alias in node.names:
                facts.import_module[alias.asname or alias.name] = alias.name

    # Transitive classification to a fixpoint: a helper that (directly or
    # through same-file helpers) reaches an assert is asserting. Needed for
    # real suites — dateutil chains testX -> self._testDST -> self._testTzFunc
    # -> assertEqual, two hops from the test body.
    everything = {**module_pool, **class_pool}
    asserting = {n for n, fn in everything.items() if _body_has_assert(fn)}
    changed = True
    while changed:
        changed = False
        for n, fn in everything.items():
            if n not in asserting and _called_names(fn) & asserting:
                asserting.add(n)
                changed = True

    for n in module_pool:
        if n.startswith("test"):
            continue  # analyzed as tests; pool membership is for call-resolution
        (facts.asserting_helpers if n in asserting
         else facts.silent_helpers).add(n)
    facts.asserting_helpers |= {n for n in module_pool
                                if n.startswith("test") and n in asserting}
    for n in class_pool:
        if n.startswith("test"):
            continue
        (facts.class_asserting if n in asserting
         else facts.class_silent).add(n)
    facts.class_asserting |= {n for n in class_pool
                              if n.startswith("test") and n in asserting}


def _resolve_sibling(mod: str, directory: Path, facts: _FileFacts,
                     cache: dict[Path, _FileFacts | None]) -> _FileFacts | None:
    """One-level resolution of `from helpers import x` to ./helpers.py."""
    name = mod.lstrip(".")
    if not name or "." in name:
        return None
    candidate = directory / f"{name}.py"
    if candidate in cache:
        return cache[candidate]
    result: _FileFacts | None = None
    if candidate.is_file():
        try:
            sub = _FileFacts()
            _collect_module_facts(ast.parse(candidate.read_text(encoding="utf-8",
                                                                errors="replace")),
                                  sub)
            facts.resolved_sources.append(str(candidate))
            result = sub
        except SyntaxError:
            result = None
    cache[candidate] = result
    return result


class _TestVerdict:
    def __init__(self) -> None:
        self.real: list[str] = []          # anything that can fail on behavior
        self.taut: list[str] = []          # provably cannot fail
        self.truthy: list[str] = []        # bare truthiness on a computed value
        self.interaction: list[str] = []   # mock call-shape assertions
        self.unresolved: list[str] = []    # calls that MIGHT assert internally
        self.has_raises_ctx = False


def _analyze_test(fn, facts: _FileFacts, directory: Path,
                  sibling_cache: dict) -> _TestVerdict:
    v = _TestVerdict()
    decos = " ".join(_decorator_names(fn))
    hypothesis_given = "given" in decos

    for n in ast.walk(fn):
        if isinstance(n, ast.Assert):
            t = n.test
            if _is_tautology(t):
                if isinstance(t, ast.Constant) and t.value is False:
                    v.real.append(f"line {n.lineno}: assert False (manual fail)")
                else:
                    v.taut.append(f"line {n.lineno}: {ast.unparse(t)}")
            elif isinstance(t, (ast.Name, ast.Call, ast.Attribute, ast.Subscript)):
                name = _call_name(t) if isinstance(t, ast.Call) else ""
                leaf = name.split(".")[-1].lstrip("_")
                if isinstance(t, ast.Call) and (_BOOLEAN_NAME.match(leaf)
                                                or leaf in _BOOLEAN_BUILTINS
                                                or _VERIFIER_NAME.match(leaf)):
                    v.real.append(f"line {n.lineno}: assert {ast.unparse(t)}")
                elif hypothesis_given:
                    v.real.append(f"line {n.lineno}: truthiness under @given")
                else:
                    v.truthy.append(f"line {n.lineno}: assert {ast.unparse(t)}")
            else:
                v.real.append(f"line {n.lineno}: assert {ast.unparse(t)[:60]}")
        elif isinstance(n, ast.Raise):
            v.real.append(f"line {n.lineno}: raise")
        elif isinstance(n, ast.withitem):
            expr = n.context_expr
            if isinstance(expr, ast.Call):
                leaf = _call_name(expr).split(".")[-1]
                if leaf in _RAISES_CTX or leaf == "assertRaises":
                    v.has_raises_ctx = True
                    v.real.append(f"line {expr.lineno}: {leaf} context")
        elif isinstance(n, ast.Call):
            name = _call_name(n)
            if not name:
                continue
            leaf = name.split(".")[-1]
            root = name.split(".")[0]
            if _MOCK_INTERACTION.match(leaf):
                v.interaction.append(f"line {n.lineno}: {name}")
            elif root == "self":
                if _UNITTEST_ASSERT.match(leaf):
                    if leaf in {"assertTrue", "assertFalse"} and n.args:
                        a = n.args[0]
                        if isinstance(a, ast.Constant):
                            v.taut.append(f"line {n.lineno}: self.{leaf}({a.value!r})")
                        elif isinstance(a, (ast.Name, ast.Attribute)):
                            v.truthy.append(f"line {n.lineno}: self.{leaf}(...)")
                        else:
                            v.real.append(f"line {n.lineno}: self.{leaf}(...)")
                    else:
                        v.real.append(f"line {n.lineno}: self.{leaf}")
                    if leaf in {"assertRaises", "assertRaisesRegex", "assertWarns"}:
                        v.has_raises_ctx = True
                elif leaf in facts.class_asserting:
                    v.real.append(f"line {n.lineno}: helper self.{leaf}() "
                                  "(asserts, resolved same-file class)")
                elif leaf in facts.class_silent:
                    pass  # resolved: does not assert
                elif _VERIFIER_NAME.match(leaf.lstrip("_")):
                    v.real.append(f"line {n.lineno}: verifier-named self.{leaf}()")
                else:
                    # a method on a base class we cannot see could assert —
                    # UNKNOWN territory, never VOID
                    v.unresolved.append(f"line {n.lineno}: self.{leaf}() "
                                        "(method not resolvable in this file)")
            elif leaf in _RAISES_CTX and root in {"pytest", leaf}:
                v.has_raises_ctx = True
                v.real.append(f"line {n.lineno}: {name}")
            elif name in {"pytest.fail", "pytest.xfail"}:
                v.real.append(f"line {n.lineno}: {name}")
            elif _VERIFIER_NAME.match(leaf):
                v.real.append(f"line {n.lineno}: verifier-named call {name}()")
            elif isinstance(n.func, ast.Name):
                fname = n.func.id
                if fname in facts.asserting_helpers:
                    v.real.append(f"line {n.lineno}: helper {fname}() (asserts, resolved same-file)")
                elif fname in facts.silent_helpers:
                    pass  # resolved: does not assert; just code under exercise
                elif fname in facts.import_module:
                    mod = facts.import_module[fname]
                    sib = _resolve_sibling(mod, directory, facts, sibling_cache)
                    if sib is not None and fname in sib.asserting_helpers:
                        v.real.append(f"line {n.lineno}: helper {fname}() "
                                      f"(asserts, resolved from {mod}.py)")
                    elif sib is not None and fname in sib.silent_helpers:
                        pass
                    elif mod.lstrip(".") .split(".")[0] in _STDLIB and not mod.startswith("."):
                        pass  # stdlib (e.g. datetime, json): product code, not a test util
                    elif mod.startswith(".") or "." not in mod:
                        # relative or bare import we could not resolve: it might
                        # be a test utility that asserts. UNKNOWN, never VOID.
                        v.unresolved.append(f"line {n.lineno}: {fname}() from '{mod}'")
    return v


_SEARCHED = ["assert statements", "unittest self.assert*/fail*",
             "pytest.raises/warns contexts", "mock interaction assertions",
             "verifier-named calls (assert*/check*/verify*/...)",
             "same-file helpers and fixtures", "sibling-module helpers"]


def scan_path(root: str | Path) -> tuple[ScanResult, dict]:
    repo = Repo(root)
    findings: list[Finding] = []
    n_files = 0
    n_tests = 0
    sibling_cache: dict[Path, _FileFacts | None] = {}

    for path in repo.files():
        if not _TEST_FILE.search(path.name):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        n_files += 1
        rel = str(path.relative_to(repo.root))
        facts = _FileFacts()
        _collect_module_facts(tree, facts)
        conftest = path.parent / "conftest.py"
        if conftest.is_file():
            try:
                _collect_module_facts(
                    ast.parse(conftest.read_text(encoding="utf-8", errors="replace")),
                    facts)
                facts.resolved_sources.append(str(conftest))
            except SyntaxError:
                pass

        tests: list = []
        for node in tree.body:
            if _is_test_func(node):
                tests.append(node)
            elif isinstance(node, ast.ClassDef):
                tests.extend(m for m in node.body if _is_test_func(m))

        for fn in tests:
            n_tests += 1
            decos = " ".join(_decorator_names(fn))
            if "xfail" in decos:
                continue
            uses_verifying_fixture = any(
                a.arg in facts.verifying_fixtures
                for a in fn.args.args + fn.args.kwonlyargs)
            v = _analyze_test(fn, facts, path.parent, sibling_cache)
            guard = f"{rel}::{fn.name}"
            searched = _SEARCHED + [f"resolved: {s}" for s in facts.resolved_sources]

            def emit(rule, verdict, mech, summary, found, question, fix):
                findings.append(Finding(
                    rule=rule, vg_class=5, verdict=verdict, guard=guard,
                    mechanism=mech,
                    evidence=Evidence(summary=summary, searched=list(searched),
                                      found=found),
                    question=question, fix=fix))

            if v.real or uses_verifying_fixture:
                pass  # verified (or verified on the test's behalf by a fixture)
            elif v.unresolved and not (v.taut or v.truthy or v.interaction):
                emit("R5a", UNKNOWN, "no assertion found; unresolved helper calls",
                     f"no assertion mechanism found; {len(v.unresolved)} call(s) "
                     f"could not be resolved and might assert internally: "
                     + "; ".join(v.unresolved[:4]),
                     v.unresolved[:6], _Q5,
                     "if these helpers assert, no action; otherwise add an "
                     "assertion on the produced value")
            elif not (v.taut or v.truthy or v.interaction or v.unresolved):
                emit("R5a", VOID, "test body contains no assertion of any kind",
                     "no assert statement, no unittest assertion, no raises "
                     "context, no mock assertion, no verifier-named or resolved "
                     "asserting helper call — the test can only fail if the code "
                     "raises",
                     [], _Q5,
                     "assert on the produced value, or rename to smoke_* and "
                     "record that only executability is claimed")
            elif v.taut and not (v.truthy or v.interaction or v.unresolved):
                emit("R5b", VOID, "every assertion is a tautology",
                     "every assertion in the body is statically true: "
                     + "; ".join(v.taut[:4]),
                     v.taut[:6], _Q5,
                     "replace the tautology with an assertion on the produced value")
            elif v.truthy and not (v.interaction or v.unresolved):
                emit("R5c", WARN, "only bare-truthiness assertions",
                     "every assertion is a bare truthiness check on a computed "
                     "value — it fails only on None/0/empty/False: "
                     + "; ".join(v.truthy[:4]),
                     v.truthy[:6], _Q5,
                     "assert the expected value or shape, not mere truthiness")
            elif v.interaction and not v.unresolved:
                emit("R5d", UNKNOWN, "asserts interactions only, never a value",
                     "every assertion pins that a call happened "
                     f"({len(v.interaction)} interaction assertion(s)); whether "
                     "interaction-only is the right contract here is not "
                     "statically decidable: " + "; ".join(v.interaction[:4]),
                     v.interaction[:6], _Q5,
                     "if this tests an adapter, no action; otherwise assert the "
                     "produced value too")
            else:
                emit("R5a", UNKNOWN, "mixed weak signals, nothing resolvable as a "
                     "real assertion",
                     "combination of unresolved calls and weak assertions; not "
                     "statically decidable",
                     (v.unresolved + v.truthy + v.taut)[:6], _Q5,
                     "add an assertion on the produced value")

            # R5e fires only when the test ALSO lacks a real assertion: mature
            # suites verify error paths without exceptions (status codes,
            # error dicts) and flagging those burned trust on first contact.
            m = _NAME_CLAIMS_ERROR.search(fn.name)
            if m and not v.has_raises_ctx and not v.real and not uses_verifying_fixture:
                emit("R5e", UNKNOWN,
                     f"name claims an error-path behavior ('{m.group(1)}') the "
                     "body never exercises",
                     f"test name contains '{m.group(1)}' but the body has no "
                     "raises/warns context and no exception assertion",
                     [f"name token: {m.group(1)}"], _Q5,
                     "exercise the claimed error path with pytest.raises, or "
                     "rename the test to what it actually checks")

    findings.sort(key=lambda f: (f.rule, f.guard))
    for i, f in enumerate(findings, 1):
        f.id = f"VG-5-{i:03d}"
    notes = [f"class5 (EXPERIMENTAL): scanned {n_tests} test function(s) "
             f"across {n_files} test file(s)"]
    stats = {"test_files": n_files, "test_functions": n_tests,
             "findings": len(findings),
             "by_rule": {r: sum(1 for f in findings if f.rule == r)
                         for r in sorted({f.rule for f in findings})},
             "by_verdict": {vd: sum(1 for f in findings if f.verdict == vd)
                            for vd in ("VOID", "WARN", "UNKNOWN")}}
    return ScanResult(root=str(repo.root), findings=findings, notes=notes), stats


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    json_out = None
    if "--json" in argv:
        i = argv.index("--json")
        json_out = argv[i + 1]
        del argv[i:i + 2]
    root = argv[0] if argv else "."
    result, stats = scan_path(root)
    print(report.render(result), end="")
    if json_out:
        payload = json.loads(report.render_json(result))
        payload["class5"] = stats
        text = json.dumps(payload, indent=2) + "\n"
        if json_out == "-":
            print(text, end="")
        else:
            Path(json_out).write_text(text, encoding="utf-8")
    return 0 if not result.findings else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
