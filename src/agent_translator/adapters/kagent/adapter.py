"""Kagent adapter: dispatches to the codec for each supported API version.

Supported: `api.kagent.dev/v1alpha3` (Kagent >= 1.0, default for emit) and
`kagent.dev/v1alpha2` (Kagent 0.x). Choose the output version with `-O api_version=v1alpha2`.
"""
from __future__ import annotations

from pathlib import Path

from ...ir import AgentIR, Resources
from ...models import adapt_ir
from ...report import Report, Severity
from ...yamlio import new_yaml
from . import common as c
from . import v1alpha2, v1alpha3

_CODECS = {"v1alpha3": v1alpha3, "v1alpha2": v1alpha2}
_HANDLED = {"Agent", "AgentTemplate", "ModelConfig", "RemoteMCPServer", "Harness"}
_SILENT = {"Namespace", "Secret"}


def _is_kagent(d: dict) -> bool:
    return str(d.get("apiVersion", "")).split("/")[0] in c.KAGENT_GROUPS


def _resolve(path: Path) -> Path:
    """A directory (as written by `fetch kagent`) stands for its single YAML file."""
    if not path.is_dir():
        return path
    files = sorted(f for f in path.iterdir() if f.suffix in (".yaml", ".yml"))
    return files[0] if len(files) == 1 else path


class KagentAdapter:
    name = "kagent"
    versions = tuple(_CODECS)

    def detect(self, path: Path) -> bool:
        path = _resolve(path)
        if path.suffix not in (".yaml", ".yml"):
            return False
        try:
            docs = [d for d in c.yaml_safe().load_all(path.read_text()) if isinstance(d, dict)]
        except Exception:
            return False
        return any(_is_kagent(d) and d.get("kind") in ("Agent", "AgentTemplate") for d in docs)

    def parse(self, path: Path) -> tuple[AgentIR, Report]:
        rep = Report(source=self.name)
        path = _resolve(path)
        if path.is_dir():
            rep.add(Severity.error, "bad-input", str(path), "expected one YAML file or a directory with exactly one")
            return AgentIR(agents=[], resources=Resources(mcp_servers=[])), rep
        docs = [d for d in c.yaml_safe().load_all(path.read_text()) if isinstance(d, dict)]
        by_version: dict[str, dict[str, dict[tuple[str, str], dict]]] = {}
        ignored: set[str] = set()
        for i, d in enumerate(docs):
            av, kind = str(d.get("apiVersion", "")), d.get("kind", "")
            if not _is_kagent(d):
                if kind not in _SILENT:
                    ignored.add(kind)
                continue
            v = c.VERSION_OF.get(av)
            if v is None:
                rep.add(Severity.error, "unsupported-api-version", f"docs[{i}]",
                        f"{av} {kind}: supported versions are {sorted(c.API.values())}")
                continue
            if kind not in _HANDLED:
                ignored.add(kind)
                continue
            by_version.setdefault(v, {}).setdefault(kind, {})[c.doc_key(d)] = d
        for kind in sorted(ignored):
            rep.add(Severity.info, "kind-ignored", kind, f"{kind} resources are not translated")

        agents, servers = [], []
        for v, by_kind in by_version.items():
            agents += _CODECS[v].Parser(by_kind, rep).parse()
            servers += c.servers_from(by_kind, rep)
        return AgentIR(agents=c.unique_ids(agents),
                       resources=Resources(mcp_servers=servers)), rep

    def emit(self, ir: AgentIR, out: Path, options: dict[str, str] | None = None) -> Report:
        rep = Report(target=self.name)
        ir = adapt_ir(ir, "kagent", options, rep)
        version = (options or {}).get("api_version", "v1alpha3")
        if version not in _CODECS:
            rep.add(Severity.error, "bad-option", "api_version",
                    f"{version!r}: choose one of {sorted(_CODECS)}")
            return rep
        docs = _CODECS[version].emit(ir, rep)
        out.mkdir(parents=True, exist_ok=True)
        path = out / "kagent.yaml"
        y = new_yaml()
        y.default_flow_style = False
        y.indent(mapping=2, sequence=4, offset=2)
        with path.open("w") as f:
            y.dump_all(docs, f)
        rep.add(Severity.info, "written", str(path), f"Kagent {version}")
        return rep
