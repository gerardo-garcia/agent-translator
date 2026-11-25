from pathlib import Path

from agent_translator.adapters.crewai.common import loads_jsonc

FIX = Path(__file__).parent.parent / "fixtures" / "crewai"
SOURCES = [FIX / "json_sequential" / "crew.jsonc", FIX / "json_hierarchical" / "crew.jsonc",
           FIX / "standalone_agent" / "agent.yaml", FIX / "standalone_agent" / "agent.json",
           FIX / "classic" / "config" / "agents.yaml"]


def validate_project(out: Path) -> None:
    """An emitted project is self-consistent (the real loader checks it in the overlay job)."""
    for crew_file in sorted(out.rglob("crew.jsonc")):
        root = crew_file.parent
        crew = loads_jsonc(crew_file.read_text())
        assert crew["process"] in ("sequential", "hierarchical")
        names = list(crew["agents"])
        extra = [crew["manager_agent"]] if isinstance(crew.get("manager_agent"), str) else []
        for n in [*names, *extra]:
            body = loads_jsonc((root / "agents" / f"{n}.jsonc").read_text())
            assert all(isinstance(body.get(k), str) and body[k] for k in ("role", "goal", "backstory"))
            for t in body.get("tools", []):
                if isinstance(t, str) and t.startswith("custom:"):
                    assert (root / "tools" / f"{t[7:]}.py").exists(), t
        assert crew["tasks"]
        for t in crew["tasks"]:
            assert t["description"] and t["expected_output"]
            assert t.get("agent") in (None, *names)
        if crew["process"] == "hierarchical":
            assert crew.get("manager_llm") or crew.get("manager_agent")
        assert (root / "pyproject.toml").exists()
