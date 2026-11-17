"""Azure AI Foundry prompt agent -> JSON the Azure adapter parses, via azure-ai-projects (read-only).

ref: ``AGENT_NAME`` or ``AGENT_NAME:VERSION`` (default: the latest version).
Options: ``endpoint`` (or env AZURE_AI_PROJECT_ENDPOINT). Authentication: DefaultAzureCredential.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..report import Report, Severity
from . import safe_filename


def make_client(options: dict[str, str]):
    endpoint = options.get("endpoint") or os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
    if not endpoint:
        raise RuntimeError("project endpoint needed: -O endpoint=https://<account>.services.ai.azure.com"
                           "/api/projects/<project> or set AZURE_AI_PROJECT_ENDPOINT")
    try:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential
    except ImportError as e:
        raise RuntimeError("pip install 'agent-translator[fetch-azure]' to fetch from Azure") from e
    return AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())


def _dict(o: Any) -> dict:
    return o.as_dict() if hasattr(o, "as_dict") else dict(o)


def fetch(ref: str, out: Path, options: dict[str, str], client: Any = None) -> tuple[list[Path], Report]:
    rep = Report(source="azure")
    name, _, version = ref.partition(":")
    try:
        client = client or make_client(options)
        if version:
            details = _dict(client.agents.get_version(agent_name=name, agent_version=version))
        else:
            details = (_dict(client.agents.get(name)).get("versions") or {}).get("latest") or {}
    except Exception as e:
        rep.add(Severity.error, "fetch-failed", f"azure:{ref}", f"{type(e).__name__}: {e}")
        return [], rep
    definition = details.get("definition")
    if not definition:
        rep.add(Severity.error, "fetch-failed", f"azure:{ref}", "the agent has no definition")
        return [], rep
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"{safe_filename(name)}.json"
    f.write_text(json.dumps({"name": name, "definition": definition}, indent=2, ensure_ascii=False) + "\n")
    rep.add(Severity.info, "fetched", str(f), f"agent {name!r} version {details.get('version', '?')}"
            f" (kind {definition.get('kind')})")
    return [f], rep
