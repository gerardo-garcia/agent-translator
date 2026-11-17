from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

from .adapters.base import Adapter


def load_adapters() -> dict[str, Adapter]:
    out: dict[str, Adapter] = {}
    for ep in entry_points(group="agent_translator.adapters"):
        out[ep.name] = ep.load()()
    return out


def detect_adapter(path: Path, adapters: dict[str, Adapter]) -> Adapter | None:
    for a in adapters.values():
        if a.detect(path):
            return a
    return None
