"""Deployers: create an agent on a platform from files this tool emitted.

Unlike fetchers, these WRITE to real accounts, so they are conservative by construction:

* ``plan`` is always computed first and never changes anything. Offline by default; with
  ``-O check=remote`` it makes read-only calls (existence checks, server-side validation).
* ``apply`` only runs when the caller asked for it explicitly (``--apply``, see the CLI).
* Create-only: an existing object/agent/stack is an error, never overwritten, updated or deleted.
  (Azure can add a *version* to an existing agent, but only with ``-O new_version=true``.)
* Files that still contain placeholders (``REPLACE-WITH-…``) are refused.
* Secrets are never created or read.

Each deployer module exposes ``plan(path, options, client=None) -> (Plan | None, Report)`` and
``apply(plan, options, client=None) -> Report``. They are registered under the entry-point group
``agent_translator.deployers``; the optional ``client`` lets tests inject a fake.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from ..report import Report, Severity

PLACEHOLDER = re.compile(r"REPLACE-WITH[-A-Za-z0-9_]*|<REPLACE[^>]*>")


@dataclass
class Step:
    action: str
    resource: str
    detail: str = ""


@dataclass
class Plan:
    platform: str
    target: str  # where it would be created: account/region, cluster context, project endpoint
    steps: list[Step] = field(default_factory=list)
    undo: str = ""  # how to remove what apply creates
    data: Any = None  # deployer-private payload (documents, definitions, template...)


def load_deployers() -> dict[str, Any]:
    return {ep.name: ep.load() for ep in entry_points(group="agent_translator.deployers")}


def find_placeholders(text: str) -> list[str]:
    return sorted(set(PLACEHOLDER.findall(text)))


def reject_placeholders(text: str, where: str, rep: Report) -> bool:
    """Add an error and return True if the file still has placeholders to fill in."""
    found = find_placeholders(text)
    if found:
        rep.add(Severity.error, "placeholder-left", where,
                f"fill in {', '.join(found)} before deploying (nothing was changed)")
    return bool(found)


def single_file(path: Path, names: tuple[str, ...], suffixes: tuple[str, ...],
                rep: Report) -> Path | None:
    """A file, or the one candidate inside a directory (preferring ``names``)."""
    if path.is_file():
        return path
    if not path.is_dir():
        rep.add(Severity.error, "bad-input", str(path), "no such file or directory")
        return None
    for n in names:
        if (path / n).is_file():
            return path / n
    files = sorted(f for f in path.iterdir() if f.suffix in suffixes)
    if len(files) == 1:
        return files[0]
    rep.add(Severity.error, "bad-input", str(path),
            f"expected one {'/'.join(suffixes)} file, found {len(files)}")
    return None
