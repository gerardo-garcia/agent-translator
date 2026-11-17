# Verification status: fetch and deploy

What has actually been exercised, and against what. Update this file whenever a real-environment test is run.

Legend: **real** = run against a live cloud/cluster; **fakes** = run in the test suite against injected fake clients (botocore `Stubber`, fake `kubectl`, fake Azure client).

| Platform | fetch | deploy |
|----------|-------|--------|
| Azure Foundry | **real** (once, 2026-11-17) + fakes | **real** (once, 2026-11-17) + fakes |
| AWS Bedrock | fakes only | fakes only |
| Kagent | **real** (once, 2026-11-17) + fakes | **real** (once, 2026-11-17) + fakes |
| GCP ADK (Agent Engine) | fakes only | fakes only |

## Real test: `fetch kagent` and `deploy kagent` (2026-11-17)

- Cluster: a single-node K3s lab cluster (v1.29) with kagent CRDs at `v1alpha2` (served and storage) and 16 existing agents in the `kagent` namespace. Access through a temporary kubeconfig (deleted afterwards).
- `fetch kagent` on an existing agent (`promql-agent`, read-only): wrote the Agent plus its ModelConfig, with no Secret, status or managedFields; the Secret appears only by name. `translate` to Azure and back to Kagent worked.
- `deploy kagent` into a new namespace `agtr-test` (the 16 existing agents were not touched; their count was unchanged afterwards):
  - `-O check=remote` (`--dry-run=server`): the real API server accepted ModelConfig, RemoteMCPServer and Agent.
  - `--apply --yes` created the three objects; the kagent controller set the Agent `ACCEPTED=True`, `READY=False` (the pod stays in `CreateContainerConfigError` until the model API key Secret exists, as the `secret-required` report item says; this tool never creates Secrets).
  - A second apply was refused by the server (`AlreadyExists`) and nothing was overwritten.
  - `fetch kagent agtr-test/prompt-agent-mcp-tools` read it back; the round trip differs from the emitted file only in namespace, key order and two server defaults (`terminateOnClose`, `runtime: python`).
- Bugs found and fixed:
  - `translate` on the directory written by `fetch kagent` failed with "could not autodetect format" (same class as the Azure one); the Kagent adapter now resolves a directory with a single YAML file.
  - The undo hint omitted `--context` and `-n`, so it would have targeted the wrong namespace; it now carries both.
- Cleanup: the namespace was deleted (which removed every object in it). Cost: none.
- Not covered: v1alpha3 (this cluster only has v1alpha2), `AgentTemplate`, Secrets, a multi-object failure part-way (a later object failing after earlier ones were created is reported but not rolled back).

## GCP / ADK (2026-11-17)

No cloud call was made. The emitted ADK projects were loaded with the real `google-adk` 2.14.1 (`from_config`, the first step of `adk run` / `adk web`): `tests/test_adk_real_loader.py`, 28 loaded, 35 skipped because their reports already ask for the user's own code or manual work (stubs, Python functions, callbacks, stdio MCP, custom agent classes).

- Bug found and fixed: ruamel wrote `threshold: OFF` unquoted; PyYAML (YAML 1.1) read it as `False` and ADK rejected the config. All emitters now go through `yamlio.new_yaml()`, which quotes such strings (also helps Go-based Kubernetes readers).
- ADK refuses stdio MCP servers in agent configs (security policy), which matches the `mcp-stdio` report item.
- The model is never called: no GCP project, API or credentials are involved.

## Real test: `fetch azure` (2026-11-17)

- Setup: a disposable `AIServices` S0 account plus one Foundry project in a test resource group, no model deployments (expected cost 0). Role "Azure AI User" (id `53ca6127-db72-4b80-b1b0-d745d6d5456d`) was needed for the agent data plane.
- Result: fetch worked end to end. It found one bug the fakes did not cover: `translate` on the directory written by `fetch azure` crashed with `IsADirectoryError`. Fixed in the Azure adapter (`_resolve`) with a regression test.
- Findings about the platform:
  - Foundry projects are managed under `…/accounts/<name>/projects/<proj>`.
  - Deleting the account requires deleting the nested project first.
  - Purging a soft-deleted account needs a subscription-scope permission (`deletedAccounts/delete`); without it the account stays soft-deleted (not billed) until auto-purge.
- Cleanup: everything was deleted except the soft-deleted account `agtr-test-11170153`, which auto-purges on 2026-11-19. The account from the deploy test (`agtr-dep-11171141`) is in the same state; both can be purged by an admin. An admin can purge it earlier with `az cognitiveservices account purge -g <rg> -n agtr-test-11170153 -l westeurope`.
- The existing team Foundry projects were not used: listing agents returned 403 (no `agents/read`), and no roles were granted on shared resources.

## Real test: `deploy azure` (2026-11-17)

- Setup: a new disposable `AIServices` S0 account + project, and one `gpt-4.1-mini` GlobalStandard deployment with capacity 1 (`gpt-4o-mini` was refused as deprecating for new deployments). Pay-per-token, no model calls were made, so cost was ~0.
- Input: `azure.yaml` produced by `translate fixtures/azure/foundry/03-mcp-tools.yaml --to azure`, with `model:` edited to the existing deployment name (the emitted `gpt-5.4-mini` did not exist in the account).
- Verified:
  - Dry run with `-O check=remote` reads the project and reports the agent as absent.
  - `--apply --yes` created the agent (version 1): the `create_version(body={definition, description})` call shape is correct.
  - `fetch azure` read the agent back.
  - A second deploy is refused (`agent-exists`, status failed, nothing changed).
  - `-O new_version=true` with an identical definition returns the existing version 1 (Azure deduplicates); with a changed definition it creates version 2.
- Cleanup: agent, model deployment, project and account deleted. The account `agtr-dep-11171141` stays soft-deleted (purge is denied, same missing subscription-scope permission); not billed.
- Not covered: `kind` other than `prompt` (refused by design), tools that need credentials, invoking the agent.

## Tested with fakes only

- `fetch adk` (`tests/test_adk_cloud.py`): `reasoningEngines.get` through a fake REST client; extraction of an inline source archive (path traversal and links refused), env-value redaction, pickled/Python-only engines reported as `manual`. The real GET response shape (in particular whether `sourceArchive` is returned) is unverified.
- `deploy adk`: the command line is built for Google's `adk deploy agent_engine` and every flag it uses was checked to exist in the real `google-adk` 2.14.1 CLI; the run itself, the engine-name parsing from its output and the `DELETE` undo command are only exercised with fakes. Stubs/placeholders refusal, create-only name check (paginated list) and consent rules are tested.

- `fetch bedrock`: `Get*/List*` calls, checked with `Stubber` (un-queued calls are rejected).
- `deploy` for all three platforms (tests in `tests/test_deploy.py`; Azure and Kagent were also run for real, above): dry-run default, `--apply` with confirmation / `--yes`, no tty without `--yes` → exit 2, placeholder refusal, create-only (existing objects/agents/stacks are errors), undo hint, `-O check=remote` read-only checks.

## Not verified

- Any real `deploy` to Bedrock.
- Bedrock CloudFormation stack creation (`OnFailure=ROLLBACK`, IAM capabilities, `ValidateTemplate`) against real AWS.
- Kagent `v1alpha3` against a real cluster.
- GCP: never touched (the test project had no AI APIs enabled): neither `fetch adk` nor `deploy adk` has talked to a real Vertex AI endpoint, and it is unknown whether `adk deploy agent_engine` accepts a YAML-config agent folder as emitted here.

## Cheapest next real tests

1. GCP: enable `aiplatform`, `deploy adk` a tiny agent, `fetch adk` it back, delete it (see the permissions list).
2. Bedrock fetch, then deploy of a stack with a role that has no extra permissions (delete the stack afterwards).
