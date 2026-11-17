"""Read-only fetchers: bring an agent definition from a cloud/cluster into local files.

Each fetcher is ``fetch(ref, out, options, client=None) -> (files, report)`` and writes files in a
format the platform's adapter already parses. They only call Get/List style read operations; nothing
is created, changed or deleted, and secrets are never read. Registered under the entry-point group
``agent_translator.fetchers``. The optional ``client`` argument lets tests inject a fake.
"""
from __future__ import annotations

import re
from importlib.metadata import entry_points
from pathlib import Path
from typing import Protocol

from ..report import Report


class Fetcher(Protocol):
    def __call__(self, ref: str, out: Path, options: dict[str, str],
                 client: object | None = None) -> tuple[list[Path], Report]: ...


def load_fetchers() -> dict[str, Fetcher]:
    return {ep.name: ep.load() for ep in entry_points(group="agent_translator.fetchers")}


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "agent"
