"""Codec for `api.kagent.dev/v1alpha3` (Kagent >= 1.0).

Agent = template (inline or AgentTemplate ref) + harnessRef. Version-specific data that has
no IR equivalent is kept in `extensions.kagent["v1alpha3"]`.
"""
from __future__ import annotations

from typing import Any

from ruamel.yaml.scalarstring import LiteralScalarString

from ...ir import AgentIR, AgentSpec, Collaboration, Memory, RemoteAgent, Tool, ToolKind
from ...report import Report, Severity
from . import common as c

V = "v1alpha3"
DEFAULT_HARNESS = "kagent-default"
# template fields with no IR equivalent
PASSTHROUGH = ("skills", "plugins", "promptTemplate", "outputSchema", "outputSchemaFrom",
               "outputSchemaStrict", "systemPromptFrom")


# ------------------------------------------------------------------------ parse
class Parser:
    def __init__(self, by_kind: dict, rep: Report) -> None:
        self.k, self.rep = by_kind, rep
        self.extra_agents: list[AgentSpec] = []

    def parse(self) -> list[AgentSpec]:
        agents: list[AgentSpec] = []
        roots = self.k.get("Agent", {})
        if roots:
            for (ns, name), doc in roots.items():
                agents.append(self.agent(ns, name, doc))
        else:
            for (ns, name), doc in self.k.get("AgentTemplate", {}).items():
                self.rep.add(Severity.info, "template-as-agent", f"AgentTemplate/{name}",
                             "no Agent in file; AgentTemplate imported as agent")
                agents.append(self.template(ns, name, doc["spec"], f"AgentTemplate/{name}"))
        return agents + self.extra_agents

    def agent(self, ns, name, doc) -> AgentSpec:
        spec = doc.get("spec", {})
        path = f"Agent/{name}"
        tmpl = spec.get("template", {})
        v3: dict[str, Any] = {}
        if "inline" in tmpl:
            t = tmpl["inline"]
        elif "ref" in tmpl:
            ref = tmpl["ref"]["name"]
            td = self.k.get("AgentTemplate", {}).get((ns, ref))
            if td is None:
                self.rep.add(Severity.manual, "template-unresolved", path,
                             f"AgentTemplate {ref!r} not found in file; instructions/tools unknown")
                t = {}
                v3.update(templateRef=ref, templateUnresolved=True)
            else:
                t = td["spec"]
                self.rep.add(Severity.info, "template-inlined", path,
                             f"AgentTemplate {ref!r} flattened into the agent")
        else:
            t = {}
            self.rep.add(Severity.error, "template-missing", path,
                         "spec.template has no inline/ref")
        if "harnessRef" in spec:
            v3["harnessRef"] = spec["harnessRef"]
        if spec.get("egressPolicyRefs"):
            v3["egressPolicyRefs"] = spec["egressPolicyRefs"]
            self.rep.add(Severity.lossy, "egress-policy", path,
                         "egressPolicyRefs preserved in extensions only (no IR equivalent)")
        a = self.template(ns, name, t, path)
        kx = a.extensions.setdefault("kagent", {})
        kx[V] = {**kx.get(V, {}), **v3}
        h = self.k.get("Harness", {}).get((ns, spec.get("harnessRef", {}).get("name", "")))
        if h:
            mem = h.get("spec", {}).get("kagent", {}).get("memory")
            if mem:
                ttl = mem.get("ttlDays")
                a.memory = Memory(enabled=True, ttl_seconds=ttl * 86400 if ttl else None)
        elif "harnessRef" in spec:
            self.rep.add(Severity.info, "harness-external", path,
                         f"Harness {spec['harnessRef'].get('name')!r} not in file")
        return a

    def template(self, ns, name, t: dict, path: str) -> AgentSpec:
        v3 = {k: t[k] for k in PASSTHROUGH if k in t}
        for k in v3:
            if k == "systemPromptFrom":
                self.rep.add(Severity.manual, "prompt-external", path,
                             "systemPrompt comes from a ConfigMap/Secret; instructions unknown")
            else:
                self.rep.add(Severity.lossy, f"{k}-preserved", path,
                             f"{k} has no IR equivalent; kept in extensions.kagent")
        model, mc_ext = c.model_from_ref(self.k, V, ns, t.get("modelConfig"), self.rep, path)
        kx: dict[str, Any] = {"namespace": ns} if ns else {}
        if v3:
            kx[V] = v3
        if mc_ext:
            kx["modelConfig"] = mc_ext
        tools: list[Tool] = []
        sub: list[str] = []
        remote: list[RemoteAgent] = []
        instructions: dict[str, str] = {}
        for i, tool in enumerate(t.get("tools", []) or []):
            if "mcp" in tool:
                m = tool["mcp"]
                srv = m["server"]
                tk = {k: srv[k] for k in ("kind", "apiGroup", "namespace") if k in srv}
                if "requireApproval" in m:
                    tk["requireApproval"] = m["requireApproval"]
                tools.append(Tool(id=f"{srv['name']}-{i}", kind=ToolKind.mcp,
                                  server_ref=srv["name"], tool_names=list(m.get("tools") or []),
                                  requires_confirmation=c.approval_in(m.get("requireApproval")),
                                  extensions={"kagent": tk} if tk else {}))
            elif "subAgent" in tool:
                sid = self.sub_agent(ns, tool["subAgent"], path)
                if sid:
                    sub.append(sid)
                    if tool["subAgent"].get("description"):
                        instructions[sid] = tool["subAgent"]["description"]
                else:
                    sa = tool["subAgent"]
                    remote.append(RemoteAgent(
                        id=sa["name"], name=sa["name"], description=sa.get("description"),
                        extensions={"kagent": {V: {"subAgent": sa}}}))
            else:
                self.rep.add(Severity.warning, "tool-unknown", f"{path}.tools[{i}]",
                             f"unrecognised tool entry keys: {sorted(tool)}")
        return AgentSpec(
            id=name, name=name, description=t.get("description"),
            instructions=t.get("systemPrompt"), model=model, tools=tools, sub_agents=sub,
            remote_agents=remote,
            collaboration=Collaboration(mode="supervisor" if sub or remote else "none",
                                        instructions=instructions),
            extensions={"kagent": kx} if kx else {},
        )

    def sub_agent(self, ns, s, path) -> str | None:
        ref = s.get("templateRef", {}).get("name")
        td = self.k.get("AgentTemplate", {}).get((ns, ref)) if ref else None
        if td is None:
            self.rep.add(Severity.manual, "subagent-unresolved", path,
                         f"subAgent {s.get('name')!r}: template {ref!r} not found in file")
            return None
        child = self.template(ns, s["name"], td["spec"], f"{path}/subAgent/{s['name']}")
        child.description = child.description or s.get("description")
        if s.get("description"):
            child.extensions.setdefault("kagent", {}).setdefault(V, {})[
                "subAgentDescription"] = s["description"]
        self.extra_agents.append(child)
        return child.id


# ------------------------------------------------------------------------- emit
def emit(ir: AgentIR, rep: Report) -> list[dict]:
    docs: list[dict] = []
    children = {s for a in ir.agents for s in a.sub_agents}
    by_id = {a.id: a for a in ir.agents}
    model_docs: dict[str, dict] = {}
    mcp_used: set[str] = set()

    for a in ir.agents:
        c.report_foreign_ext(a, V, rep)
        if a.collaboration.mode in ("sequential", "parallel", "loop", "handoff"):
            rep.add(Severity.lossy, "collaboration-mode", a.id,
                    f"{a.collaboration.mode} collaboration does not exist in Kagent; "
                    "sub-agents become tools the parent agent calls")
        if a.id in children:
            rep.add(Severity.lossy, "subagent-semantics", a.id,
                    "emitted only as AgentTemplate (shared sub-agent runs inside its parent's "
                    "runtime), not as an independent A2A agent")
            continue
        name = c.dns(a.id)
        if name != a.id:
            rep.add(Severity.info, "name-sanitized", a.id, f"renamed to {name!r}")
        kx = c.kext(a)
        v3 = kx.get(V, {})
        ns = kx.get("namespace")
        if v3.get("templateUnresolved"):
            rep.add(Severity.manual, "template-unresolved", a.id,
                    f"keeping reference to AgentTemplate {v3['templateRef']!r}; it must exist")
            tref: dict[str, Any] = {"ref": {"name": v3["templateRef"]}}
        else:
            tref = {"inline": _template(a, name, by_id, rep, model_docs, mcp_used)}
        if "harnessRef" not in v3:
            rep.add(Severity.info, "harness-default", a.id,
                    f"harnessRef={DEFAULT_HARNESS!r} (created by `kagent install`)")
        spec: dict[str, Any] = {"template": tref,
                                "harnessRef": v3.get("harnessRef") or {"name": DEFAULT_HARNESS}}
        if v3.get("egressPolicyRefs"):
            spec["egressPolicyRefs"] = v3["egressPolicyRefs"]
        docs.append({"apiVersion": c.API[V], "kind": "Agent",
                     "metadata": {"name": name, **({"namespace": ns} if ns else {})}, "spec": spec})
        if a.memory.enabled:
            rep.add(Severity.manual, "memory-harness", a.id,
                    "memory lives in the Harness (spec.kagent.memory); configure it manually")
        for _ in a.guardrails:
            rep.add(Severity.lossy, "guardrail-dropped", a.id, "Kagent has no guardrails")

    for cid in sorted(children):
        ch = by_id[cid]
        cns = c.kext(ch).get("namespace") or next(
            (c.kext(p).get("namespace") for p in ir.agents
             if cid in p.sub_agents and c.kext(p).get("namespace")), None)
        docs.insert(0, {
            "apiVersion": c.API[V], "kind": "AgentTemplate",
            "metadata": {"name": c.dns(ch.id), **({"namespace": cns} if cns else {})},
            "spec": _template(ch, c.dns(ch.id), by_id, rep, model_docs, mcp_used)})
    return list(model_docs.values()) + c.mcp_docs(ir, mcp_used, V, rep) + docs


def _template(a, name, by_id, rep, model_docs, mcp_used) -> dict:
    v3 = c.kext(a).get(V, {})
    t: dict[str, Any] = {}
    if a.description:
        t["description"] = a.description
    t["modelConfig"] = c.model_config_ref(a, name, V, rep, model_docs)
    if a.instructions:
        t["systemPrompt"] = (LiteralScalarString(a.instructions)
                             if "\n" in a.instructions else a.instructions)
    for k in PASSTHROUGH:
        if k in v3:
            t[k] = v3[k]
    tools = []
    for tool in a.tools:
        if tool.kind is ToolKind.mcp:
            server = tool.server_ref or tool.id
            mcp_used.add(server)
            tk = tool.extensions.get("kagent", {})
            m: dict[str, Any] = {"server": {
                "kind": tk.get("kind", "RemoteMCPServer"), "name": server,
                **({"apiGroup": c.map_group(tk["apiGroup"], V)} if "apiGroup" in tk else {}),
                **({"namespace": tk["namespace"]} if "namespace" in tk else {})}}
            if tk.get("v1alpha2"):
                rep.add(Severity.lossy, "kagent-fields-dropped",
                        f"agents[{a.id}].tools[{tool.id}]",
                        f"v1alpha2-only fields {sorted(tk['v1alpha2'])} not representable in v1alpha3")
            if tool.tool_names:
                m["tools"] = tool.tool_names
            ra = c.approval_for(V, c.approval_raw(tool), tool.tool_names,
                                f"agents[{a.id}].tools[{tool.id}]", rep)
            if ra is not None:
                m["requireApproval"] = ra
            tools.append({"mcp": m})
        else:
            rep.add(Severity.lossy, "tool-unsupported", f"agents[{a.id}].tools[{tool.id}]",
                    f"tool kind {tool.kind.value!r} has no Kagent equivalent; "
                    "expose it as an MCP server and reference it")
    for sid in a.sub_agents:
        s = by_id[sid]
        tools.append({"subAgent": {
            "name": c.dns(sid),
            "description": c.kext(s).get(V, {}).get("subAgentDescription")
            or a.collaboration.instructions.get(sid) or s.description or s.name,
            "templateRef": {"name": c.dns(sid)}}})
    for r in a.remote_agents:
        sa = r.extensions.get("kagent", {}).get(V, {}).get("subAgent")
        if sa:
            tools.append({"subAgent": sa})
        else:
            rep.add(Severity.lossy, "remote-agent-dropped", f"agents[{a.id}].remote_agents[{r.id}]",
                    f"remote agent {r.name!r} has no AgentTemplate to reference in Kagent")
    if tools:
        t["tools"] = tools
    return t
