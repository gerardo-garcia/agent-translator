"""Kagent -> multi-document YAML with the Agent and what it references, via `kubectl get` (read-only).

ref: ``NAME`` or ``NAMESPACE/NAME`` (default namespace: the context's). Follows ModelConfig,
RemoteMCPServer, AgentTemplate and Agent-as-tool references. Secrets are never read.
Options: ``context``, ``namespace``, ``api_version`` (v1alpha2 | v1alpha3: the version
kubectl asks the API server for), ``kubectl`` (path to the binary).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from ..report import Report, Severity
from . import safe_filename

GROUP = "kagent.dev"
NOISE_META = ("managedFields", "uid", "resourceVersion", "creationTimestamp", "generation",
              "selfLink", "ownerReferences", "finalizers")


class KubectlError(RuntimeError):
    pass


def _kubectl(options: dict[str, str], *args: str) -> dict:
    cmd = [options.get("kubectl", "kubectl")]
    if options.get("context"):
        cmd += ["--context", options["context"]]
    cmd += [*args, "-o", "json"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except FileNotFoundError as e:
        raise KubectlError(f"{cmd[0]!r} not found") from e
    if p.returncode:
        raise KubectlError(p.stderr.strip() or f"kubectl exited with {p.returncode}")
    return json.loads(p.stdout)


def refs_of(doc: dict, ns: str | None) -> list[tuple[str, str, str | None]]:
    """(resource, name, namespace) for everything the document points to."""
    found: list[tuple[str, str, str | None]] = []

    def walk(v: Any):
        if isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for k, x in v.items():
                name = x if isinstance(x, str) else (x.get("name") if isinstance(x, dict) else None)
                sub_ns = x.get("namespace") if isinstance(x, dict) else None
                res = {"modelConfig": "modelconfigs", "templateRef": "agenttemplates",
                       "agent": "agents"}.get(k)
                if k == "mcpServer" and isinstance(x, dict) and x.get("kind", "RemoteMCPServer") == "RemoteMCPServer":
                    res = "remotemcpservers"
                if res and name:
                    found.append((res, name, sub_ns or ns))
                walk(x)
    walk(doc.get("spec", {}))
    return found


def _clean(d: dict) -> dict:
    d.pop("status", None)
    meta = d.get("metadata", {})
    for k in NOISE_META:
        meta.pop(k, None)
    ann = meta.get("annotations") or {}
    ann.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    if not ann:
        meta.pop("annotations", None)
    return d


def fetch(ref: str, out: Path, options: dict[str, str], client: Any = None) -> tuple[list[Path], Report]:
    rep = Report(source="kagent")
    ns, _, name = ref.rpartition("/")
    ns = ns or options.get("namespace") or None
    ver = options.get("api_version")
    kind = lambda r: f"{r}.{ver}.{GROUP}" if ver else f"{r}.{GROUP}"
    docs: list[dict] = []
    seen: set[tuple[str, str, str | None]] = set()
    queue = [("agents", name, ns)]
    while queue:
        res, n, nspace = queue.pop(0)
        if (res, n, nspace) in seen:
            continue
        seen.add((res, n, nspace))
        try:
            d = _kubectl(options, "get", kind(res), n, *(["-n", nspace] if nspace else []))
        except KubectlError as e:
            sev = Severity.error if not docs else Severity.warning
            rep.add(sev, "fetch-failed" if not docs else "reference-missing",
                    f"{res}/{n}", str(e))
            if not docs:
                return [], rep
            continue
        docs.append(_clean(d))
        queue += refs_of(d, d.get("metadata", {}).get("namespace") or nspace)
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"{safe_filename(name)}.yaml"
    y = YAML()
    y.default_flow_style = False
    with f.open("w") as fh:
        y.dump_all(docs, fh)
    rep.add(Severity.info, "fetched", str(f), "; ".join(
        f"{d['kind']}/{d['metadata']['name']}" for d in docs))
    return [f], rep
