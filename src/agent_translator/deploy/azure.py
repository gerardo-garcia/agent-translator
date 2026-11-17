"""Azure AI Foundry: create prompt agents with azure-ai-projects ``agents.create_version``.

Input: the ``azure.yaml`` written by ``translate --to azure`` (or a fetched ``{name, definition}``
JSON), as a file or a directory with one such file. Only ``prompt`` agents; the model deployment
named in ``model`` must already exist in the project (this tool does not create deployments, which
is where Azure's cost is). Options: ``endpoint`` (or env AZURE_AI_PROJECT_ENDPOINT),
``new_version=true`` (allow adding a version to an agent that already exists; without it an
existing agent is an error), ``check=remote`` (read-only existence check in the dry run).
Authentication: DefaultAzureCredential.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..adapters.azure import foundry
from ..adapters.azure.adapter import _load
from ..report import Report, Severity
from . import Plan, Step, reject_placeholders, single_file

SERVICE_ONLY = {"host", "project", "uses", "name", "description", "$ref"}


def make_client(options: dict[str, str]):
    endpoint = options.get("endpoint") or os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
    if not endpoint:
        raise RuntimeError("project endpoint needed: -O endpoint=https://<account>.services.ai.azure.com"
                           "/api/projects/<project> or set AZURE_AI_PROJECT_ENDPOINT")
    try:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential
    except ImportError as e:
        raise RuntimeError("pip install 'agent-translator[fetch-azure]' to deploy to Azure") from e
    return AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())


def _agents(d: dict, rep: Report, where: str) -> list[tuple[str, dict, str | None]]:
    """(name, definition, description) for every agent in the file."""
    out: list[tuple[str, dict, str | None]] = []
    if foundry.is_service(d):
        for key, s in d["services"].items():
            if not isinstance(s, dict) or s.get("host") != "azure.ai.agent":
                continue
            if "$ref" in s:
                rep.add(Severity.error, "bad-input", f"services.{key}", "$ref services are not supported")
                continue
            out.append((s.get("name") or key,
                        {k: v for k, v in s.items() if k not in SERVICE_ONLY}, s.get("description")))
    else:
        body = d["definition"] if isinstance(d.get("definition"), dict) else d
        out.append((d.get("name") or body.get("name") or "agent",
                    {k: v for k, v in body.items() if k not in ("name", "description")},
                    d.get("description") or body.get("description")))
    for name, definition, _ in out:
        if definition.get("kind", "prompt") != "prompt":
            rep.add(Severity.error, "unsupported-kind", name,
                    f"only prompt agents can be deployed (found {definition.get('kind')!r})")
        definition.setdefault("kind", "prompt")
        if not definition.get("model"):
            rep.add(Severity.error, "model-required", name, "the agent has no model/deployment name")
    if not out:
        rep.add(Severity.error, "no-agents", where, "no azure.ai.agent service found")
    return out


def _exists(client: Any, name: str) -> bool:
    try:
        client.agents.get(name)
    except Exception as e:
        if type(e).__name__ == "ResourceNotFoundError" or getattr(e, "status_code", None) == 404:
            return False
        raise
    return True


def plan(path: Path, options: dict[str, str], client: Any = None) -> tuple[Plan | None, Report]:
    rep = Report(target="azure")
    f = single_file(path, ("azure.yaml",), (".yaml", ".yml", ".json"), rep)
    if f is None:
        return None, rep
    if reject_placeholders(f.read_text(), str(f), rep):
        return None, rep
    d = _load(f)
    agents = _agents(d, rep, str(f)) if isinstance(d, dict) else []
    if not isinstance(d, dict):
        rep.add(Severity.error, "bad-input", str(f), "not an agent definition")
    endpoint = options.get("endpoint") or os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
    if not endpoint:
        rep.add(Severity.error, "endpoint-required", "azure",
                "pass -O endpoint=https://<account>.services.ai.azure.com/api/projects/<project>")
    if rep.worst >= Severity.error:
        return None, rep
    new_version = options.get("new_version") == "true"
    if options.get("check") == "remote":
        try:
            client = client or make_client(options)
            for name, _, _ in agents:
                if _exists(client, name) and not new_version:
                    rep.add(Severity.error, "agent-exists", name,
                            "already exists in the project; this tool does not overwrite "
                            "(-O new_version=true adds a version)")
        except Exception as e:
            rep.add(Severity.error, "check-failed", "azure", f"{type(e).__name__}: {e}")
        if rep.worst >= Severity.error:
            return None, rep
    steps = [Step("create-agent-version" if new_version else "create-agent", n,
                  f"model/deployment {df['model']!r}") for n, df, _ in agents]
    rep.add(Severity.info, "deployment-not-created", endpoint,
            "model deployments are not created: each model above must already exist in the project")
    return Plan("azure", endpoint, steps,
                undo="; ".join(f"delete agent {n!r} (portal, or agents.delete)" for n, _, _ in agents),
                data=agents), rep


def apply(pl: Plan, options: dict[str, str], client: Any = None) -> Report:
    rep = Report(target="azure")
    new_version = options.get("new_version") == "true"
    try:
        client = client or make_client(options)
        for name, definition, description in pl.data:
            if _exists(client, name) and not new_version:  # re-check: the plan may be stale
                rep.add(Severity.error, "agent-exists", name, "appeared since the plan; not overwritten")
                return rep
            body: dict[str, Any] = {"definition": definition}
            if description:
                body["description"] = description
            v = client.agents.create_version(agent_name=name, body=body)
            rep.add(Severity.info, "created", name,
                    f"version {getattr(v, 'version', None) or (v.get('version') if isinstance(v, dict) else '?')}"
                    f" at {pl.target}")
    except Exception as e:
        rep.add(Severity.error, "create-failed", pl.target, f"{type(e).__name__}: {e}")
    return rep
