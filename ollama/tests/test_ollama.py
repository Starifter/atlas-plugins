"""The Ollama plugin, driven the way Atlas drives it: a fake `openai` client for
the `/v1` requests, and the native `/api/*` endpoints answered from a table.

Run from a checkout of Atlas (`uv run pytest path/to/ollama/tests`), which
supplies pytest-asyncio in auto mode and the `atlas.sdk` the plugin imports.
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
from atlas.sdk.runtime import ConfigError, CredentialError, ProviderError
from atlas.sdk.tool_plugin import ToolSpec

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("atlas_plugin_ollama", HERE.parent / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()
OllamaProvider = plugin.OllamaProvider
OllamaCloudProvider = plugin.OllamaCloudProvider

SPEC = ToolSpec(name="echo", description="Echo.", parameters={"type": "object", "properties": {}})

QWEN_SHOW = {
    "capabilities": ["completion", "tools", "thinking"],
    "model_info": {"qwen3.context_length": 40960},
}
GPT_OSS_SHOW = {
    "capabilities": ["completion", "tools", "thinking"],
    "thinking": {"values": ["low", "medium", "high"], "default": "medium"},
    "model_info": {"gptoss.context_length": 131072},
}
LLAVA_SHOW = {
    "capabilities": ["completion", "vision"],
    "model_info": {"llama.context_length": 8192},
}
EMBED_SHOW = {
    "capabilities": ["embedding"],
    "model_info": {"nomic-bert.embedding_length": 768},
}


class Server:
    """The native endpoints, answered from what this test says is loaded."""

    def __init__(self, *, loaded: Mapping[str, int] | None = None, load_as: int = 0) -> None:
        self.loaded = dict(loaded or {})
        self.load_as = load_as
        self.calls: list[tuple[Any, ...]] = []
        self.shows = {
            "qwen3:latest": QWEN_SHOW,
            "gpt-oss:20b": GPT_OSS_SHOW,
            "llava:latest": LLAVA_SHOW,
            "nomic-embed-text:latest": EMBED_SHOW,
        }

    def ps(self) -> dict[str, Any]:
        return {
            "models": [{"name": name, "context_length": ctx} for name, ctx in self.loaded.items()]
        }

    async def fetch(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
        self.calls.append(("GET", url))
        if url.endswith("/api/ps"):
            return self.ps()
        if url.endswith("/api/tags"):
            return {
                "models": [
                    {"name": name, "modified_at": "2026-05-01T10:00:00.123456789-07:00"}
                    for name in self.shows
                ]
            }
        raise AssertionError(url)

    async def post(self, url: str, body: Mapping[str, Any], **_: Any) -> Any:
        self.calls.append(("POST", url))
        if url.endswith("/api/generate"):
            self.loaded[plugin.tagged(body["model"])] = self.load_as
            return {"done": True}
        if url.endswith("/api/show"):
            return self.shows[plugin.tagged(body["model"])]
        raise AssertionError(url)

    def probe(self, url: str, body: Mapping[str, Any] | None = None) -> Any:
        self.calls.append(("PROBE", url))
        if url.endswith("/api/ps"):
            return self.ps()
        if url.endswith("/api/show") and body is not None:
            return self.shows.get(plugin.tagged(body["model"]), {})
        return {}


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Server:
    fake = Server()
    monkeypatch.setattr(plugin, "fetch_json", fake.fetch)
    monkeypatch.setattr(plugin, "post_json", fake.post)
    monkeypatch.setattr(plugin, "probe_json", fake.probe)
    plugin._WINDOWS.clear()
    return fake


class FakeClient:
    def __init__(self, message: Any = None, *, raises: list[Exception] | None = None) -> None:
        self.message = message or {"role": "assistant", "content": "ok"}
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
        return SimpleNamespace(
            choices=[SimpleNamespace(message=self.message)],
            usage={"prompt_tokens": 30, "completion_tokens": 2},
        )


# -- construction ---------------------------------------------------------------------


def test_the_classes_say_what_the_manifest_says() -> None:
    assert OllamaProvider.local is True and OllamaCloudProvider.local is False
    assert OllamaProvider.api_key_env_vars == ()
    assert OllamaCloudProvider.api_key_env_vars == ("OLLAMA_API_KEY",)
    assert OllamaProvider.thinking_levels == ("off", "low", "medium", "high", "max")


def test_register_installs_both_providers_and_an_embedder(server: Server) -> None:
    ctx = PluginContext(
        plugin="ollama",
        settings={"base_url": "http://box.lan:11434/v1", "context_length": 32768},
        providers=True,
    )
    plugin.OllamaPlugin().register(ctx)
    from atlas.providers import provider_class
    from atlas.providers.embedding import known_embedders

    local: Any = provider_class("ollama")
    assert local is not None and issubclass(local, OllamaProvider)
    assert local.base_url == "http://box.lan:11434/v1"
    assert local.context_length == 32768
    assert provider_class("ollama-cloud") is OllamaCloudProvider
    embedder: Any = known_embedders()["ollama"]("nomic-embed-text", client=FakeClient())
    assert embedder.base_url_in_use == "http://box.lan:11434/v1"
    assert embedder.dimensions == 768


def test_a_local_server_is_never_sent_the_users_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-the-users-openai-key")
    assert OllamaProvider("qwen3")._client.api_key == plugin.PLACEHOLDER_KEY


def test_the_cloud_wants_its_own_key(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-the-users-openai-key")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    with pytest.raises(CredentialError, match="OLLAMA_API_KEY"):
        OllamaCloudProvider("gpt-oss:120b")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-cloud-key")
    cloud = OllamaCloudProvider("gpt-oss:120b")
    assert cloud._client.api_key == "ollama-cloud-key"
    assert cloud.base_url_in_use == "https://ollama.com/v1"
    assert cloud.native_headers() == {"Authorization": "Bearer ollama-cloud-key"}


# -- the window -------------------------------------------------------------------------


def test_the_window_is_the_loaded_size_then_the_setting_then_the_floor(server: Server) -> None:
    server.loaded = {"qwen3:latest": 65536}
    assert OllamaProvider.window_for("qwen3") == 65536

    plugin._WINDOWS.clear()
    server.loaded = {}
    configured = OllamaProvider.bind(context_length=32768)
    assert configured.window_for("qwen3") == 32768
    assert OllamaProvider.window_for("qwen3") == plugin.FLOOR_CONTEXT


def test_the_window_probe_is_remembered(server: Server) -> None:
    server.loaded = {"qwen3:latest": 65536}
    OllamaProvider.window_for("qwen3")
    OllamaProvider.window_for("qwen3")
    assert [c for c in server.calls if c[0] == "PROBE"] == [
        ("PROBE", "http://127.0.0.1:11434/api/ps")
    ]


async def test_a_model_not_in_memory_is_loaded_before_the_request(server: Server) -> None:
    server.load_as = 8192
    client = FakeClient()
    provider = OllamaProvider("qwen3", client=client)
    await provider.complete(system="", messages=[Message.user("hi")])
    assert ("POST", "http://127.0.0.1:11434/api/generate") in server.calls
    assert provider.served_window == 8192
    assert client.request["model"] == "qwen3"


async def test_a_request_that_would_not_fit_is_refused_not_truncated(server: Server) -> None:
    server.loaded = {"qwen3:latest": 4096}
    client = FakeClient()
    provider = OllamaProvider("qwen3", client=client)
    with pytest.raises(ContextOverflowError, match="OLLAMA_CONTEXT_LENGTH") as raised:
        await provider.complete(system="x" * 20_000, messages=[Message.user("hi")], tools=[SPEC])
    assert raised.value.attempted >= 4096
    assert client.requests == [], "nothing was sent for Ollama to cut"


async def test_a_model_this_server_does_not_run_is_not_checked(server: Server) -> None:
    server.load_as = 0  # a -cloud model through the local server never shows in /api/ps
    client = FakeClient()
    provider = OllamaProvider("gpt-oss:120b-cloud", client=client)
    await provider.complete(system="x" * 20_000, messages=[Message.user("hi")])
    assert provider.served_window == 0
    assert len(client.requests) == 1


# -- thinking ---------------------------------------------------------------------------


async def test_levels_are_sent_as_reasoning_effort(server: Server) -> None:
    server.loaded = {"gpt-oss:20b": 131072}
    client = FakeClient()
    provider = OllamaProvider("gpt-oss:20b", client=client, thinking="off")
    await provider.complete(system="", messages=[Message.user("hi")])
    assert client.request["reasoning_effort"] == "none"
    provider.set_thinking("high")
    await provider.complete(system="", messages=[Message.user("hi")])
    assert client.request["reasoning_effort"] == "high"


async def test_a_model_that_cannot_think_is_asked_once_more_without(server: Server) -> None:
    server.loaded = {"llava:latest": 8192}
    client = FakeClient(
        raises=[RuntimeError('Error code: 400 - "llava" does not support thinking')]
    )
    provider = OllamaProvider("llava", client=client)
    reply = await provider.complete(system="", messages=[Message.user("hi")])
    assert reply.text == "ok"
    assert "reasoning_effort" in client.requests[0]
    assert "reasoning_effort" not in client.requests[1]
    assert provider.thinking_levels == ()


def test_the_menu_comes_from_what_the_model_says() -> None:
    assert plugin.thinking_levels_of(GPT_OSS_SHOW) == ("low", "medium", "high")
    assert plugin.thinking_levels_of(QWEN_SHOW) == plugin.LEVELS
    assert plugin.thinking_levels_of(LLAVA_SHOW) == ()
    switch = {"capabilities": ["thinking"], "thinking": {"values": [True, False]}}
    assert plugin.thinking_levels_of(switch) == ("off", "high")
    assert plugin.thinking_levels_of({}) is None


# -- the listing ------------------------------------------------------------------------


async def test_the_listing_reports_what_a_request_gets(server: Server) -> None:
    server.loaded = {"qwen3:latest": 65536}
    provider = OllamaProvider.bind(context_length=32768)(client=FakeClient())
    entries = {e.id: e for e in await provider.list_models() or ()}

    assert set(entries) == {"qwen3:latest", "gpt-oss:20b", "llava:latest"}, "no embedders"
    assert entries["qwen3:latest"].context_window == 65536, "loaded, not trained"
    assert entries["gpt-oss:20b"].context_window == 32768, "not loaded: the setting"
    assert entries["gpt-oss:20b"].thinking_levels == ("low", "medium", "high")
    assert entries["llava:latest"].modalities == ("text", "image")
    assert entries["qwen3:latest"].cost == plugin.FREE
    assert entries["qwen3:latest"].released == "2026-05-01"
    assert not any(c[1].endswith("/api/generate") for c in server.calls), "a listing loads nothing"


async def test_the_cloud_listing_reports_the_models_own_window(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OLLAMA_API_KEY", "k")
    cloud = OllamaCloudProvider(client=FakeClient())
    entries = {e.id: e for e in await cloud.list_models() or ()}
    assert entries["gpt-oss:20b"].context_window == 131072
    assert entries["gpt-oss:20b"].cost is None, "a subscription is not a price per token"


async def test_one_pulled_model_is_the_answer(server: Server) -> None:
    server.shows = {"qwen3:latest": QWEN_SHOW}
    server.loaded = {"qwen3:latest": 65536}
    provider = OllamaProvider(client=FakeClient())
    await provider.complete(system="", messages=[Message.user("hi")])
    assert provider.model == "qwen3:latest"

    server.shows = {"qwen3:latest": QWEN_SHOW, "llava:latest": LLAVA_SHOW}
    with pytest.raises(ConfigError, match="llava:latest, qwen3:latest"):
        await OllamaProvider(client=FakeClient()).complete(system="", messages=[Message.user("hi")])


# -- failures ---------------------------------------------------------------------------


async def test_failures_say_what_to_do(server: Server) -> None:
    server.loaded = {"qwen3:latest": 65536}

    class APIConnectionError(Exception):
        pass

    for raised, expected in [
        (APIConnectionError("Connection error."), "ollama serve"),
        (RuntimeError('404 - model "qwen3" not found, try pulling it first'), "ollama pull"),
        (RuntimeError('400 - "qwen3" does not support tools'), "tool support"),
    ]:
        provider = OllamaProvider("qwen3", client=FakeClient(raises=[raised]))
        with pytest.raises(ProviderError, match=expected) as caught:
            await provider.complete(system="", messages=[Message.user("hi")])
        assert caught.value.__cause__ is raised
