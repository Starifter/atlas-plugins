"""The xAI plugin, driven the way Atlas drives it: a fake `openai` client that
records each request and answers the way xAI's API documents it.

Run from a checkout of Atlas (`uv run pytest path/to/xai/tests`).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from atlas.sdk.provider import Message

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("atlas_plugin_xai", HERE.parent / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()
XAIProvider = plugin.XAIProvider


class FakeClient:
    def __init__(self, message: Any = None, *, models: list[Any] = ()) -> None:
        self.message = message
        self.models_data = list(models)
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=self._list)

    @property
    def request(self) -> dict[str, Any]:
        return self.requests[-1]

    async def _create(self, **request: Any) -> Any:
        self.requests.append(request)
        return SimpleNamespace(choices=[SimpleNamespace(message=self.message)], usage=None)

    async def _list(self) -> Any:
        for row in self.models_data:
            yield row


def reply(**fields: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": None, **fields}


async def test_a_reasoning_model_takes_an_effort_and_max_is_xhigh() -> None:
    client = FakeClient(reply(content="hi", reasoning_content="thinking"))
    turn = await XAIProvider("grok-4.7", client=client, thinking="high").complete(
        system="", messages=[Message.user("hi")]
    )
    assert client.request["reasoning_effort"] == "high"
    assert "max_completion_tokens" in client.request and "max_tokens" not in client.request
    assert turn.thinking == "thinking"
    assert XAIProvider.levels_for("grok-4.7") == ("low", "medium", "high")


async def test_there_is_no_off_and_a_non_reasoning_model_is_sent_nothing() -> None:
    assert "off" not in XAIProvider.thinking_levels
    client = FakeClient(reply(content="hi"))
    provider = XAIProvider("grok-4.20-0309-non-reasoning", client=client)
    assert provider.thinking_levels == ()
    await provider.complete(system="", messages=[Message.user("hi")])
    assert "reasoning_effort" not in client.request


async def test_the_listing_prices_in_dollars_with_a_long_context_tier() -> None:
    row = {
        "id": "grok-4.7",
        "created": 1754400000,
        "context_length": 2000000,
        "prompt_text_token_price": 20000,
        "cached_prompt_text_token_price": 5000,
        "completion_text_token_price": 150000,
        "prompt_text_token_price_long_context": 40000,
        "completion_text_token_price_long_context": 300000,
        "long_context_threshold": 128000,
        "capabilities": {"reasoning_effort": ["low", "medium", "high", "xhigh"]},
    }
    plain = {
        "id": "grok-4.20-0309-non-reasoning",
        "context_length": 256000,
        "capabilities": {"reasoning_effort": []},
    }
    entries = await XAIProvider(client=FakeClient(models=[row, plain])).list_models()
    assert entries is not None
    grok, other = entries
    assert grok.context_window == 2000000 and grok.released == "2025-08-05"
    assert grok.thinking_levels == ("low", "medium", "high", "max")
    assert other.thinking_levels == ()
    cost = grok.cost
    assert cost is not None
    assert (cost.input, cost.output, cost.cache_read) == (2.0, 15.0, 0.5)
    assert [(t.from_tokens, t.to_tokens, t.input, t.output) for t in cost.tiers] == [
        (0, 128000, 2.0, 15.0),
        (128000, None, 4.0, 30.0),
    ]
    assert other.cost is None, "a price nobody quoted is unknown, never zero"


# -- the transcriber (`media.md` §8.3) ------------------------------------------------


def _reading(**kw: Any) -> Any:
    from atlas.sdk.media import Reading

    fields: dict[str, Any] = {
        "kind": "audio",
        "data": b"ID3" + b"\x00" * 64,
        "media_type": "audio/mpeg",
        "name": "",
        "max_chars": 1000,
    }
    fields.update(kw)
    return Reading(**fields)


def _fake_post(
    monkeypatch: Any, status: int = 200, body: bytes = b'{"text": "hi"}'
) -> dict[str, Any]:
    from atlas.sdk import web as sdk_web

    seen: dict[str, Any] = {}

    async def post(url: str, **kwargs: Any) -> Any:
        seen["url"] = url
        seen.update(kwargs)
        return SimpleNamespace(status=status, body=body)

    monkeypatch.setattr(sdk_web, "post", post)
    return seen


def test_the_plugin_registers_the_provider_and_the_transcriber() -> None:
    registered: dict[str, Any] = {}
    readers: dict[str, Any] = {}
    ctx = SimpleNamespace(
        register_provider=lambda name, cls: registered.__setitem__(name, cls),
        register_media_reader=lambda name, factory: readers.__setitem__(name, factory),
        setting=lambda key, default=None: default,
    )
    plugin.XAIPlugin().register(ctx)  # type: ignore[arg-type]
    assert registered == {"xai": XAIProvider}
    reader = readers["xai/stt"](api_key="xai-test")
    assert reader.name == "xai/stt" and reader.model == "grok-voice-transcribe-2.0"
    assert reader.ready() == ""


def test_the_transcriber_is_not_ready_without_a_key_and_takes_no_webm() -> None:
    reader = plugin.XAITranscriber()
    assert "XAI_API_KEY" in reader.ready()
    assert "audio/webm" not in reader.accepts and "audio/ogg" in reader.accepts


async def test_the_transcriber_posts_the_audio_to_xai(monkeypatch: Any) -> None:
    seen = _fake_post(monkeypatch, body=b'{"text": "hello there", "duration": 4.2, "words": []}')
    out = await plugin.XAITranscriber(api_key="xai-test").read(_reading(language="fr"))
    assert out.text == "hello there" and out.cost == "4s"
    assert seen["url"] == "https://api.x.ai/v1/stt"
    assert seen["headers"] == {"Authorization": "Bearer xai-test"}
    body = seen["data"]
    assert b'name="model"\r\n\r\ngrok-voice-transcribe-2.0\r\n' in body
    assert b'name="language"\r\n\r\nfr\r\n' in body
    # xAI requires the file to be the last field of the form.
    assert body.index(b'name="file"') > body.index(b'name="language"')
    assert b'filename="audio.mp3"\r\nContent-Type: audio/mpeg\r\n\r\nID3' in body


async def test_a_refusal_raises_with_xais_words(monkeypatch: Any) -> None:
    _fake_post(monkeypatch, status=400, body=b'{"error": "unsupported audio format"}')
    try:
        await plugin.XAITranscriber(api_key="xai-test").read(_reading())
    except RuntimeError as exc:
        assert "HTTP 400" in str(exc) and "unsupported audio format" in str(exc)
    else:
        raise AssertionError("a 400 did not raise")
