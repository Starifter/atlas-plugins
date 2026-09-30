"""The OpenRouter plugin, driven the way Atlas drives it: a fake client that
records the request, and replies shaped the way OpenRouter shapes them.

Run from a checkout of Atlas (`uv run pytest path/to/openrouter/tests`), which
supplies pytest-asyncio in auto mode and the `atlas.sdk` the plugin imports.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from atlas.prompting import CACHE_BOUNDARY  # the marker itself is not on the SDK surface
from atlas.sdk.oauth import LoginContext
from atlas.sdk.plugin_entry import PluginContext
from atlas.sdk.provider import Message, Sampling, TextBlock, ToolResultBlock, ToolUseBlock
from atlas.sdk.runtime import ConfigError, ProviderError
from atlas.sdk.tool_plugin import ToolSpec

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "atlas_plugin_openrouter", HERE.parent / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()
OpenRouterProvider = plugin.OpenRouterProvider

SPEC = ToolSpec(name="echo", description="Echo.", parameters={"type": "object", "properties": {}})


class FakeClient:
    """Records the request; answers with a whole reply, or a stream of chunks."""

    def __init__(
        self, message: Any = None, *, chunks: list[Any] = (), models: list[Any] = ()
    ) -> None:
        self.message = message
        self.chunks = list(chunks)
        self.models_data = list(models)
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=self._list)

    @property
    def request(self) -> dict[str, Any]:
        return self.requests[-1]

    async def _create(self, **request: Any) -> Any:
        self.requests.append(request)
        if request.get("stream"):
            return self._stream()
        return SimpleNamespace(
            choices=[SimpleNamespace(message=self.message)],
            usage={
                "prompt_tokens": 120,
                "completion_tokens": 7,
                "prompt_tokens_details": {"cached_tokens": 100},
            },
        )

    async def _stream(self) -> Any:
        for chunk in self.chunks:
            yield chunk

    async def _list(self) -> Any:
        for item in self.models_data:
            yield item


def message(**fields: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": None, **fields}


# -- construction and capabilities ------------------------------------------------


async def test_a_model_is_required_at_the_first_request_and_not_for_a_listing() -> None:
    provider = OpenRouterProvider(client=FakeClient(message(content="hi")))
    assert await provider.list_models() == []
    with pytest.raises(ConfigError, match="explicit model"):
        await provider.complete(system="", messages=[Message.user("hi")])


def test_the_menu_is_openrouters_and_caching_is_the_vendors() -> None:
    assert OpenRouterProvider.levels_for("meta-llama/llama-4-maverick") == (
        "off",
        "low",
        "medium",
        "high",
        "max",
    )
    assert OpenRouterProvider.cache_ttls_for("anthropic/claude-opus-5") == ("5m", "1h")
    assert OpenRouterProvider.cache_ttls_for("google/gemini-3.1-pro-preview") == ("5m",)
    assert OpenRouterProvider.cache_ttls_for("openai/gpt-5.5") == ()
    provider = OpenRouterProvider("openai/gpt-5.5", client=FakeClient())
    assert provider.cache_ttl_seconds == 0


def test_register_binds_the_settings_onto_a_class() -> None:
    ctx = PluginContext(
        plugin="openrouter",
        settings={
            "provider_order": ["anthropic"],
            "allow_fallbacks": False,
            "data_collection": "deny",
        },
        providers=True,
    )
    plugin.OpenRouterPlugin().register(ctx)
    from atlas.providers import provider_class

    cls = provider_class("openrouter")
    assert cls is not None and issubclass(cls, OpenRouterProvider)
    assert cls.routing == {
        "order": ["anthropic"],
        "allow_fallbacks": False,
        "data_collection": "deny",
    }
    assert cls.levels_for("anthropic/claude-opus-5") == ("off", "low", "medium", "high", "max")


# -- the request -----------------------------------------------------------------


async def test_the_request_carries_reasoning_and_routing_in_extra_body() -> None:
    client = FakeClient(message(content="on it"))
    configured = OpenRouterProvider.configured(
        {"provider_order": ["anthropic"], "data_collection": "deny"}
    )
    provider = configured(
        "anthropic/claude-opus-5", client=client, thinking="medium", cache_ttl="none"
    )

    reply = await provider.complete(
        system="be brief", messages=[Message.user("hello")], tools=[SPEC]
    )

    assert client.request["model"] == "anthropic/claude-opus-5"
    assert client.request["messages"][0] == {"role": "system", "content": "be brief"}
    assert client.request["extra_body"] == {
        "reasoning": {"effort": "medium"},
        "provider": {"order": ["anthropic"], "data_collection": "deny"},
    }
    assert client.request["tools"][0]["function"]["name"] == "echo"
    assert reply.text == "on it"
    assert reply.usage is not None and reply.usage.cache_read_tokens == 100
    assert reply.usage.input_tokens == 20


async def test_off_is_enabled_false_and_max_is_max() -> None:
    client = FakeClient(message(content="ok"))
    provider = OpenRouterProvider("openai/gpt-5.5", client=client, thinking="off")
    await provider.complete(system="", messages=[Message.user("hi")])
    assert client.request["extra_body"]["reasoning"] == {"enabled": False}

    provider.set_thinking("max")
    await provider.complete(system="", messages=[Message.user("hi")])
    assert client.request["extra_body"]["reasoning"] == {"effort": "max"}


async def test_a_vendor_that_refuses_off_is_asked_once_more_at_low() -> None:
    class Refusing(FakeClient):
        async def _create(self, **request: Any) -> Any:
            self.requests.append(request)
            if request["extra_body"]["reasoning"] == {"enabled": False}:
                raise RuntimeError(
                    "400: reasoning is mandatory for this model and cannot be disabled"
                )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message(content="fine"))], usage=None
            )

    client = Refusing()
    provider = OpenRouterProvider("deepseek/deepseek-r1", client=client, thinking="off")
    reply = await provider.complete(system="", messages=[Message.user("hi")])

    assert reply.text == "fine"
    assert [r["extra_body"]["reasoning"] for r in client.requests] == [
        {"enabled": False},
        {"effort": "low"},
    ]
    assert "off" not in provider.thinking_levels


async def test_anthropic_gets_breakpoints_and_the_rest_do_not() -> None:
    client = FakeClient(message(content="ok"))
    system = f"the rules{CACHE_BOUNDARY}date=2026-09-17"
    history = [
        Message.user("first"),
        Message(role="assistant", content=(TextBlock("reply"),)),
        Message.user("second"),
    ]

    await OpenRouterProvider("anthropic/claude-opus-5", client=client, cache_ttl="1h").complete(
        system=system, messages=history
    )
    sent = client.request["messages"]
    assert sent[0] == {
        "role": "system",
        "content": [
            {
                "type": "text",
                "text": "the rules",
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            },
            {"type": "text", "text": "date=2026-09-17"},
        ],
    }
    assert sent[1]["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert sent[3]["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    await OpenRouterProvider("openai/gpt-5.5", client=client, cache_ttl="1h").complete(
        system=system, messages=history
    )
    sent = client.request["messages"]
    assert sent[0] == {"role": "system", "content": "the rules\n\ndate=2026-09-17"}
    assert "cache_control" not in json.dumps(sent)


async def test_a_sampling_is_forwarded_and_capped() -> None:
    client = FakeClient(message(content="ok"))
    provider = OpenRouterProvider("openai/gpt-5.5", client=client, max_tokens=1000)
    await provider.complete(
        system="",
        messages=[Message.user("hi")],
        sampling=Sampling(temperature=0.2, max_tokens=5000, stop=("END",), seed=7),
    )
    assert client.request["temperature"] == 0.2
    assert client.request["max_tokens"] == 1000
    assert client.request["stop"] == ["END"]
    assert client.request["seed"] == 7


# -- the reply, and replaying it ------------------------------------------------------


async def test_tool_calls_and_reasoning_details_come_back_and_are_replayed() -> None:
    details = [
        {
            "type": "reasoning.text",
            "text": "think",
            "signature": "sig",
            "index": 0,
            "format": "anthropic-claude-v1",
        }
    ]
    client = FakeClient(
        message(
            content="",
            reasoning="think",
            reasoning_details=details,
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"text": "hi"}'},
                }
            ],
        )
    )
    provider = OpenRouterProvider("anthropic/claude-opus-5", client=client, cache_ttl="none")
    reply = await provider.complete(system="", messages=[Message.user("go")], tools=[SPEC])

    assert reply.thinking == "think"
    assert reply.tool_calls == (ToolUseBlock(id="call_1", name="echo", arguments={"text": "hi"}),)
    assert reply.raw == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "echo", "arguments": '{"text": "hi"}'},
            }
        ],
        "reasoning_details": details,
    }

    followup = Message(role="user", content=(ToolResultBlock(tool_use_id="call_1", content="hi"),))
    await provider.complete(system="", messages=[Message.user("go"), reply, followup], tools=[SPEC])
    sent = client.request["messages"]
    assert sent[1] == reply.raw
    assert sent[2] == {"role": "tool", "tool_call_id": "call_1", "content": "hi"}


async def test_a_stream_is_assembled_including_reasoning_details_by_index() -> None:
    def chunk(**delta: Any) -> dict[str, Any]:
        return {"choices": [{"delta": delta}]}

    chunks = [
        chunk(
            reasoning="th", reasoning_details=[{"type": "reasoning.text", "text": "th", "index": 0}]
        ),
        chunk(
            reasoning="ink",
            reasoning_details=[
                {"type": "reasoning.text", "text": "ink", "index": 0, "signature": "sig"}
            ],
        ),
        chunk(content="hel"),
        chunk(content="lo"),
        chunk(
            tool_calls=[
                {"index": 0, "id": "call_1", "function": {"name": "echo", "arguments": '{"te'}}
            ]
        ),
        chunk(tool_calls=[{"index": 0, "function": {"arguments": 'xt": "hi"}'}}]),
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 50,
                "completion_tokens": 9,
                "prompt_tokens_details": {"cached_tokens": 40},
            },
        },
    ]
    client = FakeClient(chunks=chunks)
    provider = OpenRouterProvider("anthropic/claude-opus-5", client=client)
    seen: list[tuple[str, str]] = []

    reply = await provider.complete(
        system="",
        messages=[Message.user("go")],
        tools=[SPEC],
        on_delta=lambda d: seen.append((d.kind, d.text)),
    )

    assert client.request["stream"] is True
    assert seen == [("thinking", "th"), ("thinking", "ink"), ("text", "hel"), ("text", "lo")]
    assert reply.thinking == "think"
    assert reply.text == "hello"
    assert reply.tool_calls == (ToolUseBlock(id="call_1", name="echo", arguments={"text": "hi"}),)
    assert reply.raw["reasoning_details"] == [
        {"type": "reasoning.text", "text": "think", "index": 0, "signature": "sig"}
    ]
    assert reply.usage is not None and (
        reply.usage.input_tokens,
        reply.usage.cache_read_tokens,
    ) == (10, 40)


async def test_unparseable_arguments_are_a_provider_error() -> None:
    client = FakeClient(
        message(
            content="",
            tool_calls=[
                {"id": "c", "type": "function", "function": {"name": "echo", "arguments": "{nope"}}
            ],
        )
    )
    with pytest.raises(ProviderError, match="unparseable"):
        await OpenRouterProvider("openai/gpt-5.5", client=client).complete(
            system="", messages=[Message.user("go")]
        )


async def test_a_vendor_failure_is_ours_at_the_seam() -> None:
    class Exploding(FakeClient):
        async def _create(self, **request: Any) -> Any:
            raise RuntimeError("connection reset")

    with pytest.raises(ProviderError, match="OpenRouter request failed: connection reset"):
        await OpenRouterProvider("openai/gpt-5.5", client=Exploding()).complete(
            system="", messages=[Message.user("hi")]
        )


# -- the listing ----------------------------------------------------------------------


async def test_the_listing_keeps_numbers_prices_tiers_and_modalities() -> None:
    rows = json.loads((HERE / "models.json").read_text(encoding="utf-8"))["data"]
    provider = OpenRouterProvider("openai/gpt-5.5", client=FakeClient(models=rows))

    entries = {entry.id: entry for entry in await provider.list_models() or ()}

    opus = entries["anthropic/claude-opus-5"]
    assert opus.context_window > 0 and opus.max_output > 0
    assert opus.cost is not None and opus.cost.input == 5 and opus.cost.output == 25
    assert opus.cost.cache_read == 0.5 and opus.cost.cache_write == 6.25
    assert opus.modalities == ("text", "image", "document")
    assert opus.released.startswith("20")

    sol = entries["openai/gpt-5.6-sol"]
    assert sol.cost is not None and len(sol.cost.tiers) == 2
    low, high = sol.cost.tiers
    assert (low.from_tokens, low.to_tokens, low.input) == (0, 272000, 2)
    assert (high.from_tokens, high.to_tokens, high.input, high.output) == (272000, None, 4, 15)

    free = entries["google/gemma-4-31b-it:free"]
    assert free.cost is not None and free.cost.input == 0 and free.cost.output == 0

    auto = entries["openrouter/auto"]
    assert auto.cost is None  # `-1`: a price OpenRouter will not quote is unknown, never zero

    assert not any(entry.description for entry in entries.values())


# -- the browser sign-in ------------------------------------------------------------


class Person:
    """A `LoginContext` with a scripted person behind it."""

    def __init__(self, *, opens: bool, pastes: str = "") -> None:
        self.opens = opens
        self.pastes = pastes
        self.said: list[str] = []
        self.opened: list[str] = []
        self.ctx = LoginContext(say=self.said.append, ask_secret=self._ask, open_browser=self._open)

    def _ask(self, prompt: str) -> str:
        return self.pastes

    def _open(self, url: str) -> bool:
        self.opened.append(url)
        return self.opens


def exchange(expect_code: str) -> tuple[list[dict[str, Any]], Any]:
    posts: list[dict[str, Any]] = []

    def post(url: str, body: Mapping[str, Any]) -> tuple[int, Mapping[str, Any]]:
        posts.append({"url": url, **body})
        if body.get("code") != expect_code:
            return 403, {"error": {"message": "bad code"}}
        return 200, {"key": "sk-or-v1-minted"}

    return posts, post


def test_the_login_registers_beside_the_provider() -> None:
    ctx = PluginContext(plugin="openrouter", providers=True)
    plugin.OpenRouterPlugin().register(ctx)
    assert ctx.providers == ["openrouter"]
    assert ctx.logins == ["openrouter"]


def test_the_callback_lands_the_code_and_the_exchange_mints_a_key() -> None:
    import threading
    import urllib.request

    person = Person(opens=True)
    posts, post = exchange("abc123")
    listeners: list[Any] = []

    def listen(state: str) -> Any:
        listener = plugin._Callback(state)
        listeners.append(listener)

        def redirect() -> None:
            # The browser lands on the callback with the code, as OpenRouter sends
            # it: `callback_url` echoed verbatim - `state` inside - plus `code`.
            with urllib.request.urlopen(listener.url + "&code=abc123", timeout=5) as response:
                assert response.status == 200

        threading.Timer(0.2, redirect).start()
        return listener

    tokens = plugin._run_login(person.ctx, post=post, listen=listen)

    assert tokens.access == "sk-or-v1-minted"
    assert tokens.refresh == "" and tokens.token_url == ""  # a key: nothing to refresh
    url = person.opened[0]
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert url.startswith(plugin.AUTH_URL + "?")
    assert query["callback_url"] == listeners[0].url
    assert "state=" + listeners[0].state in query["callback_url"]
    assert query["code_challenge_method"] == "S256"
    # The exchange carries the code and the verifier the challenge was made from.
    [sent] = posts
    assert sent["url"] == plugin.KEYS_URL and sent["code"] == "abc123"
    digest = hashlib.sha256(sent["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == query["code_challenge"]
    # Nothing said carries the code, the verifier or the key.
    for line in person.said:
        for secret_word in ("abc123", sent["code_verifier"], "sk-or-v1-minted"):
            assert secret_word not in line


def test_a_pasted_url_with_another_state_is_refused() -> None:
    from atlas.sdk.runtime import CredentialError

    person = Person(opens=False, pastes="http://127.0.0.1:1/callback?state=theirs&code=x")
    _, post = exchange("x")
    with pytest.raises(CredentialError, match="state mismatch"):
        plugin._run_login(person.ctx, post=post, listen=None)


def test_the_listener_ignores_a_redirect_with_the_wrong_state_and_keeps_waiting() -> None:
    import threading
    import urllib.error
    import urllib.request

    listener = plugin._Callback("ours")
    root = listener.url.split("?", 1)[0]

    def redirects() -> None:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(root + "?state=theirs&code=stray", timeout=5)
        assert caught.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(root + "?code=stray", timeout=5)
        assert caught.value.code == 400
        urllib.request.urlopen(listener.url + "&code=real", timeout=5).close()

    threading.Timer(0.2, redirects).start()
    assert listener.wait(10) == "real"


def test_without_a_browser_the_landed_url_is_pasted_back() -> None:
    person = Person(opens=False, pastes="bare-code-pasted9")
    posts, post = exchange("bare-code-pasted9")

    tokens = plugin._run_login(person.ctx, post=post, listen=None)

    assert tokens.access == "sk-or-v1-minted"
    assert posts[0]["code"] == "bare-code-pasted9"
    assert any("open this in a browser" in line for line in person.said)


def test_a_refused_exchange_is_a_credential_error_with_the_vendors_words() -> None:
    from atlas.sdk.runtime import CredentialError

    person = Person(opens=False, pastes="wrong")
    _, post = exchange("right")
    with pytest.raises(CredentialError, match="HTTP 403 bad code"):
        plugin._run_login(person.ctx, post=post, listen=None)


def test_the_listener_answers_only_its_path_and_only_once() -> None:
    import urllib.error
    import urllib.request

    listener = plugin._Callback("s")
    root = listener.url.split("?", 1)[0].rsplit("/", 1)[0]

    def redirect() -> None:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(root + "/elsewhere", timeout=5)
        assert caught.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(listener.url, timeout=5)
        assert caught.value.code == 400
        urllib.request.urlopen(listener.url + "&code=one", timeout=5).close()

    import threading

    threading.Timer(0.2, redirect).start()
    assert listener.wait(10) == "one"
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(listener.url + "&code=two", timeout=2)


def test_the_listener_gives_up_at_the_deadline() -> None:
    listener = plugin._Callback("s")
    assert listener.wait(0.3) == ""
