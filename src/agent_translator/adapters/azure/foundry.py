"""Foundry Agent Service: prompt agents declared in azure.yaml (`host: azure.ai.agent`)
or as a bare agent definition (`kind: prompt`) -> IR and back."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml.scalarstring import LiteralScalarString

from ...ir import AgentIR, AgentSpec, Collaboration, ModelSpec, RemoteAgent, Tool, ToolKind
from ...report import Report, Severity
from . import common as c

V = "foundry"
HANDLED = {"kind", "name", "description", "model", "instructions", "temperature", "top_p",
           "tools", "host", "project", "uses"}


def is_service(d: dict) -> bool:
    return isinstance(d.get("services"), dict) and any(
        isinstance(s, dict) and s.get("host") == "azure.ai.agent" for s in d["services"].values())


def is_bare(d: dict) -> bool:
    if isinstance(d.get("definition"), dict):          # AgentVersionDetails / create_version body
        d = d["definition"]
    return d.get("kind") == "prompt" and isinstance(d.get("model"), str)


# ------------------------------------------------------------------------- parse
class Parser:
    def __init__(self, rep: Report, base: Path):
        self.rep, self.base = rep, base
        self.servers: dict[str, dict] = {}
        self.agents: list[AgentSpec] = []
        self.used: set[str] = set()

    def parse(self, d: dict) -> None:
        if is_service(d):
            deployments = {}
            for key, s in d["services"].items():
                if isinstance(s, dict) and s.get("host") == "azure.ai.project":
                    for dep in s.get("deployments") or []:
                        deployments[dep.get("name")] = dep
            for key, s in d["services"].items():
                if not isinstance(s, dict):
                    continue
                if "$ref" in s:
                    s = self._ref(key, s)
                if s.get("host") == "azure.ai.agent":
                    self.service(key, s, deployments)
                elif s.get("host") not in ("azure.ai.project",):
                    self.rep.add(Severity.info, "service-ignored", f"services.{key}",
                                 f"host {s.get('host')!r} is not translated")
        else:
            body = d["definition"] if isinstance(d.get("definition"), dict) else d
            top = {k: v for k, v in d.items() if k != "definition"}
            self.service(top.get("name") or body.get("name") or "agent", {**top, **body}, {})

    def _ref(self, key: str, s: dict) -> dict:
        from .common import yaml_safe
        p = self.base / s["$ref"]
        if not p.is_file():
            self.rep.add(Severity.manual, "ref-unresolved", f"services.{key}", f"{p} not found")
            return {k: v for k, v in s.items() if k != "$ref"}
        inner = yaml_safe().load(p.read_text()) or {}
        return {**inner, **{k: v for k, v in s.items() if k != "$ref"}}

    def service(self, key: str, s: dict, deployments: dict) -> None:
        path = f"services.{key}"
        kind = s.get("kind")
        if kind != "prompt":
            self.rep.add(Severity.manual, "agent-kind-unsupported", path,
                         f"kind {kind!r} is not translated (hosted agents are container code, "
                         "workflow and voice agents have no IR equivalent)")
            return
        name = s.get("name") or key
        model_id = s.get("model")
        model = ModelSpec(provider=c.vendor_of(model_id), id=model_id,
                          params={ir: s[az] for az, ir in c.PARAMS.items() if az in s})
        ax: dict[str, Any] = {"service": key}
        if model_id in deployments:
            ax["deployment"] = deployments[model_id]
        elif model_id:
            self.rep.add(Severity.info, "model-deployment-external", path,
                         f"deployment {model_id!r} is not declared in the file")
        self.rep.add(Severity.info, "model-unverified", path, f"model id {model_id!r} copied as-is")

        tools: list[Tool] = []
        remote: list[RemoteAgent] = []
        used = {t.id for t in tools}
        for i, t in enumerate(s.get("tools") or []):
            self.tool(t, i, f"{path}.tools[{i}]", tools, remote, used)
        left = {k: v for k, v in s.items() if k not in HANDLED}
        if left:
            ax["fields"] = left
            self.rep.add(Severity.info, "azure-fields-preserved", path,
                         f"{sorted(left)} kept in extensions.azure")
        if "toolboxes" in left or "skills" in left or "harness" in left:
            self.rep.add(Severity.warning, "toolbox-skills-azure-only", path,
                         "toolboxes, skills and harness are Foundry resources with no IR equivalent")
        aid = c.unique(key, self.used)
        self.agents.append(AgentSpec(
            id=aid, name=name, description=s.get("description"), instructions=s.get("instructions"),
            model=model, tools=tools, remote_agents=remote,
            collaboration=Collaboration(mode="supervisor" if remote else "none"),
            extensions={"azure": {V: ax}}))

    def tool(self, t: dict, i: int, where: str, tools: list[Tool], remote: list[RemoteAgent],
             used: set[str]) -> None:
        typ = t.get("type", "")
        rest = {k: v for k, v in t.items() if k != "type"}
        ext = {"azure": {V: {"tool": rest}}}

        def tid(base: str) -> str:
            return c.unique(c.safe_name(base) or f"tool-{i}", used)

        if typ == "function":
            tools.append(Tool(id=tid(t.get("name", "")), kind=ToolKind.function, name=t.get("name"),
                              description=t.get("description"), parameters=t.get("parameters"),
                              extensions={"azure": {V: {"tool": {k: v for k, v in rest.items()
                                                                  if k not in ("name", "description",
                                                                               "parameters")}}}}))
        elif typ == "mcp":
            sid = c.server_id(t.get("server_label"), t.get("server_url"), f"mcp-{i + 1}")
            entry = {"id": sid, "protocol": "streamable-http"}
            if t.get("server_url"):
                entry["url"] = t["server_url"]
            else:
                self.rep.add(Severity.manual, "mcp-server-unresolved", where,
                             f"MCP server {sid!r} has no server_url (resolved through a toolbox or "
                             "project connection)")
            self.servers.setdefault(sid, entry)
            ra = t.get("require_approval")
            conf = {"always": True, "never": False}.get(ra) if isinstance(ra, str) else None
            if isinstance(ra, dict):
                self.rep.add(Severity.warning, "approval-granular", where,
                             "per-tool approval rules kept in extensions.azure only")
            allowed = t.get("allowed_tools")
            keep = {k: v for k, v in rest.items() if k not in ("server_label", "server_url",
                                                                "require_approval") and not (
                k == "allowed_tools" and isinstance(allowed, list))}
            if isinstance(ra, dict):
                keep["require_approval"] = ra
            tools.append(Tool(id=tid(f"mcp-{sid}"), kind=ToolKind.mcp, server_ref=sid,
                              tool_names=list(allowed) if isinstance(allowed, list) else [],
                              requires_confirmation=conf,
                              extensions={"azure": {V: {"tool": keep}}} if keep else {}))
        elif typ == "azure_ai_search":
            idx = ((t.get("azure_ai_search") or {}).get("indexes") or [{}])[0]
            tools.append(Tool(id=tid(t.get("name") or typ), kind=ToolKind.knowledge,
                              name=t.get("name"), description=t.get("description"),
                              kb_ref=idx.get("index_name"), extensions=ext))
        elif typ == "file_search":
            vs = t.get("vector_store_ids") or []
            tools.append(Tool(id=tid(t.get("name") or typ), kind=ToolKind.knowledge,
                              name=t.get("name"), description=t.get("description"),
                              kb_ref=vs[0] if vs else None, top_k=t.get("max_num_results"),
                              extensions=ext))
        elif typ == "openapi":
            o = t.get("openapi") or {}
            tools.append(Tool(id=tid(o.get("name") or typ), kind=ToolKind.openapi,
                              name=o.get("name"), description=o.get("description"),
                              spec=o.get("spec"), extensions={"azure": {V: {"tool": {
                                  "openapi": {k: v for k, v in o.items()
                                              if k not in ("name", "description", "spec")}}}}}))
        elif typ in ("a2a", "a2a_preview"):
            url = t.get("base_url")
            rid = c.server_id(t.get("name"), url, f"a2a-{i + 1}")
            remote.append(RemoteAgent(id=rid, name=t.get("name") or rid, ref=url,
                                      extensions={"azure": {V: {"tool": rest, "type": typ}}}))
        else:
            if typ not in c.TOOL_TYPES:
                self.rep.add(Severity.warning, "tool-unknown", where,
                             f"{typ!r} is not a known Foundry tool type; kept as a built-in")
            tools.append(Tool(id=tid(typ), kind=ToolKind.builtin, name=typ, extensions=ext))


# ------------------------------------------------------------------------- emit
def emit(ir: AgentIR, rep: Report) -> dict[str, Any]:
    servers = {s["id"]: s for s in ir.resources.mcp_servers}
    services: dict[str, Any] = {}
    deployments: dict[str, Any] = {}
    used: set[str] = {"ai-project"}
    for a in ir.agents:
        key = c.unique(c.safe_name(c.az(a).get(V, {}).get("service") or a.id), used)
        services[key] = _agent(a, key, servers, rep, deployments)
    project: dict[str, Any] = {"host": "azure.ai.project"}
    if deployments:
        project["deployments"] = list(deployments.values())
    return {"name": c.safe_name(ir.agents[0].id) if len(ir.agents) == 1 else "agents",
            "services": {"ai-project": project, **services},
            "infra": {"provider": "microsoft.foundry"}}


def _agent(a: AgentSpec, key: str, servers: dict, rep: Report, deployments: dict) -> dict:
    path = f"agents[{a.id}]"
    ax = c.az(a).get(V, {})
    name = c.safe_name(a.name)
    if name != a.name:
        rep.add(Severity.warning, "name-sanitized", f"{path}.name",
                f"{a.name!r} -> {name!r} (letters, digits and hyphens, at most 63)")
    s: dict[str, Any] = {"host": "azure.ai.agent", "project": ".", "uses": ["ai-project"],
                         "kind": "prompt", "name": name}
    if a.description:
        s["description"] = a.description
    if a.model.id:
        s["model"] = a.model.id
        if "deployment" in ax:
            deployments.setdefault(a.model.id, ax["deployment"])
        else:
            rep.add(Severity.warning, "model-deployment-unverified", f"{path}.model",
                    f"a deployment named {a.model.id!r} must exist in the Foundry project "
                    "(the file declares none)")
        if a.model.provider not in (None, "openai", "microsoft"):
            rep.add(Severity.manual, "model-unmapped", f"{path}.model",
                    f"{a.model.id!r} ({a.model.provider}) copied as-is; Foundry needs the name of "
                    "a deployment that serves it")
    else:
        s["model"] = "REPLACE-WITH-DEPLOYMENT-NAME"
        rep.add(Severity.manual, "model-required", f"{path}.model",
                "the source has no model id; set the Foundry deployment name")
    if a.instructions:
        s["instructions"] = LiteralScalarString(a.instructions) if "\n" in a.instructions \
            else a.instructions
    for ir_k, az_k in ((v, k) for k, v in c.PARAMS.items()):
        if ir_k in a.model.params:
            s[az_k] = a.model.params[ir_k]
    for k in sorted(set(a.model.params) - set(c.PARAMS.values())):
        rep.add(Severity.lossy, "param-dropped", f"{path}.model.params.{k}",
                f"Foundry prompt agents have no {k!r} setting")
    tools = _tools(a, path, servers, rep)
    for r in a.remote_agents:
        e = _remote(r, f"{path}.remote_agents[{r.id}]", rep)
        if e:
            tools.append(e)
    if tools:
        s["tools"] = tools
    if a.sub_agents:
        rep.add(Severity.lossy, "collaboration-unsupported", path,
                f"prompt agents cannot call sub-agents ({a.collaboration.mode}); "
                f"{a.sub_agents} are emitted as independent agents. Expose them over A2A and "
                "add an `a2a` tool, or use a workflow agent (manual)")
    if a.memory.enabled:
        rep.add(Severity.lossy, "memory-dropped", path, "no equivalent in the agent definition")
    if a.guardrails:
        rep.add(Severity.lossy, "guardrails-dropped", path,
                "content filters are configured on the model deployment, not on the agent")
    for k, v in (ax.get("fields") or {}).items():
        s.setdefault(k, v)
    return s


def _tools(a: AgentSpec, path: str, servers: dict, rep: Report) -> list[dict]:
    out: list[dict] = []
    for t in a.tools:
        tp = f"{path}.tools[{t.id}]"
        raw = dict(c.az(t).get(V, {}).get("tool", {}))
        if t.kind is ToolKind.function:
            if t.requires_confirmation:
                rep.add(Severity.lossy, "confirmation-dropped", tp,
                        "function tools run in the calling application, which decides; there is "
                        "no approval setting (only MCP tools have one)")
            e = {"type": "function", "name": t.name or t.id}
            if t.description:
                e["description"] = t.description
            if t.parameters is None:
                rep.add(Severity.manual, "schema-required", tp, "function has no parameter schema")
            e["parameters"] = t.parameters or {"type": "object", "properties": {}}
            e["strict"] = raw.pop("strict", False)
            e.update(raw)
            out.append(e)
        elif t.kind is ToolKind.mcp:
            s = servers.get(t.server_ref or "")
            if s is None:
                rep.add(Severity.manual, "mcp-server-unknown", tp,
                        f"MCP server {t.server_ref!r} is not described; tool dropped")
                continue
            if str(s.get("protocol")) == "stdio":
                rep.add(Severity.manual, "mcp-stdio", tp,
                        "Foundry reaches MCP servers over HTTP; a local stdio server must be "
                        "hosted first; tool dropped")
                continue
            if not s.get("url"):
                rep.add(Severity.manual, "mcp-server-unresolved", tp,
                        f"MCP server {t.server_ref!r} has no URL (it came from a toolbox or "
                        "project connection): declare the same toolbox/connection in the target")
            e = {"type": "mcp", "server_label": c.server_id(t.server_ref, None, t.id)}
            if s and s.get("url"):
                e["server_url"] = s["url"]
            if t.requires_confirmation is not None:
                e["require_approval"] = "always" if t.requires_confirmation else "never"
            if t.tool_names:
                e["allowed_tools"] = t.tool_names
            for k, v in raw.items():
                e.setdefault(k, v)
            out.append(e)
        elif t.kind is ToolKind.openapi:
            o = dict(raw.get("openapi", {}))
            if not isinstance(t.spec, dict):
                rep.add(Severity.manual, "schema-required", tp, "no inline OpenAPI spec")
                continue
            if "auth" not in o:
                o["auth"] = {"type": "anonymous"}
                rep.add(Severity.manual, "openapi-auth", tp,
                        "authentication set to anonymous; configure the connection the API needs")
            if not t.spec.get("servers"):
                rep.add(Severity.manual, "openapi-no-server", tp,
                        "the spec has no `servers`: add the API URL")
            out.append({"type": "openapi", "openapi": {"name": c.safe_name(t.name or t.id).replace(
                "-", "_"), **({"description": t.description} if t.description else {}),
                "spec": t.spec, **o}})
        elif t.kind in (ToolKind.knowledge, ToolKind.builtin):
            typ = _builtin_type(t, tp, raw, rep)
            if typ:
                out.append({"type": typ, **raw} if raw or typ not in c.GENERIC else {"type": typ})
        else:
            rep.add(Severity.lossy, "tool-dropped", tp, f"{t.kind.value} tools have no equivalent")
    return out


def _builtin_type(t: Tool, tp: str, raw: dict, rep: Report) -> str | None:
    if t.kind is ToolKind.knowledge:
        if raw:
            return "azure_ai_search" if "azure_ai_search" in raw else "file_search"
        rep.add(Severity.lossy, "tool-dropped", tp,
                f"knowledge base {t.kb_ref or t.id!r}: create an Azure AI Search index (or a "
                "vector store) and add an azure_ai_search / file_search tool (manual)")
        return None
    name = t.name or t.id
    if name in c.TOOL_TYPES and name not in c.GENERIC:
        return name
    if name in c.BUILTIN_MAP:
        rep.add(Severity.info, "tool-mapped", tp, f"{name} -> {c.BUILTIN_MAP[name]}")
        return c.BUILTIN_MAP[name]
    if name == "AMAZON.UserInput":
        rep.add(Severity.info, "tool-implicit", tp, "agents can ask the user without a tool")
        return None
    rep.add(Severity.lossy, "tool-dropped", tp, f"built-in tool {name!r} has no Foundry equivalent")
    return None


def _remote(r: RemoteAgent, path: str, rep: Report) -> dict | None:
    if r.ref and r.ref.startswith(("http://", "https://")):
        rx = c.az(r).get(V, {})
        e = {"type": rx.get("type", "a2a"), "base_url": r.ref}
        e.update({k: v for k, v in rx.get("tool", {}).items() if k != "base_url"})
        if e["type"] == "a2a":
            e.setdefault("a2a_version", "1.0")
        return e
    rep.add(Severity.lossy, "remote-agent-dropped", path,
            f"remote agent {r.name!r} ({r.ref or 'no locator'}) is not an A2A URL; not translated")
    return None
