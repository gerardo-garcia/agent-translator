"""ADK agent on Vertex AI Agent Engine -> local ADK project, via the REST API (read-only).

ref: ``ENGINE_ID`` or a full ``projects/<p>/locations/<l>/reasoningEngines/<id>`` name.
Options: ``project`` (or env GOOGLE_CLOUD_PROJECT), ``region`` (or env GOOGLE_CLOUD_LOCATION),
``api_version`` (default ``v1``). Authentication: Application Default Credentials.

What it writes: ``<id>.json`` (the resource, with environment-variable values redacted) and, when
the engine was deployed from source (``spec.sourceCodeSpec.inlineSource``), the extracted project.
The returned file is that project's ``root_agent.yaml``, which the ADK adapter translates. Engines
deployed as a pickled Python object (``packageSpec``) or from Python-only source hold code, not an
agent definition: the resource JSON is still written, with a ``manual`` report item.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import tarfile
from pathlib import Path
from typing import Any

from ..report import Report, Severity
from . import safe_filename

NAME = re.compile(r"^projects/[^/]+/locations/[^/]+/reasoningEngines/[^/]+$")


class RestClient:
    """Minimal authorized GET client; tests inject any object with ``get(url, **params) -> dict``."""

    def __init__(self) -> None:
        try:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
        except ImportError as e:
            raise RuntimeError(
                "google-auth is needed for Google Cloud: pip install 'agent-translator[fetch-gcp]'") from e
        creds, self.default_project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        self._session = AuthorizedSession(creds)

    def get(self, url: str, **params: Any) -> dict:
        r = self._session.get(url, params=params or None, timeout=60)
        r.raise_for_status()
        return r.json()


def make_client(options: dict[str, str]) -> RestClient:
    return RestClient()


def endpoint(region: str, api_version: str) -> str:
    return f"https://{region}-aiplatform.googleapis.com/{api_version}"


def settings(options: dict[str, str]) -> tuple[str | None, str | None]:
    return (options.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT"),
            options.get("region") or os.environ.get("GOOGLE_CLOUD_LOCATION"))


def redact_env(resource: dict) -> dict:
    """Environment-variable values may be credentials: keep the names, drop the values."""
    out = json.loads(json.dumps(resource))
    dep = (out.get("spec") or {}).get("deploymentSpec") or {}
    for e in dep.get("env") or []:
        if isinstance(e, dict) and "value" in e:
            e["value"] = "<redacted>"
    return out


def extract_archive(data: bytes, dest: Path) -> list[Path]:
    """Unpack a .tar.gz, refusing absolute paths, parent traversal and links."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        members = []
        for m in tf.getmembers():
            target = (dest / m.name).resolve()
            if not (target == root or root in target.parents) or m.issym() or m.islnk():
                raise ValueError(f"unsafe path in archive: {m.name}")
            if m.isfile() or m.isdir():
                members.append(m)
        tf.extractall(dest, members=members)
    return sorted(p for p in dest.rglob("root_agent.yaml") if p.is_file())


def fetch(ref: str, out: Path, options: dict[str, str], client: Any = None) -> tuple[list[Path], Report]:
    rep = Report(source="adk")
    project, region = settings(options)
    version = options.get("api_version", "v1")
    if NAME.match(ref):
        name = ref
        region = region or ref.split("/")[3]
    else:
        if not (project and region):
            rep.add(Severity.error, "missing-option", "project/region",
                    "pass -O project=… -O region=… (or GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION), "
                    "or a full projects/…/reasoningEngines/… name")
            return [], rep
        name = f"projects/{project}/locations/{region}/reasoningEngines/{ref}"
    try:
        client = client or make_client(options)
        resource = client.get(f"{endpoint(region, version)}/{name}")
    except Exception as e:
        rep.add(Severity.error, "fetch-failed", name, f"{type(e).__name__}: {e}")
        return [], rep

    out.mkdir(parents=True, exist_ok=True)
    engine_id = name.rsplit("/", 1)[-1]
    meta = out / f"{safe_filename(engine_id)}.json"
    meta.write_text(json.dumps(redact_env(resource), indent=2) + "\n")
    spec = resource.get("spec") or {}
    rep.add(Severity.info, "fetched", name,
            f"{resource.get('displayName', engine_id)} (framework: {spec.get('agentFramework') or 'unknown'})")

    inline = ((spec.get("sourceCodeSpec") or {}).get("inlineSource") or {}).get("sourceArchive")
    if inline:
        try:
            roots = extract_archive(base64.b64decode(inline), out / safe_filename(engine_id))
        except Exception as e:
            rep.add(Severity.error, "archive-failed", name, f"{type(e).__name__}: {e}")
            return [meta], rep
        if len(roots) == 1:
            rep.add(Severity.info, "source-extracted", str(roots[0].parent), "ADK agent config found")
            return [roots[0]], rep
        rep.add(Severity.manual, "no-agent-config" if not roots else "several-agent-configs", name,
                "the deployed source has no root_agent.yaml (it is Python code, which this tool does not "
                "translate)" if not roots else
                f"found {len(roots)} root_agent.yaml files; translate the one you want from "
                f"{out / safe_filename(engine_id)}")
        return [meta], rep
    if spec.get("packageSpec"):
        rep.add(Severity.manual, "pickled-agent", name,
                "deployed as a pickled Python object (packageSpec): it holds code, not an agent definition")
    else:
        rep.add(Severity.manual, "no-source", name, "the engine response carries no deployable source")
    return [meta], rep
