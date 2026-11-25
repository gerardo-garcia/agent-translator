# CrewAI fixtures

All files are synthetic or derived from MIT-licensed templates; none comes from the
`crewAI-examples` repository (it declares no license).

| Fixture | Origin |
|---|---|
| `json_sequential/` | Synthetic, shaped after the `json_crew` template of the `crewai` package (1.15.x). Sequential crew with a custom tool and `context`. |
| `json_hierarchical/` | Synthetic. Hierarchical crew with `manager_llm`, MCP URLs, a `crewai_tools` tool and memory. |
| `standalone_agent/agent.yaml`, `agent.json` | Copied from `crewai_cli/templates/agent_config/` of the `crewai` package (MIT). |
| `classic/config/agents.yaml`, `tasks.yaml` | Copied from `crewai_cli/templates/crew/config/` of the `crewai` package (MIT). |

Both synthetic crews load with the real `crewai.project.json_loader` (checked in
`tests/test_crewai_real_loader.py`).
