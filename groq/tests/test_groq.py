"""The Groq plugin, driven the way Atlas drives it: a fake `openai` client that
records each request and answers the way Groq's API documents it.

Run from a checkout of Atlas (`uv run pytest path/to/groq/tests`).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from atlas.sdk.provider import ContextOverflowError, Message

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("atlas_plugin_groq", HERE.parent / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()
GroqProvider = plugin.GroqProvider


class FakeClient:
    def __init__(
        self, message: Any = None, *, models: list[Any] = (), fail: Exception | None = None
    ) -> None:
        self.message = message
        self.models_data = list(models)
        self.fail = fail
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=self._list)

    @property
    def request(self) -> dict[str, Any]:
        return self.requests[-1]

    async def _create(self, **request: Any) -> Any:
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        return SimpleNamespace(choices=[SimpleNamespace(message=self.message)], usage=None)

    async def _list(self) -> Any:
        for row in self.models_data:
            yield row


def reply(**fields: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": None, **fields}


async def test_the_reply_ceiling_is_max_completion_tokens() -> None:
    client = FakeClient(reply(content="hi"))
    await GroqProvider("llama-3.3-70b-versatile", client=client).complete(
        system="", messages=[Message.user("hi")]
    )
    assert "max_completion_tokens" in client.request
    assert "max_tokens" not in client.request


async def test_a_model_without_a_thinking_control_is_sent_none() -> None:
    """Llama has no menu, so the session's default `high` sends nothing."""
    client = FakeClient(reply(content="hi"))
    provider = GroqProvider("llama-3.3-70b-versatile", client=client)
    assert provider.thinking_levels == ()
    await provider.complete(system="", messages=[Message.user("hi")])
    assert "reasoning_effort" not in client.request
    assert "reasoning_format" not in client.request.get("extra_body", {})


async def test_gpt_oss_takes_an_effort_and_has_no_off() -> None:
    assert GroqProvider.levels_for("openai/gpt-oss-120b") == ("low", "medium", "high")
    client = FakeClient(reply(content="hi", reasoning="thought about it"))
    provider = GroqProvider("openai/gpt-oss-120b", client=client, thinking="medium")
    turn = await provider.complete(system="", messages=[Message.user("hi")])
    assert client.request["reasoning_effort"] == "medium"
    assert "reasoning_format" not in client.request.get("extra_body", {})
    assert turn.thinking == "thought about it" and turn.text == "hi"


async def test_qwen_turns_off_with_none_and_is_parsed() -> None:
    client = FakeClient(reply(content="hi"))
    await GroqProvider("qwen/qwen3.8-27b", client=client, thinking="off").complete(
        system="", messages=[Message.user("hi")]
    )
    assert client.request["reasoning_effort"] == "none"
    assert client.request["extra_body"]["reasoning_format"] == "parsed"


async def test_the_listing_keeps_window_ceiling_and_menu_and_drops_inactive() -> None:
    rows = [
        {
            "id": "openai/gpt-oss-120b",
            "created": 1754400000,
            "context_window": 131072,
            "max_completion_tokens": 65536,
            "active": True,
        },
        {"id": "retired-model", "context_window": 8192, "active": False},
    ]
    entries = await GroqProvider(client=FakeClient(models=rows)).list_models()
    assert entries is not None and [e.id for e in entries] == ["openai/gpt-oss-120b"]
    (entry,) = entries
    assert (entry.context_window, entry.max_output) == (131072, 65536)
    assert entry.thinking_levels == ("low", "medium", "high")
    assert entry.released == "2025-08-05"


async def test_groqs_overflow_is_a_context_overflow() -> None:
    class BadRequest(Exception):
        status_code = 400

    error = BadRequest(
        "Error code: 400 - {'error': {'message': 'Please reduce the length of the messages or "
        "completion.', 'code': 'context_length_exceeded'}}"
    )
    provider = GroqProvider("llama-3.3-70b-versatile", client=FakeClient(fail=error))
    try:
        await provider.complete(system="", messages=[Message.user("hi")])
    except ContextOverflowError as exc:
        assert exc.__cause__ is error
    else:
        raise AssertionError("an overflow was not recognised")


def test_the_plugin_registers_the_provider_and_the_transcriber() -> None:
    registered: dict[str, Any] = {}
    readers: dict[str, Any] = {}
    ctx = SimpleNamespace(
        register_provider=lambda name, cls: registered.__setitem__(name, cls),
        register_media_reader=lambda name, factory: readers.__setitem__(name, factory),
        setting=lambda key, default=None: (
            "whisper-large-v3" if key == "transcription_model" else default
        ),
    )
    plugin.GroqPlugin().register(ctx)  # type: ignore[arg-type]
    assert registered == {"groq": GroqProvider}
    # Named for the vendor, so the core hands the factory the groq profile's key.
    reader = readers["groq/whisper"](api_key="gsk-test")
    assert reader.name == "groq/whisper" and reader.model == "whisper-large-v3"
    assert reader.ready() == ""


# -- the transcriber (`media.md` §8.3) ------------------------------------------------


def _reading(**kw: Any) -> Any:
    from atlas.sdk.media import Reading

    fields: dict[str, Any] = {
        "kind": "audio",
        "data": b"OggS" + b"\x00" * 64,
        "media_type": "audio/ogg",
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


def test_the_transcriber_is_not_ready_without_a_key() -> None:
    reader = plugin.GroqWhisper()
    assert "GROQ_API_KEY" in reader.ready()
    assert reader.priority < 50, "ahead of openai/whisper, which is 50"
    assert "audio/webm" in reader.accepts and reader.kinds == ("audio",)


async def test_the_transcriber_posts_the_audio_to_groq(monkeypatch: Any) -> None:
    seen = _fake_post(monkeypatch, body=b'{"text": "turn the lights off", "language": "en"}')
    reader = plugin.GroqWhisper(api_key="gsk-test")
    out = await reader.read(_reading(language="en"))
    assert out.text == "turn the lights off"
    assert seen["url"] == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert seen["headers"] == {"Authorization": "Bearer gsk-test"}
    assert seen["content_type"].startswith("multipart/form-data; boundary=")
    body = seen["data"]
    assert b'name="model"\r\n\r\nwhisper-large-v3-turbo\r\n' in body
    assert b'name="language"\r\n\r\nen\r\n' in body
    assert b'filename="audio.ogg"\r\nContent-Type: audio/ogg\r\n\r\nOggS' in body
    # The file is the last part of the form.
    assert body.rstrip().endswith(b"--") and body.index(b'name="file"') > body.index(
        b'name="model"'
    )


async def test_no_language_hint_sends_none(monkeypatch: Any) -> None:
    seen = _fake_post(monkeypatch)
    await plugin.GroqWhisper(api_key="gsk-test").read(_reading())
    assert b'name="language"' not in seen["data"]


async def test_a_refusal_raises_with_groqs_words(monkeypatch: Any) -> None:
    _fake_post(monkeypatch, status=401, body=b'{"error": {"message": "Invalid API Key"}}')
    try:
        await plugin.GroqWhisper(api_key="gsk-bad").read(_reading())
    except RuntimeError as exc:
        assert "HTTP 401" in str(exc) and "Invalid API Key" in str(exc)
    else:
        raise AssertionError("a 401 did not raise")
