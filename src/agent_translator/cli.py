from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from ruamel.yaml import YAML

from .deploy import load_deployers
from .fetch import load_fetchers
from .ir import AgentIR
from .registry import detect_adapter, load_adapters
from .report import Report, Severity

app = typer.Typer(no_args_is_help=True, help="Translate agent definitions between platforms.")


def _print_report(rep: Report) -> None:
    for i in rep.items:
        typer.echo(f"[{i.severity.name}] {i.code} {i.path} {i.message}".rstrip(), err=True)
    typer.echo(f"status: {rep.status}", err=True)


def _resolve(adapters, name: str | None, path: Path):
    if name:
        if name not in adapters:
            raise typer.BadParameter(f"unknown platform {name!r}; available: {sorted(adapters)}")
        return adapters[name]
    a = detect_adapter(path, adapters)
    if a is None:
        raise typer.BadParameter("could not autodetect format; use --from")
    return a


@app.command("list-platforms")
def list_platforms() -> None:
    for n in sorted(load_adapters()):
        typer.echo(n)


@app.command()
def inspect(path: Path, source: str | None = typer.Option(None, "--from")) -> None:
    """Parse a platform file and print the IR as JSON."""
    ir, rep = _resolve(load_adapters(), source, path).parse(path)
    typer.echo(ir.model_dump_json(indent=2, exclude_defaults=True))
    _print_report(rep)


def _options(option: list[str]) -> dict[str, str]:
    opts = dict(o.split("=", 1) for o in option if "=" in o)
    if len(opts) != len(option):
        raise typer.BadParameter("options must be KEY=VALUE")
    return opts


def _fetch(platform: str, ref: str, out: Path, opts: dict[str, str]) -> tuple[list[Path], Report]:
    fetchers = load_fetchers()
    if platform not in fetchers:
        raise typer.BadParameter(f"cannot fetch from {platform!r}; available: {sorted(fetchers)}")
    return fetchers[platform](ref, out, opts)


@app.command()
def fetch(
    platform: str,
    ref: str,
    out: Path = typer.Option(Path("fetched"), "-o", "--out"),
    option: list[str] = typer.Option(
        [], "-O", "--option", help="Fetch option KEY=VALUE (repeatable), e.g. -O region=eu-west-1"),
) -> None:
    """Read an agent from a cloud/cluster into local files (read-only; never writes remotely).

    Examples: fetch bedrock AGENTID -O region=us-east-1 | fetch kagent kagent/my-agent |
    fetch azure my-agent -O endpoint=https://...
    """
    files, rep = _fetch(platform, ref, out, _options(option))
    _print_report(rep)
    for f in files:
        typer.echo(str(f))
    if rep.worst >= Severity.error:
        raise typer.Exit(1)


@app.command()
def deploy(
    platform: str,
    path: Path,
    option: list[str] = typer.Option(
        [], "-O", "--option", help="Deploy option KEY=VALUE (repeatable), e.g. -O endpoint=https://…"),
    apply: bool = typer.Option(
        False, "--apply", help="Really create the resources. Without it nothing is changed"),
    yes: bool = typer.Option(False, "--yes", help="With --apply: skip the interactive confirmation"),
) -> None:
    """Create an agent on a platform from files written by `translate`. DRY RUN unless --apply.

    Create-only: existing objects/agents/stacks are never overwritten or deleted, and Secrets are
    never created. Examples: deploy kagent out/kagent.yaml -O context=lab |
    deploy bedrock out/ -O region=eu-west-1 -O model=… -O role_arn=… |
    deploy azure out/azure.yaml -O endpoint=https://…  (add -O check=remote for read-only checks)
    """
    deployers = load_deployers()
    if platform not in deployers:
        raise typer.BadParameter(f"cannot deploy to {platform!r}; available: {sorted(deployers)}")
    opts = _options(option)
    pl, rep = deployers[platform].plan(path, opts)
    _print_report(rep)
    if pl is None or rep.worst >= Severity.error:
        typer.echo("nothing was changed", err=True)
        raise typer.Exit(1)
    typer.echo(f"target: {pl.target}")
    for st in pl.steps:
        typer.echo(f"  {st.action:<22}{st.resource}  {st.detail}".rstrip())
    if not apply:
        typer.echo("dry run: nothing was changed. Add --apply to create it"
                   + ("" if opts.get("check") == "remote" else
                      " (and -O check=remote to also run read-only checks against the target first)"))
        return
    if not yes:
        if not sys.stdin.isatty():
            typer.echo("refusing to create resources without confirmation: run it interactively "
                       "or pass --yes", err=True)
            raise typer.Exit(2)
        if not typer.confirm(f"Create these resources at {pl.target}?", default=False):
            typer.echo("aborted; nothing was changed")
            raise typer.Exit(1)
    arep = deployers[platform].apply(pl, opts)
    _print_report(arep)
    if arep.worst >= Severity.error:
        raise typer.Exit(1)
    typer.echo(f"to remove it: {pl.undo}")


@app.command("ir-schema")
def ir_schema() -> None:
    """Print the JSON Schema of the IR."""
    typer.echo(json.dumps(AgentIR.model_json_schema(), indent=2))


@app.command()
def validate(path: Path) -> None:
    """Validate an IR YAML/JSON file."""
    data = YAML(typ="safe").load(path.read_text())
    AgentIR.model_validate(data)
    typer.echo("ok")


@app.command()
def translate(
    path: Path,
    to: str = typer.Option(..., "--to"),
    source: str | None = typer.Option(None, "--from"),
    out: Path = typer.Option(Path("out"), "-o", "--out"),
    fetch_: bool = typer.Option(
        False, "--fetch", help="PATH is a reference to read from the platform given by --from"),
    fetch_option: list[str] = typer.Option(
        [], "--fetch-option", help="Option KEY=VALUE for the fetch (repeatable)"),
    strict: bool = typer.Option(False, "--strict"),
    report: Path | None = typer.Option(None, "--report"),
    option: list[str] = typer.Option(
        [], "-O", "--option", help="Target option KEY=VALUE (repeatable), e.g. -O api_version=v1alpha2"),
) -> None:
    adapters = load_adapters()
    fetched = None
    if fetch_:
        if not source:
            raise typer.BadParameter("--fetch needs --from <platform>")
        files, frep = _fetch(source, str(path), out / "fetched", _options(fetch_option))
        if not files:
            _print_report(frep)
            raise typer.Exit(1)
        path, fetched = files[0], frep
    src = _resolve(adapters, source, path)
    dst = _resolve(adapters, to, path)
    ir, rep = src.parse(path)
    if fetched:
        rep.extend(fetched)
    rep.source, rep.target = src.name, dst.name
    out.mkdir(parents=True, exist_ok=True)
    opts = dict(o.split("=", 1) for o in option if "=" in o)
    if len(opts) != len(option):
        raise typer.BadParameter("options must be KEY=VALUE")
    rep.extend(dst.emit(ir, out, opts))
    _print_report(rep)
    if report:
        report.write_text(json.dumps(rep.model_dump(mode="json"), indent=2))
    if rep.worst >= Severity.error or (strict and rep.worst >= Severity.lossy):
        raise typer.Exit(1)
