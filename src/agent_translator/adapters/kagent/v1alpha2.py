"""Codec for `kagent.dev/v1alpha2` (Kagent 0.x, `type: Declarative` agents).

BYO agents (a user-supplied container) cannot be translated and are reported as `manual`.
Version-specific data with no IR equivalent is kept in `extensions.kagent["v1alpha2"]`.
"""
from __future__ import annotations

from typing import Any

from ruamel.yaml.scalarstring import LiteralScalarString

from ...ir import AgentIR, AgentSpec, Collaboration, Memory, RemoteAgent, Tool, ToolKind
from ...report import Report, Severity
from . import common as c

V = "v1alpha2"
# spec-level fields (besides description/type/declarative) with no IR equivalent
SPEC_EXTRAS = ("documentationUrl", "iconUrl", "provider", "sandbox", "skills", "version",
               "allowedNamespaces")
# declarative fields handled explicitly; everything else is preserved verbatim
DECL_MAPPED = ("modelConfig", "systemMessage", "tools", "memory")
# preserved fields that change agent behaviour (reported as lossy); the rest is runtime metadata
BEHAVIOURAL = ("promptTemplate", "context", "executeCodeBlocks", "shareTools", "stream",
               "systemMessageFrom")
TOOL_V2_ONLY = ("allowedHeaders", "headersFrom", "isolateSessions")


class Parser:
    def __init__(self, by_kind: dict, rep: Report) -> None:
        self.k, self.rep = by_kind, rep

    def parse(self) -> list[AgentSpec]:
        out = []
        for (ns, name), doc in self.k.get("Agent", {}).items():
            a = self.agent(ns, name, doc)
            if a:
                out.append(a)
        return out

    def agent(self, ns, name, doc) -> AgentSpec | None:
        spec = doc.get("spec", {})
        path = f"Agent/{name}"
        if spec.get("type") != "Declarative" or "declarative" not in spec:
            self.rep.add(Severity.manual, "byo-agent", path,
                         "only `type: Declarative` agents can be translated; BYO agents are a "
                         "user container and are skipped")
            return None
        d = spec["declarative"]
        v2: dict[str, Any] = {}
        decl_extra = {k: v for k, v in d.items() if k not in DECL_MAPPED}
        if decl_extra:
            v2["declarative"] = decl_extra
        spec_extra = {k: spec[k] for k in SPEC_EXTRAS if k in spec}
        if spec_extra:
            v2["spec"] = spec_extra
        beh = sorted(k for k in list(decl_extra) + list(spec_extra) if k in BEHAVIOURAL
                     or k in ("skills", "sandbox"))
        if beh:
            self.rep.add(Severity.lossy, "fields-preserved", path,
                         f"{beh} have no IR equivalent; kept in extensions.kagent")
        rt = sorted(k for k in list(decl_extra) + list(spec_extra) if k not in beh)
        if rt:
            self.rep.add(Severity.info, "runtime-fields-preserved", path,
                         f"{rt} kept in extensions.kagent")
        if "systemMessageFrom" in d:
            self.rep.add(Severity.manual, "prompt-external", path,
                         "systemMessage comes from a ConfigMap/Secret; instructions unknown")

        # v1alpha2 modelConfig is a plain name in the Agent's namespace (default applies if unset)
        mref = {"name": d.get("modelConfig") or c.DEFAULT_MODEL_CONFIG}
        model, mc_ext = c.model_from_ref(self.k, V, ns, mref, self.rep, path)

        tools: list[Tool] = []
        sub: list[str] = []
        unresolved: list[dict] = []
        for i, t in enumerate(d.get("tools", []) or []):
            if t.get("type") == "McpServer" and "mcpServer" in t:
                m = t["mcpServer"]
                tk: dict[str, Any] = {k: m[k] for k in ("kind", "apiGroup", "namespace") if k in m}
                if "requireApproval" in m:
                    tk["requireApproval"] = m["requireApproval"]
                only = {k: m[k] for k in ("allowedHeaders",) if k in m}
                only.update({k: t[k] for k in ("headersFrom", "isolateSessions") if k in t})
                if only:
                    tk[V] = only
                tools.append(Tool(id=f"{m['name']}-{i}", kind=ToolKind.mcp, server_ref=m["name"],
                                  tool_names=list(m.get("toolNames") or []),
                                  requires_confirmation=c.approval_in(m.get("requireApproval")),
                                  extensions={"kagent": tk} if tk else {}))
            elif t.get("type") == "Agent" and "agent" in t:
                ag = t["agent"]
                target = (ag.get("namespace", ns), ag["name"])
                if target in self.k.get("Agent", {}):
                    sub.append(ag["name"])
                else:
                    self.rep.add(Severity.manual, "subagent-unresolved", f"{path}.tools[{i}]",
                                 f"agent tool {ag['name']!r} not found in file")
                    unresolved.append(RemoteAgent(
                        id=ag["name"], name=ag["name"],
                        extensions={"kagent": {V: {"agentTool": ag}}}))
            else:
                self.rep.add(Severity.warning, "tool-unknown", f"{path}.tools[{i}]",
                             f"unrecognised tool entry: type={t.get('type')!r}")

        memory = Memory()
        if d.get("memory"):
            ttl = d["memory"].get("ttlDays")
            memory = Memory(enabled=True, ttl_seconds=ttl * 86400 if ttl else None)
            v2["memoryModelConfig"] = d["memory"].get("modelConfig")

        kx: dict[str, Any] = {"namespace": ns} if ns else {}
        if v2:
            kx[V] = v2
        if mc_ext:
            kx["modelConfig"] = mc_ext
        return AgentSpec(
            id=name, name=name, description=spec.get("description"),
            instructions=d.get("systemMessage"), model=model, tools=tools, sub_agents=sub,
            remote_agents=unresolved,
            collaboration=Collaboration(mode="supervisor" if sub or unresolved else "none"),
            memory=memory, extensions={"kagent": kx} if kx else {},
        )


def emit(ir: AgentIR, rep: Report) -> list[dict]:
    docs: list[dict] = []
    model_docs: dict[str, dict] = {}
    mcp_used: set[str] = set()
    for a in ir.agents:
        c.report_foreign_ext(a, V, rep)
        if a.collaboration.mode in ("sequential", "parallel", "loop", "handoff"):
            rep.add(Severity.lossy, "collaboration-mode", a.id,
                    f"{a.collaboration.mode} collaboration does not exist in Kagent; "
                    "sub-agents become tools the parent agent calls")
        name = c.dns(a.id)
        if name != a.id:
            rep.add(Severity.info, "name-sanitized", a.id, f"renamed to {name!r}")
        kx = c.kext(a)
        v2 = kx.get(V, {})
        ns = kx.get("namespace")
        mref = c.model_config_ref(a, name, V, rep, model_docs)
        if mref.get("namespace") and mref["namespace"] != ns:
            rep.add(Severity.manual, "cross-namespace-modelconfig", a.id,
                    "v1alpha2 requires the ModelConfig in the Agent's namespace; "
                    f"{mref['namespace']}/{mref['name']} must be copied there")
        decl: dict[str, Any] = {}
        decl.update(v2.get("declarative", {}))          # runtime, a2aConfig, deployment, ...
        decl["modelConfig"] = mref["name"]
        if a.instructions:
            decl["systemMessage"] = (LiteralScalarString(a.instructions)
                                     if "\n" in a.instructions else a.instructions)
        if a.memory.enabled:
            mem: dict[str, Any] = {"modelConfig": v2.get("memoryModelConfig")
                                   or c.DEFAULT_MODEL_CONFIG}
            if a.memory.ttl_seconds:
                mem["ttlDays"] = max(1, a.memory.ttl_seconds // 86400)
            decl["memory"] = mem
            if not v2.get("memoryModelConfig"):
                rep.add(Severity.manual, "memory-model-config", a.id,
                        "memory needs an embedding-capable ModelConfig; "
                        f"{c.DEFAULT_MODEL_CONFIG!r} used as placeholder")
        tools = _tools(a, name, rep, mcp_used)
        if tools:
            decl["tools"] = tools
        for _ in a.guardrails:
            rep.add(Severity.lossy, "guardrail-dropped", a.id, "Kagent has no guardrails")
        spec: dict[str, Any] = {}
        if a.description:
            spec["description"] = a.description
        spec["type"] = "Declarative"
        spec.update(v2.get("spec", {}))
        spec["declarative"] = decl
        docs.append({"apiVersion": c.API[V], "kind": "Agent",
                     "metadata": {"name": name, **({"namespace": ns} if ns else {})}, "spec": spec})
    return list(model_docs.values()) + c.mcp_docs(ir, mcp_used, V, rep) + docs


def _tools(a: AgentSpec, name: str, rep: Report, mcp_used: set[str]) -> list[dict]:
    tools: list[dict] = []
    for tool in a.tools:
        if tool.kind is not ToolKind.mcp:
            rep.add(Severity.lossy, "tool-unsupported", f"agents[{a.id}].tools[{tool.id}]",
                    f"tool kind {tool.kind.value!r} has no Kagent equivalent; "
                    "expose it as an MCP server and reference it")
            continue
        server = tool.server_ref or tool.id
        mcp_used.add(server)
        tk = tool.extensions.get("kagent", {})
        only = tk.get(V, {})
        m: dict[str, Any] = {"name": server, "kind": tk.get("kind", "RemoteMCPServer")}
        if "apiGroup" in tk:
            m["apiGroup"] = c.map_group(tk["apiGroup"], V)
        if "namespace" in tk:
            m["namespace"] = tk["namespace"]
        if tool.tool_names:
            m["toolNames"] = tool.tool_names
        ra = c.approval_for(V, c.approval_raw(tool), tool.tool_names,
                            f"agents[{a.id}].tools[{tool.id}]", rep)
        if ra is not None:
            m["requireApproval"] = ra
        if "allowedHeaders" in only:
            m["allowedHeaders"] = only["allowedHeaders"]
        entry: dict[str, Any] = {"type": "McpServer", "mcpServer": m}
        entry.update({k: only[k] for k in ("headersFrom", "isolateSessions") if k in only})
        tools.append(entry)
    for sid in a.sub_agents:
        rep.add(Severity.info, "subagent-standalone", f"agents[{a.id}]",
                f"sub-agent {sid!r} is called as an independent Agent (A2A) in v1alpha2")
        tools.append({"type": "Agent", "agent": {"name": c.dns(sid)}})
    for r in a.remote_agents:
        ag = r.extensions.get("kagent", {}).get(V, {}).get("agentTool")
        if ag:
            tools.append({"type": "Agent", "agent": ag})
        else:
            rep.add(Severity.lossy, "remote-agent-dropped", f"agents[{a.id}].remote_agents[{r.id}]",
                    f"remote agent {r.name!r} is not a Kagent Agent; "
                    "deploy it as one (or as BYO) and reference it by name")
    return tools
