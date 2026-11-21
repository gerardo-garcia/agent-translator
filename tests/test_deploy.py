"""Deployers against fakes only (Stubber for CloudFormation, a recording kubectl runner, a fake
Azure client): no network, no account. They check what matters for tools that write to real
accounts: dry run touches nothing, apply needs explicit consent, creation is create-only."""
import json

import pytest
from typer.testing import CliRunner

from agent_translator import cli
from agent_translator.deploy import azure as dazure
from agent_translator.deploy import bedrock as dbedrock
from agent_translator.deploy import kagent as dkagent

KAGENT = """\
apiVersion: api.kagent.dev/v1alpha3
kind: ModelConfig
metadata: {name: m}
spec: {provider: OpenAI, model: gpt-4o, apiKeySecret: k, apiKeySecretKey: api-key}
---
apiVersion: api.kagent.dev/v1alpha3
kind: Agent
metadata: {name: a}
spec: {template: {inline: {modelConfig: {name: m}, systemPrompt: hi}}}
"""
AZURE = """\
name: p
services:
  ai-project: {host: azure.ai.project}
  a:
    host: azure.ai.agent
    project: .
    uses: [ai-project]
    kind: prompt
    name: a
    description: d
    model: gpt-4o
    instructions: hi
"""
CFN = """\
AWSTemplateFormatVersion: '2010-09-09'
Parameters:
  AFoundationModel: {Type: String}
  AAgentResourceRoleArn: {Type: String}
Resources:
  A:
    Type: AWS::Bedrock::Agent
    Properties:
      AgentName: my-agent
      FoundationModel: {Ref: AFoundationModel}
      AgentResourceRoleArn: {Ref: AAgentResourceRoleArn}
"""
runner = CliRunner()


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


class Kubectl:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def __call__(self, cmd, stdin):
        self.calls.append((cmd, json.loads(stdin) if stdin else None))
        if self.fail_on and stdin and self.fail_on in stdin:
            return 1, "", "AlreadyExists"
        return 0, "", ""


# --------------------------------------------------------------------------------- Kagent
def test_kagent_plan_is_offline_and_lists_objects(tmp_path):
    f = write(tmp_path, "kagent.yaml", KAGENT)
    pl, _ = dkagent.plan(f, {}, client=lambda *a: pytest.fail("dry run must not call kubectl"))
    assert [s.resource for s in pl.steps] == ["ModelConfig/m", "Agent/a"]
    assert pl.undo.startswith("kubectl delete agent a")  # reverse order: agent before its model


def test_kagent_apply_uses_create_only_with_context(tmp_path):
    f = write(tmp_path, "kagent.yaml", KAGENT)
    opts = {"context": "lab", "namespace": "kagent"}
    pl, _ = dkagent.plan(f, opts)
    k = Kubectl()
    rep = dkagent.apply(pl, opts, client=k)
    assert rep.worst.name == "info" and len(k.calls) == 2
    for cmd, _ in k.calls:
        assert cmd[1:5] == ["--context", "lab", "-n", "kagent"]
        assert "create" in cmd and not {"apply", "replace", "patch", "delete", "edit"} & set(cmd)


def test_kagent_stops_at_first_failure(tmp_path):
    f = write(tmp_path, "kagent.yaml", KAGENT)
    pl, _ = dkagent.plan(f, {})
    k = Kubectl(fail_on='"ModelConfig"')
    rep = dkagent.apply(pl, {}, client=k)
    assert any(i.code == "create-failed" for i in rep.items) and len(k.calls) == 1


def test_kagent_refuses_secrets_foreign_kinds_and_placeholders(tmp_path):
    secret = "apiVersion: v1\nkind: Secret\nmetadata: {name: s}\nstringData: {k: v}\n"
    pl, rep = dkagent.plan(write(tmp_path, "s.yaml", secret), {})
    assert pl is None and any(i.code == "secret-refused" for i in rep.items)
    dep = "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: d}\n"
    pl, rep = dkagent.plan(write(tmp_path, "d.yaml", dep), {})
    assert pl is None and any(i.code == "foreign-kind" for i in rep.items)
    pl, rep = dkagent.plan(write(tmp_path, "p.yaml", KAGENT + "# REPLACE-WITH-SOMETHING\n"), {})
    assert pl is None and any(i.code == "placeholder-left" for i in rep.items)


def test_kagent_remote_check_is_server_dry_run(tmp_path):
    f = write(tmp_path, "kagent.yaml", KAGENT)
    k = Kubectl()
    pl, rep = dkagent.plan(f, {"check": "remote"}, client=k)
    assert pl and all("--dry-run=server" in c for c, _ in k.calls)
    bad = Kubectl(fail_on='"Agent"')
    pl, rep = dkagent.plan(f, {"check": "remote"}, client=bad)
    assert pl is None and any(i.code == "server-rejected" for i in rep.items)


# ---------------------------------------------------------------------------------- Azure
class NotFound(Exception):
    pass


NotFound.__name__ = "ResourceNotFoundError"


class FakeAzure:
    def __init__(self, existing=()):
        self.existing, self.created, self.calls = set(existing), [], []
        self.agents = self

    def get(self, name):
        self.calls.append(("get", name))
        if name not in self.existing:
            raise NotFound(name)
        return {"name": name}

    def create_version(self, agent_name, body):
        self.calls.append(("create_version", agent_name))
        self.created.append((agent_name, body))
        return {"version": "1"}


EP = {"endpoint": "https://x.services.ai.azure.com/api/projects/p"}


def test_azure_plan_offline_and_apply_creates(tmp_path):
    f = write(tmp_path, "azure.yaml", AZURE)
    pl, rep = dazure.plan(f, EP, client=None)
    assert pl and pl.steps[0].action == "create-agent"
    fake = FakeAzure()
    rep = dazure.apply(pl, EP, client=fake)
    assert rep.worst.name == "info"
    assert fake.created == [("a", {"definition": {"kind": "prompt", "model": "gpt-4o",
                                                    "instructions": "hi"}, "description": "d"})]


def test_azure_never_overwrites_existing_agent(tmp_path):
    f = write(tmp_path, "azure.yaml", AZURE)
    pl, _ = dazure.plan(f, EP)
    fake = FakeAzure(existing={"a"})
    rep = dazure.apply(pl, EP, client=fake)
    assert any(i.code == "agent-exists" for i in rep.items) and not fake.created
    rep = dazure.apply(pl, {**EP, "new_version": "true"}, client=FakeAzure(existing={"a"}))
    assert rep.worst.name == "info"
    pl, rep = dazure.plan(f, {**EP, "check": "remote"}, client=FakeAzure(existing={"a"}))
    assert pl is None and any(i.code == "agent-exists" for i in rep.items)


def test_azure_refuses_bad_inputs(tmp_path):
    pl, rep = dazure.plan(write(tmp_path, "a.yaml", AZURE), {})
    assert pl is None and any(i.code == "endpoint-required" for i in rep.items)
    pl, rep = dazure.plan(write(tmp_path, "b.yaml", AZURE.replace("gpt-4o", "REPLACE-WITH-DEPLOYMENT-NAME")), EP)
    assert pl is None and any(i.code == "placeholder-left" for i in rep.items)
    wf = write(tmp_path, "c.json", json.dumps({"name": "w", "definition": {"kind": "workflow", "model": "m"}}))
    pl, rep = dazure.plan(wf, EP)
    assert pl is None and any(i.code == "unsupported-kind" for i in rep.items)


def test_azure_accepts_fetch_output_shape(tmp_path):
    f = write(tmp_path, "x.json", json.dumps(
        {"name": "x", "definition": {"kind": "prompt", "model": "gpt-4o", "instructions": "hi"}}))
    pl, _ = dazure.plan(tmp_path, EP)  # a directory with one file
    assert pl and pl.steps[0].resource == "x" and f.exists()


# --------------------------------------------------------------------------------- Bedrock
def cf_client():
    boto3 = pytest.importorskip("boto3")
    from botocore.stub import Stubber
    c = boto3.client("cloudformation", region_name="us-east-1",
                     aws_access_key_id="x", aws_secret_access_key="x")
    return c, Stubber(c)


BOPTS = {"model": "anthropic.claude-sonnet-5-5", "role_arn": "arn:aws:iam::123456789012:role/r"}
PARAMS = [{"ParameterKey": "AFoundationModel", "ParameterValue": BOPTS["model"]},
          {"ParameterKey": "AAgentResourceRoleArn", "ParameterValue": BOPTS["role_arn"]}]
NOT_THERE = {"service_error_code": "ValidationError", "http_status_code": 400,
             "service_message": "Stack with id agtr-my-agent does not exist"}


def test_bedrock_plan_is_offline_and_maps_parameters(tmp_path):
    f = write(tmp_path, "template.yaml", CFN)
    pl, _ = dbedrock.plan(tmp_path, BOPTS, client=object())
    assert pl.data["stack"] == "agtr-my-agent" and pl.data["params"] == PARAMS
    assert "delete-stack --stack-name agtr-my-agent" in pl.undo and f.exists()


def test_bedrock_missing_parameters_and_sdk_output_and_iam(tmp_path):
    f = write(tmp_path, "template.yaml", CFN)
    pl, rep = dbedrock.plan(f, {})
    assert pl is None and any(i.code == "param-missing" for i in rep.items)
    iam = CFN + "  R:\n    Type: AWS::IAM::Role\n    Properties: {}\n"
    pl, rep = dbedrock.plan(write(tmp_path, "i.yaml", iam), BOPTS)
    assert pl is None and any(i.code == "capabilities-required" for i in rep.items)
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    (sdk / "01-A-create-agent.json").write_text("{}")
    pl, rep = dbedrock.plan(sdk, BOPTS)
    assert pl is None and any(i.code == "sdk-output" for i in rep.items)


def test_bedrock_apply_creates_stack_with_rollback(tmp_path):
    f = write(tmp_path, "template.yaml", CFN)
    pl, _ = dbedrock.plan(f, BOPTS)
    c, st = cf_client()
    st.add_client_error("describe_stacks", **NOT_THERE)
    st.add_response("create_stack", {"StackId": "arn:aws:cloudformation:us-east-1:1:stack/x/1"}, {
        "StackName": "agtr-my-agent", "TemplateBody": CFN, "Parameters": PARAMS,
        "OnFailure": "ROLLBACK", "Tags": [{"Key": "managed-by", "Value": "agent-translator"}]})
    with st:
        rep = dbedrock.apply(pl, {}, client=c)
    assert rep.worst.name == "info" and any(i.code == "stack-creating" for i in rep.items)


def test_bedrock_never_touches_existing_stack(tmp_path):
    f = write(tmp_path, "template.yaml", CFN)
    pl, _ = dbedrock.plan(f, BOPTS)
    c, st = cf_client()
    st.add_response("describe_stacks", {"Stacks": [{
        "StackName": "agtr-my-agent", "CreationTime": "2026-01-01", "StackStatus": "CREATE_COMPLETE"}]})
    with st:  # a create_stack here would raise: it was not queued
        rep = dbedrock.apply(pl, {}, client=c)
    assert any(i.code == "stack-exists" for i in rep.items)


def test_bedrock_remote_check_only_reads(tmp_path):
    f = write(tmp_path, "template.yaml", CFN)
    c, st = cf_client()
    st.add_response("validate_template", {}, {"TemplateBody": CFN})
    st.add_client_error("describe_stacks", **NOT_THERE)
    with st:
        pl, rep = dbedrock.plan(f, {**BOPTS, "check": "remote"}, client=c)
    assert pl and any(i.code == "template-valid" for i in rep.items)


# ------------------------------------------------------------------------------------- CLI
def test_cli_dry_run_changes_nothing_and_apply_needs_consent(tmp_path, monkeypatch):
    f = write(tmp_path, "kagent.yaml", KAGENT)
    monkeypatch.setattr(dkagent, "_run", lambda *a: pytest.fail("kubectl must not run"))
    r = runner.invoke(cli.app, ["deploy", "kagent", str(f)])
    assert r.exit_code == 0 and "dry run: nothing was changed" in r.output
    r = runner.invoke(cli.app, ["deploy", "kagent", str(f), "--apply"])  # not a tty, no --yes
    assert r.exit_code == 2 and "without confirmation" in r.output


def test_cli_apply_yes_creates_and_prints_undo(tmp_path, monkeypatch):
    f = write(tmp_path, "kagent.yaml", KAGENT)
    k = Kubectl()
    monkeypatch.setattr(dkagent, "_run", k)
    r = runner.invoke(cli.app, ["deploy", "kagent", str(f), "--apply", "--yes", "-O", "context=lab"])
    assert r.exit_code == 0 and len(k.calls) == 2 and "to remove it: kubectl --context lab delete" in r.output


def test_cli_plan_error_exits_1_and_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(dkagent, "_run", lambda *a: pytest.fail("kubectl must not run"))
    f = write(tmp_path, "s.yaml", "apiVersion: v1\nkind: Secret\nmetadata: {name: s}\n")
    r = runner.invoke(cli.app, ["deploy", "kagent", str(f), "--apply", "--yes"])
    assert r.exit_code == 1 and "nothing was changed" in r.output


def test_cli_unknown_platform():
    r = runner.invoke(cli.app, ["deploy", "nope", "x"])
    assert r.exit_code != 0


def test_kagent_undo_carries_context_and_namespace(tmp_path):
    """Found in the real mccluster test: the undo hint omitted the namespace."""
    f = tmp_path / "k.yaml"
    f.write_text("apiVersion: kagent.dev/v1alpha2\nkind: Agent\nmetadata: {name: a}\n"
                 "spec: {type: Declarative, declarative: {modelConfig: m, systemMessage: hi}}\n")
    pl, _ = dkagent.plan(f, {"namespace": "agtr-test", "context": "lab"}, client=lambda *a: (0, "", ""))
    assert pl.undo == "kubectl --context lab -n agtr-test delete agent a"


HARNESS = """\
AWSTemplateFormatVersion: '2010-09-09'
Parameters:
  AExecutionRoleArn:
    Type: String
Resources:
  AHarness:
    Type: AWS::BedrockAgentCore::Harness
    Properties:
      HarnessName: my_probe
      ExecutionRoleArn:
        Ref: AExecutionRoleArn
      Model:
        BedrockModelConfig:
          ModelId: eu.amazon.nova-lite-v1:0
"""


def test_bedrock_deployer_accepts_an_agentcore_template(tmp_path):
    write(tmp_path, "template.yaml", HARNESS)
    pl, _ = dbedrock.plan(tmp_path, {"role_arn": BOPTS["role_arn"]}, client=object())
    assert pl.data["stack"] == "agtr-my-probe"
    assert pl.data["params"] == [{"ParameterKey": "AExecutionRoleArn",
                                  "ParameterValue": BOPTS["role_arn"]}]
    assert pl.steps[-1].detail == "AWS::BedrockAgentCore::Harness"

