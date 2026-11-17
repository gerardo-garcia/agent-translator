"""Bedrock: create a CloudFormation stack from the emitted ``template.yaml``.

A stack, rather than a chain of ``Create*`` calls, because it is atomic (``OnFailure=ROLLBACK``),
resolves the ids that link agent, action groups and alias by itself, and is removed completely
with ``aws cloudformation delete-stack``. Input: the CloudFormation template written by
``translate --to bedrock`` (a file, or a directory with ``template.yaml``); the SDK-calls output
(``-O format=sdk``) is not deployable here.

Options: ``region``, ``profile``, ``stack_name`` (default ``agtr-<agent name>``),
``param.<Name>=value`` for any template parameter, shortcuts ``model=`` (every ``*FoundationModel``
parameter) and ``role_arn=`` (every ``*AgentResourceRoleArn``), ``capabilities=CAPABILITY_IAM``
(required only if the template creates IAM resources), ``wait=true`` (block until the stack is
created), ``check=remote`` (read-only ``ValidateTemplate`` + existence check in the dry run).
An existing stack is an error; nothing is updated.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from ..report import Report, Severity
from . import Plan, Step, single_file

SHORTCUTS = (("model", "FoundationModel"), ("role_arn", "AgentResourceRoleArn"))


def make_client(options: dict[str, str]):
    try:
        import boto3
    except ImportError as e:
        raise RuntimeError("boto3 is needed to deploy to AWS: pip install 'agent-translator[fetch-aws]'") from e
    session = boto3.Session(profile_name=options.get("profile"), region_name=options.get("region"))
    return session.client("cloudformation")


def _stack_exists(cf: Any, name: str) -> bool:
    try:
        cf.describe_stacks(StackName=name)
    except Exception as e:
        if "does not exist" in str(e):
            return False
        raise
    return True


def _parameters(tpl: dict, options: dict[str, str], rep: Report) -> list[dict[str, str]]:
    out, missing = [], []
    for name, spec in (tpl.get("Parameters") or {}).items():
        v = options.get(f"param.{name}")
        for opt, suffix in SHORTCUTS:
            if v is None and name.endswith(suffix) and opt in options:
                v = options[opt]
        if v is None and "Default" not in (spec or {}):
            missing.append(name)
        elif v is not None:
            out.append({"ParameterKey": name, "ParameterValue": v})
    if missing:
        rep.add(Severity.error, "param-missing", "Parameters",
                f"no value for {', '.join(missing)}: pass -O model=… -O role_arn=… or -O param.<Name>=…")
    return out


def plan(path: Path, options: dict[str, str], client: Any = None) -> tuple[Plan | None, Report]:
    rep = Report(target="bedrock")
    if path.is_dir() and not (path / "template.yaml").is_file() and list(path.glob("*-*-*.json")):
        rep.add(Severity.error, "sdk-output", str(path),
                "this is the SDK-calls output; deploy needs the CloudFormation template "
                "(translate without -O format=sdk)")
        return None, rep
    f = single_file(path, ("template.yaml", "template.yml", "template.json"),
                    (".yaml", ".yml", ".json"), rep)
    if f is None:
        return None, rep
    body = f.read_text()
    tpl = YAML(typ="safe").load(body)
    if not isinstance(tpl, dict) or not isinstance(tpl.get("Resources"), dict):
        rep.add(Severity.error, "bad-input", str(f), "not a CloudFormation template")
        return None, rep
    res = tpl["Resources"]
    iam = sorted(k for k, r in res.items() if str((r or {}).get("Type", "")).startswith("AWS::IAM::"))
    if iam and not options.get("capabilities"):
        rep.add(Severity.error, "capabilities-required", ", ".join(iam),
                "the template creates IAM resources; review them and pass -O capabilities=CAPABILITY_IAM")
    params = _parameters(tpl, options, rep)
    agent = next((r["Properties"].get("AgentName") for r in res.values()
                  if r.get("Type") == "AWS::Bedrock::Agent"), None)
    base = re.sub(r"[^A-Za-z0-9-]+", "-", str(agent or "agent")).strip("-")
    stack = options.get("stack_name") or (base if base.startswith("agtr-") else f"agtr-{base}")
    if rep.worst >= Severity.error:
        return None, rep
    region = options.get("region") or "(default region of the AWS profile)"
    pl = Plan("bedrock", f"{region}, stack {stack}",
              [Step("create-stack", stack, f"{len(res)} resources")]
              + [Step("  resource", k, str(r.get("Type"))) for k, r in res.items()],
              undo=f"aws cloudformation delete-stack --stack-name {stack}"
                   + (f" --region {options['region']}" if options.get("region") else ""),
              data={"stack": stack, "body": body, "params": params,
                    "capabilities": [c for c in options.get("capabilities", "").split(",") if c]})
    if options.get("check") == "remote":
        try:
            cf = client or make_client(options)
            cf.validate_template(TemplateBody=body)
            if _stack_exists(cf, stack):
                rep.add(Severity.error, "stack-exists", stack,
                        "a stack with this name already exists; this tool never updates stacks "
                        "(choose -O stack_name=…)")
            else:
                rep.add(Severity.info, "template-valid", stack, "CloudFormation accepted the template")
        except Exception as e:
            rep.add(Severity.error, "check-failed", "cloudformation", f"{type(e).__name__}: {e}")
        if rep.worst >= Severity.error:
            return None, rep
    return pl, rep


def apply(pl: Plan, options: dict[str, str], client: Any = None) -> Report:
    rep = Report(target="bedrock")
    d = pl.data
    try:
        cf = client or make_client(options)
        if _stack_exists(cf, d["stack"]):  # re-check: the plan may be stale
            rep.add(Severity.error, "stack-exists", d["stack"], "appeared since the plan; not touched")
            return rep
        kw: dict[str, Any] = {"StackName": d["stack"], "TemplateBody": d["body"],
                              "Parameters": d["params"], "OnFailure": "ROLLBACK",
                              "Tags": [{"Key": "managed-by", "Value": "agent-translator"}]}
        if d["capabilities"]:
            kw["Capabilities"] = d["capabilities"]
        r = cf.create_stack(**kw)
        rep.add(Severity.info, "stack-creating", d["stack"], f"{r.get('StackId', '')} at {pl.target}")
        if options.get("wait") == "true":
            cf.get_waiter("stack_create_complete").wait(StackName=d["stack"])
            rep.add(Severity.info, "stack-created", d["stack"], "CREATE_COMPLETE")
        else:
            rep.add(Severity.info, "not-waiting", d["stack"],
                    "creation continues in AWS; follow it with `aws cloudformation describe-stacks`")
    except Exception as e:
        rep.add(Severity.error, "create-failed", pl.target, f"{type(e).__name__}: {e}")
    return rep
