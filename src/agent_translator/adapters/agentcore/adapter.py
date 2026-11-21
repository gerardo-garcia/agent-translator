"""Amazon Bedrock AgentCore harness adapter (emit-only): IR -> CloudFormation template.

AWS closed Bedrock Agents Classic to new accounts on 2026-07-30 and recommends AgentCore. The
harness (``AWS::BedrockAgentCore::Harness``) is its declarative agent: model, system prompt, tools,
memory. Reading a harness back into the IR is not implemented.
"""
from __future__ import annotations

from pathlib import Path

from ...ir import AgentIR
from ...models import adapt_ir
from ...report import Report, Severity
from .emitter import emit_template


class AgentCoreAdapter:
    name = "agentcore"

    def detect(self, path: Path) -> bool:
        return False

    def parse(self, path: Path) -> tuple[AgentIR, Report]:
        rep = Report(source=self.name)
        rep.add(Severity.error, "parse-unsupported", str(path),
                "agentcore is an emit-only target: reading a harness is not implemented")
        return AgentIR(agents=[]), rep

    def emit(self, ir: AgentIR, out: Path, options: dict[str, str] | None = None) -> Report:
        rep = Report(target=self.name)
        ir = adapt_ir(ir, "agentcore", options, rep)
        if not ir.agents:
            rep.add(Severity.error, "no-agents", "ir", "nothing to emit")
            return rep
        out.mkdir(parents=True, exist_ok=True)
        p = out / "template.yaml"
        p.write_text(emit_template(ir, rep))
        rep.add(Severity.info, "written", str(p), "CloudFormation template (AWS::BedrockAgentCore::Harness)")
        return rep
