from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..ir import AgentIR
from ..report import Report


@runtime_checkable
class Adapter(Protocol):
    name: str

    def detect(self, path: Path) -> bool: ...
    def parse(self, path: Path) -> tuple[AgentIR, Report]: ...
    def emit(self, ir: AgentIR, out: Path, options: dict[str, str] | None = None) -> Report: ...
