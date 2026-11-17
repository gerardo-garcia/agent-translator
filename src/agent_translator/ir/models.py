"""Common intermediate representation (IR) for agent definitions."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

IR_VERSION = "0.1"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolKind(str, Enum):
    mcp = "mcp"
    function = "function"
    openapi = "openapi"
    knowledge = "knowledge"
    builtin = "builtin"
    agent = "agent"


class ModelSpec(_Base):
    provider: str | None = None  # anthropic | openai | google | amazon | other
    id: str | None = None  # native or logical model id
    params: dict[str, Any] = Field(default_factory=dict)


class Tool(_Base):
    id: str
    kind: ToolKind
    name: str | None = None
    description: str | None = None
    # mcp
    server_ref: str | None = None
    tool_names: list[str] = Field(default_factory=list)
    # function / openapi
    parameters: dict[str, Any] | None = None  # JSON Schema
    spec: Any | None = None  # openapi spec (inline or URL)
    impl_ref: str | None = None  # Lambda ARN, python path, URL... preserved, not portable
    # knowledge
    kb_ref: str | None = None
    top_k: int | None = None
    # agent-as-tool
    agent_ref: str | None = None
    # human approval before each call: True = required, False = explicitly not, None = unspecified
    requires_confirmation: bool | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)


class RemoteAgent(_Base):
    """An agent outside this IR that the owning agent delegates to (A2A endpoint, alias ARN...)."""
    id: str
    name: str
    description: str | None = None
    instruction: str | None = None  # how/when to delegate to it
    ref: str | None = None  # URL, ARN or other locator, when known as a literal
    extensions: dict[str, Any] = Field(default_factory=dict)


class Collaboration(_Base):
    # none | supervisor (parent calls sub-agents and keeps control) | router |
    # handoff (control is transferred to the sub-agent) |
    # sequential | parallel | loop (workflow agents: sub_agents run without an LLM deciding)
    mode: str = "none"
    max_iterations: int | None = None  # loop only
    # sub-agent id -> when/how to delegate to it (Bedrock CollaborationInstruction,
    # Kagent subAgent.description, ADK sub-agent description)
    instructions: dict[str, str] = Field(default_factory=dict)


class Memory(_Base):
    enabled: bool = False
    ttl_seconds: int | None = None


class AgentSpec(_Base):
    id: str
    name: str
    description: str | None = None
    instructions: str | None = None
    model: ModelSpec = Field(default_factory=ModelSpec)
    tools: list[Tool] = Field(default_factory=list)
    sub_agents: list[str] = Field(default_factory=list)
    remote_agents: list[RemoteAgent] = Field(default_factory=list)
    collaboration: Collaboration = Field(default_factory=Collaboration)
    memory: Memory = Field(default_factory=Memory)
    guardrails: list[dict[str, Any]] = Field(default_factory=list)
    extensions: dict[str, Any] = Field(default_factory=dict)


class Resources(_Base):
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    knowledge_bases: list[dict[str, Any]] = Field(default_factory=list)
    functions: list[dict[str, Any]] = Field(default_factory=list)


class AgentIR(_Base):
    irVersion: str = IR_VERSION
    agents: list[AgentSpec]
    resources: Resources = Field(default_factory=Resources)

    @model_validator(mode="after")
    def _check_refs(self) -> AgentIR:
        ids = [a.id for a in self.agents]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"duplicate agent ids: {sorted(dup)}")
        known = set(ids)
        for a in self.agents:
            for s in a.sub_agents:
                if s not in known:
                    raise ValueError(f"agent {a.id!r}: unknown sub_agent {s!r}")
            for t in a.tools:
                if t.kind is ToolKind.agent and t.agent_ref and t.agent_ref not in known:
                    raise ValueError(f"agent {a.id!r}: tool {t.id!r} references unknown agent")
        return self
