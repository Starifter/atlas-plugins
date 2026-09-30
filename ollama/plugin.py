"""Ollama - models on this machine through a local Ollama, or in Ollama's cloud.

Ollama speaks OpenAI's Chat Completions dialect at `/v1`, so both providers here
are the SDK's OpenAI-compatible base (`plugin-sdk.md` §6.7) with Ollama's own
parts declared: `reasoning_effort` as Ollama maps it onto a model's thinking,
the native `/api/tags`, `/api/show` and `/api/ps` for what `/v1/models` does not
say, and - for a local server - the context it actually loaded a model with.

Two providers, one plugin. `ollama` is the server on this machine (or your
network): no key, a server address, every model priced at zero. `ollama-cloud`
is ollama.com directly, with `OLLAMA_API_KEY`. They are two names rather than
one with a mode because `local` is a declaration about a provider, and one that
changed with a setting would be a manifest that lies half the time.

**The context a local request gets is the server's, not the model's.** Ollama
loads a model with `OLLAMA_CONTEXT_LENGTH`, or 4k/32k/256k by the GPU's memory,
and its OpenAI endpoint has no way to ask for more. A prompt longer than that is
cut from the front, silently. So `ollama` reads the loaded size from `/api/ps`,
tells the session that size when it can, loads the model before the first
request so the size is known, and refuses a request that would not fit - with
how to raise it - rather than let Ollama cut it.

Requires the `openai` package (`pip install openai`, or `pip install "atlas[openai]"`).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

from atlas.sdk.openai_compat import OpenAICompatEmbedder, OpenAICompatProvider, check_base_url
from atlas.sdk.plugin_entry import Plugin, PluginContext
from atlas.sdk.provider import (
    DeltaSink,
    Message,
    ModelEntry,
    Pricing,
    Sampling,
    ThinkingLevel,
)
from atlas.sdk.runtime import ConfigError, ProviderError
from atlas.sdk.tool_plugin import ToolSpec

DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
"""Where Ollama listens unless `OLLAMA_HOST` says otherwise."""

CLOUD_BASE_URL = "https://ollama.com/v1"

PLACEHOLDER_KEY = "ollama"
"""What the `openai` package is handed for a local server: it requires a key and
Ollama ignores it. Never the user's `OPENAI_API_KEY`."""

LEVELS: tuple[ThinkingLevel, ...] = ("off", "low", "medium", "high", "max")
"""Ollama's menu before it has been asked about a model: `reasoning_effort` is
one field it maps onto whatever the model has - a named level, on or off - and
an unsupported name falls to the model's default rather than failing. The
listing narrows it per model from `/api/show`."""

EFFORT: dict[ThinkingLevel, str] = {
    "off": "none",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "max",
}
"""Atlas's levels in Ollama's words. `none` is Ollama's `think: false`."""

ON_OFF: tuple[ThinkingLevel, ...] = ("off", "high")
"""The menu for a model whose thinking is a switch: off, or on at the level a
session asks for by default."""

FREE = Pricing(input=0.0, output=0.0, cache_read=0.0, cache_write=0.0)
"""A declaration, not a guess (`model-catalog.md` C6): a model on this machine
costs nothing per token. Ollama's cloud is a subscription, not a price per token,
so `ollama-cloud` declares nothing and its cost is unknown."""

FLOOR_CONTEXT = 4096
"""What the session is told before anything better is known: Ollama's smallest
default. Too small is a session that compacts early; too large is one Ollama
truncates. The first is the one that fails softly."""

PROBE_TIMEOUT = 1.0
PROBE_CACHE_SECONDS = 30.0
LOAD_TIMEOUT = 600.0
NATIVE_TIMEOUT = 15.0
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


# -- the native API -----------------------------------------------------------------


def native_root(base_url: str) -> str:
    """The server itself, for the endpoints Ollama keeps beside `/v1`."""
    root = base_url.rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


def tagged(model: str) -> str:
    """A model name as `/api/ps` writes it: `qwen3` is `qwen3:latest`."""
    return model if ":" in model.rsplit("/", 1)[-1] else f"{model}:latest"


async def fetch_json(url: str, *, headers: Mapping[str, str] | None = None) -> Mapping[str, Any]:
    """One GET through the core's client. `allow_private` because the address is
    the operator's setting, and a local Ollama is by its nature on this machine
    or this network; the URL was never the model's."""
    from atlas.sdk.web import get

    response = await get(
        url,
        allow_private=True,
        timeout=NATIVE_TIMEOUT,
        headers=dict(headers or {}),
        user_agent="atlas-ollama",
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
    """One POST through the core's client, as JSON."""
    from atlas.sdk.web import post

    response = await post(
        url,
        json=dict(body),
        allow_private=True,
        timeout=timeout,
        headers=dict(headers or {}),
        user_agent="atlas-ollama",
        max_bytes=4_000_000,
    )
    return _decoded(response.status, response.body, url)


def _decoded(status: int, body: bytes, url: str) -> Mapping[str, Any]:
    if status >= 400:
        raise ProviderError(f"HTTP {status} from {url}: {body[:200].decode('utf-8', 'replace')}")
    decoded = json.loads(body.decode("utf-8") or "{}")
    return decoded if isinstance(decoded, Mapping) else {}


def probe_json(url: str, body: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    """One synchronous round trip with a short deadline, or `{}`.

    For the two questions the core asks from synchronous code - a model's window
    and an embedder's width - before anything async has run. A server that is
    down refuses at once, so this never holds the caller for long.
    """
    data = json.dumps(dict(body)).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def loaded_context(ps: Mapping[str, Any], model: str) -> int:
    """The context `/api/ps` says `model` is loaded with, or zero."""
    wanted = tagged(model)
    listed = ps.get("models")
    for item in listed if isinstance(listed, list) else []:
        row = _mapping(item)
        if tagged(str(row.get("name") or row.get("model") or "")) == wanted:
            value = row.get("context_length")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
    return 0


def trained_context(show: Mapping[str, Any]) -> int:
    """`<arch>.context_length` from `/api/show`: the model's own ceiling."""
    for key, value in _mapping(show.get("model_info")).items():
        if str(key).endswith(".context_length") and isinstance(value, int) and value > 0:
            return value
    return 0


def embedding_width(show: Mapping[str, Any]) -> int:
    """`<arch>.embedding_length` from `/api/show`."""
    for key, value in _mapping(show.get("model_info")).items():
        if str(key).endswith(".embedding_length") and isinstance(value, int) and value > 0:
            return value
    return 0


def thinking_levels_of(show: Mapping[str, Any]) -> tuple[ThinkingLevel, ...] | None:
    """A model's menu from `/api/show`, or `None` where it says nothing.

    No `thinking` capability is no control at all. Named levels are offered as
    named, with `off` where `false` is allowed; a switch is `off` and one level.
    A thinking model with no metadata keeps the server's whole menu, since Ollama
    maps an unknown name to the model's default rather than refusing it.
    """
    capabilities = show.get("capabilities")
    if not isinstance(capabilities, list):
        return None
    if "thinking" not in capabilities:
        return ()
    metadata = _mapping(show.get("thinking"))
    values = metadata.get("values")
    if not isinstance(values, list) or not values:
        return LEVELS
    off: tuple[ThinkingLevel, ...] = ("off",) if False in values else ()
    named = [level for level in LEVELS if level != "off" and level in values]
    if named:
        return off + tuple(named)
    if True in values:
        return off + ON_OFF[1:]
    return off


def modalities_of(show: Mapping[str, Any]) -> tuple[str, ...]:
    capabilities = show.get("capabilities")
    if not isinstance(capabilities, list):
        return ()
    return ("text", "image") if "vision" in capabilities else ("text",)


def released_of(row: Mapping[str, Any]) -> str:
    """The day `modified_at` names. Ollama writes nanoseconds, which not every
    Python's ISO parser takes, and only the date is kept anyway."""
    stamp = str(row.get("modified_at") or "")
    return stamp[:10] if DATE.match(stamp) else ""


# -- the providers ------------------------------------------------------------------


class _Ollama(OpenAICompatProvider):
    """What the local server and the cloud share: the dialect's quirks, the
    thinking switch, and the native listing."""

    thinking_levels = LEVELS
    streaming = True
    sampling = True
    """Temperature, top_p, the penalties, seed, stop and the token cap are all
    taken; the rest of a `Sampling` has nowhere to go."""

    cost: Pricing | None = None

    def thinking_request(self, level: ThinkingLevel) -> dict[str, Any]:
        # A model with no thinking control is sent nothing: `think` on a model
        # that cannot think is an error, not a no-op.
        if not self.thinking_levels:
            return {}
        return {"reasoning_effort": EFFORT[level]}

    def native_headers(self) -> dict[str, str]:
        key = str(getattr(self._client, "api_key", "") or "")
        if not key or key == PLACEHOLDER_KEY:
            return {}
        return {"Authorization": f"Bearer {key}"}

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        on_delta: DeltaSink | None = None,
        sampling: Sampling | None = None,
    ) -> Message:
        try:
            return await super().complete(
                system=system, messages=messages, tools=tools, on_delta=on_delta, sampling=sampling
            )
        except ProviderError as exc:
            # A thinking level sent to a model that cannot think. The menu was
            # the server's rather than the model's; correct it and ask once more.
            if "does not support thinking" not in str(exc).lower() or not self.thinking_levels:
                raise
            self.thinking_levels = ()
            return await super().complete(
                system=system, messages=messages, tools=tools, on_delta=on_delta, sampling=sampling
            )

    def describe_failure(self, exc: Exception) -> Exception | None:
        message = str(exc)
        lowered = message.lower()
        if "not found" in lowered and ("pull" in lowered or "model" in lowered):
            return ProviderError(
                f"{self.vendor()} has no model {self.model!r} - `ollama pull {self.model}`, "
                f"or `atlas models list` for what it has ({exc})"
            )
        if "does not support tools" in lowered:
            return ProviderError(
                f"{self.model!r} cannot call tools, and Atlas's turns use them - choose a "
                f"model with tool support (ollama.com/search?c=tools) ({exc})"
            )
        return None

    # -- the listing ------------------------------------------------------------

    async def list_models(self) -> Sequence[ModelEntry] | None:
        """`/api/tags` for what there is, and `/api/show` for each: its window,
        whether it sees pictures, and its thinking menu. `/v1/models` says none
        of that. It never loads a model to answer: a listing is a question."""
        root = native_root(self.base_url_in_use)
        headers = self.native_headers()
        try:
            tags = await fetch_json(root + "/api/tags", headers=headers)
        except Exception as exc:  # the SDK client raises its own; keep ours at the seam
            raise self._failure(exc) from exc
        rows = [_mapping(item) for item in tags.get("models") or () if _mapping(item)]
        rows = [row for row in rows if str(row.get("name") or row.get("model") or "")]
        loaded = await self._loaded_windows()
        gate = asyncio.Semaphore(8)

        async def one(row: Mapping[str, Any]) -> ModelEntry | None:
            name = str(row.get("name") or row.get("model"))
            async with gate:
                try:
                    show = await post_json(root + "/api/show", {"model": name}, headers=headers)
                except Exception:  # a model that will not describe itself is still listed
                    show = {}
            capabilities = show.get("capabilities")
            if isinstance(capabilities, list) and capabilities == ["embedding"]:
                return None
            return ModelEntry(
                id=name,
                context_window=self.window_of(name, show, loaded),
                thinking_levels=thinking_levels_of(show),
                modalities=modalities_of(show),
                cost=self.cost,
                released=released_of(row),
            )

        entries = await asyncio.gather(*(one(row) for row in rows))
        return [entry for entry in entries if entry is not None]

    async def _loaded_windows(self) -> Mapping[str, Any]:
        return {}

    def window_of(self, name: str, show: Mapping[str, Any], loaded: Mapping[str, Any]) -> int:
        """The window a listing reports for one model."""
        return trained_context(show)

    async def resolve_model(self) -> str:
        raise ConfigError(
            f"the {self.vendor()} provider needs a model - set ATLAS_MODEL (or `model` in "
            f"~/.atlas/config.json) to one of `atlas models list {self.name}`"
        )


_WINDOWS: dict[tuple[str, str], tuple[float, int]] = {}
"""What `/api/ps` last said, per server and model, for the synchronous ask."""


def remember_window(root: str, model: str, context: int) -> None:
    if context > 0:
        _WINDOWS[(root, tagged(model))] = (time.monotonic(), context)


class OllamaProvider(_Ollama):
    """The Ollama on this machine, or on this network.

    There is no default model: a server holds as many as were pulled, and the
    choice is the person's. One pulled model is the exception - it is the answer.
    """

    name = "ollama"
    label = "Ollama"
    local = True
    base_url = DEFAULT_BASE_URL
    placeholder_key = PLACEHOLDER_KEY
    default_context_window = FLOOR_CONTEXT
    cost = FREE
    overflow_markers = ("exceeds the context", "context length")

    context_length: int = 0
    """What the person says their server loads models with - the value of
    `OLLAMA_CONTEXT_LENGTH` - for before a model is loaded. A claim: the loaded
    size wins the moment there is one, and every request is checked against it."""

    @classmethod
    def configured(cls, settings: Mapping[str, Any]) -> type[OllamaProvider]:
        """This class with the plugin's settings bound, for `register_provider`."""
        length = settings.get("context_length")
        return cls.bind(
            base_url=check_base_url(settings.get("base_url"), default=DEFAULT_BASE_URL),
            context_length=length if isinstance(length, int) and length > 0 else 0,
        )

    @classmethod
    def model_families(cls) -> tuple[str, ...]:
        return ("*",)

    @classmethod
    def window_for(cls, model: str) -> int:
        """The loaded size where the server has one, the person's word where it
        does not, and the floor where nobody has said anything.

        Asked from synchronous code when a session builds its provider, so it is
        one loopback GET with a short deadline, remembered for half a minute."""
        if not model:
            return super().window_for(model)
        root = native_root(check_base_url(cls.base_url, default=DEFAULT_BASE_URL))
        key = (root, tagged(model))
        seen = _WINDOWS.get(key)
        if seen is None or time.monotonic() - seen[0] > PROBE_CACHE_SECONDS:
            context = loaded_context(probe_json(root + "/api/ps"), model)
            if context:
                remember_window(root, model, context)
                return context
        elif seen[1]:
            return seen[1]
        if cls.context_length:
            return cls.context_length
        return super().window_for(model)

    async def loaded_window(self) -> int:
        """The context Ollama loaded this model with, loading it first if it is
        not in memory - with no options, exactly as the request that follows
        would load it - so the size is known before a prompt is sent to it
        rather than after one was cut. The base checks every request against it
        and the session's budget follows it (`plugin-sdk.md` §6.7).

        A model Ollama runs elsewhere - a `-cloud` model through this server -
        is not in `/api/ps`, and its size is not this server's to report: zero,
        and nothing is checked."""
        root = native_root(self.base_url_in_use)
        try:
            context = loaded_context(await fetch_json(root + "/api/ps"), self.model)
            if not context:
                await post_json(root + "/api/generate", {"model": self.model}, timeout=LOAD_TIMEOUT)
                context = loaded_context(await fetch_json(root + "/api/ps"), self.model)
        except Exception as exc:  # the SDK client raises its own; keep ours at the seam
            raise self._failure(exc) from exc
        remember_window(root, self.model, context)
        return context

    def does_not_fit(self, needed: int, model: str) -> str:
        return (
            f"this turn is about {needed:,} tokens and Ollama loaded {model} with "
            f"{self.served_window:,} - it would cut the start of the conversation rather "
            f"than refuse. Raise OLLAMA_CONTEXT_LENGTH (or the Ollama app's context "
            f"slider), restart Ollama, and set context_length under "
            f"plugins_settings.ollama to match"
        )

    async def _loaded_windows(self) -> Mapping[str, Any]:
        try:
            return await fetch_json(native_root(self.base_url_in_use) + "/api/ps")
        except Exception:  # the listing stands without it
            return {}

    def window_of(self, name: str, show: Mapping[str, Any], loaded: Mapping[str, Any]) -> int:
        """What a request to this server gets: the loaded size, else the person's
        word. Never the model's trained ceiling, which the server does not load
        it with - reporting that is how a session comes to be truncated."""
        return loaded_context(loaded, name) or self.context_length

    async def resolve_model(self) -> str:
        """The one model a server holds, when it holds one."""
        try:
            tags = await fetch_json(native_root(self.base_url_in_use) + "/api/tags")
        except Exception as exc:  # the SDK client raises its own; keep ours at the seam
            raise self._failure(exc) from exc
        names = [str(_mapping(item).get("name") or "") for item in tags.get("models") or () if item]
        names = [name for name in names if name]
        if len(names) == 1:
            self.model = names[0]
            return names[0]
        if not names:
            raise ProviderError(
                f"Ollama at {self.base_url_in_use} has no models - `ollama pull qwen3` "
                "(or any model with tool support) first"
            )
        raise ConfigError(
            "Ollama holds several models - set `model` (or ATLAS_MODEL) to one of: "
            + ", ".join(sorted(names))
        )

    def describe_failure(self, exc: Exception) -> Exception | None:
        if type(exc).__name__ in ("APIConnectionError", "WebError", "ConnectionRefusedError"):
            return ProviderError(
                f"no Ollama answered at {self.base_url_in_use} - start the Ollama app or "
                f"`ollama serve`, or set base_url under plugins_settings.ollama ({exc})"
            )
        return super().describe_failure(exc)


class OllamaCloudProvider(_Ollama):
    """ollama.com directly, with `OLLAMA_API_KEY`. No Ollama install needed.

    Cloud models run at their full context, so the window is the model's own
    from `/api/show`, and there is nothing on this machine to load or check.
    """

    name = "ollama-cloud"
    label = "Ollama Cloud"
    api_key_env_vars = ("OLLAMA_API_KEY",)
    base_url = CLOUD_BASE_URL
    local = False

    @classmethod
    def model_families(cls) -> tuple[str, ...]:
        return ("*",)


# -- the embedder ---------------------------------------------------------------------


class OllamaEmbedder(OpenAICompatEmbedder):
    """Vectors from an embedding model in a local Ollama, for memory search.

    `memory_embedder: ollama` and `memory_embedder_model: nomic-embed-text` (or
    any embedding model pulled). The width is read from `/api/show` - the core
    asks for it from synchronous code before it ever embeds - and learned from
    the first reply if the server says nothing.

    **It may raise.** Memory search answers from the keyword index when an
    embedder fails, and never raises at the model.
    """

    name = "ollama"
    label = "Ollama"
    base_url = DEFAULT_BASE_URL
    placeholder_key = PLACEHOLDER_KEY

    def __init__(self, model: str = "", **kwargs: Any) -> None:
        if not model.strip():
            raise ConfigError(
                "the Ollama embedder needs a model - set memory_embedder_model to an "
                "embedding model you pulled, e.g. nomic-embed-text"
            )
        super().__init__(model, **kwargs)

    @property
    def dimensions(self) -> int:
        if self._dimensions <= 0:
            show = probe_json(
                native_root(self.base_url_in_use) + "/api/show", {"model": self.model}
            )
            self._dimensions = embedding_width(show)
        return self._dimensions


class OllamaPlugin(Plugin):
    """Ollama, on this machine and in its cloud."""

    name = "ollama"
    description = "Models through a local Ollama, or Ollama's cloud with a key."

    def register(self, ctx: PluginContext) -> None:
        settings = {
            "base_url": ctx.setting("base_url", DEFAULT_BASE_URL),
            "context_length": ctx.setting("context_length", 0),
        }
        local = OllamaProvider.configured(settings)
        ctx.register_provider("ollama", local)
        ctx.register_provider("ollama-cloud", OllamaCloudProvider)
        width = int(ctx.setting("embedding_dimensions", 0) or 0)
        embedder = OllamaEmbedder.bind(base_url=local.base_url)

        def build(model: str = "", **kwargs: Any) -> OllamaEmbedder:
            kwargs.setdefault("dimensions", width)
            return embedder(model, **kwargs)

        ctx.register_embedder("ollama", build)
