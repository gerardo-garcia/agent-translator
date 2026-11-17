from pathlib import Path

from agent_translator.adapters.kagent import KagentAdapter

FIX = Path(__file__).parent.parent / "fixtures" / "kagent"
ad = KagentAdapter()


def fixtures(version: str) -> list[Path]:
    return sorted((FIX / version).glob("*.yaml"))


def agent_fixtures(version: str) -> list[Path]:
    return [f for f in fixtures(version) if ad.detect(f)]


def codes(rep) -> set[str]:
    return {i.code for i in rep.items}


def comparable(ir):
    """IR content without list-position-dependent tool ids, sorted by agent id."""
    return [a.model_dump(exclude={"tools": {"__all__": {"id"}}})
            for a in sorted(ir.agents, key=lambda a: a.id)]


def by_id(ir, agent_id):
    return next(a for a in ir.agents if a.id == agent_id)
