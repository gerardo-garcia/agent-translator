"""Load every emitted ADK project with the real ``google-adk`` loader (``from_config``).

Complements the JSON-schema check in test_matrix: this is what ``adk run`` / ``adk web`` do first.
No network and no credentials are involved; the model is never called.
"""
import sys

import pytest

pytest.importorskip("google.adk")
from google.adk.agents.config_agent_utils import from_config

from agent_translator.report import Severity

from .test_matrix import ADAPTERS, SOURCES, sid


@pytest.mark.parametrize("src", SOURCES, ids=sid)
def test_emitted_adk_loads_with_real_adk(src, tmp_path, monkeypatch):
    name, f = src
    ir, prep = ADAPTERS[name].parse(f)
    rep = ADAPTERS["adk"].emit(ir, tmp_path, {})
    items = [*prep.items, *rep.items]
    if any(i.severity >= Severity.manual or i.code == "function-signature-unknown" for i in items):
        pytest.skip("the reports say the project needs the user's own code or manual work")
    root = next(tmp_path.rglob("root_agent.yaml"))
    monkeypatch.syspath_prepend(str(tmp_path))    # emitted projects import their own modules
    before = set(sys.modules)
    try:
        agent = from_config(str(root))
        assert agent.name
    finally:
        for m in set(sys.modules) - before:       # emitted package names repeat across fixtures
            if not m.startswith(("google", "pydantic")):
                sys.modules.pop(m, None)
