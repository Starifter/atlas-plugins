"""Firecrawl, through its scrape API: the fetcher for the pages `reader` cannot read."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from atlas.sdk.auth import SecretRef, read_dotenv, resolve
from atlas.sdk.plugin_entry import Plugin, PluginContext
from atlas.sdk.runtime import CredentialError
from atlas.sdk.web import Fetch, Page, post

DEFAULT_BASE_URL = "https://api.firecrawl.dev"
SCRAPE_PATH = "/v2/scrape"
RESPONSE_BYTES = 10_000_000
"""Cap on Firecrawl's own reply, which carries the page as JSON and is larger
than the page. `Fetch.max_bytes` bounds the page the model reads; this bounds
the wire."""


class Firecrawl:
    name = "firecrawl"
    priority = 50
    """After the built-in fetcher, which costs nothing and reads most pages: this
    one gets what it could not - a PDF, a page that came back thin, a page behind
    a bot wall."""

    def __init__(
        self,
        *,
        workspace: Path | str = ".",
        api_key_env: str = "FIRECRAWL_API_KEY",
        base_url: str = DEFAULT_BASE_URL,
        keyless: bool = True,
        only_main_content: bool = True,
        max_age_ms: int = 0,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.workspace = Path(workspace)
        self.ref = SecretRef("env", api_key_env or "FIRECRAWL_API_KEY")
        self.base_url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        self.keyless = bool(keyless)
        self.only_main_content = bool(only_main_content)
        self.max_age_ms = max(0, int(max_age_ms))
        self.timeout = max(1.0, float(timeout_seconds))

    def ready(self) -> bool:
        """With a key, always. Without one, only while `keyless` says a URL may
        go to Firecrawl's starter tier on this install's behalf."""
        return self.keyless or bool(self._key())

    def _key(self) -> str:
        """The key, or an empty string. Read fresh, never cached at install."""
        try:
            return resolve(self.ref, dotenv=read_dotenv(self.workspace))
        except CredentialError:
            return ""

    async def fetch(self, request: Fetch) -> Page:
        key = self._key()
        if not key and not self.keyless:
            raise RuntimeError("firecrawl has no key and keyless access is off")
        body: dict[str, Any] = {
            "url": request.url,
            "formats": ["markdown"],
            "onlyMainContent": self.only_main_content and request.readability,
            "timeout": int(self.timeout * 1000),
        }
        if self.max_age_ms:
            body["maxAge"] = self.max_age_ms
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"

        # The operator's policy rides on the request and is what a self-hosted
        # `base_url` on a private address is checked against - the same rule a
        # redirect is re-checked under, and for the same reason.
        response = await post(
            f"{self.base_url}{SCRAPE_PATH}",
            json=body,
            headers=headers,
            allow_private=request.allow_private,
            hosts=request.hosts,
            max_bytes=RESPONSE_BYTES,
            timeout=self.timeout,
            trusted_proxy=request.trusted_proxy,
        )
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} from Firecrawl: {_why(response.body)}")
        try:
            payload = json.loads(response.body)
        except ValueError as error:
            raise RuntimeError(f"Firecrawl returned something that is not JSON: {error}") from None
        if not isinstance(payload, dict) or not payload.get("success"):
            raise RuntimeError(f"Firecrawl could not scrape: {_why(response.body)}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Firecrawl answered without a page")
        raw_meta = data.get("metadata")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        status = _int(meta.get("statusCode"))
        if status >= 400:
            # The same terms as the built-in: the status is the answer, not
            # the error page's furniture.
            raise RuntimeError(f"HTTP {status} from {meta.get('url') or request.url}")
        text = data.get("markdown")
        if not isinstance(text, str):
            raise RuntimeError("Firecrawl answered without markdown")
        return Page(
            url=str(meta.get("url") or meta.get("sourceURL") or request.url),
            text=text,
            title=str(meta.get("title") or ""),
            content_type=str(meta.get("contentType") or "text/html").split(";", 1)[0].strip(),
            # Firecrawl produces markdown and nothing plainer; asking for text
            # is a preference the backend answers by saying what it made.
            mode="markdown",
            truncated=response.truncated,
        )


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _why(body: bytes) -> str:
    """Firecrawl's error text, if it sent one, without printing a page."""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            error = data.get("error") or data.get("message")
            if isinstance(error, str):
                return error
    except ValueError:
        pass
    return body[:200].decode("utf-8", "replace").strip()


class FirecrawlPlugin(Plugin):
    name = "firecrawl"
    description = "Firecrawl as a web_fetch backend, for the pages the built-in cannot read."

    def register(self, ctx: PluginContext) -> None:
        workspace = ctx.workspace
        ctx.register_fetcher(
            "firecrawl",
            lambda: Firecrawl(
                workspace=workspace,
                api_key_env=str(ctx.setting("api_key_env", "FIRECRAWL_API_KEY")),
                base_url=str(ctx.setting("base_url", DEFAULT_BASE_URL)),
                keyless=bool(ctx.setting("keyless", True)),
                only_main_content=bool(ctx.setting("only_main_content", True)),
                max_age_ms=int(ctx.setting("max_age_ms", 0)),
                timeout_seconds=float(ctx.setting("timeout_seconds", 30.0)),
            ),
        )
