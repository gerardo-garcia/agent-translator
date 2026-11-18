"""ADK -> Vertex AI Agent Engine, by running Google's own ``adk deploy agent_engine``.

Why the official CLI rather than raw API calls: it owns the packaging (wrapper app, requirements,
Cloud Build / source upload) and changes with ADK; this tool only decides *whether* to run it and
keeps the same safety rules as the other deployers.

Input: the project written by ``translate --to adk`` (the agent folder with ``root_agent.yaml``, or
the output directory that holds exactly one such folder). Options: ``project`` (or env
GOOGLE_CLOUD_PROJECT) and ``region`` (or GOOGLE_CLOUD_LOCATION) are required; ``display_name``
(default: the agent name), ``description``, ``service_account``, ``adk`` (path to the binary,
default ``adk``), ``api_version`` (default ``v1``), ``check=remote`` (read-only: lists the engines
of the region and refuses a display name that already exists). Authentication: Application
Default Credentials. ``google-adk[gcp]`` is not a dependency of this package (it cannot share a
resolution with the Azure SDK): install it in the environment that runs the deploy. The ``gcp`` extra
is required: without it ``adk deploy agent_engine`` stops with "No module named 'agentplatform'".

Create-only: ``adk deploy agent_engine`` is never given ``--agent_engine_id``, so an existing engine
is never updated. An Agent Engine bills while it exists; the undo command is printed with its id.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from ..fetch.adk import RestClient, endpoint
from ..report import Report, Severity
from . import Plan, Step, reject_placeholders

ENGINE = re.compile(r"projects/[^\s/]+/locations/[^\s/]+/reasoningEngines/[0-9A-Za-z_-]+")
STUBS = re.compile(r"raise NotImplementedError\(|build the toolset here")


class Backend:
    """What the deployer needs from the outside: authorized GETs and running a command."""

    def __init__(self) -> None:
        self._rest: RestClient | None = None

    def get(self, url: str, **params: Any) -> dict:
        self._rest = self._rest or RestClient()
        return self._rest.get(url, **params)

    def run(self, cmd: list[str]) -> tuple[int, str, str]:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, check=False, env=os.environ.copy())
        except FileNotFoundError:
            return 127, "", (f"{cmd[0]!r} not found: install google-adk in this environment "
                             "(pip install 'google-adk[gcp]') or pass -O adk=/path/to/adk")
        return p.returncode, p.stdout, p.stderr


def _agent_dir(path: Path, rep: Report) -> Path | None:
    if path.is_file():
        path = path.parent if path.name == "root_agent.yaml" else path
    if (path / "root_agent.yaml").is_file():
        return path
    found = sorted(p.parent for p in path.glob("*/root_agent.yaml")) if path.is_dir() else []
    if len(found) == 1:
        return found[0]
    rep.add(Severity.error, "bad-input", str(path),
            "expected an ADK agent folder with root_agent.yaml, or a directory holding exactly one "
            f"(found {len(found)})")
    return None


def _settings(options: dict[str, str], rep: Report) -> tuple[str, str] | None:
    project = options.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    region = options.get("region") or os.environ.get("GOOGLE_CLOUD_LOCATION")
    if not (project and region):
        rep.add(Severity.error, "missing-option", "project/region",
                "pass -O project=… -O region=… (or set GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION)")
        return None
    return project, region


def _agent_name(folder: Path) -> str:
    m = re.search(r"^name:\s*['\"]?([^'\"\n]+)", (folder / "root_agent.yaml").read_text(), re.MULTILINE)
    return m.group(1).strip() if m else folder.name


def _delete_hint(name: str, region: str, api_version: str) -> str:
    return ("curl -X DELETE -H \"Authorization: Bearer $(gcloud auth print-access-token)\" "
            f"\"{endpoint(region, api_version)}/{name}?force=true\"")


def _existing(client: Any, project: str, region: str, version: str) -> list[dict]:
    """Every reasoning engine of the region (read-only, paginated; filtered by the caller)."""
    out: list[dict] = []
    token = None
    while True:
        params = {"pageSize": 100, **({"pageToken": token} if token else {})}
        page = client.get(f"{endpoint(region, version)}/projects/{project}/locations/{region}"
                          "/reasoningEngines", **params)
        out += page.get("reasoningEngines") or []
        token = page.get("nextPageToken")
        if not token:
            return out


def plan(path: Path, options: dict[str, str], client: Any = None) -> tuple[Plan | None, Report]:
    rep = Report(target="adk")
    folder = _agent_dir(path, rep)
    st = _settings(options, rep)
    if folder is None or st is None:
        return None, rep
    project, region = st
    for f in sorted(folder.rglob("*")):
        if f.is_file() and f.suffix in (".yaml", ".yml", ".py"):
            text = f.read_text()
            reject_placeholders(text, str(f), rep)
            if f.suffix == ".py" and STUBS.search(text):
                rep.add(Severity.error, "stubs-unimplemented", str(f),
                        "this file still holds generated stubs: implement them before deploying "
                        "(nothing was changed)")
    display = options.get("display_name") or _agent_name(folder)
    version = options.get("api_version", "v1")
    if rep.worst >= Severity.error:
        return None, rep
    if options.get("check") == "remote":
        try:
            same = [e for e in _existing(client or Backend(), project, region, version)
                    if e.get("displayName") == display]
        except Exception as e:
            rep.add(Severity.error, "check-failed", "reasoningEngines", f"{type(e).__name__}: {e}")
            return None, rep
        if same:
            rep.add(Severity.error, "engine-exists", same[0].get("name", display),
                    f"an Agent Engine named {display!r} already exists; this tool never updates one "
                    "(choose -O display_name=…)")
            return None, rep
        rep.add(Severity.info, "no-name-clash", display, "no Agent Engine with this display name")
    rep.add(Severity.info, "billing", display,
            "an Agent Engine is billed while it exists; delete it when you no longer need it")
    cmd = [options.get("adk", "adk"), "deploy", "agent_engine", "--project", project,
           "--region", region, "--display_name", display]
    if options.get("description"):
        cmd += ["--description", options["description"]]
    if options.get("service_account"):
        cmd += ["--service_account", options["service_account"]]
    cmd.append(str(folder))
    pl = Plan("adk", f"{project}, {region}",
              [Step("create-agent-engine", display, str(folder))],
              undo=f"delete the Agent Engine (its id is printed after creation; "
                   f"{_delete_hint('<NAME>', region, version)})",
              data={"cmd": cmd, "region": region, "version": version})
    return pl, rep


def apply(pl: Plan, options: dict[str, str], client: Any = None) -> Report:
    rep = Report(target="adk")
    backend = client or Backend()
    try:
        rc, out, err = backend.run(pl.data["cmd"])
    except Exception as e:
        rep.add(Severity.error, "create-failed", pl.target, f"{type(e).__name__}: {e}")
        return rep
    found = ENGINE.findall(out + "\n" + err)
    name = found[-1] if found else None
    if name:
        pl.undo = _delete_hint(name, pl.data["region"], pl.data["version"])
    if rc:
        tail = (err.strip() or out.strip())[-600:]
        if "agentplatform" in tail:
            tail += " (install the ADK Google Cloud extra: pip install 'google-adk[gcp]')"
        rep.add(Severity.error, "create-failed", pl.target,
                f"adk exited with {rc}: {tail}" + (f" (an engine may have been created: {name})" if name else ""))
        return rep
    if name:
        rep.add(Severity.info, "created", name, f"Agent Engine created at {pl.target}")
    else:
        rep.add(Severity.warning, "created-id-unknown", pl.target,
                "adk finished but no engine name was found in its output; list the engines to find it")
    return rep
