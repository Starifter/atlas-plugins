"""The Firecrawl plugin, driven the way Atlas drives it: its `post` replaced by one
that records each request and answers the way Firecrawl's scrape API documents it,
so nothing here touches a socket.

Run from a checkout of Atlas (`uv run pytest path/to/firecrawl/tests`).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from atlas.web import reader
from atlas.web.base import Fetch
from atlas.web.client import Response

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "atlas_plugin_firecrawl", HERE.parent / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


firecrawl = _load()


def response(body: bytes, *, kind: str = "text/html; charset=utf-8", status: int = 200) -> Response:
    return Response(
        url="https://example.tld/final",
        status=status,
        headers=(("content-type", kind),),
        body=body,
        truncated=False,
    )


SCRAPED = json.dumps(
    {
        "success": True,
        "data": {
            "markdown": "# Hi\n\nbody",
            "metadata": {
                "title": "T",
                "sourceURL": "https://example.tld/a",
                "url": "https://example.tld/final",
                "statusCode": 200,
                "contentType": "text/html; charset=utf-8",
            },
        },
    }
).encode()


class CannedPost:
    """A `post` that answers from a table and records what it was asked."""

    def __init__(self, answer: Response) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, url: str, **kwargs: Any) -> Response:
        self.calls.append({"url": url, **kwargs})
        return self.answer


def fc_keyed(tmp_path: Path, monkeypatch: Any, **settings: Any) -> Any:
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    env = tmp_path / ".atlas"
    env.mkdir(exist_ok=True)
    (env / ".env").write_text("FIRECRAWL_API_KEY=fc-secret-123\n", encoding="utf-8")
    return firecrawl.Firecrawl(workspace=tmp_path, **settings)


def test_firecrawl_is_ready_without_a_key_unless_told_otherwise(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Keyless by default, which is the whole reason a fresh install has a
    second fetcher; `keyless: false` is the operator's no, and then a key is
    what makes it ready."""
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    assert firecrawl.Firecrawl(workspace=tmp_path).ready() is True
    assert firecrawl.Firecrawl(workspace=tmp_path, keyless=False).ready() is False
    assert fc_keyed(tmp_path, monkeypatch, keyless=False).ready() is True


def test_firecrawl_goes_after_the_built_in() -> None:
    from atlas.web.registry import priority_of

    assert priority_of(reader.Reader()) < priority_of(firecrawl.Firecrawl())


async def test_firecrawl_posts_the_url_and_reads_the_page(tmp_path: Path, monkeypatch: Any) -> None:
    canned = CannedPost(response(SCRAPED, kind="application/json"))
    monkeypatch.setattr(firecrawl, "post", canned)

    page = await fc_keyed(tmp_path, monkeypatch).fetch(Fetch(url="https://example.tld/a"))

    sent = canned.calls[0]
    assert sent["url"] == "https://api.firecrawl.dev/v2/scrape"
    assert sent["json"]["url"] == "https://example.tld/a"
    assert sent["json"]["formats"] == ["markdown"]
    assert sent["json"]["onlyMainContent"] is True
    assert sent["headers"]["Authorization"] == "Bearer fc-secret-123"
    assert page.text == "# Hi\n\nbody"
    assert page.title == "T"
    assert page.url == "https://example.tld/final"
    assert page.content_type == "text/html"
    assert page.mode == "markdown"


async def test_firecrawl_sends_no_bearer_when_it_has_no_key(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    canned = CannedPost(response(SCRAPED, kind="application/json"))
    monkeypatch.setattr(firecrawl, "post", canned)

    await firecrawl.Firecrawl(workspace=tmp_path).fetch(Fetch(url="https://example.tld/a"))

    assert "Authorization" not in canned.calls[0]["headers"]


async def test_firecrawl_refuses_to_fetch_keyless_when_told_not_to(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """`ready()` already says no; a pinned call still reaches `fetch`, and a
    URL must not go out on a setting the operator turned off."""
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    canned = CannedPost(response(SCRAPED, kind="application/json"))
    monkeypatch.setattr(firecrawl, "post", canned)

    with pytest.raises(RuntimeError, match="keyless access is off"):
        await firecrawl.Firecrawl(workspace=tmp_path, keyless=False).fetch(
            Fetch(url="https://example.tld/a")
        )
    assert canned.calls == []


async def test_firecrawl_hands_the_operators_policy_to_the_client(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A self-hosted `base_url` on a private address is checked under the
    operator's rules, never stock ones - the same reason a redirect is."""
    from atlas.web.ssrf import HostRules

    canned = CannedPost(response(SCRAPED, kind="application/json"))
    monkeypatch.setattr(firecrawl, "post", canned)
    rules = HostRules(allowed=("localhost",))

    await fc_keyed(tmp_path, monkeypatch, base_url="http://localhost:3002/").fetch(
        Fetch(url="https://example.tld/a", allow_private=True, hosts=rules, trusted_proxy=True)
    )

    sent = canned.calls[0]
    assert sent["url"] == "http://localhost:3002/v2/scrape"
    assert sent["allow_private"] is True
    assert sent["hosts"] == rules
    assert sent["trusted_proxy"] is True


async def test_firecrawl_passes_readability_off_and_max_age_through(
    tmp_path: Path, monkeypatch: Any
) -> None:
    canned = CannedPost(response(SCRAPED, kind="application/json"))
    monkeypatch.setattr(firecrawl, "post", canned)

    await fc_keyed(tmp_path, monkeypatch, max_age_ms=60_000).fetch(
        Fetch(url="https://example.tld/a", readability=False)
    )

    assert canned.calls[0]["json"]["onlyMainContent"] is False
    assert canned.calls[0]["json"]["maxAge"] == 60_000


async def test_firecrawl_turns_an_error_status_into_a_sentence(
    tmp_path: Path, monkeypatch: Any
) -> None:
    body = b'{"success": false, "error": "Rate limit exceeded"}'
    monkeypatch.setattr(
        firecrawl, "post", CannedPost(response(body, kind="application/json", status=429))
    )

    with pytest.raises(RuntimeError, match="HTTP 429 from Firecrawl: Rate limit exceeded"):
        await fc_keyed(tmp_path, monkeypatch).fetch(Fetch(url="https://example.tld/a"))


async def test_firecrawl_says_the_pages_status_rather_than_rendering_it(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Firecrawl answers 200 for a page it scraped that was itself a 404. The
    same terms as the built-in: the status is the answer."""
    body = json.dumps(
        {"success": True, "data": {"markdown": "Not found", "metadata": {"statusCode": 404}}}
    ).encode()
    monkeypatch.setattr(firecrawl, "post", CannedPost(response(body, kind="application/json")))

    with pytest.raises(RuntimeError, match="HTTP 404"):
        await fc_keyed(tmp_path, monkeypatch).fetch(Fetch(url="https://example.tld/missing"))


async def test_firecrawl_never_sends_the_key_anywhere_but_its_endpoint(
    tmp_path: Path, monkeypatch: Any
) -> None:
    canned = CannedPost(response(SCRAPED, kind="application/json"))
    monkeypatch.setattr(firecrawl, "post", canned)

    await fc_keyed(tmp_path, monkeypatch).fetch(Fetch(url="https://example.tld/a"))

    assert all(call["url"].startswith("https://api.firecrawl.dev/") for call in canned.calls)
