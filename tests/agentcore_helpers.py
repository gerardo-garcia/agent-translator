import json
from pathlib import Path

from jsonschema import Draft202012Validator, validators

from agent_translator.adapters.bedrock import cfn

from .bedrock_helpers import _mark, _Marker, _skip_for_marker

FIX = Path(__file__).parent.parent / "fixtures" / "agentcore"
HARNESS = "AWS::BedrockAgentCore::Harness"
READ_ONLY = {"Arn", "HarnessId", "Status", "Version", "CreatedAt", "UpdatedAt"}


def _validator():
    base = Draft202012Validator
    tc = base.TYPE_CHECKER
    for t in ("string", "number", "integer", "boolean", "array", "object"):
        tc = tc.redefine(t, lambda c, i, t=t, orig=base.TYPE_CHECKER.is_type: (
            isinstance(i, _Marker) or orig(i, t)))
    skip = {k: _skip_for_marker(base.VALIDATORS[k])
            for k in ("pattern", "minLength", "maxLength", "minimum", "maximum", "enum")}
    cls = validators.extend(base, validators=skip, type_checker=tc)
    schema = json.loads((FIX / "schemas" / "aws-bedrockagentcore-harness.json").read_text())
    return cls({"definitions": schema["definitions"], "type": "object",
                "properties": schema["properties"], "required": schema["required"],
                "additionalProperties": False})


_V = None


def validate_template(path: Path) -> None:
    """Every AWS::BedrockAgentCore::Harness in `path` must satisfy the official CloudFormation schema."""
    global _V
    _V = _V or _validator()
    tpl = cfn.loader().load(path.read_text())
    found = 0
    for lid, r in tpl["Resources"].items():
        assert r.get("Type") == HARNESS, lid
        found += 1
        props = _mark(r["Properties"])
        assert not (READ_ONLY & set(props)), f"{lid}: read-only properties set"
        errs = list(_V.iter_errors(props))
        assert not errs, f"{lid}: {errs[0].message} at {list(errs[0].path)}"
    assert found
