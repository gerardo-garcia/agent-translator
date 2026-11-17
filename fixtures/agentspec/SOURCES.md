# Agent Spec fixtures

Copied verbatim from oracle/agent-spec @ `9beda2b283b63aefc5bfe047f644279a83191b4b`,
`docs/pyagentspec/source/agentspec_config_examples/<name>.yaml`. License: Apache-2.0 or UPL-1.0 (at
your option), as stated in each file's header. All official; none synthetic.

- Agents: autogen_to_agentspec, ext_code_assistant_tutorial (MCP), ext_ops_assistant_tutorial_agent,
  howto_ag_ui, howto_agent_with_remote_tools (RemoteTool), howto_agents, howto_mcp_agent,
  math_homework_agent, simple_agent_with_rag_tool.
- Multi-agent: howto_managerworkers, howto_swarm.
- Not translated (the adapter reports them): howto_a2aagent (`A2AAgent` as a root component),
  howto_flowbuilder (`Flow`).

`howto_managerworkers` declares `agentspec_version: 26.4.0`, newer than the `pyagentspec` 26.3.1
used to validate; the test rewrites the version before loading it.
