import ast
import json
import sys
from pathlib import Path


def validate_project(out: Path) -> None:
    """The emitted project is well formed: modules compile, langgraph.json points at them."""
    cfg = json.loads((out / "langgraph.json").read_text())
    assert cfg["dependencies"] == ["."] and cfg["graphs"]
    for target in cfg["graphs"].values():
        path, _, attr = target.partition(":")
        tree = ast.parse((out / path).read_text(), filename=path)
        names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
        names |= {t.id for n in tree.body if isinstance(n, ast.Assign)
                  for t in n.targets if isinstance(t, ast.Name)}
        assert attr in names, f"{target}: {attr} is not defined"
    assert (out / ".env.example").read_text().startswith("# Copy to .env")
    assert "langgraph dev" in (out / "README.md").read_text()
    reqs = (out / "requirements.txt").read_text().split()
    assert {"langgraph", "langchain"} <= set(reqs)


def load_module(path: Path):
    """Import a generated module by path (the way the LangGraph server does)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"_lg_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod
