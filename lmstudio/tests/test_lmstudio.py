"""The LM Studio plugin, driven the way Atlas drives it: a fake `openai` client
for `/v1`, and LM Studio's REST API answered from a table.

Run from a checkout of Atlas (`uv run pytest path/to/lmstudio/tests`).
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from atlas.sdk.plugin_entry import PluginContext
from atlas.sdk.provider import ContextOverflowError, Message
from atlas.sdk.runtime import ConfigError, ProviderError
from atlas.sdk.tool_plugin import ToolSpec

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "atlas_plugin_lmstudio", HERE.parent / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()
LMStudioProvider = plugin.LMStudioProvider

SPEC = ToolSpec(name="echo", description="Echo.", parameters={"type": "object", "properties": {}})


def llm(
    key: str, *, loaded: int = 0, vision: bool = False, reasoning: list[str] | None = None
) -> dict[str, Any]:
    capabilities: dict[str, Any] = {"vision": vision, "trained_for_tool_use": True}
    if reasoning is not None:
        capabilities["reasoning"] = {"allowed_options": reasoning, "default": reasoning[-1]}
    return {
        "type": "llm",
        "key": key,
        "loaded_instances": ([{"id": key, "config": {"context_length": loaded}}] if loaded else []),
        "max_context_length": 131072,
        "capabilities": capabilities,
    }


EMBED = {
    "type": "embedding",
    "key": "text-embedding-nomic-embed-text-v1.5",
    "loaded_instances": [],
    "max_context_length": 2048,
}


class Server:
    def __init__(self, models: list[dict[str, Any]], *, load_as: int = 8192) -> None:
        self.models = models
        self.load_as = load_as
        self.calls: list[tuple[Any, ...]] = []

    def listing(self) -> dict[str, Any]:
        return {"models": self.models}

    async def fetch(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
        self.calls.append(("GET", url, dict(headers or {})))
        assert url.endswith("/api/v1/models"), url
        return self.listing()

    async def post(self, url: str, body: Mapping[str, Any], **_: Any) -> Any:
        self.calls.append(("POST", url, dict(body)))
        assert url.endswith("/api/v1/models/load"), url
        size = body.get("context_length", self.load_as)
        for row in self.models:
            if row["key"] == body["model"]:
                row["loaded_instances"] = [{"id": row["key"], "config": {"context_length": size}}]
        return {"status": "loaded", "load_config": {"context_length": size}}

    def probe(self, url: str, body: Mapping[str, Any] | None = None, **_: Any) -> Any:
        self.calls.append(("PROBE", url, dict(body or {})))
        if url.endswith("/api/v1/models"):
            return self.listing()
        if url.endswith("/embeddings"):
            return {"data": [{"embedding": [0.0] * 768}]}
        return {}


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Server:
    fake = Server([llm("qwen/qwen3-8b"), llm("google/gemma-3-12b", vision=True), EMBED])
    monkeypatch.setattr(plugin, "fetch_json", fake.fetch)
    monkeypatch.setattr(plugin, "post_json", fake.post)
    monkeypatch.setattr(plugin, "probe_json", fake.probe)
    plugin._WINDOWS.clear()
    plugin._ROWS.clear()
    return fake


class FakeClient:
    def __init__(self, *, raises: list[Exception] | None = None) -> None:
        self.raises = list(raises or [])
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.api_key = plugin.PLACEHOLDER_KEY

    @property
    def request(self) -> dict[str, Any]:
        return self.requests[-1]

    async def _create(self, **request: Any) -> Any:
        self.requests.append(request)
        if self.raises:
            raise self.raises.pop(0)
        message = {"role": "assistant", "content": "ok", "reasoning_content": "hm"}
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def test_the_class_says_what_the_manifest_says() -> None:
    assert LMStudioProvider.local is True
    assert LMStudioProvider.api_key_env_vars == ("LM_API_TOKEN",)
    assert LMStudioProvider.thinking_levels == ()


def test_no_token_is_the_ordinary_case(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-the-users-openai-key")
    monkeypatch.delenv("LM_API_TOKEN", raising=False)
    provider = LMStudioProvider("qwen/qwen3-8b")
    assert provider._client.api_key == plugin.PLACEHOLDER_KEY
    assert provider.native_headers() == {}
    monkeypatch.setenv("LM_API_TOKEN", "lm-token")
    assert LMStudioProvider("m").native_headers() == {"Authorization": "Bearer lm-token"}


def test_register_binds_the_settings(server: Server) -> None:
    ctx = PluginContext(plugin="lmstudio", settings={"context_length": 32768}, providers=True)
    plugin.LMStudioPlugin().register(ctx)
    from atlas.providers import provider_class
    from atlas.providers.embedding import known_embedders

    cls: Any = provider_class("lmstudio")
    assert cls is not None and issubclass(cls, LMStudioProvider)
    assert cls.context_length == 32768
    embedder = known_embedders()["lmstudio"](
        "text-embedding-nomic-embed-text-v1.5", client=FakeClient()
    )
    assert embedder.dimensions == 768


def test_the_window_is_loaded_then_configured_then_the_floor(server: Server) -> None:
    server.models = [llm("qwen/qwen3-8b", loaded=16384)]
    assert LMStudioProvider.window_for("qwen/qwen3-8b") == 16384
    plugin._WINDOWS.clear()
    plugin._ROWS.clear()
    server.models = [llm("qwen/qwen3-8b")]
    assert LMStudioProvider.bind(context_length=32768).window_for("qwen/qwen3-8b") == 32768
    assert LMStudioProvider.window_for("qwen/qwen3-8b") == plugin.FLOOR_CONTEXT


async def test_a_model_is_loaded_at_the_configured_context_before_the_request(
    server: Server,
) -> None:
    client = FakeClient()
    provider = LMStudioProvider.bind(context_length=32768)("qwen/qwen3-8b", client=client)
    reply = await provider.complete(system="", messages=[Message.user("hi")])
    loads = [c for c in server.calls if c[0] == "POST"]
    assert loads == [
        (
            "POST",
            "http://127.0.0.1:1234/api/v1/models/load",
            {"model": "qwen/qwen3-8b", "echo_load_config": True, "context_length": 32768},
        )
    ]
    assert provider.served_window == 32768
    assert reply.text == "ok" and reply.thinking == "hm"
    assert "reasoning_effort" not in client.request


async def test_a_loaded_model_is_used_as_it_is(server: Server) -> None:
    server.models = [llm("qwen/qwen3-8b", loaded=16384)]
    provider = LMStudioProvider.bind(context_length=32768)("qwen/qwen3-8b", client=FakeClient())
    await provider.complete(system="", messages=[Message.user("hi")])
    assert not [c for c in server.calls if c[0] == "POST"]
    assert provider.served_window == 16384


async def test_a_request_that_would_not_fit_is_refused(server: Server) -> None:
    server.models = [llm("qwen/qwen3-8b", loaded=4096)]
    client = FakeClient()
    provider = LMStudioProvider("qwen/qwen3-8b", client=client)
    with pytest.raises(ContextOverflowError, match="context_length") as raised:
        await provider.complete(system="x" * 20_000, messages=[Message.user("hi")], tools=[SPEC])
    assert raised.value.attempted >= 4096
    assert client.requests == []


async def test_the_listing_reports_what_a_request_gets(server: Server) -> None:
    server.models[0] = llm("qwen/qwen3-8b", loaded=16384)
    provider = LMStudioProvider.bind(context_length=32768)(client=FakeClient())
    entries = {e.id: e for e in await provider.list_models() or ()}
    assert set(entries) == {"qwen/qwen3-8b", "google/gemma-3-12b"}, "no embedders"
    assert entries["qwen/qwen3-8b"].context_window == 16384
    assert entries["google/gemma-3-12b"].context_window == 32768
    assert entries["google/gemma-3-12b"].modalities == ("text", "image")
    assert entries["qwen/qwen3-8b"].cost == plugin.FREE
    assert not [c for c in server.calls if c[0] == "POST"], "a listing loads nothing"


async def test_one_loaded_model_is_the_answer(server: Server) -> None:
    server.models[1] = llm("google/gemma-3-12b", loaded=8192)
    provider = LMStudioProvider(client=FakeClient())
    await provider.complete(system="", messages=[Message.user("hi")])
    assert provider.model == "google/gemma-3-12b"

    server.models = [llm("a"), llm("b")]
    with pytest.raises(ConfigError, match="a, b"):
        await LMStudioProvider(client=FakeClient()).complete(
            system="", messages=[Message.user("hi")]
        )


async def test_failures_say_what_to_do(server: Server) -> None:
    server.models = [llm("qwen/qwen3-8b", loaded=16384)]

    class APIConnectionError(Exception):
        pass

    for raised, expected in [
        (APIConnectionError("Connection error."), "lms server start"),
        (RuntimeError("Error code: 401 - Unauthorized"), "LM_API_TOKEN"),
    ]:
        provider = LMStudioProvider("qwen/qwen3-8b", client=FakeClient(raises=[raised]))
        with pytest.raises(ProviderError, match=expected) as caught:
            await provider.complete(system="", messages=[Message.user("hi")])
        assert caught.value.__cause__ is raised


# -- thinking (measured against a live LM Studio: only reasoning_effort counts) --


def test_the_menu_comes_from_what_lm_studio_lists() -> None:
    assert plugin.levels_of(llm("a", reasoning=["off", "on"])) == ("off", "high")
    assert plugin.levels_of(llm("b", reasoning=["on"])) == ("high",)
    assert plugin.levels_of(llm("c", reasoning=["low", "medium", "high"])) == (
        "low",
        "medium",
        "high",
    )
    assert plugin.levels_of(llm("d")) == (), "no reasoning entry is no control"
    assert plugin.levels_of({"key": "e"}) is None, "no capabilities says nothing"


async def test_think_is_sent_as_reasoning_effort(server: Server) -> None:
    server.models = [llm("qwen/qwen3-1.7b", loaded=16384, reasoning=["off", "on"])]
    assert LMStudioProvider.levels_for("qwen/qwen3-1.7b") == ("off", "high")
    client = FakeClient()
    provider = LMStudioProvider("qwen/qwen3-1.7b", client=client)
    assert provider.thinking == "high", "on, by default"
    await provider.complete(system="", messages=[Message.user("hi")])
    assert client.request["reasoning_effort"] == "high"
    provider.set_thinking("off")
    await provider.complete(system="", messages=[Message.user("hi")])
    assert client.request["reasoning_effort"] == "none"


async def test_a_model_without_reasoning_is_sent_nothing(server: Server) -> None:
    server.models = [llm("plain", loaded=16384)]
    client = FakeClient()
    await LMStudioProvider("plain", client=client).complete(
        system="", messages=[Message.user("hi")]
    )
    assert "reasoning_effort" not in client.request


async def test_the_listing_carries_the_menu(server: Server) -> None:
    server.models = [llm("qwen/qwen3-1.7b", reasoning=["off", "on"])]
    entries = await LMStudioProvider(client=FakeClient()).list_models() or ()
    assert [e.thinking_levels for e in entries] == [("off", "high")]
