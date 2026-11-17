"""Bedrock Agents API/CLI JSON (GetAgent and friends) -> a CloudFormation-shaped template.

`aws bedrock-agent get-agent --agent-id X` returns `{"agent": {...}}` with the agent itself only:
action groups, knowledge bases and collaborators come from separate calls. A *bundle* is one JSON
(or YAML) document that adds them, each as the list its call returns (or the objects inside it):

    {"agent": <GetAgent.agent>,
     "agentActionGroups": [<GetAgentActionGroup.agentActionGroup>, ...],
     "agentKnowledgeBases": [<GetAgentKnowledgeBase.agentKnowledgeBase>, ...],
     "agentCollaborators": [<GetAgentCollaborator.agentCollaborator>, ...]}

(`ListAgent*` summaries are accepted too, but carry no schemas or executors.) The API objects have
the same fields as the CloudFormation properties in camelCase, so they are converted into an
`AWS::Bedrock::Agent` resource and handed to the regular parser.
"""
from __future__ import annotations

from typing import Any

from ...report import Report, Severity
from . import cfn

AGENT_KEYS = ("agent", "agentVersion")  # GetAgent / GetAgentVersion responses
LISTS = {  # bundle key -> (CloudFormation property, wrapper key of the single-object response)
    "agentActionGroups": ("ActionGroups", "agentActionGroup"),
    "agentKnowledgeBases": ("KnowledgeBases", "agentKnowledgeBase"),
    "agentCollaborators": ("AgentCollaborators", "agentCollaborator"),
}
SUMMARIES = {"actionGroupSummaries": "agentActionGroups",
             "agentKnowledgeBaseSummaries": "agentKnowledgeBases",
             "agentCollaboratorSummaries": "agentCollaborators"}
# service-side bookkeeping with no CloudFormation counterpart
RUNTIME = {"agentId", "agentArn", "agentVersion", "version", "agentStatus", "clientToken",
           "createdAt", "updatedAt", "lastUpdatedAt", "preparedAt", "failureReasons",
           "recommendedActions", "actionGroupId", "collaboratorId"}
# the one field the API and CloudFormation name differently
RENAMED = {"parentActionSignature": "ParentActionGroupSignature"}
# API-only fields CloudFormation has no property for
NO_CFN = {"parentActionGroupSignatureParams"}
# maps whose keys are user data, not field names
MAPS = {"parameters", "parentActionGroupSignatureParams", "additionalModelRequestFields"}


def agent_obj(d: Any) -> dict | None:
    if not isinstance(d, dict):
        return None
    for k in AGENT_KEYS:
        v = d.get(k)
        if isinstance(v, dict) and isinstance(v.get("agentName"), str):
            return v
    return None


def is_getagent(d: Any) -> bool:
    return agent_obj(d) is not None and "Resources" not in d


def _pascal(v: Any, key: str = "") -> Any:
    if isinstance(v, list):
        return [_pascal(x, key) for x in v]
    if not isinstance(v, dict):
        return v
    out = {}
    for k, x in v.items():
        if key in MAPS:  # keys are data; the values are field structures
            out[k] = _pascal(x, "")
        elif k not in RUNTIME and k not in NO_CFN:
            out[RENAMED.get(k, k[:1].upper() + k[1:])] = _pascal(x, k)
    return out


def _items(bundle: dict, key: str, wrapper: str, rep: Report) -> list[dict]:
    raw = bundle.get(key)
    if raw is None:
        for sk, bk in SUMMARIES.items():
            if bk == key and bundle.get(sk) is not None:
                raw = bundle[sk]
                rep.add(Severity.warning, "getagent-summaries", sk,
                        "List* summaries carry no schemas or executors; use the Get* call of "
                        "each item for a faithful translation")
    items = []
    for x in raw or []:
        x = x.get(wrapper, x) if isinstance(x, dict) else x
        if isinstance(x, dict):
            items.append(x)
    return items


def to_template(d: dict, rep: Report) -> dict:
    agent = agent_obj(d)
    assert agent is not None
    props = _pascal(agent)
    ignored = sorted(k for k in agent if k in RUNTIME)
    rep.add(Severity.info, "getagent-runtime-fields", "agent",
            f"service bookkeeping not translated: {', '.join(ignored) or 'none'}")
    if agent.get("failureReasons"):
        rep.add(Severity.warning, "getagent-failure-reasons", "agent.failureReasons",
                f"the agent was not healthy when exported: {agent['failureReasons']}")
    for key, (prop, wrapper) in LISTS.items():
        items = _items(d, key, wrapper, rep)
        if items:
            props[prop] = [_pascal(x) for x in items]
    for i, raw in enumerate(_items(d, "agentActionGroups", "agentActionGroup", Report())):
        if raw.get("parentActionGroupSignatureParams"):
            rep.add(Severity.lossy, "action-group-signature-params",
                    f"agentActionGroups[{i}].parentActionGroupSignatureParams",
                    "CloudFormation has no property for built-in action group parameters; dropped")
    for i, ag in enumerate(props.get("ActionGroups") or []):
        if not (ag.get("ActionGroupExecutor") or ag.get("ApiSchema") or ag.get("FunctionSchema")
                or ag.get("ParentActionGroupSignature")):
            rep.add(Severity.manual, "action-group-incomplete", f"ActionGroups[{i}]",
                    f"action group {ag.get('ActionGroupName')!r} has neither schema nor executor "
                    "(a List summary?): describe its tools manually")
    for k in ("AgentName", "AgentResourceRoleArn"):
        props.setdefault(k, None)
    props = {k: v for k, v in props.items() if v is not None}
    return {"Resources": {cfn.logical_id(agent["agentName"]):
                          {"Type": cfn.AGENT, "Properties": props}}}
