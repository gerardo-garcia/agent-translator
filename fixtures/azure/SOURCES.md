# Azure fixtures

Two formats, both declarative:

| Folder | Format | Origin | License | Kind |
|---|---|---|---|---|
| foundry/ | Foundry Agent Service prompt agents in `azure.yaml` (`host: azure.ai.agent`, `kind: prompt`) | microsoft-foundry/foundry-samples @ `28731fe678455c0cf2b440765e4fb097ab634211`, `samples/cli/prompt-agents/<name>/azure.yaml` | MIT | official |
| agent-framework/ | Agent Framework declarative `kind: Prompt` YAML | microsoft/agent-framework @ `10801303c1bdf08811ba2c1066b55722b86b3fc2`, `declarative-agents/agent-samples/<dir>/<file>.yaml` (file name = `<dir>-<file>`) | MIT | official |
| synthetic/ | one of each | written for this project (comment at the top of each file) | n/a | synthetic |
| schemas/azure.yaml.json | azd's `azure.yaml` schema (Azure/azure-dev main; it does not describe agent services, only the surrounding file) | Azure/azure-dev | MIT | official |

Output is validated with Microsoft's own models (optional extra `validate-azure`): the
`azure-ai-projects` SDK classes for Foundry tools and `agent-framework-declarative` for the
Agent Framework YAML. Tests that need them are skipped when they are not installed.
The Foundry service has no ARM/Bicep resource for the agent itself: the definition is sent to the
data plane (SDK/REST/azd), so no template format exists for it.
