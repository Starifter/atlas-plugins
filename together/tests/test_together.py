"""The Together plugin, driven the way Atlas drives it: a fake `openai` client that
records each request and answers the way Together's API documents it.

Run from a checkout of Atlas (`uv run pytest path/to/together/tests`).
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
    spec = importlib.util.spec_from_file_location(
        "atlas_plugin_together", HERE.parent / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()
TogetherProvider = plugin.TogetherProvider


class FakeClient:
    def __init__(
        self,
        message: Any = None,
        *,
        usage: Any = None,
        listing: Any = (),
        fail: Exception | None = None,
    ) -> None:
        self.message = message
        self.usage = usage
        self.listing = listing
        self.fail = fail
        self.requests: list[dict[str, Any]] = []
        self.gets: list[tuple[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    @property
    def request(self) -> dict[str, Any]:
        return self.requests[-1]

    async def _create(self, **request: Any) -> Any:
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        return SimpleNamespace(choices=[SimpleNamespace(message=self.message)], usage=self.usage)

    async def get(self, path: str, *, cast_to: Any) -> Any:
        self.gets.append((path, cast_to))
        return self.listing


def reply(**fields: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": None, **fields}


async def test_an_overflow_is_refused_rather_than_cut() -> None:
    client = FakeClient(reply(content="hi"))
    await TogetherProvider("deepseek-ai/DeepSeek-V4-Pro", client=client).complete(
        system="", messages=[Message.user("hi")]
    )
    assert client.request["extra_body"]["context_length_exceeded_behavior"] == "error"
    assert "reasoning_effort" not in client.request


async def test_togethers_overflow_words_are_a_context_overflow() -> None:
    class Forbidden(Exception):
        status_code = 403

    error = Forbidden(
        "Error code: 403 - Input token count + `max_tokens` parameter must be less than "
        "the context length"
    )
    provider = TogetherProvider("m", client=FakeClient(fail=error))
    try:
        await provider.complete(system="", messages=[Message.user("hi")])
    except ContextOverflowError as exc:
        assert exc.__cause__ is error
    else:
        raise AssertionError("an overflow was not recognised")


async def test_the_bare_array_listing_keeps_chat_models_with_prices() -> None:
    rows = [
        {
            "id": "deepseek-ai/DeepSeek-V4-Pro",
            "type": "chat",
            "created": 1754400000,
            "context_length": 1048576,
            "pricing": {"input": 1.25, "output": 5.0, "cached_input": 0.25},
        },
        {
            "id": "dedicated/only",
            "type": "chat",
            "context_length": 8192,
            "pricing": {"input": 0, "output": 0},
        },
        {"id": "BAAI/bge-large", "type": "embedding", "context_length": 512},
    ]
    client = FakeClient(listing=rows)
    entries = await TogetherProvider(client=client).list_models()
    assert client.gets == [("/models", object)]
    assert entries is not None and [e.id for e in entries] == [
        "deepseek-ai/DeepSeek-V4-Pro",
        "dedicated/only",
    ]
    priced, unpriced = entries
    assert priced.context_window == 1048576 and priced.released == "2025-08-05"
    assert priced.cost is not None
    assert (priced.cost.input, priced.cost.output, priced.cost.cache_read) == (1.25, 5.0, 0.25)
    assert unpriced.cost is None, "zero for both is unknown, never free"


async def test_an_openai_shaped_listing_is_read_too() -> None:
    client = FakeClient(listing={"data": [{"id": "m", "type": "chat", "context_length": 4096}]})
    entries = await TogetherProvider(client=client).list_models()
    assert entries is not None and [e.id for e in entries] == ["m"]


async def test_a_flat_cached_tokens_is_a_cache_read() -> None:
    usage = {"prompt_tokens": 500, "completion_tokens": 10, "cached_tokens": 300}
    turn = await TogetherProvider(
        "m", client=FakeClient(reply(content="hi"), usage=usage)
    ).complete(system="", messages=[Message.user("hi")])
    assert turn.usage is not None
    assert (turn.usage.input_tokens, turn.usage.cache_read_tokens) == (200, 300)
