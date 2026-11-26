"""Run emitted LlamaIndex modules with the real llama-index (skipped when not installed).

Run: uv run --all-extras --with llama-index-core --with llama-index-llms-anthropic
  --with llama-index-llms-openai --with llama-index-llms-google-genai
  --with llama-index-llms-bedrock-converse --with llama-index-tools-mcp
  pytest tests/test_llamaindex_real_loader.py
build() must construct real agents (FunctionAgent/AgentWorkflow validate their fields and tools);
no model is called. Modules with MCP tools or GoogleGenAI are skipped: building them opens a
network connection.
"""
import asyncio

import pytest

pytest.importorskip("llama_index.core")
pytest.importorskip("llama_index.llms.anthropic")
pytest.importorskip("llama_index.llms.openai")
pytest.importorskip("llama_index.llms.google_genai")
pytest.importorskip("llama_index.llms.bedrock_converse")
pytest.importorskip("llama_index.tools.mcp")

from .llamaindex_helpers import load_module
from .test_matrix import ADAPTERS, SOURCES, sid


@pytest.mark.parametrize("src", SOURCES, ids=sid)
def test_every_translation_builds_real_agents(src, tmp_path, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(k, "test")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    name, f = src
    ir, _ = ADAPTERS[name].parse(f)
    ADAPTERS["llamaindex"].emit(ir, tmp_path, {})
    for py in sorted(tmp_path.glob("*.py")):
        text = py.read_text()
        if "McpToolSpec(" in text or "GoogleGenAI(" in text:
            continue        # both open a network connection while building
        mod = load_module(py)
        runner = asyncio.run(mod.build())
        assert hasattr(runner, "run")
