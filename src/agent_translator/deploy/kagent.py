"""Kagent: create the emitted resources with ``kubectl create`` (never apply/replace/delete).

Input: the YAML written by ``translate --to kagent`` (a file, or a directory with one YAML file).
Options: ``context``, ``namespace`` (passed to kubectl; objects that name their own namespace keep
it), ``kubectl`` (path to the binary), ``check=remote`` (server-side dry run: asks the API server
to validate each object without persisting it).

Only ``kagent.dev`` kinds are accepted; ``Secret`` and anything else is refused, so credentials
never travel through this tool (create the key Secret yourself, as the report says).
"""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from ..report import Report, Severity
from . import Plan, Step, reject_placeholders, single_file

GROUP = re.compile(r"(^|\.)kagent\.dev/")
Runner = Callable[[list[str], str | None], tuple[int, str, str]]


def _run(cmd: list[str], stdin: str | None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=120, check=False)
    except FileNotFoundError:
        return 127, "", f"{cmd[0]!r} not found"
    return p.returncode, p.stdout, p.stderr


def _kubectl(options: dict[str, str], *args: str) -> list[str]:
    cmd = [options.get("kubectl", "kubectl")]
    if options.get("context"):
        cmd += ["--context", options["context"]]
    if options.get("namespace"):
        cmd += ["-n", options["namespace"]]
    return [*cmd, *args]


def _ref(d: dict) -> str:
    meta = d.get("metadata") or {}
    ns = f" -n {meta['namespace']}" if meta.get("namespace") else ""
    return f"{d.get('kind')}/{meta.get('name')}{ns}"


def _undo(d: dict, options: dict[str, str]) -> str:
    """`kubectl delete` for one object, carrying the same context and namespace as the deploy."""
    meta = d.get("metadata") or {}
    cmd = f"{options.get('kubectl', 'kubectl')}"
    if options.get("context"):
        cmd += f" --context {options['context']}"
    ns = meta.get("namespace") or options.get("namespace")
    if ns:
        cmd += f" -n {ns}"
    return f"{cmd} delete {str(d.get('kind')).lower()} {meta.get('name')}"


def plan(path: Path, options: dict[str, str], client: Runner | None = None) -> tuple[Plan | None, Report]:
    rep = Report(target="kagent")
    f = single_file(path, ("kagent.yaml",), (".yaml", ".yml"), rep)
    if f is None:
        return None, rep
    text = f.read_text()
    if reject_placeholders(text, str(f), rep):
        return None, rep
    docs: list[dict[str, Any]] = [d for d in YAML(typ="safe").load_all(text) if d]
    if not docs:
        rep.add(Severity.error, "no-objects", str(f), "the file has no Kubernetes objects")
        return None, rep
    ok = True
    for d in docs:
        if not isinstance(d, dict) or not (d.get("metadata") or {}).get("name"):
            rep.add(Severity.error, "bad-object", str(f), "every document needs kind and metadata.name")
            ok = False
        elif d.get("kind") == "Secret":
            rep.add(Severity.error, "secret-refused", _ref(d),
                    "this tool never creates Secrets; create it yourself with kubectl")
            ok = False
        elif not GROUP.search(str(d.get("apiVersion", ""))):
            rep.add(Severity.error, "foreign-kind", _ref(d),
                    f"apiVersion {d.get('apiVersion')!r} is not a kagent.dev resource")
            ok = False
    if not ok:
        return None, rep
    ctx = options.get("context") or "(current kubectl context)"
    target = f"{ctx}, namespace {options.get('namespace') or '(the context default)'}"
    pl = Plan("kagent", target, [Step("create", _ref(d), d["apiVersion"]) for d in docs],
              undo="; ".join(_undo(d, options) for d in reversed(docs)), data=docs)
    if options.get("check") == "remote":
        run = client or _run
        for d in docs:
            rc, _, err = run(_kubectl(options, "create", "--dry-run=server", "-f", "-"), json.dumps(d))
            if rc:
                rep.add(Severity.error, "server-rejected", _ref(d), err.strip() or f"exit {rc}")
        if rep.worst >= Severity.error:
            return None, rep
        rep.add(Severity.info, "server-validated", target, "the API server accepted every object (dry run)")
    return pl, rep


def apply(pl: Plan, options: dict[str, str], client: Runner | None = None) -> Report:
    rep = Report(target="kagent")
    run = client or _run
    for d in pl.data:
        rc, _, err = run(_kubectl(options, "create", "-f", "-"), json.dumps(d))
        if rc:
            rep.add(Severity.error, "create-failed", _ref(d),
                    (err.strip() or f"kubectl exited with {rc}") + "; stopped here, later objects were not created")
            return rep
        rep.add(Severity.info, "created", _ref(d), pl.target)
    return rep
