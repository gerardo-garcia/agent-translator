"""Read-only fetchers, exercised against fakes: botocore's Stubber for AWS (so request and response
shapes are checked against the real service model), a fake `kubectl` executable, a fake Azure
client. Nothing here touches a network."""
import functools
import json
import operator
import stat
import sys

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from agent_translator import cli
from agent_translator.adapters.bedrock import BedrockAdapter
from agent_translator.adapters.kagent import KagentAdapter
from agent_translator.fetch import bedrock as fbedrock
from agent_translator.fetch import kagent as fkagent
from agent_translator.fetch.azure import fetch as fetch_azure
from agent_translator.registry import load_adapters

from .bedrock_helpers import FIX as BFIX
from .kagent_helpers import FIX as KFIX
from .kagent_helpers import comparable

BUNDLE = BFIX / "api" / "getagent-bundle.json"


# ------------------------------------------------------------------------------------- Bedrock
def stubbed_client(bundle: dict, version="DRAFT", pages=False):
    boto3 = pytest.importorskip("boto3")
    from botocore.stub import Stubber
    c = boto3.client("bedrock-agent", region_name="us-east-1",
                     aws_access_key_id="x", aws_secret_access_key="x")
    st = Stubber(c)
    aid = bundle["agent"]["agentId"]
    if version == "DRAFT":
        st.add_response("get_agent", {"agent": bundle["agent"]}, {"agentId": aid})
    else:
        ver = {k: v for k, v in bundle["agent"].items() if k != "agentVersion"}
        st.add_response("get_agent_version", {"agentVersion": {**ver, "version": version}},
                        {"agentId": aid, "agentVersion": version})
    for key, lister, rkey, getter, idf, gkey in fbedrock.PARTS:
        items = bundle.get(key, [])
        members = c.meta.service_model.operation_model(
            "".join(p.title() for p in lister.split("_"))).output_shape.members[rkey].member.members
        summaries = [{k: v for k, v in i.items() if k in members} for i in items]
        args = {"agentId": aid, "agentVersion": version}
        if pages and len(summaries) > 1:
            st.add_response(lister, {rkey: summaries[:1], "nextToken": "t1"}, args)
            st.add_response(lister, {rkey: summaries[1:]}, {**args, "nextToken": "t1"})
        else:
            st.add_response(lister, {rkey: summaries}, args)
        for i in items:
            st.add_response(getter, {gkey: i}, {**args, idf: i[idf]})
    st.activate()
    return c, st


def test_bedrock_fetch_reproduces_the_bundle(tmp_path):
    bundle = json.loads(BUNDLE.read_text())
    client, stub = stubbed_client(bundle)
    files, _rep = fbedrock.fetch("FGHIJ67890", tmp_path, {}, client)
    stub.assert_no_pending_responses()
    assert [f.name for f in files] == ["travel-planner.json"]
    ad = BedrockAdapter()
    got, _ = ad.parse(files[0])
    want, _ = ad.parse(BUNDLE)
    assert comparable(got) == comparable(want)


def test_bedrock_fetch_follows_pagination_and_versions(tmp_path):
    bundle = json.loads(BUNDLE.read_text())
    client, stub = stubbed_client(bundle, version="3", pages=True)
    files, _ = fbedrock.fetch("FGHIJ67890:3", tmp_path, {}, client)
    stub.assert_no_pending_responses()
    saved = json.loads(files[0].read_text())
    assert len(saved["agentActionGroups"]) == 3 and "agentVersion" in saved
    assert BedrockAdapter().detect(files[0])


def test_bedrock_fetch_only_reads(tmp_path):
    bundle = json.loads(BUNDLE.read_text())
    client, _stub = stubbed_client(bundle)
    ops = []
    client.meta.events.register("provide-client-params.bedrock-agent.*",
                                lambda params, model, **kw: ops.append(model.name))
    fbedrock.fetch("FGHIJ67890", tmp_path, {}, client)
    assert ops and all(o.startswith(("Get", "List")) for o in ops), ops


def test_bedrock_fetch_errors_are_reported_not_raised(tmp_path):
    boto3 = pytest.importorskip("boto3")
    from botocore.stub import Stubber
    c = boto3.client("bedrock-agent", region_name="us-east-1",
                     aws_access_key_id="x", aws_secret_access_key="x")
    st = Stubber(c)
    st.add_client_error("get_agent", "ResourceNotFoundException", "no such agent", 404)
    st.activate()
    files, rep = fbedrock.fetch("NOPE", tmp_path, {}, c)
    assert not files and rep.worst.name == "error"
    assert any(i.code == "fetch-failed" and "no such agent" in i.message for i in rep.items)
    assert not list(tmp_path.iterdir())


# ------------------------------------------------------------------------------------- Kagent
FAKE_KUBECTL = '''#!{python}
import json, os, sys
from ruamel.yaml import YAML
args = sys.argv[1:]
log = open(os.environ["FAKE_LOG"], "a"); log.write(json.dumps(args) + "\\n"); log.close()
if "--context" in args:
    i = args.index("--context"); del args[i:i + 2]
assert args[0] == "get" and args[-2:] == ["-o", "json"], args
res, name = args[1], args[2]
ns = args[args.index("-n") + 1] if "-n" in args else None
kind = {{"agents": "Agent", "modelconfigs": "ModelConfig", "remotemcpservers": "RemoteMCPServer",
        "agenttemplates": "AgentTemplate"}}[res.split(".")[0]]
for f in os.environ["FAKE_DOCS"].split(os.pathsep):
    for d in YAML(typ="safe").load_all(open(f)):
        m = (d or {{}}).get("metadata", {{}})
        if d and d.get("kind") == kind and m.get("name") == name and (ns is None or m.get("namespace") == ns):
            d["metadata"].update(uid="u-1", resourceVersion="42", managedFields=[])
            d["status"] = {{"ready": True}}
            print(json.dumps(d)); sys.exit(0)
sys.stderr.write(f'Error from server (NotFound): {{res}} "{{name}}" not found\\n'); sys.exit(1)
'''


@pytest.fixture
def kubectl(tmp_path, monkeypatch):
    f = tmp_path / "kubectl"
    f.write_text(FAKE_KUBECTL.format(python=sys.executable))
    f.chmod(f.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.log"))
    return f


def test_kagent_fetch_follows_references_and_is_read_only(tmp_path, kubectl, monkeypatch):
    docs = [KFIX / "v1alpha2" / n for n in ("observability-multiagent.yaml", "promql-agent.yaml",
                                           "k8sgpt-remote-mcpserver.yaml")]
    monkeypatch.setenv("FAKE_DOCS", ":".join(map(str, docs)))
    out = tmp_path / "out"
    files, _rep = fkagent.fetch("kagent/observability-agent", out,
                               {"kubectl": str(kubectl), "context": "lab"})
    calls = [json.loads(line) for line in (tmp_path / "calls.log").read_text().splitlines()]
    assert calls and all("get" in c for c in calls)
    assert not any(w in " ".join(functools.reduce(operator.iadd, calls, [])) for w in ("secret", "delete", "apply", "patch", "create"))
    assert all("--context" in c and "lab" in c for c in calls)
    d = list(YAML(typ="safe").load_all(files[0].read_text()))
    assert d[0]["kind"] == "Agent" and d[0]["metadata"]["name"] == "observability-agent"
    assert all("uid" not in x["metadata"] and "status" not in x for x in d)
    ad = KagentAdapter()
    assert ad.detect(files[0])
    ir, _ = ad.parse(files[0])
    assert any(a.id == "observability-agent" for a in ir.agents)
    assert len(d) > 1                       # model config / referenced agents came along


def test_kagent_fetch_missing_agent_and_missing_reference(tmp_path, kubectl, monkeypatch):
    monkeypatch.setenv("FAKE_DOCS", str(KFIX / "v1alpha2" / "observability-multiagent.yaml"))
    files, rep = fkagent.fetch("kagent/ghost", tmp_path / "a", {"kubectl": str(kubectl)})
    assert not files and any(i.code == "fetch-failed" and "NotFound" in i.message for i in rep.items)
    files, rep = fkagent.fetch("kagent/observability-agent", tmp_path / "b", {"kubectl": str(kubectl)})
    assert files and any(i.code == "reference-missing" for i in rep.items)  # referenced, not in docs


def test_kagent_fetch_without_kubectl(tmp_path):
    files, rep = fkagent.fetch("x", tmp_path, {"kubectl": str(tmp_path / "nope")})
    assert not files and any("not found" in i.message for i in rep.items)


def test_kagent_api_version_is_part_of_the_resource(tmp_path, kubectl, monkeypatch):
    monkeypatch.setenv("FAKE_DOCS", str(KFIX / "v1alpha2" / "promql-agent.yaml"))
    fkagent.fetch("kagent/promql-agent", tmp_path / "o",
                  {"kubectl": str(kubectl), "api_version": "v1alpha2"})
    first = json.loads((tmp_path / "calls.log").read_text().splitlines()[0])
    assert first[1] == "agents.v1alpha2.kagent.dev"


# -------------------------------------------------------------------------------------- Azure
class FakeObj(dict):
    def as_dict(self):
        return dict(self)


class FakeAgents:
    def __init__(self, definition):
        self.d, self.calls = definition, []

    def get(self, name):
        self.calls.append(("get", name))
        return FakeObj(name=name, versions={"latest": {"version": "4", "definition": self.d}})

    def get_version(self, agent_name, agent_version):
        self.calls.append(("get_version", agent_name, agent_version))
        return FakeObj(name=agent_name, version=agent_version, definition=self.d)


class FakeProject:
    def __init__(self, definition):
        self.agents = FakeAgents(definition)


DEFINITION = {"kind": "prompt", "model": "gpt-4.1", "instructions": "You answer HR questions briefly.",
              "temperature": 0.3, "tools": [{"type": "mcp", "server_label": "docs",
                                              "server_url": "https://docs.example.com/mcp"}]}


def test_azure_fetch_latest_and_version(tmp_path):
    client = FakeProject(DEFINITION)
    files, _rep = fetch_azure("hr-bot", tmp_path, {}, client)
    assert client.agents.calls == [("get", "hr-bot")]
    ad = load_adapters()["azure"]
    assert ad.detect(files[0])
    ir, _ = ad.parse(files[0])
    assert ir.agents[0].model.id == "gpt-4.1" and ir.agents[0].tools
    files, _ = fetch_azure("hr-bot:2", tmp_path / "v", {}, client)
    assert client.agents.calls[-1] == ("get_version", "hr-bot", "2") and files


def test_azure_fetch_failures_are_reported(tmp_path):
    class Boom:
        class agents:
            @staticmethod
            def get(name):
                raise RuntimeError("401 unauthorized")
    files, rep = fetch_azure("x", tmp_path, {}, Boom())
    assert not files and any("401" in i.message for i in rep.items)
    files, rep = fetch_azure("x", tmp_path, {}, FakeProject(None))
    assert not files and any("no definition" in i.message for i in rep.items)


def test_azure_fetch_needs_an_endpoint(tmp_path, monkeypatch):
    monkeypatch.delenv("AZURE_AI_PROJECT_ENDPOINT", raising=False)
    files, rep = fetch_azure("x", tmp_path, {})
    assert not files and any("endpoint" in i.message for i in rep.items)


# ------------------------------------------------------------------------------------------ CLI
def test_cli_translate_fetch(tmp_path, monkeypatch):
    def fake(ref, out, opts, client=None):
        from agent_translator.report import Report
        out.mkdir(parents=True, exist_ok=True)
        (out / "a.json").write_text(BUNDLE.read_text())
        assert ref == "AGENT" and opts == {"region": "eu-west-1"}
        return [out / "a.json"], Report()
    monkeypatch.setattr(cli, "load_fetchers", lambda: {"bedrock": fake})
    r = CliRunner().invoke(cli.app, ["translate", "AGENT", "--fetch", "--from", "bedrock", "--to", "kagent",
                                     "--fetch-option", "region=eu-west-1", "-o", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "kagent.yaml").exists() and (tmp_path / "fetched" / "a.json").exists()
    r = CliRunner().invoke(cli.app, ["translate", "AGENT", "--fetch", "--to", "kagent"])
    assert r.exit_code != 0


def test_cli_fetch_unknown_platform():
    r = CliRunner().invoke(cli.app, ["fetch", "adk", "x"])
    assert r.exit_code != 0 and "cannot fetch" in r.output


def test_azure_translate_accepts_fetch_output_directory(tmp_path):
    """`fetch azure` writes a directory; translate must accept it (found against real Azure)."""
    import json

    from agent_translator.adapters.azure.adapter import AzureAdapter

    d = tmp_path / "fetched"
    d.mkdir()
    (d / "a.json").write_text(json.dumps(
        {"name": "a", "definition": {"kind": "prompt", "model": "gpt-4o", "instructions": "hi"}}))
    ad = AzureAdapter()
    assert ad.detect(d)
    ir, rep = ad.parse(d)
    assert [a.name for a in ir.agents] == ["a"]
    (d / "b.json").write_text("{}")
    ir, rep = ad.parse(d)
    assert any(i.code == "bad-input" for i in rep.items)
