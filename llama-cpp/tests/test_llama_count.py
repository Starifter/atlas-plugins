"""The exact count (`count_tokens`): `/apply-template` renders the request the
way a chat completion would, `/tokenize` counts it, and the fit check uses that
number instead of an estimate (`plugin-sdk.md` §6.7)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from llama_cpp_testing import FakeClient, LlamaCppProvider, message, plugin

from atlas.sdk.provider import ContextOverflowError, ImageBlock, Message, TextBlock


class Server:
    """`/props`, `/apply-template` and `/tokenize`, answered from a table."""

    def __init__(self, *, n_ctx: int, tokens: int, status: int = 200) -> None:
        self.n_ctx = n_ctx
        self.tokens = tokens
        self.status = status
        self.posts: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def fetch(self, url: str) -> Mapping[str, Any]:
        assert url.endswith("/props?autoload=false"), url
        return {"default_generation_settings": {"n_ctx": self.n_ctx}}

    async def post(
        self, url: str, body: Mapping[str, Any], *, headers: Mapping[str, str] | None = None
    ) -> tuple[int, Mapping[str, Any]]:
        self.posts.append((url, dict(body), dict(headers or {})))
        if self.status != 200:
            return self.status, {}
        if url.endswith("/apply-template"):
            return 200, {"prompt": "<|im_start|>rendered"}
        if url.endswith("/tokenize"):
            return 200, {"tokens": list(range(self.tokens))}
        raise AssertionError(url)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Server:
    fake = Server(n_ctx=8192, tokens=100)
    monkeypatch.setattr(plugin, "fetch_json", fake.fetch)
    monkeypatch.setattr(plugin, "post_json", fake.post)
    return fake


async def test_the_count_is_the_rendered_prompt_tokenized(server: Server) -> None:
    client = FakeClient(message(content="ok"))
    provider = LlamaCppProvider("m", client=client, thinking="off")
    await provider.complete(system="be brief", messages=[Message.user("hi")])

    (template, rendered, _), (tokenize, counted, _) = server.posts
    assert template.endswith("/apply-template") and tokenize.endswith("/tokenize")
    # What the chat completion is sent, with `extra_body` laid flat as the wire has it.
    assert rendered["messages"] == client.requests[-1]["messages"]
    assert rendered["chat_template_kwargs"] == {"enable_thinking": False}
    assert "extra_body" not in rendered
    assert counted == {"content": "<|im_start|>rendered", "add_special": True}
    assert provider.served_window == 8192


async def test_an_exact_count_refuses_what_the_estimate_would_have_sent(server: Server) -> None:
    server.tokens = 9000  # a tokenizer that disagrees with a three-characters estimate
    client = FakeClient(message(content="ok"))
    with pytest.raises(ContextOverflowError, match="8,192 per slot") as raised:
        await LlamaCppProvider("m", client=client).complete(
            system="short", messages=[Message.user("hi")]
        )
    assert raised.value.attempted == 9000
    assert client.requests == []


async def test_a_picture_is_estimated_not_counted(server: Server) -> None:
    image = ImageBlock(media_type="image/png", sha256="ab", size=3, source="t", data=b"png")
    provider = LlamaCppProvider("m", client=FakeClient(message(content="ok")))
    await provider.complete(
        system="", messages=[Message(role="user", content=(TextBlock("look"), image))]
    )
    assert server.posts == [], "the rendered prompt holds a marker, not the picture's tokens"


async def test_a_server_without_the_endpoints_is_asked_once(server: Server) -> None:
    server.status = 404
    provider = LlamaCppProvider("m", client=FakeClient(message(content="ok")))
    await provider.complete(system="", messages=[Message.user("hi")])
    await provider.complete(system="", messages=[Message.user("again")])
    assert len(server.posts) == 1
    assert provider.counts is False


async def test_the_servers_key_goes_with_the_count(server: Server) -> None:
    client: Any = FakeClient(message(content="ok"))
    client.api_key = "server-key"
    await LlamaCppProvider("m", client=client).complete(system="", messages=[Message.user("hi")])
    assert {headers["Authorization"] for _, _, headers in server.posts} == {"Bearer server-key"}
