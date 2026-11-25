"""CrewAI adapter: JSON crew projects, standalone agents and classic ``config/*.yaml``."""
from __future__ import annotations

from pathlib import Path

from ...ir import AgentIR
from ...models import adapt_ir
from ...report import Report, Severity
from . import common as c
from .emitter import emit_project
from .parser import kind_of, parse

SUFFIXES = (".json", ".jsonc", ".yaml", ".yml")


class CrewAIAdapter:
    name = "crewai"

    def detect(self, path: Path) -> bool:
        if path.suffix not in SUFFIXES:
            return False
        try:
            d = c.load_any(path)
        except Exception:
            return False
        return kind_of(d) is not None

    def parse(self, path: Path) -> tuple[AgentIR, Report]:
        rep = Report(source=self.name)
        ir = parse(path, rep)
        if not ir.agents:
            rep.add(Severity.error, "no-agents", str(path), "no CrewAI agent found")
        return ir, rep

    def emit(self, ir: AgentIR, out: Path, options: dict[str, str] | None = None) -> Report:
        rep = Report(target=self.name)
        ir = adapt_ir(ir, "crewai", options, rep)
        if not ir.agents:
            rep.add(Severity.error, "no-agents", "ir", "nothing to emit")
            return rep
        for rel, text in emit_project(ir, rep).items():
            f = out / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
            rep.add(Severity.info, "written", str(f), "CrewAI JSON project")
        return rep
