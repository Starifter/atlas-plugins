"""llama.cpp provider - a `llama-server` on this machine, no key, no bill.

`llama-server` (the HTTP server in ggml-org/llama.cpp) speaks OpenAI's Chat
Completions dialect at `/v1`, so this is the SDK's OpenAI-compatible base
(`plugin-sdk.md` §6.7) pointed at wherever the server listens. What is
llama.cpp's alone is here: a server that needs no credential, a model the server
already chose, `GET /props` for the context the server was started with and what
it can see, `reasoning_effort` and `chat_template_kwargs.enable_thinking` for the
thinking switch a template honours, and a price of zero because the machine is
yours.

Two ways to run. Point `base_url` at a server you started (`llama-server -m
model.gguf`, with `--mmproj` for pictures and `--api-key` if it is not on
loopback), or name a model in `server_model` and the plugin starts `llama-server`
for you - the binary you installed, found on `PATH` or named in `server_binary`,
on a scrubbed environment, on loopback, with `-hf` doing the download - and
keeps it up for as long as this process runs. Looking for the program is the
plugin's, as `ready()` is for any plugin; the core never does it, and nothing
here fetches an executable.

Requires the `openai` package (`pip install openai`, or `pip install "atlas[openai]"`).
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atlas.sdk.openai_compat import OpenAICompatEmbedder, OpenAICompatProvider, check_base_url
from atlas.sdk.plugin_entry import Plugin, PluginContext
from atlas.sdk.provider import ModelEntry, Pricing, ThinkingLevel
from atlas.sdk.runtime import ConfigError, ProviderError, scrubbed_environment

DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"
"""Where `llama-server` listens unless told otherwise (`--host`, `--port`)."""

PLACEHOLDER_KEY = "no-key"
"""What the `openai` SDK is handed when there is no credential. The SDK refuses
to build a client without one; a server started without `--api-key` ignores the
header. A server started *with* one refuses this, and says so with a 401 that
reads as: add `LLAMA_SERVER_API_KEY` to `~/.atlas/.env`."""

LEVELS: tuple[ThinkingLevel, ...] = ("off", "low", "medium", "high", "max")
"""One menu for every model, because the switch is one request field. `off`
is `reasoning_effort: none` and `enable_thinking: false` for the template - the
two spellings a chat template may read; the rest are `reasoning_effort` as the
server takes it. A template that reads neither ignores both, so a level on a
model that does not reason costs nothing rather than failing the turn - which
is why this menu is the server's rather than the model's, and why
`model_catalog` in `config.json` is the place to narrow it for a model whose
template you know."""

EFFORT: dict[ThinkingLevel, str] = {
    "off": "none",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "max",
}
"""Atlas's levels in `llama-server`'s `reasoning_effort` words."""

FREE = Pricing(input=0.0, output=0.0, cache_read=0.0, cache_write=0.0)
"""A declaration, not a guess (`model-catalog.md` C6): a model on this machine
costs nothing per token, and saying so is what lets `/status` say `$0.00`
rather than *at least*. Electricity is not a token price."""

PROPS_TIMEOUT = 10.0

LLAMA_OVERFLOW_MARKERS = ("exceeds the available context", "context size", "context shift")
"""How `llama-server` words a prompt that does not fit - "the request exceeds
the available context size, try increasing the context size or enable context
shift" - which the core's own list of vendor phrasings does not know."""


class LlamaCppProvider(OpenAICompatProvider):
    """Chat Completions at a `llama-server`, on the SDK's OpenAI-compatible base.

    A `llama-server` usually serves one model, chosen on its command line, and
    answers a request under any name. So `model` is optional here: left empty,
    the first request asks `GET /v1/models` and takes the one id a server with
    one model lists. A server with several (`--models-dir`, the router) needs
    the name, and is told which it could be.

    What the server reasons arrives as `reasoning_content` and is shown; it is
    never sent back, because a template that thinks does not want last turn's
    thoughts and a local context has no room for them. The server's prompt-cache
    reuse is reported in `timings`, not in `usage`, so no cache figure is shown
    rather than a zero that would claim there was none.
    """

    name = "llama-cpp"
    label = "llama.cpp"
    api_key_env_vars = ("LLAMA_SERVER_API_KEY",)
    thinking_levels = LEVELS
    streaming = True
    sampling = True
    """Every field of a `Sampling` is forwarded: `llama-server` takes all of them."""
    local = True

    base_url = DEFAULT_BASE_URL
    placeholder_key = PLACEHOLDER_KEY
    overflow_markers = LLAMA_OVERFLOW_MARKERS

    counts: bool = True
    """Whether the server has `/apply-template` and `/tokenize`. An older build
    answers 404, once, and is then estimated instead of asked every request."""

    server: ManagedServer | None = None
    """The `llama-server` this plugin runs, when `server_model` names one. Class
    state, one per process: every provider the core builds - per profile, per
    model change - shares it."""

    @classmethod
    def configured(
        cls, settings: Mapping[str, Any], *, server: ManagedServer | None = None
    ) -> type[LlamaCppProvider]:
        """This class with the plugin's settings bound, for `register_provider`."""
        base_url = server.base_url if server is not None else _base_url(settings.get("base_url"))
        return cls.bind(base_url=base_url, server=server)

    @classmethod
    def model_families(cls) -> tuple[str, ...]:
        # A `llama-server` model id is whatever the server was started with: a
        # file name, an alias (`--alias`), or `vendor/repo` from `-hf`.
        return ("*",)

    # -- the hooks the base asks ------------------------------------------------

    def thinking_request(self, level: ThinkingLevel) -> dict[str, Any]:
        """`reasoning_effort` is in the SDK's signature; `chat_template_kwargs` is
        llama.cpp's and rides `extra_body`."""
        return {
            "reasoning_effort": EFFORT[level],
            "extra_body": {"chat_template_kwargs": {"enable_thinking": level != "off"}},
        }

    async def prepare(self) -> None:
        """A managed server is up before a request goes to it; an existing
        server is simply asked, and answers for itself."""
        if self.server is not None:
            await self.server.ensure()

    async def resolve_model(self) -> str:
        """The one model a single-model server serves, asked for once.

        Not a guess: the server is asked what it has, and an answer of one id
        is the answer. Several is a router, and the person chooses.
        """
        ids = [str(row.get("id") or "") for row in await self.listing()]
        ids = [i for i in ids if i]
        if len(ids) == 1:
            self.model = ids[0]
            return ids[0]
        if not ids:
            raise ProviderError(
                f"the llama.cpp server at {self.base_url_in_use} lists no model - start it with "
                "`llama-server -m <model.gguf>`"
            )
        raise ConfigError(
            f"the llama.cpp server at {self.base_url_in_use} serves several models - set `model` "
            "(or ATLAS_MODEL) to one of: " + ", ".join(ids)
        )

    def describe_failure(self, exc: Exception) -> Exception | None:
        """What a local server's failures mean, in words that say what to do."""
        message = str(exc)
        where = self.base_url_in_use
        if "Loading model" in message or " 503 " in f" {message} ":
            return ProviderError(
                f"the llama.cpp server at {where} is still loading its model - "
                f"try again in a moment ({exc})"
            )
        if " 401 " in f" {message} " or "Unauthorized" in message:
            return ProviderError(
                f"the llama.cpp server at {where} wants a key - put the server's "
                f"--api-key in ~/.atlas/.env as LLAMA_SERVER_API_KEY ({exc})"
            )
        if type(exc).__name__ == "APIConnectionError":
            return ProviderError(
                f"no llama.cpp server answered at {where} - start one with "
                f"`llama-server -m <model.gguf>`, set base_url under "
                f"plugins_settings.llama-cpp, or set server_model and let the plugin "
                f"start it ({exc})"
            )
        return None

    # -- the listing ------------------------------------------------------------

    async def list_models(self) -> Sequence[ModelEntry] | None:
        """`GET /v1/models`, with `GET /props` beside it for what the listing
        does not say: the context the server was *started* with (`-c`), which
        is the window that matters and is usually far below the model's own,
        and whether a projector is loaded for pictures.

        `/props` is asked with `autoload=false`: on a router it must never
        load a model to answer a question about one. A server that does not
        answer it - an older build, a proxy in front - costs the listing
        nothing but those two facts.
        """
        rows = await self.listing()
        props = await self._props()
        n_ctx = _n_ctx(props)
        modalities = _modalities(props)
        entries = (_entry_of(row, n_ctx=n_ctx, modalities=modalities) for row in rows)
        return [entry for entry in entries if entry is not None]

    async def count_tokens(self, request: Mapping[str, Any]) -> int:
        """The request's exact size, as the server will see it.

        `/apply-template` renders the request through the same parser a chat
        completion goes through - the chat template, the tool schemas, the
        thinking switch - and `/tokenize` counts what it rendered. Two loopback
        round trips, and the fit check needs no estimate.

        Zero, so the base estimates instead, for a request carrying a picture or
        audio (the rendered prompt holds a marker, not the tokens the projector
        will add) and for a server too old to have the endpoints - asked once,
        then not again."""
        if not self.counts or _carries_media(request):
            return 0
        body = {k: v for k, v in request.items() if k not in ("extra_body", "stream")}
        body.update(request.get("extra_body") or {})
        root = _root_of(self.base_url_in_use)
        headers = self._bearer()
        status, rendered = await post_json(root + "/apply-template", body, headers=headers)
        prompt = rendered.get("prompt")
        if status == 404:
            self.counts = False
        if status >= 400 or not isinstance(prompt, str):
            return 0
        status, counted = await post_json(
            root + "/tokenize", {"content": prompt, "add_special": True}, headers=headers
        )
        tokens = counted.get("tokens")
        if status == 404:
            self.counts = False
        return len(tokens) if status < 400 and isinstance(tokens, list) else 0

    def _bearer(self) -> dict[str, str]:
        """The server's `--api-key`, for the endpoints beside `/v1` that check it too."""
        key = str(getattr(self._client, "api_key", "") or "")
        return {"Authorization": f"Bearer {key}"} if key and key != PLACEHOLDER_KEY else {}

    async def loaded_window(self) -> int:
        """The context one slot of the server holds (`-c`, split across
        `--parallel`), from `/props`. The session's budget follows it and every
        request is checked against it before it is sent (`plugin-sdk.md` §6.7);
        a server that does not answer `/props` checks nothing."""
        return _n_ctx(await self._props())

    def does_not_fit(self, needed: int, model: str) -> str:
        return (
            f"this turn is about {needed:,} tokens and the llama.cpp server holds "
            f"{self.served_window:,} per slot - start it with a larger -c (or set "
            f"server_context under plugins_settings.llama-cpp), or fewer --parallel slots"
        )

    async def _props(self) -> Mapping[str, Any]:
        try:
            return await fetch_json(_root_of(self.base_url_in_use) + "/props?autoload=false")
        except Exception:  # the listing stands without it
            return {}


# -- helpers: the address ----------------------------------------------------------


def _base_url(value: Any) -> str:
    """The server's `/v1` root, checked for shape by the SDK's rule."""
    return check_base_url(value, default=DEFAULT_BASE_URL)


def _root_of(base_url: str) -> str:
    """The server itself, for the endpoints `llama-server` keeps beside `/v1`."""
    root = base_url.rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


async def fetch_json(url: str) -> Mapping[str, Any]:
    """One GET through the core's client, as JSON. `allow_private` because the
    address is the operator's setting and a llama.cpp server is, by the plugin's
    whole point, on this machine or this network; the URL was never the model's."""
    from atlas.sdk.web import get

    response = await get(
        url, allow_private=True, timeout=PROPS_TIMEOUT, user_agent="atlas-llama-cpp"
    )
    if response.status >= 400:
        raise ProviderError(f"HTTP {response.status} from {url}")
    decoded = json.loads(response.body.decode("utf-8"))
    return decoded if isinstance(decoded, Mapping) else {}


async def post_json(
    url: str, body: Mapping[str, Any], *, headers: Mapping[str, str] | None = None
) -> tuple[int, Mapping[str, Any]]:
    """One POST through the core's client: the status, and the body as JSON.
    Never raises for a status or a network failure - a count that cannot be
    had is an estimate, not a failed turn."""
    from atlas.sdk.web import post

    try:
        response = await post(
            url,
            json=dict(body),
            allow_private=True,
            timeout=PROPS_TIMEOUT,
            headers=dict(headers or {}),
            user_agent="atlas-llama-cpp",
            max_bytes=8_000_000,
        )
        decoded = json.loads(response.body.decode("utf-8") or "{}")
    except Exception:  # unreachable, refused, not JSON: nothing counted
        return 599, {}
    return response.status, decoded if isinstance(decoded, Mapping) else {}


def _carries_media(request: Mapping[str, Any]) -> bool:
    """Whether any message carries a part that is not text."""
    for message in request.get("messages") or ():
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, list) and any(
            isinstance(part, Mapping) and part.get("type") != "text" for part in content
        ):
            return True
    return False


# -- helpers: the listing --------------------------------------------------------------


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _n_ctx(props: Mapping[str, Any]) -> int:
    """The context one slot holds: what the server was started with, split
    across `--parallel`. This is the window a request actually gets."""
    settings = _mapping(props.get("default_generation_settings"))
    value = settings.get("n_ctx")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _modalities(props: Mapping[str, Any]) -> tuple[str, ...]:
    """`text`, plus what a loaded projector (`--mmproj`) lets the server take.
    Empty where `/props` said nothing, because the catalog never guesses."""
    listed = _mapping(props.get("modalities"))
    if not listed:
        return ()
    out = ["text"]
    if listed.get("vision"):
        out.append("image")
    if listed.get("audio"):
        out.append("audio")
    return tuple(out)


def _entry_of(
    item: Mapping[str, Any], *, n_ctx: int, modalities: tuple[str, ...]
) -> ModelEntry | None:
    """One row of `GET /v1/models` as a catalog entry - ids, numbers and dates.

    The window is the server's `n_ctx` where it answered, else the model's
    trained context from `meta` - the ceiling, which a server started with
    `-c` is usually well under."""
    model_id = str(item.get("id", "") or "")
    if not model_id:
        return None
    meta = _mapping(item.get("meta"))
    trained = meta.get("n_ctx_train")
    trained = int(trained) if isinstance(trained, int) and not isinstance(trained, bool) else 0
    window = min(n_ctx, trained) if n_ctx and trained else (n_ctx or trained)
    created = item.get("created")
    released = ""
    if isinstance(created, int) and not isinstance(created, bool) and created > 0:
        released = datetime.fromtimestamp(created, tz=UTC).strftime("%Y-%m-%d")
    return ModelEntry(
        id=model_id,
        context_window=window,
        cost=FREE,
        modalities=modalities,
        released=released,
    )


# -- the embedder ---------------------------------------------------------------------


class LlamaCppEmbedder(OpenAICompatEmbedder):
    """Vectors from a `llama-server --embedding`, for memory search.

    An embedding model is its own server: `embedding_model` names one for the
    plugin to run (`--embedding --pooling mean`, on `embedding_port`), or
    `embedding_url` names one you started. The width is read from the server -
    `n_embd` in `GET /v1/models` - so nobody has to know it; `embedding_dimensions`
    overrides for a server that lists nothing.

    The width is asked for synchronously, because the core reads `dimensions`
    from synchronous code before it ever calls `embed`, and zero there means
    "cannot embed yet". The ask is one GET on loopback with a short deadline,
    made only while the width is unknown and never again once it is; a managed
    server that is not running is started on that first ask and answers a later
    one, so the session's first memory refresh runs on keyword and the next
    embed pass has its vectors. That is the cost of a width nobody declared.

    **It may raise.** Memory search treats a missing, failing or slow embedder
    as a reason to answer from the keyword index, never as an error to raise
    at the model.
    """

    name = "llama-cpp"
    label = "llama.cpp"
    base_url = DEFAULT_BASE_URL
    api_key_env_vars = ("LLAMA_SERVER_API_KEY",)
    placeholder_key = PLACEHOLDER_KEY
    default_model = "default"
    """What is sent as the model on a server that serves one: it answers under
    any name."""

    def __init__(
        self,
        model: str = "",
        *,
        api_key: str | None = None,
        auth_token: str | None = None,
        base_url: str | None = None,
        dimensions: int = 0,
        server: ManagedServer | None = None,
        client: Any = None,
        probe: Any = None,
    ) -> None:
        super().__init__(
            model,
            api_key=api_key,
            auth_token=auth_token,
            base_url=_base_url(
                server.base_url if server is not None else (base_url or DEFAULT_BASE_URL)
            ),
            dimensions=dimensions,
            client=client,
        )
        self._server = server
        self._probe = probe if probe is not None else _probe_width

    @property
    def dimensions(self) -> int:
        if self._dimensions <= 0:
            width = self._probe(_root_of(self.base_url_in_use))
            if width > 0:
                self._dimensions = width
            elif self._server is not None:
                # Not answering: start it, and let a later ask find it up. A
                # server that cannot start (no binary) leaves the width at
                # zero - nothing may raise out of here into a search - and
                # says why on the first `embed`, or on the chat provider's
                # first request when it is managed too.
                with contextlib.suppress(ProviderError):
                    self._server.start()
        return self._dimensions

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if texts and self._server is not None:
            await self._server.ensure()
        return await super().embed(texts)


def _probe_width(root: str) -> int:
    """`n_embd` from `GET /v1/models`, synchronously, or zero. One loopback
    round trip with a short deadline; a server that is down refuses at once
    and one that is loading answers 503 at once, so neither holds the caller."""
    try:
        with urllib.request.urlopen(root + "/v1/models", timeout=PROBE_TIMEOUT) as response:
            listed = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return 0
    data = listed.get("data") if isinstance(listed, Mapping) else None
    for item in data if isinstance(data, list) else []:
        width = _mapping(_mapping(item).get("meta")).get("n_embd")
        if isinstance(width, int) and not isinstance(width, bool) and width > 0:
            return width
    return 0


# -- the managed server ---------------------------------------------------------------

READY_POLL = 0.5
PROBE_TIMEOUT = 1.0
STOP_GRACE = 5.0
LOG_TAIL = 12
HF_REF = re.compile(r"^[\w.-]+/[\w.-]+(:[\w.-]+)?$")
"""`org/repo` or `org/repo:quant`, as `llama-server -hf` takes it."""


class ServerSpec:
    """What one managed `llama-server` is started with. Every field is a
    setting the operator wrote; the argv is built from them here and nowhere
    else, and is never a template a setting could inject into.

    A plain class and not a dataclass: Atlas imports a plugin's module
    without registering it in `sys.modules`, and `dataclass` under
    `from __future__ import annotations` looks the module up there.
    """

    def __init__(
        self,
        *,
        role: str,
        binary: str,
        model: str,
        port: int,
        context: int = 0,
        gpu_layers: int | None = None,
        mmproj: str = "",
        args: tuple[str, ...] = (),
        startup_timeout: float = 600.0,
        log_dir: Path | None = None,
    ) -> None:
        self.role = role
        """`chat` or `embedding` - which settings it came from, and its log's name."""
        self.binary = binary
        self.model = model
        self.port = port
        self.context = context
        self.gpu_layers = gpu_layers
        self.mmproj = mmproj
        self.args = tuple(args)
        self.startup_timeout = startup_timeout
        self.log_dir = log_dir

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def argv(self) -> list[str]:
        """The command line. A model that exists on disk is `-m`; `hf:org/repo`
        or a bare `org/repo[:quant]` is `-hf`, and the server does the download
        into its own cache. Loopback only, always: a managed server is this
        machine's, and `--api-key` is not offered because nothing off it should
        be able to reach the port at all."""
        argv = [self.binary]
        model = self.model.strip()
        if model.startswith("hf:"):
            argv += ["-hf", model[3:]]
        elif not os.path.exists(model) and HF_REF.match(model):
            argv += ["-hf", model]
        else:
            argv += ["-m", model]
        argv += ["--host", "127.0.0.1", "--port", str(self.port)]
        if self.context > 0:
            argv += ["-c", str(self.context)]
        if self.gpu_layers is not None:
            argv += ["-ngl", str(self.gpu_layers)]
        if self.mmproj:
            argv += ["--mmproj", self.mmproj]
        if self.role == "embedding":
            argv += ["--embedding", "--pooling", "mean"]
        argv += list(self.args)
        return argv


class ManagedServer:
    """One `llama-server` this process owns.

    Started on first need and never at import or register - `atlas --tools`
    installs this plugin too and must not leave a model loaded behind it.
    Stopped when this process exits (`atexit`), which is the gateway's exit for
    a detached gateway and the REPL's for `--local`: the server is a resource of
    the process, not of a session, because every lane in a gateway shares it.
    A cancelled turn never kills it - ownership left the call the moment it
    started, as with a backgrounded command.

    A server already answering on the port is used and not replaced: the
    operator started one by hand, or a previous run's is still up. Ownership
    then stays with whoever started it.
    """

    def __init__(self, spec: ServerSpec, *, environment: Mapping[str, str] | None = None) -> None:
        self.spec = spec
        self.process: subprocess.Popen[bytes] | None = None
        self.adopted = False
        """Whether something else's server answered before this started one."""
        self._environment = environment
        self._lock = threading.Lock()
        self._starting: asyncio.Lock | None = None
        self._log: Any = None

    @property
    def base_url(self) -> str:
        return self.spec.base_url

    @property
    def root(self) -> str:
        return f"http://127.0.0.1:{self.spec.port}"

    @property
    def log_path(self) -> Path | None:
        if self.spec.log_dir is None:
            return None
        return self.spec.log_dir / f"llama-server-{self.spec.role}.log"

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        """Spawn, if not already running. Synchronous and instant: the process
        is started and not waited for, so this is safe from a property."""
        with self._lock:
            if self.running or self.adopted:
                return
            if _probe_health(self.root) is not None:
                self.adopted = True
                return
            binary = self.spec.binary
            if not binary:
                raise ProviderError(
                    "llama-server was not found on PATH - install llama.cpp "
                    "(`winget install ggml.llamacpp`, `brew install llama.cpp`, or a release "
                    "from github.com/ggml-org/llama.cpp) or set server_binary under "
                    "plugins_settings.llama-cpp"
                )
            argv = self.spec.argv()
            log_path = self.log_path
            if log_path is not None:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log = log_path.open("ab")
                self._log.write(
                    f"\n--- {datetime.now(UTC).isoformat()} {' '.join(argv)}\n".encode()
                )
                self._log.flush()
            environment = (
                self._environment if self._environment is not None else scrubbed_environment()
            )
            try:
                self.process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=self._log if self._log is not None else subprocess.DEVNULL,
                    stderr=subprocess.STDOUT,
                    env=dict(environment),
                )
            except OSError as exc:
                self._close_log()
                raise ProviderError(f"could not start {binary}: {exc}") from exc
            atexit.register(self.stop)

    async def ensure(self) -> None:
        """Up and answering `/health` with 200, or a `ProviderError` that says
        why not - with the log's last lines when the process died."""
        if self.adopted or (self.running and await self._healthy()):
            return
        if self._starting is None:
            self._starting = asyncio.Lock()
        async with self._starting:
            if self.adopted:
                return
            if not self.running:
                self.start()
                if self.adopted:
                    return
            deadline = time.monotonic() + self.spec.startup_timeout
            while time.monotonic() < deadline:
                if not self.running:
                    code = self.process.returncode if self.process is not None else None
                    raise ProviderError(
                        f"llama-server ({self.spec.role}) exited with code {code}"
                        + self._log_tail()
                    )
                if await self._healthy():
                    return
                await asyncio.sleep(READY_POLL)
            raise ProviderError(
                f"llama-server ({self.spec.role}) did not become ready within "
                f"{self.spec.startup_timeout:.0f}s - a first `-hf` download or a CPU-only load "
                f"can take longer; raise server_startup_timeout" + self._log_tail()
            )

    async def _healthy(self) -> bool:
        try:
            await fetch_json(self.root + "/health")
        except Exception:
            return False
        return True

    def stop(self) -> None:
        """Terminate, wait a grace, kill. Idempotent; registered with `atexit`."""
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            self._close_log()
            return
        process.terminate()
        try:
            process.wait(STOP_GRACE)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        self._close_log()

    def _close_log(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None

    def _log_tail(self) -> str:
        path = self.log_path
        if path is None or not path.is_file():
            return ""
        try:
            lines = path.read_text("utf-8", errors="replace").splitlines()[-LOG_TAIL:]
        except OSError:
            return ""
        return f"; the log ({path}) ends:\n" + "\n".join(lines) if lines else ""


def _probe_health(root: str) -> int | None:
    """`/health`'s status, synchronously, or `None` for nothing listening."""
    try:
        with urllib.request.urlopen(root + "/health", timeout=PROBE_TIMEOUT) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _find_binary(configured: Any) -> str:
    """`server_binary`, else `llama-server` on PATH, else nothing - and nothing
    is reported when the server is needed, not at register."""
    named = str(configured or "").strip()
    if named:
        return named
    return shutil.which("llama-server") or ""


def _server_spec(
    ctx: PluginContext, role: str, *, model: str, port: int, default_port: int
) -> ServerSpec:
    chat = role == "chat"
    args = ctx.setting("server_args", []) if chat else ()
    return ServerSpec(
        role=role,
        binary=_find_binary(ctx.setting("server_binary", "")),
        model=model,
        port=int(port or default_port),
        context=int(ctx.setting("server_context", 0) or 0) if chat else 0,
        gpu_layers=_optional_int(ctx.setting("server_gpu_layers")),
        mmproj=str(ctx.setting("server_mmproj", "") or "") if chat else "",
        args=tuple(str(a) for a in args) if isinstance(args, list | tuple) else (),
        startup_timeout=float(ctx.setting("server_startup_timeout", 600) or 600),
        log_dir=Path(ctx.workspace) / ".atlas" / "llama-cpp",
    )


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


class LlamaCppPlugin(Plugin):
    """The llama.cpp provider: a server on this machine, no key, no bill."""

    name = "llama-cpp"
    description = "A model provider for a llama.cpp server on this machine - no key, no bill."

    def register(self, ctx: PluginContext) -> None:
        # The discriminator: a model named for the
        # plugin to serve means the plugin owns the process; none means
        # `base_url` names a server somebody else started.
        chat_model = str(ctx.setting("server_model", "") or "").strip()
        server = (
            ManagedServer(
                _server_spec(
                    ctx,
                    "chat",
                    model=chat_model,
                    port=int(ctx.setting("server_port", 0) or 0),
                    default_port=8080,
                )
            )
            if chat_model
            else None
        )
        settings = {"base_url": ctx.setting("base_url", DEFAULT_BASE_URL)}
        ctx.register_provider("llama-cpp", LlamaCppProvider.configured(settings, server=server))

        embedding_model = str(ctx.setting("embedding_model", "") or "").strip()
        embedding_url = str(ctx.setting("embedding_url", "") or "").strip()
        width = int(ctx.setting("embedding_dimensions", 0) or 0)
        embedding_server = (
            ManagedServer(
                _server_spec(
                    ctx,
                    "embedding",
                    model=embedding_model,
                    port=int(ctx.setting("embedding_port", 0) or 0),
                    default_port=8081,
                )
            )
            if embedding_model and not embedding_url
            else None
        )

        def embedder(model: str = "", **kwargs: Any) -> LlamaCppEmbedder:
            return LlamaCppEmbedder(
                # On a server the plugin runs, the id is whatever it loaded;
                # on one you started, it is what you named.
                model if embedding_server is not None else (model or embedding_model),
                base_url=embedding_url or settings["base_url"],
                dimensions=width,
                server=embedding_server,
                **kwargs,
            )

        ctx.register_embedder("llama-cpp", embedder)
