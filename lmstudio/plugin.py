"""LM Studio - models loaded in the LM Studio app on this machine, no bill.

LM Studio's server speaks OpenAI's Chat Completions dialect at `/v1`, so this is
the SDK's OpenAI-compatible base (`plugin-sdk.md` §6.7) pointed at it. What is LM
Studio's own is here: its REST API at `/api/v1`, which says what is downloaded,
what is loaded and with how much context, and which loads a model at a context
this plugin chooses; an optional API token; and a price of zero.

**The context a request gets is the one the model was loaded with.** LM Studio
sets it at load, and a model loaded on demand by a `/v1` request gets LM Studio's
default. So this plugin loads the model itself - at `context_length` when set -
before the first request, reads the size it was loaded with, tells the session
that size when it can, and refuses a request that would not fit rather than send
one the server would have to cut or reject.

Requires the `openai` package (`pip install openai`, or `pip install "atlas[openai]"`).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

from atlas.sdk.openai_compat import OpenAICompatEmbedder, OpenAICompatProvider, check_base_url
from atlas.sdk.plugin_entry import Plugin, PluginContext
from atlas.sdk.provider import (
    ModelEntry,
    Pricing,
    ThinkingLevel,
)
from atlas.sdk.runtime import ConfigError, ProviderError

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
"""Where LM Studio's server listens unless its Developer settings say otherwise."""

PLACEHOLDER_KEY = "lm-studio"
"""What the `openai` package is handed when there is no token: it requires a key,
and LM Studio ignores it unless authentication is switched on. Never the user's
`OPENAI_API_KEY`."""

LEVELS: tuple[ThinkingLevel, ...] = ("off", "low", "medium", "high", "max")

FREE = Pricing(input=0.0, output=0.0, cache_read=0.0, cache_write=0.0)
"""A declaration, not a guess (`model-catalog.md` C6): a model on this machine
costs nothing per token."""

FLOOR_CONTEXT = 4096
"""What the session is told before anything better is known: LM Studio's usual
default for a model loaded on demand. Too small compacts early; too large is a
turn the server cannot hold. The first fails softly."""

PROBE_TIMEOUT = 1.0
PROBE_CACHE_SECONDS = 30.0
EMBED_PROBE_TIMEOUT = 3.0
LOAD_TIMEOUT = 600.0
NATIVE_TIMEOUT = 15.0


# -- the native API -----------------------------------------------------------------


def native_root(base_url: str) -> str:
    """The server itself, for the REST API LM Studio keeps beside `/v1`."""
    root = base_url.rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


async def fetch_json(url: str, *, headers: Mapping[str, str] | None = None) -> Mapping[str, Any]:
    """One GET through the core's client. `allow_private` because the address is
    the operator's setting and LM Studio is on this machine; the URL was never
    the model's."""
    from atlas.sdk.web import get

    response = await get(
        url,
        allow_private=True,
        timeout=NATIVE_TIMEOUT,
        headers=dict(headers or {}),
        user_agent="atlas-lmstudio",
        max_bytes=4_000_000,
    )
    return _decoded(response.status, response.body, url)


async def post_json(
    url: str,
    body: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = NATIVE_TIMEOUT,
) -> Mapping[str, Any]:
    from atlas.sdk.web import post

    response = await post(
        url,
        json=dict(body),
        allow_private=True,
        timeout=timeout,
        headers=dict(headers or {}),
        user_agent="atlas-lmstudio",
        max_bytes=4_000_000,
    )
    return _decoded(response.status, response.body, url)


def _decoded(status: int, body: bytes, url: str) -> Mapping[str, Any]:
    if status >= 400:
        raise ProviderError(f"HTTP {status} from {url}: {body[:200].decode('utf-8', 'replace')}")
    decoded = json.loads(body.decode("utf-8") or "{}")
    return decoded if isinstance(decoded, Mapping) else {}


def probe_json(
    url: str,
    body: Mapping[str, Any] | None = None,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = PROBE_TIMEOUT,
) -> Mapping[str, Any]:
    """One synchronous round trip with a short deadline, or `{}` - for the two
    questions the core asks from synchronous code, a model's window and an
    embedder's width."""
    data = json.dumps(dict(body)).encode("utf-8") if body is not None else None
    sent = dict(headers or {})
    if data:
        sent["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=sent, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _positive(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def rows_of(listing: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The models `/api/v1/models` lists, as mappings."""
    listed = listing.get("models")
    return [_mapping(item) for item in listed if _mapping(item)] if isinstance(listed, list) else []


def find(listing: Mapping[str, Any], model: str) -> Mapping[str, Any]:
    """The row for `model`, by its key or by a loaded instance's id."""
    for row in rows_of(listing):
        if row.get("key") == model:
            return row
        for instance in row.get("loaded_instances") or ():
            if _mapping(instance).get("id") == model:
                return row
    return {}


def loaded_context(row: Mapping[str, Any], model: str = "") -> int:
    """The context a loaded instance of this row holds, preferring the instance
    named `model` when there are several."""
    instances = [_mapping(i) for i in row.get("loaded_instances") or () if _mapping(i)]
    named = [i for i in instances if i.get("id") == model]
    for instance in named or instances:
        size = _positive(_mapping(instance.get("config")).get("context_length"))
        if size:
            return size
    return 0


_WINDOWS: dict[tuple[str, str], tuple[float, int]] = {}
"""The loaded size last seen, per server and model, for the synchronous ask."""

_ROWS: dict[tuple[str, str], tuple[float, Mapping[str, Any]]] = {}
"""The listing's row last seen, per server and model, for the synchronous asks."""

ON: ThinkingLevel = "high"
"""What a model whose reasoning is a switch offers as its one level: on, at the
level a session asks for by default."""


def levels_of(row: Mapping[str, Any]) -> tuple[ThinkingLevel, ...] | None:
    """A model's `/think` menu from its `capabilities.reasoning`, or `None` where
    the row says nothing about capabilities at all.

    Measured against a live LM Studio: `reasoning_effort: "none"` switches
    thinking off and every other field is ignored, so `off` is offered where the
    model allows it, `on` is offered as `high`, and named levels as named. A
    model with no `reasoning` entry has no control."""
    if "capabilities" not in row:
        return None
    reasoning = _mapping(_mapping(row.get("capabilities")).get("reasoning"))
    allowed = reasoning.get("allowed_options")
    if not isinstance(allowed, list):
        return ()
    offered = set(str(option) for option in allowed)
    if "on" in offered:
        offered.add(ON)
    return tuple(level for level in LEVELS if level in offered)


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"} if key and key != PLACEHOLDER_KEY else {}


# -- the provider --------------------------------------------------------------------


class LMStudioProvider(OpenAICompatProvider):
    """The models in LM Studio on this machine.

    There is no default model. One loaded model is the answer, and failing that
    one downloaded model; with several, the person chooses.
    """

    name = "lmstudio"
    label = "LM Studio"
    api_key_env_vars = ("LM_API_TOKEN",)
    placeholder_key = PLACEHOLDER_KEY
    base_url = DEFAULT_BASE_URL
    local = True
    streaming = True
    sampling = True
    default_context_window = FLOOR_CONTEXT
    thinking_levels = ()
    """None before LM Studio has been asked about a model: the menu is per model,
    from the listing's `capabilities.reasoning` (`levels_for`). What a model
    reasons is shown either way - it arrives as `reasoning` or
    `reasoning_content`, and the base reads both."""

    context_length: int = 0
    """The context to load a model with. Zero leaves it to LM Studio."""

    @classmethod
    def configured(cls, settings: Mapping[str, Any]) -> type[LMStudioProvider]:
        length = settings.get("context_length")
        return cls.bind(
            base_url=check_base_url(settings.get("base_url"), default=DEFAULT_BASE_URL),
            context_length=length if isinstance(length, int) and length > 0 else 0,
        )

    @classmethod
    def model_families(cls) -> tuple[str, ...]:
        return ("*",)

    @classmethod
    def row_for(cls, model: str) -> Mapping[str, Any]:
        """The listing's row for `model`: one short synchronous GET, remembered
        for half a minute, because the core asks while building a session."""
        root = native_root(check_base_url(cls.base_url, default=DEFAULT_BASE_URL))
        key = (root, model)
        seen = _ROWS.get(key)
        if seen is not None and time.monotonic() - seen[0] <= PROBE_CACHE_SECONDS:
            return seen[1]
        listing = probe_json(root + "/api/v1/models", headers=_auth(cls.credential(None)))
        row = find(listing, model)
        if row:
            _ROWS[key] = (time.monotonic(), row)
        return row

    @classmethod
    def window_for(cls, model: str) -> int:
        """The loaded size where LM Studio has one; else the size this plugin
        will load it at; else the floor."""
        if not model:
            return super().window_for(model)
        root = native_root(check_base_url(cls.base_url, default=DEFAULT_BASE_URL))
        seen = _WINDOWS.get((root, model))
        if seen is not None and time.monotonic() - seen[0] <= PROBE_CACHE_SECONDS:
            return seen[1]
        size = loaded_context(cls.row_for(model), model)
        if size:
            _WINDOWS[(root, model)] = (time.monotonic(), size)
            return size
        if cls.context_length:
            return cls.context_length
        return super().window_for(model)

    @classmethod
    def levels_for(cls, model: str) -> tuple[ThinkingLevel, ...]:
        """The model's own menu, from what LM Studio lists about it; the
        catalog's word where LM Studio does not answer."""
        levels = levels_of(cls.row_for(model)) if model else None
        return levels if levels is not None else super().levels_for(model)

    def thinking_request(self, level: ThinkingLevel) -> dict[str, Any]:
        """`reasoning_effort`, the one field LM Studio honours: `none` is off, a
        named level is that level, and `high` is on for a switch. A model with
        no control is sent nothing."""
        if not self.thinking_levels:
            return {}
        return {"reasoning_effort": "none" if level == "off" else level}

    def native_headers(self) -> dict[str, str]:
        return _auth(str(getattr(self._client, "api_key", "") or ""))

    async def loaded_window(self) -> int:
        """The context LM Studio loaded this model with, loading it first when it
        is not loaded - at `context_length`, rather than at whatever an
        on-demand load would pick. The base checks every request against it and
        the session's budget follows it (`plugin-sdk.md` §6.7)."""
        root = native_root(self.base_url_in_use)
        headers = self.native_headers()
        try:
            row = find(await fetch_json(root + "/api/v1/models", headers=headers), self.model)
            size = loaded_context(row, self.model)
            if not size:
                body: dict[str, Any] = {"model": self.model, "echo_load_config": True}
                if self.context_length:
                    body["context_length"] = self.context_length
                loaded = await post_json(
                    root + "/api/v1/models/load", body, headers=headers, timeout=LOAD_TIMEOUT
                )
                size = _positive(_mapping(loaded.get("load_config")).get("context_length"))
        except Exception as exc:  # the SDK client raises its own; keep ours at the seam
            raise self._failure(exc) from exc
        if size:
            _WINDOWS[(root, self.model)] = (time.monotonic(), size)
        return size

    def does_not_fit(self, needed: int, model: str) -> str:
        return (
            f"this turn is about {needed:,} tokens and LM Studio loaded {model} with "
            f"{self.served_window:,} - raise context_length under plugins_settings.lmstudio "
            f"(the model allows up to its max_context_length) and unload it in LM Studio "
            f"so the next request loads it at the new size"
        )

    async def list_models(self) -> Sequence[ModelEntry] | None:
        """`/api/v1/models`: every chat model downloaded, with the context a
        request gets - the loaded size, or the size this plugin loads at - and
        whether it sees pictures. Never the model's `max_context_length`, which
        a model is almost never loaded with. A listing loads nothing."""
        try:
            listing = await fetch_json(
                native_root(self.base_url_in_use) + "/api/v1/models", headers=self.native_headers()
            )
        except Exception as exc:  # the SDK client raises its own; keep ours at the seam
            raise self._failure(exc) from exc
        entries: list[ModelEntry] = []
        for row in rows_of(listing):
            key = str(row.get("key") or "")
            if not key or row.get("type") == "embedding":
                continue
            capabilities = _mapping(row.get("capabilities"))
            entries.append(
                ModelEntry(
                    id=key,
                    context_window=loaded_context(row) or self.context_length,
                    modalities=("text", "image") if capabilities.get("vision") else ("text",),
                    thinking_levels=levels_of(row),
                    cost=FREE,
                )
            )
        return entries

    async def resolve_model(self) -> str:
        """The one model loaded, else the one downloaded, else the person's choice."""
        try:
            listing = await fetch_json(
                native_root(self.base_url_in_use) + "/api/v1/models", headers=self.native_headers()
            )
        except Exception as exc:  # the SDK client raises its own; keep ours at the seam
            raise self._failure(exc) from exc
        chat = [row for row in rows_of(listing) if row.get("type") != "embedding"]
        loaded = [str(row.get("key")) for row in chat if row.get("loaded_instances")]
        keys = [str(row.get("key")) for row in chat if row.get("key")]
        for candidates in (loaded, keys):
            if len(candidates) == 1:
                self.model = candidates[0]
                return candidates[0]
        if not keys:
            raise ProviderError(
                "LM Studio has no chat model downloaded - get one in the app, or `lms get qwen3-8b`"
            )
        raise ConfigError(
            "LM Studio has several models - set `model` (or ATLAS_MODEL) to one of: "
            + ", ".join(sorted(keys))
        )

    def describe_failure(self, exc: Exception) -> Exception | None:
        message = str(exc)
        if type(exc).__name__ in ("APIConnectionError", "WebError", "ConnectionRefusedError"):
            return ProviderError(
                f"LM Studio's server is not running at {self.base_url_in_use} - start it in the "
                f"app's Developer tab or with `lms server start`, or set base_url under "
                f"plugins_settings.lmstudio ({exc})"
            )
        if " 401 " in f" {message} " or "Unauthorized" in message:
            return ProviderError(
                "LM Studio requires an API token - create one in the app's server settings and "
                f"put it in ~/.atlas/.env as LM_API_TOKEN ({exc})"
            )
        return None


# -- the embedder ---------------------------------------------------------------------


class LMStudioEmbedder(OpenAICompatEmbedder):
    """Vectors from an embedding model in LM Studio, for memory search.

    `memory_embedder: lmstudio` and `memory_embedder_model` set to the model's key
    (`text-embedding-nomic-embed-text-v1.5`). LM Studio's listing does not say a
    width, so it is asked once, synchronously, by embedding one word - and
    `embedding_dimensions` answers instead where that is not wanted.
    """

    name = "lmstudio"
    label = "LM Studio"
    base_url = DEFAULT_BASE_URL
    api_key_env_vars = ("LM_API_TOKEN",)
    placeholder_key = PLACEHOLDER_KEY

    def __init__(self, model: str = "", **kwargs: Any) -> None:
        if not model.strip():
            raise ConfigError(
                "the LM Studio embedder needs a model - set memory_embedder_model to an "
                "embedding model's key, e.g. text-embedding-nomic-embed-text-v1.5"
            )
        super().__init__(model, **kwargs)

    @property
    def dimensions(self) -> int:
        if self._dimensions <= 0:
            answer = probe_json(
                self.base_url_in_use + "/embeddings",
                {"model": self.model, "input": "width"},
                headers=_auth(str(getattr(self._client, "api_key", "") or "")),
                timeout=EMBED_PROBE_TIMEOUT,
            )
            data = answer.get("data")
            if isinstance(data, list) and data:
                vector = _mapping(data[0]).get("embedding")
                self._dimensions = len(vector) if isinstance(vector, list) else 0
        return self._dimensions


class LMStudioPlugin(Plugin):
    """LM Studio, on this machine."""

    name = "lmstudio"
    description = "Models loaded in LM Studio on this machine - no key, no bill."

    def register(self, ctx: PluginContext) -> None:
        provider = LMStudioProvider.configured(
            {
                "base_url": ctx.setting("base_url", DEFAULT_BASE_URL),
                "context_length": ctx.setting("context_length", 0),
            }
        )
        ctx.register_provider("lmstudio", provider)
        width = int(ctx.setting("embedding_dimensions", 0) or 0)
        embedder = LMStudioEmbedder.bind(base_url=provider.base_url)

        def build(model: str = "", **kwargs: Any) -> LMStudioEmbedder:
            kwargs.setdefault("dimensions", width)
            return embedder(model, **kwargs)

        ctx.register_embedder("lmstudio", build)
