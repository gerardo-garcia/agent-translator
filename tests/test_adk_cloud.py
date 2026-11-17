"""fetch/deploy for ADK on Vertex AI Agent Engine, against fakes (no Google Cloud account)."""
import base64
import io
import tarfile

import pytest
from typer.testing import CliRunner

from agent_translator import cli
from agent_translator.adapters.adk import AdkAdapter
from agent_translator.deploy import adk as dadk
from agent_translator.fetch import adk as fadk
from agent_translator.report import Severity

runner = CliRunner()
NAME = "projects/p1/locations/europe-west1/reasoningEngines/123"
ROOT = ("name: helper\nmodel: gemini-2.5-flash\ndescription: Helps.\n"
        "instruction: Be helpful.\n")


def tarball(files: dict[str, str | bytes], extra=None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for n, c in files.items():
            data = c.encode() if isinstance(c, str) else c
            ti = tarfile.TarInfo(n)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        if extra:
            extra(tf)
    return buf.getvalue()


def engine(archive: bytes | None = None, **spec):
    s = dict(spec)
    if archive is not None:
        s["sourceCodeSpec"] = {"inlineSource": {"sourceArchive": base64.b64encode(archive).decode()}}
    return {"name": NAME, "displayName": "helper", "spec": s}


class FakeRest:
    def __init__(self, resource=None, pages=None, fail=None):
        self.resource, self.pages, self.fail, self.calls = resource, pages or [], fail, []

    def get(self, url, **params):
        self.calls.append((url, params))
        if self.fail:
            raise self.fail
        if url.endswith("/reasoningEngines"):
            return self.pages.pop(0)
        return self.resource


# ---------------------------------------------------------------- fetch


def test_fetch_extracts_source_and_translate_reads_it(tmp_path):
    rest = FakeRest(engine(tarball({"helper/root_agent.yaml": ROOT, "helper/__init__.py": ""}),
                           agentFramework="google-adk"))
    files, rep = fadk.fetch("123", tmp_path, {"project": "p1", "region": "europe-west1"}, client=rest)
    assert rep.worst < Severity.error
    assert [f.name for f in files] == ["root_agent.yaml"]
    assert rest.calls[0][0] == f"https://europe-west1-aiplatform.googleapis.com/v1/{NAME}"
    ir, prep = AdkAdapter().parse(files[0])
    assert ir.agents[0].id == "helper" and prep.worst < Severity.error


def test_fetch_accepts_full_resource_name_without_options(tmp_path):
    rest = FakeRest(engine(tarball({"a/root_agent.yaml": ROOT})))
    files, rep = fadk.fetch(NAME, tmp_path, {}, client=rest)
    assert files and rep.worst < Severity.error


def test_fetch_redacts_environment_values(tmp_path):
    res = engine(tarball({"a/root_agent.yaml": ROOT}))
    res["spec"]["deploymentSpec"] = {"env": [{"name": "API_KEY", "value": "s3cret"}]}
    fadk.fetch("123", tmp_path, {"project": "p1", "region": "europe-west1"}, client=FakeRest(res))
    text = (tmp_path / "123.json").read_text()
    assert "s3cret" not in text and "API_KEY" in text and "<redacted>" in text


def test_fetch_pickled_agent_is_manual(tmp_path):
    rest = FakeRest(engine(None, packageSpec={"pickleObjectGcsUri": "gs://b/x.pkl"}))
    files, rep = fadk.fetch("123", tmp_path, {"project": "p1", "region": "r"}, client=rest)
    assert [i.code for i in rep.items if i.severity >= Severity.manual] == ["pickled-agent"]
    assert [f.suffix for f in files] == [".json"]


def test_fetch_python_only_source_is_manual(tmp_path):
    rest = FakeRest(engine(tarball({"app/agent.py": "root_agent = None\n"})))
    files, rep = fadk.fetch("123", tmp_path, {"project": "p1", "region": "r"}, client=rest)
    assert any(i.code == "no-agent-config" for i in rep.items) and files[0].suffix == ".json"


def test_fetch_refuses_unsafe_archive(tmp_path):
    def link(tf):
        ti = tarfile.TarInfo("evil")
        ti.type, ti.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tf.addfile(ti)
    rest = FakeRest(engine(tarball({"a/root_agent.yaml": ROOT}, extra=link)))
    _, rep = fadk.fetch("123", tmp_path, {"project": "p1", "region": "r"}, client=rest)
    assert any(i.code == "archive-failed" for i in rep.items)
    traversal = tarball({"../escape.txt": "x"})
    _, rep = fadk.fetch("123", tmp_path / "o", {"project": "p1", "region": "r"},
                        client=FakeRest(engine(traversal)))
    assert any(i.code == "archive-failed" for i in rep.items)
    assert not (tmp_path / "escape.txt").exists()


def test_fetch_needs_project_and_region(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    files, rep = fadk.fetch("123", tmp_path, {}, client=FakeRest())
    assert not files and rep.items[0].code == "missing-option"


def test_fetch_reports_api_errors(tmp_path):
    files, rep = fadk.fetch("123", tmp_path, {"project": "p", "region": "r"},
                            client=FakeRest(fail=PermissionError("403")))
    assert not files and rep.items[0].code == "fetch-failed"


def test_fetch_only_ever_reads():
    rest = FakeRest(engine(tarball({"a/root_agent.yaml": ROOT})))
    assert not hasattr(rest, "post") and not hasattr(rest, "delete")


# ---------------------------------------------------------------- deploy


class FakeBackend(FakeRest):
    def __init__(self, out="", err="", rc=0, **kw):
        super().__init__(**kw)
        self.cmds, self._out = [], (out, err, rc)

    def run(self, cmd):
        self.cmds.append(cmd)
        return self._out[2], self._out[0], self._out[1]


def project(tmp_path, root=ROOT, tools=None):
    d = tmp_path / "out" / "helper"
    d.mkdir(parents=True)
    (d / "root_agent.yaml").write_text(root)
    if tools:
        (d / "tools.py").write_text(tools)
    return tmp_path / "out"


OPTS = {"project": "p1", "region": "europe-west1"}


def test_plan_is_offline_and_builds_the_official_command(tmp_path):
    be = FakeBackend()
    pl, rep = dadk.plan(project(tmp_path), {**OPTS, "service_account": "sa@p1.iam"}, client=be)
    assert pl and rep.worst < Severity.error and be.calls == [] and be.cmds == []
    assert pl.data["cmd"][:3] == ["adk", "deploy", "agent_engine"]
    cmd = " ".join(pl.data["cmd"])
    assert "--project p1 --region europe-west1 --display_name helper" in cmd
    assert "--service_account sa@p1.iam" in cmd and "--agent_engine_id" not in cmd
    assert pl.data["cmd"][-1].endswith("out/helper")
    assert any(i.code == "billing" for i in rep.items)


def test_plan_requires_project_and_region(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    pl, rep = dadk.plan(project(tmp_path), {}, client=FakeBackend())
    assert pl is None and rep.items[0].code == "missing-option"


def test_plan_refuses_placeholders_and_unimplemented_stubs(tmp_path):
    pl, rep = dadk.plan(project(tmp_path, root=ROOT + "# REPLACE-WITH-PROJECT\n"), OPTS)
    assert pl is None and any(i.code == "placeholder-left" for i in rep.items)
    stub = "def f(x: str) -> dict:\n    raise NotImplementedError('f')\n"
    pl, rep = dadk.plan(project(tmp_path / "b", tools=stub), OPTS)
    assert pl is None and any(i.code == "stubs-unimplemented" for i in rep.items)
    toolset = "# openapi_x: OpenAPI spec of 'x' unknown; build the toolset here\n"
    pl, rep = dadk.plan(project(tmp_path / "c", tools=toolset), OPTS)
    assert pl is None
    done = "def f(x: str) -> dict:\n    return {'x': x}\n"
    pl, _ = dadk.plan(project(tmp_path / "d", tools=done), OPTS)
    assert pl is not None


def test_plan_accepts_folder_file_or_output_dir_and_rejects_others(tmp_path):
    out = project(tmp_path)
    for p in (out, out / "helper", out / "helper" / "root_agent.yaml"):
        pl, _ = dadk.plan(p, OPTS)
        assert pl is not None, p
    pl, rep = dadk.plan(tmp_path / "empty", OPTS)
    assert pl is None and rep.items[0].code == "bad-input"


def test_remote_check_refuses_an_existing_display_name_and_paginates(tmp_path):
    pages = [{"reasoningEngines": [{"displayName": "other"}], "nextPageToken": "t"},
             {"reasoningEngines": [{"displayName": "helper", "name": NAME}]}]
    be = FakeBackend(pages=pages)
    pl, rep = dadk.plan(project(tmp_path), {**OPTS, "check": "remote"}, client=be)
    assert pl is None and any(i.code == "engine-exists" for i in rep.items)
    assert be.calls[1][1].get("pageToken") == "t" and be.cmds == []


def test_remote_check_passes_when_the_name_is_free(tmp_path):
    be = FakeBackend(pages=[{"reasoningEngines": [{"displayName": "other"}]}])
    pl, rep = dadk.plan(project(tmp_path), {**OPTS, "check": "remote"}, client=be)
    assert pl is not None and any(i.code == "no-name-clash" for i in rep.items)


def test_apply_runs_adk_and_prints_a_precise_undo(tmp_path):
    be = FakeBackend(out=f"Creating AgentEngine\nCreated agent engine: {NAME}\n")
    pl, _ = dadk.plan(project(tmp_path), OPTS)
    rep = dadk.apply(pl, OPTS, client=be)
    assert rep.worst < Severity.error and len(be.cmds) == 1
    assert any(i.code == "created" and NAME in i.path for i in rep.items)
    assert NAME in pl.undo and "-X DELETE" in pl.undo and "force=true" in pl.undo


def test_apply_failure_is_an_error_and_keeps_the_engine_name_if_any(tmp_path):
    be = FakeBackend(err=f"boom\nengine {NAME} half-created", rc=1)
    pl, _ = dadk.plan(project(tmp_path), OPTS)
    rep = dadk.apply(pl, OPTS, client=be)
    assert rep.worst >= Severity.error and NAME in rep.items[0].message
    assert NAME in pl.undo


def test_apply_without_engine_name_warns(tmp_path):
    pl, _ = dadk.plan(project(tmp_path), OPTS)
    rep = dadk.apply(pl, OPTS, client=FakeBackend(out="done"))
    assert [i.code for i in rep.items] == ["created-id-unknown"]


def test_missing_adk_binary_has_a_clear_message(tmp_path):
    pl, _ = dadk.plan(project(tmp_path), {**OPTS, "adk": "/nonexistent/adk"})
    rep = dadk.apply(pl, OPTS, client=dadk.Backend())
    assert rep.worst >= Severity.error and "install google-adk" in rep.items[0].message


# ---------------------------------------------------------------- CLI


def test_cli_dry_run_and_consent(tmp_path, monkeypatch):
    be = FakeBackend(out=NAME)
    monkeypatch.setattr(dadk, "Backend", lambda: be)
    out = project(tmp_path)
    r = runner.invoke(cli.app, ["deploy", "adk", str(out), "-O", "project=p1", "-O", "region=r"])
    assert r.exit_code == 0 and "dry run: nothing was changed" in r.output and be.cmds == []
    r = runner.invoke(cli.app, ["deploy", "adk", str(out), "-O", "project=p1", "-O", "region=r",
                                "--apply"])
    assert r.exit_code == 2 and be.cmds == []
    r = runner.invoke(cli.app, ["deploy", "adk", str(out), "-O", "project=p1", "-O", "region=r",
                                "--apply", "--yes"])
    assert r.exit_code == 0 and len(be.cmds) == 1 and "to remove it: curl -X DELETE" in r.output


def test_cli_fetch_then_translate_chain(tmp_path, monkeypatch):
    rest = FakeRest(engine(tarball({"helper/root_agent.yaml": ROOT})))
    monkeypatch.setattr(fadk, "make_client", lambda options: rest)
    r = runner.invoke(cli.app, ["translate", "123", "--from", "adk", "--fetch", "--to", "kagent",
                                "--fetch-option", "project=p1", "--fetch-option", "region=r",
                                "-o", str(tmp_path / "o")])
    assert r.exit_code == 0, r.output
    assert "kind: Agent" in (tmp_path / "o" / "kagent.yaml").read_text()


@pytest.mark.parametrize("ref", ["123", NAME])
def test_ref_forms(ref, tmp_path):
    files, rep = fadk.fetch(ref, tmp_path, {"project": "p1", "region": "europe-west1"},
                            client=FakeRest(engine(tarball({"a/root_agent.yaml": ROOT}))))
    assert files and rep.worst < Severity.error
