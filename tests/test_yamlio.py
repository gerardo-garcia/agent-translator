import io

import pytest
from ruamel.yaml import YAML

from agent_translator.yamlio import needs_quotes, new_yaml


@pytest.mark.parametrize("s", ["OFF", "off", "No", "yes", "Y", "n", "on", "true", "null", "~", "1:30", "0755"])
def test_yaml11_ambiguous_strings_are_quoted(s):
    buf = io.StringIO()
    new_yaml().dump({"k": s}, buf)
    assert f"'{s}'" in buf.getvalue()
    assert needs_quotes(s)


@pytest.mark.parametrize("s", ["gemini-2.5-flash", "BLOCK_NONE", "hello", "1.5", "nope"])
def test_ordinary_strings_are_untouched(s):
    assert not needs_quotes(s)


def test_roundtrip_with_a_yaml11_reader():
    yaml = pytest.importorskip("yaml")          # PyYAML, the reader ADK uses
    buf = io.StringIO()
    doc = {"threshold": "OFF", "flag": "no", "names": ["yes", "on"]}
    new_yaml().dump(doc, buf)
    assert yaml.safe_load(buf.getvalue()) == doc
    assert YAML(typ="safe").load(buf.getvalue()) == doc
