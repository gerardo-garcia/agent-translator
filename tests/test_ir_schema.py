import json
from pathlib import Path

from agent_translator.ir import AgentIR


def test_published_schema_is_current():
    published = json.loads((Path(__file__).parent.parent / "schema" / "ir.schema.json").read_text())
    assert published == AgentIR.model_json_schema(), (
        "regenerate with: agent-translator ir-schema > schema/ir.schema.json")
