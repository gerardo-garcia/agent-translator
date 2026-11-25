"""Load emitted CrewAI projects with the real crewai (skipped when it is not installed).

Run: uv run --all-extras --with crewai --with crewai-tools pytest tests/test_crewai_real_loader.py
Structure, task wiring, tool resolution (custom tool modules are executed) and the LLM
objects are checked by CrewAI's own loader; nothing calls a model.
"""
import pytest

pytest.importorskip("crewai")
pytest.importorskip("crewai_tools")

from crewai.project.json_loader import load_json_crew_project

from .crewai_helpers import FIX
from .test_matrix import ADAPTERS, SOURCES, sid


@pytest.mark.parametrize("n", ["json_sequential", "json_hierarchical"])
def test_the_hand_written_fixtures_are_valid_crewai(n):
    project = load_json_crew_project(FIX / n / "crew.jsonc")
    assert list(project.agents)


@pytest.mark.parametrize("src", SOURCES, ids=sid)
def test_every_translation_loads_in_crewai(src, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    name, f = src
    ir, _ = ADAPTERS[name].parse(f)
    ADAPTERS["crewai"].emit(ir, tmp_path, {})
    for crew_file in sorted(tmp_path.rglob("crew.jsonc")):
        project = load_json_crew_project(crew_file)
        assert project.agents and project.task_definitions


def test_a_translated_crew_builds_real_agents(tmp_path, monkeypatch):
    from crewai.project.crew_loader import load_crew

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    ir, _ = ADAPTERS["crewai"].parse(FIX / "json_hierarchical" / "crew.jsonc")
    for a in ir.agents:                     # SerperDevTool needs a key; MCP needs a network
        a.tools = []
    ADAPTERS["crewai"].emit(ir, tmp_path, {})
    crew, inputs = load_crew(tmp_path / "crew.jsonc")
    assert [a.role for a in crew.agents] == ["Market researcher", "Strategy analyst"]
    assert crew.process.value == "hierarchical" and inputs == {"topic": "edge AI accelerators"}
