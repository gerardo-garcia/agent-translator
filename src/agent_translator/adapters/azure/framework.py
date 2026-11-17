"""Microsoft Agent Framework declarative YAML (`kind: Prompt`) -> IR and back."""
from __future__ import annotations

from typing import Any

from ruamel.yaml.scalarstring import LiteralScalarString

from ...ir import AgentIR, AgentSpec, Collaboration, ModelSpec, Tool, ToolKind
from ...report import Report, Severity
from . import common as c

V = "agent-framework"
OPTIONS = {"temperature": "temperature", "topP": "top_p", "topK": "top_k",
           "maxOutputTokens": "max_tokens"}
HANDLED = {"kind", "name", "description", "instructions"}
PROVIDER = {"AzureOpenAI": "openai", "OpenAI": "openai", "AzureAIAgent": None}


def is_agent(d: dict) -> bool:
    return d.get("kind") == "Prompt" and isinstance(d.get("name"), str)


# ------------------------------------------------------------------------- parse
def parse(d: dict, rep: Report) -> tuple[AgentSpec, list[dict]]:
    name = d["name"]
    path = f"agents[{name}]"
    ax: dict[str, Any] = {}
    m = dict(d.get("model") or {})
    mid = m.get("id")
    if isinstance(mid, str) and mid.startswith("="):
        ax["modelExpr"] = mid
        rep.add(Severity.manual, "model-expression", f"{path}.model",
                f"model id is the expression {mid!r}: its value comes from the environment")
        mid = None
    provider = PROVIDER.get(m.get("provider")) or c.vendor_of(mid)
    opts = dict(m.get("options") or {})
    params = {ir: opts.pop(az) for az, ir in OPTIONS.items() if az in opts}
    if opts:
        ax["modelOptions"] = opts
        rep.add(Severity.warning, "model-options-azure-only", path,
                f"{sorted(opts)} kept in extensions.azure only")
    for k in ("provider", "apiType", "connection"):
        if k in m:
            ax.setdefault("model", {})[k] = m[k]
    if mid:
        rep.add(Severity.info, "model-unverified", path, f"model id {mid!r} copied as-is")
    servers: list[dict] = []
    tools: list[Tool] = []
    used: set[str] = set()
    for i, t in enumerate(d.get("tools") or []):
        _tool(t, i, f"{path}.tools[{i}]", tools, servers, used, rep)
    left = {k: v for k, v in d.items() if k not in HANDLED | {"model", "tools"}}
    if left:
        ax["fields"] = left
        rep.add(Severity.info, "azure-fields-preserved", path,
                f"{sorted(left)} kept in extensions.azure")
    return AgentSpec(id=name, name=name, description=d.get("description"),
                     instructions=d.get("instructions"),
                     model=ModelSpec(provider=provider, id=mid, params=params), tools=tools,
                     collaboration=Collaboration(mode="none"),
                     extensions={"azure": {V: ax}}), servers


def _tool(t: dict, i: int, where: str, tools: list[Tool], servers: list[dict], used: set[str],
          rep: Report) -> None:
    kind = str(t.get("kind", ""))
    rest = {k: v for k, v in t.items() if k not in ("kind", "name", "description")}
    base = c.safe_name(t.get("name") or kind or f"tool-{i}")

    def tid() -> str:
        return c.unique(base, used)

    if kind == "function":
        keep = {k: v for k, v in rest.items() if k != "parameters"}
        tools.append(Tool(id=tid(), kind=ToolKind.function, name=t.get("name"),
                          description=t.get("description"),
                          parameters=c.schema_to_json(t.get("parameters"))
                          or {"type": "object", "properties": {}},
                          extensions={"azure": {V: {"tool": keep}}} if keep else {}))
    elif kind == "mcp":
        sid = c.server_id(t.get("serverName") or t.get("name"), t.get("url"), f"mcp-{i + 1}")
        if t.get("url"):
            servers.append({"id": sid, "url": t["url"], "protocol": "streamable-http"})
        else:
            rep.add(Severity.manual, "mcp-server-unresolved", where, "MCP tool without url")
        am = t.get("approvalMode") or {}
        mode = am.get("kind") if isinstance(am, dict) else am
        conf = {"always": True, "never": False}.get(mode)
        if mode == "specify":
            rep.add(Severity.warning, "approval-granular", where,
                    "per-tool approval rules kept in extensions.azure only")
        keep = {k: v for k, v in rest.items() if k not in ("url", "allowedTools", "serverName")
                and not (k == "approvalMode" and conf is not None)}
        tools.append(Tool(id=tid(), kind=ToolKind.mcp, name=t.get("name"),
                          description=t.get("description"), server_ref=sid,
                          tool_names=list(t.get("allowedTools") or []), requires_confirmation=conf,
                          extensions={"azure": {V: {"tool": keep, "kind": kind,
                                                    "serverName": t.get("serverName")}}}))
    else:
        knowledge = kind == "file_search"
        if kind not in {"web_search", "code_interpreter", "file_search", "openapi", "custom"}:
            rep.add(Severity.warning, "tool-unknown", where, f"{kind!r} kept as a built-in")
        tools.append(Tool(id=tid(), kind=ToolKind.knowledge if knowledge else ToolKind.builtin,
                          name=t.get("name") or kind, description=t.get("description"),
                          kb_ref=(rest.get("vectorStoreIds") or [None])[0] if knowledge else None,
                          extensions={"azure": {V: {"tool": rest, "kind": kind}}}))


# ------------------------------------------------------------------------- emit
def emit(a: AgentSpec, ir: AgentIR, rep: Report) -> dict[str, Any]:
    path = f"agents[{a.id}]"
    ax = c.az(a).get(V, {})
    servers = {s["id"]: s for s in ir.resources.mcp_servers}
    d: dict[str, Any] = {"kind": "Prompt", "name": a.name}
    left = dict(ax.get("fields") or {})
    if "displayName" in left:
        d["displayName"] = left.pop("displayName")
    if a.description:
        d["description"] = a.description
    if a.instructions:
        d["instructions"] = LiteralScalarString(a.instructions) if "\n" in a.instructions \
            else a.instructions
    model: dict[str, Any] = {}
    mid = a.model.id or ax.get("modelExpr")
    if mid:
        model["id"] = mid
        if a.model.provider not in (None, "openai", "microsoft"):
            rep.add(Severity.manual, "model-unmapped", f"{path}.model",
                    f"{mid!r} ({a.model.provider}) copied as-is; the chat client you configure "
                    "must serve a model with that id")
    else:
        rep.add(Severity.info, "model-inherited", f"{path}.model",
                "no model id: the chat client you pass to the factory decides")
    model.update(ax.get("model", {}))
    opts = {az: a.model.params[ir_k] for az, ir_k in OPTIONS.items() if ir_k in a.model.params}
    for k in sorted(set(a.model.params) - set(OPTIONS.values())):
        rep.add(Severity.lossy, "param-dropped", f"{path}.model.params.{k}",
                f"no {k!r} option in the model definition")
    opts.update(ax.get("modelOptions", {}))
    if opts:
        model["options"] = opts
    if model:
        d["model"] = model
    tools = _tools(a, path, servers, rep)
    if tools:
        d["tools"] = tools
    if a.sub_agents or a.remote_agents:
        rep.add(Severity.lossy, "collaboration-unsupported", path,
                "a declarative agent has no sub-agents; use a declarative workflow "
                "(manual). Sub-agents are emitted as independent files")
    if a.memory.enabled:
        rep.add(Severity.lossy, "memory-dropped", path, "no equivalent")
    if a.guardrails:
        rep.add(Severity.lossy, "guardrails-dropped", path, "no equivalent")
    d.update(left)
    return d


def _tools(a: AgentSpec, path: str, servers: dict, rep: Report) -> list[dict]:
    out: list[dict] = []
    for t in a.tools:
        tp = f"{path}.tools[{t.id}]"
        tx = c.az(t).get(V, {})
        raw = dict(tx.get("tool", {}))
        if t.kind is ToolKind.function:
            if t.requires_confirmation:
                rep.add(Severity.lossy, "confirmation-dropped", tp,
                        "function tools have no approval setting (only MCP tools do)")
            e: dict[str, Any] = {"kind": "function", "name": t.name or t.id}
            if t.description:
                e["description"] = t.description
            e["parameters"] = c.json_to_schema(t.parameters)
            if t.impl_ref and "bindings" not in raw:
                rep.add(Severity.manual, "function-binding", tp,
                        f"bind the function to code at load time (original: {t.impl_ref})")
            e.update(raw)
            out.append(e)
        elif t.kind is ToolKind.mcp:
            s = servers.get(t.server_ref or "")
            if not s or not s.get("url"):
                rep.add(Severity.manual, "mcp-server-unknown", tp,
                        f"MCP server {t.server_ref!r} has no URL; tool dropped")
                continue
            if str(s.get("protocol")) == "stdio":
                rep.add(Severity.manual, "mcp-stdio", tp, "stdio MCP server; tool dropped")
                continue
            e = {"kind": "mcp", "name": t.name or t.id}
            if t.description:
                e["description"] = t.description
            if tx.get("serverName"):
                e["serverName"] = tx["serverName"]
            e["url"] = s["url"]
            if t.requires_confirmation is not None:
                e["approvalMode"] = {"kind": "always" if t.requires_confirmation else "never"}
            if t.tool_names:
                e["allowedTools"] = t.tool_names
            for k, v in raw.items():
                e.setdefault(k, v)
            out.append(e)
        elif t.kind in (ToolKind.builtin, ToolKind.knowledge) and tx.get("kind"):
            e = {"kind": tx["kind"], "name": t.name or tx["kind"], **raw}
            if t.description:
                e["description"] = t.description
            out.append(e)
        elif t.kind is ToolKind.builtin and (t.name in c.BUILTIN_MAP):
            typ = c.BUILTIN_MAP[t.name]
            rep.add(Severity.info, "tool-mapped", tp, f"{t.name} -> {typ}")
            out.append({"kind": typ, "name": typ})
        elif t.kind is ToolKind.builtin and t.name == "AMAZON.UserInput":
            rep.add(Severity.info, "tool-implicit", tp, "agents can ask the user without a tool")
        else:
            rep.add(Severity.lossy, "tool-dropped", tp,
                    f"{t.kind.value} tool {t.name or t.id!r} cannot be declared here")
    return out
