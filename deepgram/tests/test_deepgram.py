"""The Deepgram plugin, driven the way Atlas drives it: a fake transport that
records each request and answers the way Deepgram's API documents it.

Run from a checkout of Atlas (`uv run pytest path/to/deepgram/tests`).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from atlas.sdk.media import Reading

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "atlas_plugin_deepgram", HERE.parent / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()

REPLY = (
    b'{"metadata": {"duration": 12.4}, "results": {"channels": [{"alternatives": '
    b'[{"transcript": "Call me back at 5 pm.", "confidence": 0.99}]}]}}'
)


def _reading(**kw: Any) -> Reading:
    fields: dict[str, Any] = {
        "kind": "audio",
        "data": b"OggS" + b"\x00" * 64,
        "media_type": "audio/ogg",
        "name": "",
        "max_chars": 1000,
    }
    fields.update(kw)
    return Reading(**fields)


@pytest.fixture
def keyed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    return plugin.Deepgram(workspace=tmp_path)


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    recorded: dict[str, Any] = {"status": 200, "body": REPLY}

    async def post(url: str, **kwargs: Any) -> Any:
        recorded["url"] = url
        recorded.update(kwargs)
        return SimpleNamespace(status=recorded["status"], body=recorded["body"])

    monkeypatch.setattr(plugin, "post", post)
    return recorded


def test_not_ready_without_a_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    reader = plugin.Deepgram(workspace=tmp_path)
    assert "DEEPGRAM_API_KEY" in reader.ready()
    assert reader.kinds == ("audio",) and 45 < reader.priority < 50


def test_a_key_in_the_env_file_is_read_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    reader = plugin.Deepgram(workspace=tmp_path, api_key_env="MY_DG_KEY")
    assert reader.ready()
    monkeypatch.setenv("MY_DG_KEY", "dg-later")
    assert reader.ready() == ""


async def test_the_audio_is_the_body_and_the_model_is_in_the_query(
    keyed: Any, seen: dict[str, Any]
) -> None:
    out = await keyed.read(_reading(language="en"))
    assert out.text == "Call me back at 5 pm." and out.cost == "12s"
    assert seen["url"].startswith("https://api.deepgram.com/v1/listen?")
    assert "model=nova-3" in seen["url"] and "smart_format=true" in seen["url"]
    assert "language=en" in seen["url"] and "detect_language" not in seen["url"]
    assert seen["data"] == _reading().data and seen["content_type"] == "audio/ogg"
    assert seen["headers"] == {"Authorization": "Token dg-test"}


async def test_without_a_hint_the_language_is_detected(keyed: Any, seen: dict[str, Any]) -> None:
    await keyed.read(_reading())
    assert "detect_language=true" in seen["url"] and "language=" not in seen["url"].replace(
        "detect_language=", ""
    )


async def test_an_empty_reply_is_empty_text(keyed: Any, seen: dict[str, Any]) -> None:
    seen["body"] = b'{"results": {"channels": []}}'
    out = await keyed.read(_reading())
    assert out.text == "" and out.cost == ""


async def test_a_refusal_raises_with_deepgrams_words(keyed: Any, seen: dict[str, Any]) -> None:
    seen["status"] = 401
    seen["body"] = b'{"err_code": "INVALID_AUTH", "err_msg": "Invalid credentials."}'
    with pytest.raises(RuntimeError, match=r"HTTP 401 from Deepgram: Invalid credentials\."):
        await keyed.read(_reading())


def test_the_plugin_registers_the_reader_with_its_settings(tmp_path: Path) -> None:
    readers: dict[str, Any] = {}
    settings = {"model": "nova-2", "smart_format": False}
    ctx = SimpleNamespace(
        workspace=tmp_path,
        register_media_reader=lambda name, factory: readers.__setitem__(name, factory),
        setting=lambda key, default=None: settings.get(key, default),
    )
    plugin.DeepgramPlugin().register(ctx)  # type: ignore[arg-type]
    reader = readers["deepgram"]()
    assert reader.name == "deepgram" and reader.model == "nova-2"
    assert reader.query(_reading()) == {"model": "nova-2", "detect_language": "true"}
