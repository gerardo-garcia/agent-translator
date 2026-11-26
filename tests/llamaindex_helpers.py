import ast
import importlib.util
import sys
from pathlib import Path


def validate_project(out: Path) -> None:
    """Every module compiles and exposes build() and run(); requirements name llama-index-core."""
    mods = sorted(out.glob("*.py"))
    assert mods
    for f in mods:
        tree = ast.parse(f.read_text(), filename=str(f))
        names = {n.name for n in tree.body if isinstance(n, ast.AsyncFunctionDef)}
        assert {"build", "run"} <= names, f
    assert "llama-index-core" in (out / "requirements.txt").read_text().split()
    assert (out / ".env.example").exists() and (out / "README.md").exists()


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"_li_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod
