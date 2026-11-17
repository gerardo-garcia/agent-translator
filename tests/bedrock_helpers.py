import json
from pathlib import Path

from jsonschema import Draft202012Validator, validators

from agent_translator.adapters.bedrock import BedrockAdapter, cfn

FIX = Path(__file__).parent.parent / "fixtures" / "bedrock"
ad = BedrockAdapter()


def fixtures() -> list[Path]:
    return sorted((FIX / "cfn").glob("*.yaml"))


class _Marker(str):
    """Stands for a CloudFormation intrinsic: satisfies any type, enum, pattern or bound."""
    def __eq__(self, other):
        return True
    __hash__ = str.__hash__


def _skip_for_marker(orig):
    def check(validator, value, instance, schema):
        if isinstance(instance, _Marker):
            return
        yield from orig(validator, value, instance, schema)
    return check


def _validator():
    base = Draft202012Validator
    tc = base.TYPE_CHECKER
    for t in ("string", "number", "integer", "boolean", "array", "object"):
        tc = tc.redefine(t, lambda c, i, t=t, orig=base.TYPE_CHECKER.is_type: (
            isinstance(i, _Marker) or orig(i, t)))
    skip = {k: _skip_for_marker(base.VALIDATORS[k])
            for k in ("pattern", "minLength", "maxLength", "minimum", "maximum", "enum")}
    cls = validators.extend(base, validators=skip, type_checker=tc)
    schema = json.loads((FIX / "schemas" / "aws-bedrock-agent.json").read_text())
    return cls({"definitions": schema["definitions"], "type": "object",
                "properties": schema["properties"], "required": schema["required"],
                "additionalProperties": False})


_V = None


def _mark(v):
    if cfn.is_intrinsic(v):
        return _Marker("x")
    if isinstance(v, dict):
        return {k: _mark(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_mark(x) for x in v]
    return v


def validate_template(path: Path) -> None:
    """Every AWS::Bedrock::Agent in `path` must satisfy the official CloudFormation schema."""
    global _V
    _V = _V or _validator()
    tpl = cfn.loader().load(path.read_text())
    for lid, r in tpl["Resources"].items():
        if r.get("Type") == cfn.AGENT:
            props = _mark(r["Properties"])
            read_only = {"AgentArn", "AgentId", "AgentStatus", "AgentVersion", "CreatedAt"}
            assert not (read_only & set(props)), f"{lid}: read-only properties set"
            errs = list(_V.iter_errors(props))
            assert not errs, f"{lid}: {errs[0].message} at {list(errs[0].path)}"


def codes(rep) -> set[str]:
    return {i.code for i in rep.items}


def core(ir):
    """IR without extensions and tool ids (these may legitimately change on a round trip)."""
    out = []
    for a in sorted(ir.agents, key=lambda a: a.id):
        d = a.model_dump(mode="json", exclude={"extensions"})
        d["tools"] = [{k: v for k, v in t.items() if k not in ("id", "extensions")}
                      for t in d["tools"]]
        out.append(d)
    return out


def validate_sdk_dir(out: Path) -> list[str]:
    """Every request file must satisfy botocore's input shape of its operation. Returns the
    operations in file order (skipped without botocore)."""
    import pytest
    pytest.importorskip("botocore")
    from botocore.session import get_session
    from botocore.validate import ParamValidator
    model = get_session().get_service_model("bedrock-agent")
    ops = []
    for f in sorted(out.glob("[0-9][0-9]-*.json")):
        op = "".join(p.title() for p in f.stem.split("-", 2)[2].split("-"))
        shape = model.operation_model(op).input_shape
        report = ParamValidator().validate(json.loads(f.read_text()), shape)
        assert not report.has_errors(), f"{f.name}: {report.generate_report()}"
        ops.append(op)
    return ops
