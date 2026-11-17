from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from ruamel.yaml import YAML

FIX = Path(__file__).parent.parent / "fixtures" / "kagent"


def _load_all(text: str) -> list:
    return list(YAML(typ="safe").load_all(text))   # fresh instance per read


API_TO_VERSION = {"api.kagent.dev/v1alpha3": "v1alpha3", "kagent.dev/v1alpha2": "v1alpha2"}


def _validators(version: str) -> dict[str, Draft202012Validator]:
    out = {}
    for f in (FIX / "crds" / version).glob("*.yaml"):
        crd = _load_all(f.read_text())[0]
        v = next(v for v in crd["spec"]["versions"] if v["name"] == version)
        out[crd["spec"]["names"]["kind"]] = Draft202012Validator(v["schema"]["openAPIV3Schema"])
    return out


_CACHE: dict[str, dict] = {}


def validate_against_crds(path: Path) -> None:
    """Every kagent resource in `path` must satisfy the CRD schema of its own version."""
    for d in _load_all(path.read_text()):
        v = API_TO_VERSION.get((d or {}).get("apiVersion"))
        if not v:
            continue
        if v not in _CACHE:
            _CACHE[v] = _validators(v)
        vals = _CACHE[v]
        if d["kind"] in vals:
            errs = list(vals[d["kind"]].iter_errors(d))
            assert not errs, f"{v} {d['kind']}/{d['metadata']['name']}: {errs[0].message}"


@pytest.fixture
def crd_validate():
    return validate_against_crds
