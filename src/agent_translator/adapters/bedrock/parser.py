"""AWS::Bedrock::Agent resources (CloudFormation) -> IR."""
from __future__ import annotations

import re
from typing import Any

from ...ir import AgentSpec, Collaboration, Memory, ModelSpec, RemoteAgent, Tool, ToolKind
from ...report import Report, Severity
from . import cfn

MODE = {"SUPERVISOR": "supervisor", "SUPERVISOR_ROUTER": "router", "DISABLED": "none"}
BUILTIN_SIGNATURES = {"AMAZON.UserInput": "user-input", "AMAZON.CodeInterpreter": "code-interpreter"}
PARAM_TYPES = {"string", "number", "integer", "boolean", "array"}
# properties mapped to IR fields; everything else is kept verbatim in extensions.bedrock.properties
MAPPED = {"AgentName", "Description", "Instruction", "FoundationModel", "ActionGroups",
          "KnowledgeBases", "AgentCollaboration", "AgentCollaborators", "GuardrailConfiguration",
          "MemoryConfiguration"}
_REGION_PREFIX = re.compile(r"^(?:us|eu|apac|global|us-gov|jp|au|ca)\.")


def model_from_id(model_id: str) -> ModelSpec:
    tail = model_id.rsplit("/", 1)[-1]
    vendor = _REGION_PREFIX.sub("", tail).split(".", 1)[0] if "." in tail else None
    return ModelSpec(provider=vendor, id=tail)


class Parser:
    def __init__(self, tpl: dict, rep: Report):
        self.params: dict = tpl.get("Parameters") or {}
        self.res: dict = tpl.get("Resources") or {}
        self.rep = rep
        self.alias_of: dict[str, str] = {}      # alias logical id -> agent logical id
        self.alias_name: dict[str, str] = {}    # agent logical id -> alias name (literal)
        for lid, r in self.res.items():
            if r.get("Type") == cfn.ALIAS:
                p = r.get("Properties") or {}
                target = self._agent_of(p.get("AgentId"))
                if target:
                    self.alias_of[lid] = target
                    if isinstance(p.get("AgentAliasName"), str):
                        self.alias_name.setdefault(target, p["AgentAliasName"])

    def _agent_of(self, v: Any) -> str | None:
        refs = cfn.refs_in(v)
        agents = [x for x in refs if (self.res.get(x) or {}).get("Type") == cfn.AGENT]
        return agents[0] if len(agents) == 1 else None

    def parse(self) -> list[AgentSpec]:
        agents = [self._agent(lid, r) for lid, r in self.res.items()
                  if r.get("Type") == cfn.AGENT]
        kinds = {r.get("Type") for r in self.res.values()} - {cfn.AGENT, cfn.ALIAS}
        bedrock_other = sorted(k for k in kinds if str(k).startswith("AWS::Bedrock::")
                               and k != cfn.GUARDRAIL)
        if bedrock_other:
            self.rep.add(Severity.warning, "resource-ignored", "Resources",
                         f"not translated: {bedrock_other}")
        return agents

    # ------------------------------------------------------------------ agent
    def _text(self, props: dict, key: str, path: str, intr: dict) -> str | None:
        v = props.get(key)
        if v is None:
            return None
        text = cfn.resolve_text(v, self.params, keep_pseudo=key == "FoundationModel")
        if key == "FoundationModel" and text and "${" in text.rsplit("/", 1)[-1]:
            text = None
        if cfn.is_intrinsic(v):
            intr[key] = {"raw": v, "resolved": text}
            if text is None:
                self.rep.add(Severity.warning, "value-unresolved", f"{path}.{key}",
                             f"{key} depends on a value unknown offline; left empty in the IR")
        return text

    def _agent(self, lid: str, res: dict) -> AgentSpec:
        path = f"Resources.{lid}"
        props: dict = res.get("Properties") or {}
        if res.get("Condition"):
            self.rep.add(Severity.info, "condition-ignored", path,
                         f"Condition {res['Condition']!r} ignored: the agent is always included")
        intr: dict[str, Any] = {}
        name = self._text(props, "AgentName", path, intr) or lid
        description = self._text(props, "Description", path, intr)
        instruction = self._text(props, "Instruction", path, intr)
        fm = self._text(props, "FoundationModel", path, intr)
        model = model_from_id(fm) if fm else ModelSpec()
        if fm:
            self.rep.add(Severity.info, "model-unverified", f"{path}.FoundationModel",
                         f"model id {model.id!r} copied as-is")

        tools: list[Tool] = []
        used: set[str] = set()

        def tid(base: str) -> str:
            n, cand = 2, base
            while cand in used:
                cand, n = f"{base}-{n}", n + 1
            used.add(cand)
            return cand

        for i, ag in enumerate(props.get("ActionGroups") or []):
            tools += self._action_group(ag, f"{path}.ActionGroups[{i}]", tid)
        for i, kb in enumerate(props.get("KnowledgeBases") or []):
            kid = kb.get("KnowledgeBaseId")
            literal = kid if isinstance(kid, str) else None
            if literal is None:
                self.rep.add(Severity.warning, "knowledge-base-external",
                             f"{path}.KnowledgeBases[{i}]",
                             "KnowledgeBaseId is a reference outside this file")
            ext = {k: v for k, v in kb.items() if k not in ("Description",)}
            tools.append(Tool(id=tid(literal or f"kb-{i + 1}"), kind=ToolKind.knowledge,
                              kb_ref=literal, description=kb.get("Description"),
                              extensions={"bedrock": ext}))

        sub, remote, collab, instructions = [], [], {}, {}
        for i, c in enumerate(props.get("AgentCollaborators") or []):
            alias = (c.get("AgentDescriptor") or {}).get("AliasArn")
            cname = c.get("CollaboratorName")
            target = None
            if isinstance(alias, dict) and "Fn::GetAtt" in alias:
                target = self.alias_of.get(alias["Fn::GetAtt"][0])
            entry = {"name": cname, "instruction": c.get("CollaborationInstruction")}
            if "RelayConversationHistory" in c:
                entry["relayConversationHistory"] = c["RelayConversationHistory"]
            if target:
                sub.append(target)
                if isinstance(entry["instruction"], str):
                    instructions[target] = entry.pop("instruction")
                entry.pop("instruction", None)
                collab[target] = entry
            else:
                self.rep.add(Severity.warning, "collaborator-remote",
                             f"{path}.AgentCollaborators[{i}]",
                             f"collaborator {cname!r} points to an alias outside this file; "
                             "kept as a remote agent")
                rx: dict[str, Any] = {"aliasArn": alias}
                if "relayConversationHistory" in entry:
                    rx["relayConversationHistory"] = entry["relayConversationHistory"]
                remote.append(RemoteAgent(
                    id=cfn.safe_name(str(cname or f"collaborator-{i + 1}")),
                    name=str(cname or f"collaborator-{i + 1}"),
                    instruction=entry["instruction"],
                    ref=alias if isinstance(alias, str) else None,
                    extensions={"bedrock": rx}))

        mem_cfg = props.get("MemoryConfiguration")
        memory = Memory()
        if mem_cfg:
            days = mem_cfg.get("StorageDays")
            memory = Memory(enabled="SESSION_SUMMARY" in (mem_cfg.get("EnabledMemoryTypes") or []),
                            ttl_seconds=int(days * 86400) if isinstance(days, int | float) else None)

        guardrails: list[dict] = []
        gcfg = props.get("GuardrailConfiguration")
        bx: dict[str, Any] = {"logicalId": lid}
        if gcfg:
            gid, gver = gcfg.get("GuardrailIdentifier"), gcfg.get("GuardrailVersion")
            g = {"provider": "bedrock",
                 "id": gid if isinstance(gid, str) else None,
                 "version": gver if isinstance(gver, str) else None}
            gres = self._guardrail_resource(gid)
            if gres:
                bx["guardrailResource"] = gres
                g["id"] = gres["logicalId"]
            guardrails.append({k: v for k, v in g.items() if v is not None})
            bx["guardrailConfiguration"] = gcfg
            self.rep.add(Severity.warning, "guardrail-platform-specific", f"{path}.GuardrailConfiguration",
                         "Bedrock guardrails have no portable form; kept in extensions.bedrock")

        mode = MODE.get(props.get("AgentCollaboration", "DISABLED"), "none")
        if (sub or remote) and mode == "none":
            mode = "supervisor"

        leftover = {k: v for k, v in props.items() if k not in MAPPED}
        if leftover:
            bx["properties"] = leftover
        if "PromptOverrideConfiguration" in leftover:
            self.rep.add(Severity.warning, "prompt-override-platform-specific",
                         f"{path}.PromptOverrideConfiguration",
                         "custom prompt templates kept in extensions.bedrock only")
        if mem_cfg:
            bx["memoryConfiguration"] = mem_cfg
        if collab:
            bx["collaborators"] = collab
        if intr:
            bx["intrinsics"] = intr
        if lid in self.alias_name:
            bx["aliasName"] = self.alias_name[lid]
        pnames = cfn.refs_in(props) & set(self.params)
        if pnames:
            bx["parameters"] = {p: self.params[p] for p in sorted(pnames)}
        return AgentSpec(
            id=lid, name=name, description=description, instructions=instruction, model=model,
            tools=tools, sub_agents=sub, remote_agents=remote, collaboration=Collaboration(mode=mode, instructions=instructions), memory=memory,
            guardrails=guardrails, extensions={"bedrock": bx})

    def _guardrail_resource(self, gid: Any) -> dict | None:
        for x in cfn.refs_in(gid):
            r = self.res.get(x)
            if r and r.get("Type") == cfn.GUARDRAIL:
                return {"logicalId": x, "resource": r}
        return None

    # ----------------------------------------------------------- action groups
    def _action_group(self, ag: dict, path: str, tid) -> list[Tool]:
        name = ag.get("ActionGroupName", "action-group")
        ginfo = {k: ag[k] for k in ("ActionGroupState", "SkipResourceInUseCheckOnDelete")
                 if k in ag}
        base = {"actionGroup": name, **({"props": ginfo} if ginfo else {})}
        sig = ag.get("ParentActionGroupSignature")
        if sig:
            kind_id = BUILTIN_SIGNATURES.get(sig, cfn.safe_name(sig.lower()))
            return [Tool(id=tid(kind_id), kind=ToolKind.builtin, name=sig,
                         description=ag.get("Description"), extensions={"bedrock": base})]
        ex = ag.get("ActionGroupExecutor")
        if ex is not None:
            base["executor"] = ex
        lam = ex.get("Lambda") if isinstance(ex, dict) else None
        impl = lam if isinstance(lam, str) else None
        if "ApiSchema" in ag:
            schema = ag["ApiSchema"]
            spec = None
            payload = schema.get("Payload") if isinstance(schema, dict) else None
            if isinstance(payload, str):
                try:
                    spec = cfn.loader().load(payload)
                except Exception:
                    self.rep.add(Severity.warning, "openapi-unparsable", path,
                                 "inline OpenAPI payload is not valid YAML/JSON; kept as text")
                    spec = payload
            else:
                base["apiSchema"] = schema
                self.rep.add(Severity.manual, "schema-external", path,
                             "OpenAPI schema lives in S3 or a reference; contents unknown")
            return [Tool(id=tid(name), kind=ToolKind.openapi, name=name,
                         description=ag.get("Description"), spec=spec, impl_ref=impl,
                         extensions={"bedrock": base})]
        fns = (ag.get("FunctionSchema") or {}).get("Functions") or []
        if not fns:
            self.rep.add(Severity.warning, "action-group-empty", path,
                         f"action group {name!r} defines neither ApiSchema nor FunctionSchema")
        if ag.get("Description"):
            base["groupDescription"] = ag["Description"]
        tools = []
        for f in fns:
            props: dict[str, Any] = {}
            req = []
            for pn, pd in (f.get("Parameters") or {}).items():
                prop = {"type": pd.get("Type")}
                if "Description" in pd:
                    prop["description"] = pd["Description"]
                props[pn] = prop
                if pd.get("Required"):
                    req.append(pn)
            schema = {"type": "object", "properties": props, **({"required": req} if req else {})}
            confirm = {"ENABLED": True, "DISABLED": False}.get(f.get("RequireConfirmation"))
            tools.append(Tool(id=tid(f"{name}.{f['Name']}"), kind=ToolKind.function,
                              name=f["Name"], description=f.get("Description"),
                              parameters=schema, impl_ref=impl, requires_confirmation=confirm,
                              extensions={"bedrock": dict(base)}))
        return tools
