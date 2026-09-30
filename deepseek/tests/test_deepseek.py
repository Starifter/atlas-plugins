"""The DeepSeek plugin, driven the way Atlas drives it: a fake `openai` client that
records each request and answers the way DeepSeek's API documents it.

Run from a checkout of Atlas (`uv run pytest path/to/deepseek/tests`).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from atlas.sdk.provider import Message, ToolResultBlock
from atlas.sdk.runtime import ProviderError
from atlas.sdk.tool_plugin import ToolSpec

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "atlas_plugin_deepseek", HERE.parent / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()
DeepSeekProvider = plugin.DeepSeekProvider

SPEC = ToolSpec(name="echo", description="Echo.", parameters={"type": "object", "properties": {}})


class FakeClient:
    def __init__(
        self,
        replies: list[Any] = (),
        *,
        usage: Any = None,
        models: list[Any] = (),
        fail: Exception | None = None,
    ) -> None:
        self.replies = list(replies)
        self.usage = usage
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
        message = self.replies.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=self.usage)

    async def _list(self) -> Any:
        for row in self.models_data:
            yield row


def reply(**fields: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": None, **fields}


CALL = {"id": "call_1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}


async def test_thinking_is_a_type_and_an_effort() -> None:
    client = FakeClient([reply(content="a"), reply(content="b")])
    await DeepSeekProvider("deepseek-v4-pro", client=client, thinking="max").complete(
        system="", messages=[Message.user("hi")]
    )
    assert client.request["reasoning_effort"] == "max"
    assert client.request["extra_body"]["thinking"] == {"type": "enabled"}
    await DeepSeekProvider("deepseek-v4-pro", client=client, thinking="off").complete(
        system="", messages=[Message.user("hi")]
    )
    assert "reasoning_effort" not in client.request
    assert client.request["extra_body"]["thinking"] == {"type": "disabled"}


async def test_the_reasoning_goes_back_through_a_tool_loop() -> None:
    """DeepSeek refuses a request with tools unless every earlier assistant turn
    carries its own `reasoning_content` back."""
    client = FakeClient(
        [
            reply(reasoning_content="I should echo", tool_calls=[CALL]),
            reply(reasoning_content="done now", content="echoed"),
        ]
    )
    provider = DeepSeekProvider("deepseek-v4-pro", client=client)
    history: list[Message] = [Message.user("echo it")]
    first = await provider.complete(system="", messages=history, tools=[SPEC])
    history += [
        first,
        Message(role="user", content=(ToolResultBlock(tool_use_id="call_1", content="ok"),)),
    ]
    await provider.complete(system="", messages=history, tools=[SPEC])
    assistant = [m for m in client.request["messages"] if m["role"] == "assistant"]
    assert assistant == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [CALL],
            "reasoning_content": "I should echo",
        }
    ]


async def test_the_cache_is_read_from_deepseeks_fields() -> None:
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 20,
        "prompt_cache_hit_tokens": 800,
        "prompt_cache_miss_tokens": 200,
    }
    turn = await DeepSeekProvider(
        "deepseek-flash", client=FakeClient([reply(content="hi")], usage=usage)
    ).complete(system="", messages=[Message.user("hi")])
    assert turn.usage is not None
    assert (turn.usage.input_tokens, turn.usage.cache_read_tokens) == (200, 800)


async def test_the_listing_says_window_ceiling_pictures_and_efforts() -> None:
    rows = [
        {
            "id": "deepseek-flash",
            "context_window": 1048576,
            "max_output_tokens": 393216,
            "input_modalities": ["text", "image"],
            "effort": {"supported_levels": ["none", "low", "high", "max"], "default_level": "high"},
        },
        {
            "id": "deepseek-v4-pro",
            "context_window": 1048576,
            "input_modalities": ["text"],
            "effort": {"supported_levels": ["low", "high"]},
        },
    ]
    entries = await DeepSeekProvider(client=FakeClient(models=rows)).list_models()
    assert entries is not None
    flash, pro = entries
    assert (flash.context_window, flash.max_output) == (1048576, 393216)
    assert flash.modalities == ("text", "image") and pro.modalities == ("text",)
    assert flash.thinking_levels == ("off", "low", "high", "max")
    assert pro.thinking_levels == ("low", "high")


async def test_an_empty_balance_says_so() -> None:
    class PaymentRequired(Exception):
        status_code = 402

    error = PaymentRequired("Error code: 402 - Insufficient Balance")
    try:
        await DeepSeekProvider("deepseek-flash", client=FakeClient(fail=error)).complete(
            system="", messages=[Message.user("hi")]
        )
    except ProviderError as exc:
        assert "run out of balance" in str(exc) and exc.__cause__ is error
    else:
        raise AssertionError("402 was not described")
