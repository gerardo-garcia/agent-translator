# ADK fixtures

ADK agents are declared with the *Agent Config* YAML (`root_agent.yaml` plus the files it references).
Everything under `samples/` is copied verbatim from google/adk-python (Apache-2.0) at commit
`c6586f19ab8a2bb8b53e70041633b10858d9902e`, from `contributing/samples/**` (folders ending in
`_config`). `schemas/AgentConfig.json` is the schema published in the same repo
(`src/google/adk/agents/config_schemas/AgentConfig.json`); it marks its root as deprecated, so the
tests validate each file against the `$defs` entry for its `agent_class`.

| Path | Kind |
|---|---|
| samples/config/*, hitl/*, mcp/*, multi_agent/*, tools/* | official |
| samples/workflows/loop_config | official (`agent_class: Workflow`, graph with `edges`; outside the schema, parsed only partially) |
| synthetic/remote_tools | synthetic: remote MCP (HTTP and SSE) with tool filter, knowledge tool, long-running tool, generation config |
| synthetic/parallel_research | synthetic: `ParallelAgent` with two LLM sub-agents, state placeholders |

Synthetic files carry a comment saying so.
