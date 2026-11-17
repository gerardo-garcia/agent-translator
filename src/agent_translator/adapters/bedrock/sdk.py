"""IR -> Bedrock Agents API request files (`aws bedrock-agent <op> --cli-input-json file://...`).

Built from the CloudFormation template the regular emitter produces, so both outputs always agree:
every `AWS::Bedrock::Agent` becomes a CreateAgent request plus one request per action group,
knowledge base and collaborator, then PrepareAgent and CreateAgentAlias. Files are numbered in the
order to run them (collaborators before the supervisors that call them). Nothing is sent anywhere.
Values only known after earlier calls (agent ids, alias ARNs) are `REPLACE-WITH-...` placeholders.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...report import Report, Severity
from . import cfn

# CloudFormation-only properties and the maps whose keys are user data
CFN_ONLY = {"AutoPrepare", "SkipResourceInUseCheckOnDelete", "TestAliasTags"}
CHILDREN = {"ActionGroups", "KnowledgeBases", "AgentCollaborators"}
MAPS = {"Parameters", "ParentActionGroupSignatureParams", "AdditionalModelRequestFields", "Tags"}


class _Resolver:
    def __init__(self, tpl: dict, rep: Report):
        self.params = tpl.get("Parameters") or {}
        self.rep = rep
        self.todo: dict[str, str] = {}  # placeholder -> what to put there

    def placeholder(self, ph: str, what: str, path: str) -> str:
        if ph not in self.todo:
            self.todo[ph] = what
            self.rep.add(Severity.manual, "sdk-placeholder", path, f"{ph}: {what}")
        return ph

    def value(self, v: Any, path: str, key: str = "") -> Any:
        if isinstance(v, list):
            return [self.value(x, f"{path}[{i}]", key) for i, x in enumerate(v)]
        if not isinstance(v, dict):
            return v
        if "Ref" in v and len(v) == 1:
            p = self.params.get(v["Ref"])
            if p is None:
                return self.placeholder(f"REPLACE-WITH-{v['Ref']}", "value of a CloudFormation "
                                        "resource reference", path)
            if "Default" in p:
                return p["Default"]
            return self.placeholder(f"REPLACE-WITH-{v['Ref']}", p.get("Description", v["Ref"]), path)
        if "Fn::GetAtt" in v:
            res, attr = v["Fn::GetAtt"] if isinstance(v["Fn::GetAtt"], list) else \
                str(v["Fn::GetAtt"]).split(".", 1)
            what = ("ARN of the alias returned by CreateAgentAlias for that agent"
                    if attr == "AgentAliasArn" else f"{res}.{attr} of the earlier call")
            return self.placeholder(f"REPLACE-WITH-{res}-{attr}", what, path)
        if any(k.startswith("Fn::") for k in v):
            return self.placeholder("REPLACE-WITH-" + "-".join(k[4:] for k in v if k.startswith("Fn::")),
                                    f"CloudFormation function {sorted(v)} cannot be evaluated offline", path)
        out = {}
        for k, x in v.items():
            if key in MAPS:
                out[k] = self.value(x, f"{path}.{k}", "")
            elif k in CFN_ONLY:      # lifecycle switches of the stack, not API fields
                continue
            else:
                out[k[:1].lower() + k[1:]] = self.value(x, f"{path}.{k}", k)
        return out


def _order(agents: dict[str, dict]) -> list[str]:
    """Collaborators before the agents that reference their alias."""
    deps = {}
    for lid, r in agents.items():
        found = set()
        for c in r["Properties"].get("AgentCollaborators") or []:
            for ref in cfn.refs_in(c):
                if ref.endswith("Alias") and ref[:-5] in agents:
                    found.add(ref[:-5])
        deps[lid] = found
    done: list[str] = []

    def visit(n: str, stack=()):
        if n in done or n in stack:
            return
        for d in sorted(deps[n]):
            visit(d, (*stack, n))
        done.append(n)
    for n in agents:
        visit(n)
    return done


def emit(tpl: dict, out: Path, rep: Report) -> list[Path]:
    rv = _Resolver(tpl, rep)
    agents = {k: r for k, r in tpl["Resources"].items() if r["Type"] == cfn.AGENT}
    aliases = {k[:-5]: r for k, r in tpl["Resources"].items() if r["Type"] == cfn.ALIAS}
    steps: list[tuple[str, str, dict]] = []  # (agent, operation, request)
    for lid in _order(agents):
        props = agents[lid]["Properties"]
        name = props.get("AgentName", lid)
        path = f"Resources.{lid}"
        idph = f"REPLACE-WITH-{lid}-AgentId"
        main = {k: v for k, v in props.items() if k not in CFN_ONLY | CHILDREN}
        steps.append((lid, "create-agent", rv.value(main, path)))
        agent_ref = {"agentId": rv.placeholder(idph, f"agentId returned by create-agent for {name!r}",
                                               path), "agentVersion": "DRAFT"}
        for i, ag in enumerate(props.get("ActionGroups") or []):
            steps.append((lid, "create-agent-action-group",
                          {**agent_ref, **rv.value(ag, f"{path}.ActionGroups[{i}]")}))
        for i, kb in enumerate(props.get("KnowledgeBases") or []):
            steps.append((lid, "associate-agent-knowledge-base",
                          {**agent_ref, **rv.value(kb, f"{path}.KnowledgeBases[{i}]")}))
        for i, c in enumerate(props.get("AgentCollaborators") or []):
            steps.append((lid, "associate-agent-collaborator",
                          {**agent_ref, **rv.value(c, f"{path}.AgentCollaborators[{i}]")}))
        steps.append((lid, "prepare-agent", {"agentId": agent_ref["agentId"]}))
        al = (aliases.get(lid) or {}).get("Properties", {})
        steps.append((lid, "create-agent-alias",
                      {"agentId": agent_ref["agentId"],
                       "agentAliasName": al.get("AgentAliasName", "live")}))
    out.mkdir(parents=True, exist_ok=True)
    files, lines = [], []
    for n, (lid, op, req) in enumerate(steps, 1):
        f = out / f"{n:02d}-{cfn.logical_id(lid)}-{op}.json"
        f.write_text(json.dumps(req, indent=2, ensure_ascii=False) + "\n")
        files.append(f)
        lines.append(f"aws bedrock-agent {op} --cli-input-json file://{f.name}")
    readme = out / "README.md"
    todo = "\n".join(f"- `{ph}`: {what}" for ph, what in rv.todo.items()) or "- none"
    readme.write_text(
        "# Bedrock Agents API requests\n\nGenerated by agent-translator. Run in order; after each "
        "call copy the values it returns into the placeholders of the next files "
        "(`jq -r .agent.agentId`, `.agentAlias.agentAliasArn`...).\n\n```bash\n"
        + "\n".join(lines) + "\n```\n\nPlaceholders to fill:\n\n" + todo + "\n")
    files.append(readme)
    return files
