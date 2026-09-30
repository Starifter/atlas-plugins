"""The Fireworks plugin, driven the way Atlas drives it: a fake `openai` client that
records each request and answers the way Fireworks' API documents it.

Run from a checkout of Atlas (`uv run pytest path/to/fireworks/tests`).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from atlas.sdk.provider import Message, ToolResultBlock
from atlas.sdk.tool_plugin import ToolSpec

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "atlas_plugin_fireworks", HERE.parent / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()
FireworksProvider = plugin.FireworksProvider

SPEC = ToolSpec(name="echo", description="Echo.", parameters={"type": "object", "properties": {}})
MODEL = "accounts/fireworks/models/kimi-k2p6"


class FakeClient:
    def __init__(self, replies: list[Any] = (), *, chunks: list[Any] = ()) -> None:
        self.replies = list(replies)
        self.chunks = list(chunks)
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    @property
    def request(self) -> dict[str, Any]:
        return self.requests[-1]

    async def _create(self, **request: Any) -> Any:
        self.requests.append(request)
        if request.get("stream"):
            return self._stream()
        message = self.replies.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    async def _stream(self) -> Any:
        for chunk in self.chunks:
            yield chunk


def reply(**fields: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": None, **fields}


CALL = {"id": "call_1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}


async def test_an_overflow_is_refused_and_no_effort_is_sent() -> None:
    client = FakeClient([reply(content="hi")])
    provider = FireworksProvider(MODEL, client=client)
    assert provider.thinking_levels == ()
    await provider.complete(system="", messages=[Message.user("hi")])
    assert client.request["extra_body"]["context_length_exceeded_behavior"] == "error"
    assert "reasoning_effort" not in client.request


async def test_a_streamed_reasoning_goes_back_through_a_tool_loop() -> None:
    streamed = FakeClient(
        chunks=[
            {"choices": [{"delta": {"reasoning_content": "I should "}}]},
            {"choices": [{"delta": {"reasoning_content": "echo"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, **CALL}]}}]},
        ]
    )
    first = await FireworksProvider(MODEL, client=streamed).complete(
        system="", messages=[Message.user("echo it")], tools=[SPEC], on_delta=lambda _: None
    )
    client = FakeClient([reply(content="echoed")])
    await FireworksProvider(MODEL, client=client).complete(
        system="",
        messages=[
            Message.user("echo it"),
            first,
            Message(role="user", content=(ToolResultBlock(tool_use_id="call_1", content="ok"),)),
        ],
        tools=[SPEC],
    )
    (assistant,) = [m for m in client.request["messages"] if m["role"] == "assistant"]
    assert assistant["reasoning_content"] == "I should echo"
    assert assistant["tool_calls"][0]["id"] == "call_1"
