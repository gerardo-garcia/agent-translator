"""LlamaIndex adapter (emit-only): IR -> a Python project (``agent.py`` + requirements).

LlamaIndex agents are Python code, not a declarative document, so there is nothing reliable to
parse back (see DESIGN.md "LlamaIndex target").
"""
from __future__ import annotations

from pathlib import Path

from ...ir import AgentIR
from ...models import adapt_ir
from ...report import Report, Severity
from .emitter import emit_project


class LlamaIndexAdapter:
    name = "llamaindex"

    def detect(self, path: Path) -> bool:
        return False

    def parse(self, path: Path) -> tuple[AgentIR, Report]:
        rep = Report(source=self.name)
        rep.add(Severity.error, "parse-unsupported", str(path),
                "llamaindex is an emit-only target: agents are Python code and cannot be read back")
        return AgentIR(agents=[]), rep

    def emit(self, ir: AgentIR, out: Path, options: dict[str, str] | None = None) -> Report:
        rep = Report(target=self.name)
        ir = adapt_ir(ir, "llamaindex", options, rep)
        if not ir.agents:
            rep.add(Severity.error, "no-agents", "ir", "nothing to emit")
            return rep
        out.mkdir(parents=True, exist_ok=True)
        for name, text in emit_project(ir, rep).items():
            (out / name).write_text(text)
            rep.add(Severity.info, "written", str(out / name), "LlamaIndex project")
        return rep
