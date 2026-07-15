"""Class 1 — tests that never run.

Python is first-class (AST-based). Go / Rust / JS detection is best-effort
regex and every such finding says so in its evidence.
"""

from __future__ import annotations

import ast
import re
import subprocess
import time
from pathlib import Path

from .model import UNKNOWN, VOID, WARN, Evidence, Finding
from .repo import Repo
from .searchset import env_search

_Q1 = "has this guard ever been observed to fail? — it has never been observed to RUN."

_BUILTIN_MARKS = {
    "skip", "skipif", "xfail", "parametrize", "usefixtures", "filterwarnings",
    "timeout", "asyncio", "anyio", "trio", "django_db", "flaky", "no_cover",
}

_STALE_DAYS = 90

#: Variables the CI platform itself sets on every run — no repo file needs to.
#: A guard gated on one of these is live wherever any CI runs it; flagging it
#: as VOID would be a false positive (measured on real repos, not hypothesized).
_PLATFORM_SET_VARS = {
    "CI", "GITHUB_ACTIONS", "GITHUB_WORKFLOW", "GITHUB_RUN_ID", "GITHUB_SHA",
    "GITHUB_REF", "RUNNER_OS", "RUNNER_TEMP", "TF_BUILD", "TRAVIS", "CIRCLECI",
    "GITLAB_CI", "JENKINS_URL", "JENKINS_HOME", "BUILDKITE", "APPVEYOR",
    "TEAMCITY_VERSION", "DRONE", "CODEBUILD_BUILD_ID",
}


# -- AST helpers ---------------------------------------------------------------


def _env_vars_in(node: ast.AST) -> set[str]:
    """Env var names read anywhere inside an expression."""

    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            # os.environ.get("X") / os.getenv("X")
            if isinstance(f, ast.Attribute) and f.attr in {"get", "getenv"}:
                if n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                    out.add(n.args[0].value)
        elif isinstance(n, ast.Subscript):
            # os.environ["X"]
            if isinstance(n.value, ast.Attribute) and n.value.attr == "environ":
                s = n.slice
                if isinstance(s, ast.Constant) and isinstance(s.value, str):
                    out.add(s.value)
        elif isinstance(n, ast.Compare):
            # "X" in os.environ
            if (
                isinstance(n.left, ast.Constant)
                and isinstance(n.left.value, str)
                and any(isinstance(op, (ast.In, ast.NotIn)) for op in n.ops)
                and any(
                    isinstance(c, ast.Attribute) and c.attr == "environ"
                    for c in n.comparators
                )
            ):
                out.add(n.left.value)
    return out


class _Parents(ast.NodeVisitor):
    def __init__(self) -> None:
        self.parent: dict[ast.AST, ast.AST] = {}

    def visit(self, node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            self.parent[child] = node
        super().generic_visit(node)


def _enclosing(node: ast.AST, parents: dict, kind) -> ast.AST | None:
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, kind):
            return cur
        cur = parents.get(cur)
    return None


def _is_pytest_skip_call(node: ast.Call) -> bool:
    f = node.func
    return (
        isinstance(f, ast.Attribute)
        and f.attr == "skip"
        and isinstance(f.value, ast.Name)
        and f.value.id == "pytest"
    )


def _skipif_decorators(fn: ast.AST) -> list[ast.Call]:
    out = []
    for dec in getattr(fn, "decorator_list", []):
        call = dec if isinstance(dec, ast.Call) else None
        target = call.func if call else dec
        # pytest.mark.skipif / mark.skipif
        parts = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if parts and parts[0] == "skipif" and call is not None:
            out.append(call)
    return out


def _unconditional_skip_line(fn: ast.AST) -> int | None:
    for dec in getattr(fn, "decorator_list", []):
        call = dec if isinstance(dec, ast.Call) else None
        target = call.func if call else dec
        parts = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if parts and parts[0] == "skip":
            return dec.lineno
    return None


# -- R1a: env-gated skips --------------------------------------------------------


def _emit_env_gate(
    repo: Repo, rel: str, guard: str, mechanism_prefix: str, var: str
) -> Finding:
    if var in _PLATFORM_SET_VARS:
        return None  # type: ignore[return-value]  # the CI platform sets this itself
    search = env_search(repo, var, exclude_rel=rel)
    if search.executable_hits:
        return None  # type: ignore[return-value]  # live guard: the flag IS set somewhere the repo runs
    verdict = WARN if search.docs_hits else VOID
    fix = (
        "set the flag in a dedicated job, or delete the test and record the decision"
        if verdict == VOID
        else "runnable by hand, never run by machine — add a CI job that sets the "
             "flag, so the guard is observed to run without a human remembering"
    )
    return Finding(
        rule="R1a",
        vg_class=1,
        verdict=verdict,
        guard=guard,
        mechanism=f"{mechanism_prefix} gated on {var}",
        evidence=Evidence(
            summary=search.summary(),
            searched=search.searched,
            found=search.docs_hits,
        ),
        question=_Q1,
        fix=fix,
    )


def _scan_python_file(repo: Repo, path: Path) -> tuple[list[Finding], list[tuple[str, str]]]:
    rel = repo.rel(path)
    src = repo.read(path)
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return [], []
    parents = _Parents()
    parents.visit(tree)

    # module-level names whose value reads env vars (one hop of indirection)
    name_env: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and node.targets:
            t = node.targets[0]
            if isinstance(t, ast.Name):
                envs = _env_vars_in(node.value)
                if envs:
                    name_env[t.id] = envs

    def resolve(cond: ast.AST) -> set[str]:
        envs = set(_env_vars_in(cond))
        for n in ast.walk(cond):
            if isinstance(n, ast.Name) and n.id in name_env:
                envs |= name_env[n.id]
        return envs

    findings: list[Finding] = []
    unknowns: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for call in _skipif_decorators(node):
                if not call.args:
                    continue
                cond = call.args[0]
                envs = resolve(cond)
                guard = f"{rel}::{node.name}"
                if envs:
                    for var in sorted(envs):
                        key = (guard, var)
                        if key in seen:
                            continue
                        seen.add(key)
                        f = _emit_env_gate(repo, rel, guard, "pytest.mark.skipif", var)
                        if f:
                            findings.append(f)
                else:
                    # runtime-expression conditions (platform probes, capability
                    # checks) are not statically decidable; collected and reported
                    # as ONE aggregated UNKNOWN per repo — 70 identical UNKNOWNs
                    # is noise, and a noisy scanner gets uninstalled in a day
                    cond_src = ast.get_source_segment(src, cond) or "<condition>"
                    key = (guard, cond_src)
                    if key in seen:
                        continue
                    seen.add(key)
                    unknowns.append((guard, cond_src[:100]))
        elif isinstance(node, ast.Call) and _is_pytest_skip_call(node):
            encl_if = _enclosing(node, parents.parent, ast.If)
            fn = _enclosing(node, parents.parent, (ast.FunctionDef, ast.AsyncFunctionDef))
            fn_name = getattr(fn, "name", "<module>")
            guard = f"{rel}::{fn_name}"
            if encl_if is not None:
                envs = resolve(encl_if.test)
                for var in sorted(envs):
                    key = (guard, var)
                    if key in seen:
                        continue
                    seen.add(key)
                    f = _emit_env_gate(repo, rel, guard, "pytest.skip", var)
                    if f:
                        findings.append(f)
    return findings, unknowns


# -- R1b: stale unconditional skips ------------------------------------------------


def _blame_epoch(repo: Repo, rel: str, line: int) -> int | None:
    try:
        proc = subprocess.run(
            ["git", "blame", "-L", f"{line},{line}", "--porcelain", "--", rel],
            capture_output=True, text=True, cwd=repo.root, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    m = re.search(r"^committer-time (\d+)$", proc.stdout, re.M)
    return int(m.group(1)) if m else None


def _scan_stale_skips(repo: Repo, path: Path) -> list[Finding]:
    rel = repo.rel(path)
    try:
        tree = ast.parse(repo.read(path))
    except SyntaxError:
        return []
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            line = _unconditional_skip_line(node)
            if line is None:
                continue
            epoch = _blame_epoch(repo, rel, line)
            if epoch is None:
                continue  # no history available: emit nothing rather than guess
            age_days = int((time.time() - epoch) / 86400)
            if age_days > _STALE_DAYS:
                findings.append(Finding(
                    rule="R1b", vg_class=1, verdict=WARN,
                    guard=f"{rel}::{node.name}",
                    mechanism="unconditional pytest.mark.skip",
                    evidence=Evidence(
                        summary=f"skip is unconditional and {age_days} days old "
                                f"(git blame on line {line}); threshold {_STALE_DAYS} days",
                        searched=[f"git blame {rel}:{line}"],
                    ),
                    question=f"is this skip a decision or a leftover? — unconditional "
                             f"for {age_days} days.",
                    fix="delete the test, fix it, or record the permanent-skip decision "
                        "where the next reader will find it",
                ))
    return findings


# -- R1c: markers deselected by every CI invocation ---------------------------------


def _pytest_invocations(repo: Repo) -> list[tuple[str, str]]:
    """(workflow_rel, invocation_line) for every pytest run line in workflows."""

    out = []
    for wf in repo.workflows():
        for line in wf.text.splitlines():
            if re.search(r"\bpytest\b", line) and not line.strip().startswith("#"):
                out.append((wf.rel, line.strip()))
    return out


def _m_expression(line: str) -> str | None:
    m = re.search(r"-m\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))", line)
    if not m:
        return None
    return next(g for g in m.groups() if g is not None)


def _markers_in_use(repo: Repo) -> dict[str, str]:
    """marker -> one example file where it is used."""

    out: dict[str, str] = {}
    declared = repo.pytest_markers()
    for p in repo.python_test_files():
        for m in re.finditer(r"@pytest\.mark\.(\w+)", repo.read(p)):
            name = m.group(1)
            if name in _BUILTIN_MARKS:
                continue
            if declared and name not in declared:
                # undeclared markers may be plugins; only weigh declared ones
                # when a declaration list exists
                continue
            out.setdefault(name, repo.rel(p))
    return out


def scan_marker_exclusion(repo: Repo) -> list[Finding]:
    invocations = _pytest_invocations(repo)
    if not invocations:
        return []
    markers = _markers_in_use(repo)
    findings = []
    for marker, example in sorted(markers.items()):
        included = False
        for _, line in invocations:
            expr = _m_expression(line)
            if expr is None:
                included = True  # a run with no -m runs everything
                break
            negated = set(re.findall(r"not\s+(\w+)", expr))
            positive = (
                set(re.findall(r"\w+", expr)) - negated - {"not", "and", "or"}
            )
            if marker in negated:
                continue  # this invocation deselects it
            if positive and marker not in positive:
                continue  # positive selection of OTHER markers excludes it
            # purely negative expression not naming this marker, or a positive
            # selection that names it: the marker runs here
            included = True
            break
        if included:
            continue
        findings.append(Finding(
            rule="R1c", vg_class=1, verdict=VOID,
            guard=f"tests marked '@pytest.mark.{marker}' (e.g. {example})",
            mechanism=f"every CI pytest invocation deselects marker '{marker}'",
            evidence=Evidence(
                summary=f"{len(invocations)} pytest invocation(s) in CI; every one "
                        f"excludes '{marker}' via -m; no invocation selects it",
                searched=[f"{wf}: {ln[:100]}" for wf, ln in invocations],
            ),
            question="do these marked tests ever run in CI? — every CI invocation "
                     "deselects them. (They may still run locally; the claim is "
                     "scoped to CI.)",
            fix=f"add a job that runs `pytest -m {marker}`, or delete the marked "
                "tests and record the decision",
        ))
    return findings


# -- R1d: best-effort polyglot -------------------------------------------------------


def scan_polyglot(repo: Repo) -> list[Finding]:
    findings: list[Finding] = []

    # go: t.Skip gated on os.Getenv (regex; best-effort)
    for p in repo.glob("**/*_test.go"):
        rel = repo.rel(p)
        text = repo.read(p)
        for m in re.finditer(
            r"if[^\n{]*os\.Getenv\(\"(\w+)\"\)[^\n{]*\{[^}]*?\bt\.Skip", text, re.S
        ):
            var = m.group(1)
            f = _emit_env_gate(repo, rel, f"{rel} (go test)", "t.Skip (best-effort regex)", var)
            if f:
                f.rule = "R1d-go"
                f.evidence.summary += " [best-effort go detection]"
                findings.append(f)

    # rust: #[ignore] tests never invoked with --ignored in CI
    rs_ignored = []
    for p in repo.glob("**/*.rs"):
        n = len(re.findall(r"#\[\s*ignore\s*[\]\(]", repo.read(p)))
        if n:
            rs_ignored.append((repo.rel(p), n))
    if rs_ignored:
        wf_text = "\n".join(w.text for w in repo.workflows())
        if not re.search(r"--(include-)?ignored\b", wf_text):
            total = sum(n for _, n in rs_ignored)
            findings.append(Finding(
                rule="R1d-rs", vg_class=1, verdict=WARN,
                guard=f"{total} #[ignore] test(s) across {len(rs_ignored)} file(s)",
                mechanism="rust #[ignore] with no CI invocation of --ignored/--include-ignored",
                evidence=Evidence(
                    summary=f"{total} ignored tests; searched {len(repo.workflows())} "
                            "workflow(s) for --ignored/--include-ignored: 0 hits "
                            "[best-effort rust detection]",
                    searched=[w.rel for w in repo.workflows()],
                    found=[f"{rel} ({n})" for rel, n in rs_ignored[:10]],
                ),
                question="do the ignored tests ever run in CI? — no CI invocation "
                         "includes them.",
                fix="add a job running `cargo test -- --include-ignored`, or record "
                    "why they are permanently manual",
            ))

    # js: unconditional skips (only when the repo has a package.json)
    if repo.exists("package.json"):
        js_hits = []
        for p in repo.glob("**/*.test.js", "**/*.test.ts", "**/*.spec.js", "**/*.spec.ts"):
            n = len(re.findall(
                r"\b(?:it|test|describe)\.skip\(|\bx(?:it|test|describe)\(", repo.read(p)
            ))
            if n:
                js_hits.append((repo.rel(p), n))
        if js_hits:
            total = sum(n for _, n in js_hits)
            findings.append(Finding(
                rule="R1d-js", vg_class=1, verdict=WARN,
                guard=f"{total} skipped js/ts test(s) across {len(js_hits)} file(s)",
                mechanism="unconditional test.skip/describe.skip/x-prefixed tests",
                evidence=Evidence(
                    summary=f"{total} unconditionally skipped tests "
                            "[best-effort js detection; staleness not assessed]",
                    searched=["**/*.test.{js,ts}", "**/*.spec.{js,ts}"],
                    found=[f"{rel} ({n})" for rel, n in js_hits[:10]],
                ),
                question="are these skips decisions or leftovers? — they are "
                         "unconditional and never run.",
                fix="un-skip, delete, or record the permanent-skip decision",
            ))
    return findings


# -- entry point ---------------------------------------------------------------------


def scan(repo: Repo) -> list[Finding]:
    findings: list[Finding] = []
    all_unknowns: list[tuple[str, str]] = []
    for p in repo.python_test_files():
        file_findings, unknowns = _scan_python_file(repo, p)
        findings.extend(file_findings)
        all_unknowns.extend(unknowns)
        findings.extend(_scan_stale_skips(repo, p))
    if all_unknowns:
        files = sorted({g.split("::")[0] for g, _ in all_unknowns})
        findings.append(Finding(
            rule="R1a", vg_class=1, verdict=UNKNOWN,
            guard=f"{len(all_unknowns)} skipif guard(s) across {len(files)} file(s)",
            mechanism="skipif conditions that are runtime expressions "
                      "(platform/capability probes), aggregated",
            evidence=Evidence(
                summary="these skip conditions are not statically decidable — no "
                        "environment variable to trace. Reported once, in aggregate: "
                        "each may be a legitimate platform probe or a permanently-"
                        "true condition; only observing the tests RUN distinguishes "
                        "them.",
                searched=files,
                found=[f"{g} :: {c}" for g, c in all_unknowns[:10]]
                      + ([f"... and {len(all_unknowns) - 10} more"]
                         if len(all_unknowns) > 10 else []),
            ),
            question="could any of these skip conditions be permanently true? — "
                     "static analysis cannot decide.",
            fix="for capability probes, ensure at least one CI job provides the "
                "capability and would FAIL (not skip) without it",
        ))
    findings.extend(scan_marker_exclusion(repo))
    findings.extend(scan_polyglot(repo))
    return findings
