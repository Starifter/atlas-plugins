"""The llama.cpp plugin, driven the way Atlas drives it: a fake client that
records the request, and replies shaped the way `llama-server` shapes them.

Run from a checkout of Atlas (`uv run pytest path/to/llama-cpp/tests`), which
supplies pytest-asyncio in auto mode and the `atlas.sdk` the plugin imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from llama_cpp_testing import SPEC, FakeClient, LlamaCppProvider, message, plugin

from atlas.sdk.plugin_entry import PluginContext
from atlas.sdk.provider import (
    ContextOverflowError,
    ImageBlock,
    Message,
    Sampling,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from atlas.sdk.runtime import ConfigError, ProviderError


@pytest.fixture
def props(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """What `GET /props` answers, and a record of what was asked."""
    state: dict[str, Any] = {"answer": {}, "urls": []}

    async def fetch(url: str) -> Mapping[str, Any]:
        state["urls"].append(url)
        if isinstance(state["answer"], Exception):
            raise state["answer"]
        return state["answer"]

    monkeypatch.setattr(plugin, "fetch_json", fetch)
    return state


# -- construction and capabilities ------------------------------------------------


def test_the_class_declares_what_the_manifest_says() -> None:
    assert LlamaCppProvider.local is True
    assert LlamaCppProvider.streaming is True
    assert LlamaCppProvider.sampling is True
    assert LlamaCppProvider.api_key_env_vars == ("LLAMA_SERVER_API_KEY",)
    assert LlamaCppProvider.levels_for("anything") == ("off", "low", "medium", "high", "max")
    provider = LlamaCppProvider("x", client=FakeClient())
    assert provider.cache_ttl_seconds == 0


def test_no_key_is_the_ordinary_case(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("openai")
    # A user's OpenAI key is never what a local server is sent.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-the-users-openai-key")
    monkeypatch.delenv("LLAMA_SERVER_API_KEY", raising=False)
    provider = LlamaCppProvider("x")
    assert provider._client.api_key == plugin.PLACEHOLDER_KEY
    assert provider.base_url_in_use == "http://127.0.0.1:8080/v1"
    provider = LlamaCppProvider("x", api_key="secret", base_url="http://box.lan:9000/v1/")
    assert provider._client.api_key == "secret"
    assert provider.base_url_in_use == "http://box.lan:9000/v1"


def test_a_base_url_with_credentials_or_a_bad_scheme_is_refused() -> None:
    with pytest.raises(ConfigError, match="username or password"):
        LlamaCppProvider("x", base_url="http://user:pw@127.0.0.1:8080/v1", client=FakeClient())
    with pytest.raises(ConfigError, match="http"):
        LlamaCppProvider("x", base_url="127.0.0.1:8080", client=FakeClient())


def test_register_binds_the_settings_and_installs_an_embedder() -> None:
    ctx = PluginContext(
        plugin="llama-cpp",
        settings={
            "base_url": "http://10.0.0.5:8080/v1",
            "embedding_url": "http://10.0.0.5:8081/v1",
            "embedding_dimensions": 3,
        },
        providers=True,
    )
    plugin.LlamaCppPlugin().register(ctx)
    from atlas.providers import provider_class
    from atlas.providers.embedding import known_embedders

    cls = provider_class("llama-cpp")
    assert cls is not None and issubclass(cls, LlamaCppProvider)
    assert cls.base_url == "http://10.0.0.5:8080/v1"
    assert cls.local is True
    assert ctx.providers == ["llama-cpp"]
    assert ctx.embedders == ["llama-cpp"]
    factory = known_embedders()["llama-cpp"]
    embedder = factory(client=FakeClient())
    assert embedder.dimensions == 3


# -- the model ------------------------------------------------------------------------


async def test_an_empty_model_is_the_one_the_server_serves() -> None:
    client = FakeClient(message(content="hi"))
    provider = LlamaCppProvider(client=client)
    reply = await provider.complete(system="", messages=[Message.user("hi")])
    assert reply.content == (TextBlock("hi"),)
    assert client.request["model"] == "ggml-org/Qwen3-8B-GGUF"
    assert provider.model == "ggml-org/Qwen3-8B-GGUF"
    await provider.complete(system="", messages=[Message.user("again")])
    assert client.listings == 1, "asked once, then remembered"


async def test_a_router_with_several_models_needs_a_name() -> None:
    client = FakeClient(message(content="hi"), models=[{"id": "a.gguf"}, {"id": "b.gguf"}])
    with pytest.raises(ConfigError, match=r"a\.gguf, b\.gguf"):
        await LlamaCppProvider(client=client).complete(system="", messages=[Message.user("hi")])
    with pytest.raises(ProviderError, match="lists no model"):
        await LlamaCppProvider(client=FakeClient(models=[])).complete(
            system="", messages=[Message.user("hi")]
        )


# -- the request ---------------------------------------------------------------------


async def test_the_request_carries_the_thinking_switch_both_ways() -> None:
    client = FakeClient(message(content="on it"))
    provider = LlamaCppProvider("m", thinking="high", client=client)
    await provider.complete(system="Be brief.", messages=[Message.user("hi")], tools=[SPEC])
    request = client.request
    assert request["model"] == "m"
    assert request["reasoning_effort"] == "high"
    assert request["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}
    assert request["messages"][0] == {"role": "system", "content": "Be brief."}
    assert request["tools"][0]["function"]["name"] == "echo"
    assert "cache_control" not in str(request)

    provider.set_thinking("off")
    await provider.complete(system="", messages=[Message.user("hi")])
    assert client.request["reasoning_effort"] == "none"
    assert client.request["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


async def test_the_system_prompts_cache_marker_is_stripped() -> None:
    from atlas.prompting import CACHE_BOUNDARY

    client = FakeClient(message(content="ok"))
    await LlamaCppProvider("m", client=client).complete(
        system=f"stable{CACHE_BOUNDARY}volatile", messages=[Message.user("hi")]
    )
    content = client.request["messages"][0]["content"]
    assert CACHE_BOUNDARY not in content
    assert content.startswith("stable") and content.endswith("volatile")


async def test_sampling_is_forwarded_and_capped() -> None:
    client = FakeClient(message(content="ok"))
    provider = LlamaCppProvider("m", max_tokens=1000, client=client)
    await provider.complete(
        system="",
        messages=[Message.user("hi")],
        sampling=Sampling(temperature=0.2, seed=7, stop=("END",), max_tokens=5000),
    )
    request = client.request
    assert request["temperature"] == 0.2
    assert request["seed"] == 7
    assert request["stop"] == ["END"]
    assert request["max_tokens"] == 1000


async def test_tool_results_fan_out_and_pictures_ride_the_user_message() -> None:
    client = FakeClient(message(content="ok"))
    provider = LlamaCppProvider("m", client=client)
    call = ToolUseBlock(id="c1", name="echo", arguments={"x": 1})
    turn = Message(role="assistant", content=(TextBlock("calling"), call))
    result = Message(
        role="user",
        content=(
            ToolResultBlock(
                tool_use_id="c1",
                content="done",
                images=(
                    ImageBlock(
                        media_type="image/png",
                        sha256="ab" * 32,
                        size=4,
                        source="test",
                        data=b"\x89PNG",
                    ),
                ),
                trailer="[image attached]",
            ),
            TextBlock("next"),
        ),
    )
    await provider.complete(system="", messages=[Message.user("hi"), turn, result])
    messages = client.request["messages"]
    assert messages[1] == {
        "role": "assistant",
        "content": "calling",
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"x": 1}'}}
        ],
    }
    assert messages[2] == {"role": "tool", "tool_call_id": "c1", "content": "done"}
    parts = messages[3]["content"]
    assert parts[0]["type"] == "image_url"
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[1] == {"type": "text", "text": "[image attached]"}
    assert parts[2] == {"type": "text", "text": "next"}


async def test_a_reply_this_provider_made_is_replayed_without_its_reasoning() -> None:
    client = FakeClient(
        message(
            content="42",
            reasoning_content="six times seven",
            tool_calls=[
                {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}
            ],
        )
    )
    provider = LlamaCppProvider("m", client=client)
    reply = await provider.complete(system="", messages=[Message.user("hi")])
    assert reply.content == (
        ThinkingBlock("six times seven"),
        TextBlock("42"),
        ToolUseBlock(id="c1", name="echo", arguments={}),
    )
    assert reply.usage is not None
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (120, 7)
    assert reply.usage.cache_read_tokens == 0
    await provider.complete(system="", messages=[Message.user("hi"), reply])
    replayed = client.request["messages"][1]
    assert replayed["content"] == "42"
    assert "reasoning_content" not in replayed
    assert "reasoning" not in replayed


async def test_a_stream_is_put_back_together() -> None:
    def chunk(**delta: Any) -> dict[str, Any]:
        return {"choices": [{"delta": delta}]}

    client = FakeClient(
        chunks=[
            chunk(reasoning_content="hmm"),
            chunk(content="hel"),
            chunk(content="lo"),
            chunk(tool_calls=[{"index": 0, "id": "c1", "function": {"name": "echo"}}]),
            chunk(tool_calls=[{"index": 0, "function": {"arguments": '{"x":'}}]),
            chunk(tool_calls=[{"index": 0, "function": {"arguments": " 1}"}}]),
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3}},
        ]
    )
    seen: list[tuple[str, str]] = []
    reply = await LlamaCppProvider("m", client=client).complete(
        system="", messages=[Message.user("hi")], on_delta=lambda d: seen.append((d.kind, d.text))
    )
    assert client.request["stream"] is True
    assert client.request["stream_options"] == {"include_usage": True}
    assert seen == [("thinking", "hmm"), ("text", "hel"), ("text", "lo")]
    assert reply.content == (
        ThinkingBlock("hmm"),
        TextBlock("hello"),
        ToolUseBlock(id="c1", name="echo", arguments={"x": 1}),
    )
    assert reply.usage is not None and reply.usage.input_tokens == 10
    assert reply.raw["tool_calls"][0]["function"]["arguments"] == '{"x": 1}'


# -- failures ------------------------------------------------------------------------


async def test_failures_are_named_for_what_they_are() -> None:
    class APIConnectionError(Exception):
        pass

    provider = LlamaCppProvider(
        "m", client=FakeClient(raises=APIConnectionError("Connection error."))
    )
    with pytest.raises(ProviderError, match=r"no llama.cpp server answered"):
        await provider.complete(system="", messages=[Message.user("hi")])

    provider = LlamaCppProvider(
        "m", client=FakeClient(raises=RuntimeError("Error code: 503 - Loading model"))
    )
    with pytest.raises(ProviderError, match="still loading"):
        await provider.complete(system="", messages=[Message.user("hi")])

    provider = LlamaCppProvider(
        "m", client=FakeClient(raises=RuntimeError("Error code: 401 - Invalid API Key"))
    )
    with pytest.raises(ProviderError, match="LLAMA_SERVER_API_KEY"):
        await provider.complete(system="", messages=[Message.user("hi")])

    provider = LlamaCppProvider(
        "m",
        client=FakeClient(
            raises=RuntimeError(
                "Error code: 400 - the request exceeds the available context size, "
                "try increasing it"
            )
        ),
    )
    with pytest.raises(ContextOverflowError):
        await provider.complete(system="", messages=[Message.user("hi")])


# -- the listing ---------------------------------------------------------------------


async def test_the_listing_reads_the_servers_context_and_projector(props: dict[str, Any]) -> None:
    props["answer"] = {
        "default_generation_settings": {"n_ctx": 32768},
        "modalities": {"vision": True, "audio": False},
        "model_path": "/models/qwen3.gguf",
    }
    provider = LlamaCppProvider(client=FakeClient(), base_url="http://box:8080/v1")
    entries = await provider.list_models()
    assert props["urls"] == ["http://box:8080/props?autoload=false"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.id == "ggml-org/Qwen3-8B-GGUF"
    assert entry.context_window == 32768, "the server's -c, not the model's ceiling"
    assert entry.modalities == ("text", "image")
    assert entry.released == "2024-12-25"
    assert entry.cost is not None and entry.cost.input == 0.0 and entry.cost.output == 0.0


async def test_the_listing_stands_without_props(props: dict[str, Any]) -> None:
    props["answer"] = ProviderError("HTTP 404")
    entries = await LlamaCppProvider(client=FakeClient()).list_models()
    assert len(entries) == 1
    assert entries[0].context_window == 40960, "the trained context is all there is"
    assert entries[0].modalities == (), "nothing said, nothing guessed"


def test_the_root_is_found_under_a_prefix() -> None:
    assert plugin._root_of("http://127.0.0.1:8080/v1") == "http://127.0.0.1:8080"
    assert plugin._root_of("https://box.lan/llama/v1/") == "https://box.lan/llama"
    assert plugin._root_of("http://box:8080") == "http://box:8080"


# -- the embedder --------------------------------------------------------------------
