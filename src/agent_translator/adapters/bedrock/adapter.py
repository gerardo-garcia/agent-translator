"""Bedrock Agents adapter (CloudFormation templates with AWS::Bedrock::Agent)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...ir import AgentIR
from ...models import adapt_ir
from ...report import Report, Severity
from . import cfn, emitter, getagent, sdk
from .parser import Parser

_SUFFIXES = (".yaml", ".yml", ".json", ".template")


def _load(path: Path) -> Any:
    return cfn.loader().load(path.read_text())


class BedrockAdapter:
    name = "bedrock"

    def detect(self, path: Path) -> bool:
        if path.suffix not in _SUFFIXES:
            return False
        try:
            d = _load(path)
        except Exception:
            return False
        if getagent.is_getagent(d):
            return True
        res = d.get("Resources") if isinstance(d, dict) else None
        return isinstance(res, dict) and any(
            isinstance(r, dict) and r.get("Type") == cfn.AGENT for r in res.values())

    def parse(self, path: Path) -> tuple[AgentIR, Report]:
        rep = Report(source=self.name)
        tpl = _load(path)
        if getagent.is_getagent(tpl):
            rep.add(Severity.info, "format", str(path), "Bedrock API/CLI JSON (GetAgent)")
            tpl = getagent.to_template(tpl, rep)
        if not isinstance(tpl, dict) or "Resources" not in tpl:
            rep.add(Severity.error, "not-a-template", str(path), "no Resources section")
            return AgentIR(agents=[]), rep
        if "Transform" in tpl:
            rep.add(Severity.warning, "macro-ignored", "Transform",
                    f"template macros ({tpl['Transform']}) are not expanded")
        agents = Parser(tpl, rep).parse()
        if not agents:
            rep.add(Severity.error, "no-agents", str(path), "no AWS::Bedrock::Agent resource")
        return AgentIR(agents=agents), rep

    def emit(self, ir: AgentIR, out: Path, options: dict[str, str] | None = None) -> Report:
        rep = Report(target=self.name)
        fmt = (options or {}).get("format", "cloudformation")
        if fmt not in ("cloudformation", "sdk"):
            rep.add(Severity.error, "bad-option", "format",
                    f"{fmt!r}: choose one of ['cloudformation', 'sdk']")
            return rep
        ir = adapt_ir(ir, "bedrock", options, rep)
        tpl = emitter.emit(ir, rep)
        if not tpl:
            return rep
        if fmt == "sdk":
            for f in sdk.emit(tpl, out, rep):
                rep.add(Severity.info, "written", str(f), "Bedrock API request")
            return rep
        out.mkdir(parents=True, exist_ok=True)
        path = out / "template.yaml"
        with path.open("w") as f:
            cfn.dumper().dump(cfn.lit(tpl), f)
        rep.add(Severity.info, "written", str(path), "CloudFormation template")
        return rep
