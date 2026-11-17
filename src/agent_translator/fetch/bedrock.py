"""Bedrock Agents -> one bundle JSON (see adapters/bedrock/getagent.py), via boto3 (read-only).

Calls only GetAgent / GetAgentVersion and the List*/Get* calls of action groups, knowledge bases
and collaborators. ref: ``AGENT_ID`` or ``AGENT_ID:VERSION`` (default: the working draft).
Options: ``region``, ``profile``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..report import Report, Severity
from . import safe_filename

PARTS = (  # bundle key, list op, list result key, get op, id field, get result key
    ("agentActionGroups", "list_agent_action_groups", "actionGroupSummaries",
     "get_agent_action_group", "actionGroupId", "agentActionGroup"),
    ("agentKnowledgeBases", "list_agent_knowledge_bases", "agentKnowledgeBaseSummaries",
     "get_agent_knowledge_base", "knowledgeBaseId", "agentKnowledgeBase"),
    ("agentCollaborators", "list_agent_collaborators", "agentCollaboratorSummaries",
     "get_agent_collaborator", "collaboratorId", "agentCollaborator"),
)


def make_client(options: dict[str, str]):
    try:
        import boto3
    except ImportError as e:
        raise RuntimeError("boto3 is needed to fetch from AWS: pip install 'agent-translator[fetch-aws]'") from e
    session = boto3.Session(profile_name=options.get("profile"), region_name=options.get("region"))
    return session.client("bedrock-agent")


def _pages(call, key: str, **kw) -> list[dict]:
    items: list[dict] = []
    token = None
    while True:
        r = call(**kw, **({"nextToken": token} if token else {}))
        items += r.get(key, [])
        token = r.get("nextToken")
        if not token:
            return items


def _plain(v: Any) -> Any:
    return json.loads(json.dumps(v, default=lambda o: o.isoformat() if hasattr(o, "isoformat") else str(o)))


def fetch(ref: str, out: Path, options: dict[str, str], client: Any = None) -> tuple[list[Path], Report]:
    rep = Report(source="bedrock")
    agent_id, _, version = ref.partition(":")
    version = version or "DRAFT"
    try:
        client = client or make_client(options)
        if version == "DRAFT":
            agent = client.get_agent(agentId=agent_id)["agent"]
            bundle: dict[str, Any] = {"agent": agent}
        else:
            agent = client.get_agent_version(agentId=agent_id, agentVersion=version)["agentVersion"]
            bundle = {"agentVersion": agent}
        for key, lister, rkey, getter, idf, gkey in PARTS:
            got = []
            for s in _pages(getattr(client, lister), rkey, agentId=agent_id, agentVersion=version):
                r = getattr(client, getter)(agentId=agent_id, agentVersion=version, **{idf: s[idf]})
                got.append(r[gkey])
            if got:
                bundle[key] = got
    except Exception as e:  # botocore ClientError, missing credentials, no boto3...
        rep.add(Severity.error, "fetch-failed", f"bedrock:{ref}", f"{type(e).__name__}: {e}")
        return [], rep
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"{safe_filename(agent['agentName'])}.json"
    f.write_text(json.dumps(_plain(bundle), indent=2, ensure_ascii=False) + "\n")
    rep.add(Severity.info, "fetched", str(f),
            f"agent {agent['agentName']!r} ({version}) with "
            + ", ".join(f"{len(bundle.get(k, []))} {k[5:].lower()}" for k, *_ in PARTS))
    return [f], rep
