"""The class-1 heart: is this environment variable set anywhere the repo runs?

Every answer enumerates exactly what was searched — present files by name,
absent conventional locations marked ``(absent)`` — because a verdict without
its search set is an unverified claim, and this tool does not get to make any.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .repo import Repo

#: Conventional executable-config locations, searched whether or not present.
_CONVENTIONAL = (
    "tox.ini", "noxfile.py", "Makefile", "makefile", "GNUmakefile",
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
    "Dockerfile", "pytest.ini", "setup.cfg", "pyproject.toml",
)


@dataclass
class EnvSearch:
    var: str
    workflow_hits: list[str] = field(default_factory=list)
    config_hits: list[str] = field(default_factory=list)
    script_hits: list[str] = field(default_factory=list)
    docs_hits: list[str] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)
    n_workflows: int = 0

    @property
    def executable_hits(self) -> list[str]:
        return self.workflow_hits + self.config_hits + self.script_hits

    def summary(self) -> str:
        base = (
            f"flag set in {len(self.workflow_hits)} of {self.n_workflows} workflows, "
            f"{len(self.script_hits)} scripts, {len(self.config_hits)} config files"
        )
        if self.docs_hits and not self.executable_hits:
            base += (
                f"; mentioned in {len(self.docs_hits)} docs file(s) — runnable by "
                "hand, never run by machine (set in no executable path)"
            )
        return base


def _set_pattern(var: str) -> re.Pattern:
    # "VAR:" (YAML env key), "VAR=" (shell / dotenv / make), "ENV VAR" (Dockerfile),
    # setenv("VAR"/setdefault("VAR"/environ["VAR"] = (conftest-style programmatic set).
    v = re.escape(var)
    return re.compile(
        rf"(?:^|\s|\"|'){v}\s*[:=]"
        rf"|ENV\s+{v}\b"
        rf"|setenv\(\s*[\"']{v}[\"']"
        rf"|setdefault\(\s*[\"']{v}[\"']"
        rf"|environ\[\s*[\"']{v}[\"']\s*\]\s*=",
        re.M,
    )


def _mention_pattern(var: str) -> re.Pattern:
    return re.compile(rf"\b{re.escape(var)}=", re.M)


def env_search(repo: Repo, var: str, *, exclude_rel: str = "") -> EnvSearch:
    """Search everywhere the repo runs for VAR being *set* (not merely read).

    ``exclude_rel`` is the guard's own file: a guard's docstring saying "set
    VAR=1" is the guard talking about itself, not the repo setting it.
    """

    res = EnvSearch(var=var)
    setter = _set_pattern(var)
    mention = _mention_pattern(var)

    workflows = repo.glob(".github/workflows/*.yml", ".github/workflows/*.yaml")
    res.n_workflows = len(workflows)
    for p in workflows:
        rel = repo.rel(p)
        res.searched.append(rel)
        if setter.search(repo.read(p)):
            res.workflow_hits.append(rel)

    for name in _CONVENTIONAL:
        p = repo.root / name
        if p.exists():
            res.searched.append(name)
            if setter.search(repo.read(p)):
                res.config_hits.append(name)
        else:
            res.searched.append(f"{name}(absent)")

    extra_config = repo.glob(
        ".devcontainer/**", ".env", ".env.*", "Dockerfile*", "docker/**",
    )
    for p in extra_config:
        rel = repo.rel(p)
        if rel in res.searched or rel == exclude_rel:
            continue
        res.searched.append(rel)
        if setter.search(repo.read(p)):
            res.config_hits.append(rel)

    scripts = repo.glob("scripts/**", "bin/**", "*.sh", "ci/**")
    conftests = [p for p in repo.files() if p.name == "conftest.py"]
    for p in list(scripts) + conftests:
        rel = repo.rel(p)
        if rel == exclude_rel or rel in res.searched:
            continue
        res.searched.append(rel)
        if setter.search(repo.read(p)):
            res.script_hits.append(rel)

    docs = repo.glob("README*", "docs/**/*.md", "docs/**/*.rst", "*.md", "*.rst")
    for p in docs:
        rel = repo.rel(p)
        if rel == exclude_rel or rel in res.searched:
            continue
        # docs are searched for *invocation* mentions (VAR=...), not YAML keys
        if mention.search(repo.read(p)):
            res.docs_hits.append(rel)
    res.searched.append("README*/docs/** (invocation mentions)")
    return res
