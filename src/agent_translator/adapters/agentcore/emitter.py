"""IR -> CloudFormation template with ``AWS::BedrockAgentCore::Harness`` resources.

One harness per agent. Anything a harness cannot express is reported, never dropped quietly.
"""
from __future__ import annotations

import io
import re
from typing import Any

from ...ir import AgentIR, AgentSpec, ToolKind
from ...models import lookup
from ...report import Report, Severity
from ..bedrock import cfn
from ..bedrock.emitter import BEDROCK_MODEL

HARNESS = "AWS::BedrockAgentCore::Harness"
TOOL_NAME = re.compile(r"[^a-zA-Z0-9_-]+")
# IR param -> (bedrock/openai key, gemini key)
PARAMS = {"temperature": "Temperature", "top_p": "TopP", "max_tokens": "MaxTokens",
          "top_k": "TopK"}


def _num(v: Any) -> Any:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() and not isinstance(v, float) else f


class Ctx:
    def __init__(self) -> None:
        self.parameters: dict[str, dict] = {}

    def param(self, name: str, desc: str) -> dict:
        name = re.sub(r"[^0-9a-zA-Z]", "", name) or "Param"
        self.parameters.setdefault(name, {"Type": "String", "Description": desc})
        return {"Ref": name}


def harness_name(a: AgentSpec, rep: Report, path: str) -> str:
    raw = a.name or a.id
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", raw).strip("_") or "agent"
    if not s[0].isalpha():
        s = f"a_{s}"
    s = s[:40].rstrip("_")
    if s != raw:
        rep.add(Severity.warning, "name-sanitized", f"{path}.name",
                f"{raw!r} -> {s!r} (letters, digits and underscores, at most 40, starting with a letter)")
    return s


def _model(a: AgentSpec, L: str, ctx: Ctx, rep: Report, path: str) -> dict:
    m = a.model
    match = lookup(m.id)
    vendor = (match.vendor if match else m.provider) or ""
    if m.id and BEDROCK_MODEL.match(m.id) or not m.id:
        kind = "BedrockModelConfig"
    elif vendor == "openai":
        kind = "OpenAiModelConfig"
    elif vendor == "google":
        kind = "GeminiModelConfig"
    else:
        kind = "BedrockModelConfig"
    cfg: dict[str, Any] = {}
    if kind == "BedrockModelConfig" and not (m.id and BEDROCK_MODEL.match(m.id)):
        rep.add(Severity.manual, "model-required", f"{path}.model",
                f"model {m.id!r} ({m.provider}) is not a Bedrock model id; choose one "
                "(see -O model_policy=tier)")
        cfg["ModelId"] = ctx.param(f"{L}ModelId", f"Bedrock model id or inference profile "
                                                   f"(source model: {m.id})")
    else:
        cfg["ModelId"] = m.id
    if kind != "BedrockModelConfig":
        cfg["ApiKeyArn"] = ctx.param(f"{L}ApiKeyArn", "ARN of the AgentCore API key credential "
                                                      "provider for this model")
        rep.add(Severity.manual, "secret-required", f"{path}.model",
                "create an AgentCore API key credential provider holding the model key and pass its ARN")
    for k, v in m.params.items():
        key = PARAMS.get(k)
        n = _num(v)
        if key == "TopK" and kind != "GeminiModelConfig":
            key = None
        if key is None or n is None:
            rep.add(Severity.lossy, "param-dropped", f"{path}.model.params.{k}",
                    f"not supported by {kind}")
            continue
        cfg[key] = n
    return {kind: cfg}


def _tool_name(s: str, used: set[str]) -> str:
    base = (TOOL_NAME.sub("-", s).strip("-") or "tool")[:60]
    n, i = base, 2
    while n in used:
        n, i = f"{base}-{i}", i + 1
    used.add(n)
    return n


def _tools(a: AgentSpec, servers: dict[str, dict], rep: Report, path: str) -> list[dict]:
    out: list[dict] = []
    used: set[str] = set()
    done: set[str] = set()
    for t in a.tools:
        tp = f"{path}.tools[{t.id}]"
        if t.requires_confirmation:
            rep.add(Severity.lossy, "confirmation-dropped", tp,
                    "no per-call human approval setting in the harness resource")
        if t.kind is ToolKind.mcp:
            ref = t.server_ref or ""
            s = servers.get(ref)
            if not s or not s.get("url"):
                rep.add(Severity.manual, "mcp-server-undefined", tp,
                        f"no URL known for MCP server {ref!r}; add a remote_mcp tool manually")
                continue
            if t.tool_names:
                rep.add(Severity.lossy, "tool-filter-dropped", tp,
                        f"all tools of {ref!r} are exposed; filtering {t.tool_names} is not "
                        "translated (AllowedTools semantics are not documented for MCP tools)")
            if ref in done:
                continue
            done.add(ref)
            if str(s.get("protocol", "")).lower() == "sse":
                rep.add(Severity.warning, "mcp-protocol", tp,
                        "the harness reference does not say it supports SSE servers; check")
            if s.get("headers"):
                rep.add(Severity.manual, "mcp-headers-dropped", tp,
                        "headers (possibly credentials) are not copied; set OutboundAuth or Headers")
            out.append({"Type": "remote_mcp", "Name": _tool_name(ref, used),
                        "Config": {"RemoteMcp": {"Url": s["url"]}}})
        elif t.kind is ToolKind.function:
            desc = t.description or t.name or t.id
            schema = t.parameters or {"type": "object", "properties": {}}
            if t.impl_ref:
                rep.add(Severity.lossy, "function-impl-dropped", tp,
                        f"{t.impl_ref!r} is not used: an inline function is returned to the caller "
                        "for execution")
            else:
                rep.add(Severity.info, "inline-function", tp,
                        "the harness returns this call to the caller, which must run it")
            out.append({"Type": "inline_function", "Name": _tool_name(t.name or t.id, used),
                        "Config": {"InlineFunction": {"Description": desc,
                                                      "InputSchema": schema}}})
        else:
            rep.add(Severity.manual, "tool-unsupported", tp,
                    f"{t.kind.value} tools have no direct harness equivalent (a gateway, browser or "
                    "code interpreter tool must be configured by hand)")
    return out


def _memory(a: AgentSpec, rep: Report, path: str) -> dict:
    if not a.memory.enabled:
        rep.add(Severity.info, "memory-disabled", f"{path}.memory",
                "memory is explicitly disabled: the harness would otherwise create a managed "
                "memory by default")
        return {"Disabled": {}}
    cfg: dict[str, Any] = {}
    if a.memory.ttl_seconds:
        days = -(-a.memory.ttl_seconds // 86400)
        cfg["EventExpiryDuration"] = min(max(days, 3), 365)
        if cfg["EventExpiryDuration"] != days:
            rep.add(Severity.warning, "memory-ttl-clamped", f"{path}.memory",
                    f"{days} days clamped to {cfg['EventExpiryDuration']} (allowed 3-365)")
    return {"ManagedMemoryConfiguration": cfg}


def _harness(a: AgentSpec, ir: AgentIR, ctx: Ctx, rep: Report) -> dict:
    path = f"agents[{a.id}]"
    L = cfn.logical_id(a.id)
    servers = {s["id"]: s for s in ir.resources.mcp_servers}
    props: dict[str, Any] = {"HarnessName": harness_name(a, rep, path),
                             "ExecutionRoleArn": ctx.param(
                                 f"{L}ExecutionRoleArn",
                                 "ARN of the IAM role the harness assumes (trusts "
                                 "bedrock-agentcore.amazonaws.com)")}
    rep.add(Severity.manual, "role-required", path,
            "an IAM execution role for the harness is needed; pass its ARN")
    props["Model"] = _model(a, L, ctx, rep, path)
    if a.instructions:
        props["SystemPrompt"] = [{"Text": a.instructions}]
    else:
        rep.add(Severity.warning, "instruction-missing", f"{path}.instructions",
                "the harness will run with its default system prompt")
    if a.description:
        rep.add(Severity.warning, "description-dropped", f"{path}.description",
                "the harness resource has no description field")
    tools = _tools(a, servers, rep, path)
    if tools:
        props["Tools"] = tools
    props["Memory"] = _memory(a, rep, path)
    if a.guardrails:
        rep.add(Severity.lossy, "guardrails-dropped", f"{path}.guardrails",
                "the harness reference documents no guardrail setting")
    for s in a.sub_agents:
        rep.add(Severity.lossy, "collaboration-dropped", f"{path}.sub_agents",
                f"the harness has no multi-agent mode; {s!r} is emitted as a separate harness "
                "and is not wired to this one")
    for r in a.remote_agents:
        rep.add(Severity.manual, "remote-agent-unsupported", f"{path}.remote_agents[{r.id}]",
                "remote agents (A2A) cannot be declared on a harness")
    return {"Type": HARNESS, "Properties": props}


def emit_template(ir: AgentIR, rep: Report) -> str:
    ctx = Ctx()
    resources: dict[str, Any] = {}
    for a in ir.agents:
        resources[f"{cfn.logical_id(a.id)}Harness"] = _harness(a, ir, ctx, rep)
    tpl: dict[str, Any] = {"AWSTemplateFormatVersion": "2010-09-09"}
    if ctx.parameters:
        tpl["Parameters"] = ctx.parameters
    tpl["Resources"] = resources
    buf = io.StringIO()
    cfn.dumper().dump(cfn.lit(tpl), buf)
    return buf.getvalue()
