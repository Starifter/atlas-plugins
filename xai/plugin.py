"""xAI provider - Grok, at xAI.

xAI speaks OpenAI's Chat Completions at `https://api.x.ai/v1`, so this is the SDK's
OpenAI-compatible base with xAI's own parts declared on it (`plugin-sdk.md` §6.7):
the reply ceiling under `max_completion_tokens`, `reasoning_effort` on the models that
reason - which cannot be switched off - and a listing that prices each model, with a
second rate above its long-context threshold.

Requires the `openai` package (`pip install openai`).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from atlas.sdk.openai_compat import OpenAICompatProvider
from atlas.sdk.plugin_entry import Plugin, PluginContext
from atlas.sdk.provider import ModelEntry, PriceTier, Pricing, ThinkingLevel

BASE_URL = "https://api.x.ai/v1"
STT_URL = f"{BASE_URL}/stt"
DEFAULT_TRANSCRIPTION_MODEL = "grok-voice-transcribe-2.0"

AUDIO_EXTENSIONS: dict[str, str] = {
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/wav": "wav",
    "audio/flac": "flac",
}
"""The audio types Atlas stores that xAI documents taking. WebM is not one of
them, so a `.weba` voice note goes to another reader."""

LEVELS: tuple[ThinkingLevel, ...] = ("low", "medium", "high", "max")
"""xAI's efforts, with its `xhigh` as Atlas's `max`. There is no `off`: a Grok model
that reasons cannot be told not to, and one that does not is a different model id."""

EFFORT: dict[ThinkingLevel, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "xhigh",
}
FROM_EFFORT: dict[str, ThinkingLevel] = {value: key for key, value in EFFORT.items()}

CENTS_PER_100M = 10_000
"""xAI prices in US cents per 100 million tokens; the catalog in dollars per million."""


def family_levels(model: str) -> tuple[ThinkingLevel, ...]:
    """The menu before a listing has said: a Grok 4 model reasons at low to high,
    and one named `non-reasoning` - or anything else - has no control."""
    name = model.lower()
    if "non-reasoning" in name:
        return ()
    if name.startswith("grok-4"):
        return ("low", "medium", "high")
    return ()


def _dollars(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return float(value) / CENTS_PER_100M


def pricing_of(row: Mapping[str, Any]) -> Pricing | None:
    """The row's text prices, with the long-context rates as a second tier from the
    threshold on. xAI sells no cache write, so it is priced as the nothing it costs."""
    base = Pricing(
        input=_dollars(row.get("prompt_text_token_price")),
        output=_dollars(row.get("completion_text_token_price")),
        cache_read=_dollars(row.get("cached_prompt_text_token_price")),
        cache_write=0.0,
    )
    if base.input is None or base.output is None:
        return None
    threshold = row.get("long_context_threshold")
    long_in = _dollars(row.get("prompt_text_token_price_long_context"))
    long_out = _dollars(row.get("completion_text_token_price_long_context"))
    if not isinstance(threshold, int) or threshold <= 0 or long_in is None or long_out is None:
        return base
    long_cached = _dollars(row.get("cached_prompt_text_token_price_long_context"))
    cached = base.cache_read if base.cache_read is not None else 0.0
    return Pricing(
        input=base.input,
        output=base.output,
        cache_read=base.cache_read,
        cache_write=0.0,
        tiers=(
            PriceTier(0, threshold, base.input, base.output, cached, 0.0),
            PriceTier(
                threshold,
                None,
                long_in,
                long_out,
                long_cached if long_cached is not None else cached,
                0.0,
            ),
        ),
    )


class XAIProvider(OpenAICompatProvider):
    """Chat Completions at xAI, on the SDK's OpenAI-compatible base."""

    name = "xai"
    label = "xAI"
    api_key_env_vars = ("XAI_API_KEY",)
    base_url = BASE_URL
    thinking_levels = LEVELS
    streaming = True
    sampling = False
    """Not declared: Grok's reasoning models refuse `presence_penalty`,
    `frequency_penalty` and `stop` with an error rather than ignoring them."""
    max_tokens_field = "max_completion_tokens"
    """`max_tokens` is deprecated at xAI in favour of this."""

    @classmethod
    def levels_for(cls, model: str) -> tuple[ThinkingLevel, ...]:
        declared = cls.declared_entry(model).thinking_levels
        return family_levels(model) if declared is None else super().levels_for(model)

    def thinking_request(self, level: ThinkingLevel) -> dict[str, Any]:
        if level not in self.thinking_levels:
            return {}
        return {"reasoning_effort": EFFORT[level]}

    def entry_of(self, row: Mapping[str, Any]) -> ModelEntry | None:
        """One row of `GET /models`: the window, the price, and the efforts the
        model takes - an empty list is a model that does not reason."""
        model_id = str(row.get("id", "") or "")
        if not model_id:
            return None
        window = row.get("context_length")
        capabilities = row.get("capabilities")
        efforts = (
            capabilities.get("reasoning_effort") if isinstance(capabilities, Mapping) else None
        )
        levels: tuple[ThinkingLevel, ...] | None = None
        if isinstance(efforts, list):
            named = {FROM_EFFORT[str(e)] for e in efforts if str(e) in FROM_EFFORT}
            levels = tuple(level for level in LEVELS if level in named)
        base = super().entry_of(row)
        return ModelEntry(
            id=model_id,
            context_window=window if isinstance(window, int) and window > 0 else 0,
            thinking_levels=levels,
            cost=pricing_of(row),
            released=base.released if base is not None else "",
        )


class XAITranscriber:
    """xAI's speech-to-text endpoint as a media reader (`media.md` §8.3).

    Named `xai/stt`, so the core hands it the key the `xai` provider would use -
    a profile, or `XAI_API_KEY` in `~/.atlas/.env` - and never reads it on the
    reader's behalf. It sees the bytes and a language hint, never the
    conversation. Priority 45: between `groq/whisper` (40) and `openai/whisper`
    (50), in the order of what an hour of audio costs at each.
    """

    name = "xai/stt"
    kinds: tuple[str, ...] = ("audio",)
    accepts: tuple[str, ...] = tuple(AUDIO_EXTENSIONS)
    priority = 45

    def __init__(
        self,
        *,
        model: str = "",
        api_key: str | None = None,
        auth_token: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model = (model or DEFAULT_TRANSCRIPTION_MODEL).strip()
        self._key = api_key or auth_token or ""
        self._url = f"{base_url.rstrip('/')}/stt" if base_url else STT_URL

    def ready(self) -> str:
        if not self._key:
            return "no xai key (atlas auth add xai, or XAI_API_KEY in ~/.atlas/.env)"
        return ""

    async def read(self, reading: Any) -> Any:
        from atlas.sdk.media import Understood
        from atlas.sdk.web import post

        fields = [("model", self.model)]
        if reading.language:
            fields.append(("language", reading.language))
        ext = AUDIO_EXTENSIONS.get(reading.media_type, "bin")
        # xAI wants the file as the last field of the form, which `multipart`
        # always puts it.
        body, content_type = multipart(fields, f"audio.{ext}", reading.media_type, reading.data)
        response = await post(
            self._url,
            data=body,
            content_type=content_type,
            headers={"Authorization": f"Bearer {self._key}"},
            timeout=reading.timeout,
            max_bytes=4 * 1024 * 1024,
            user_agent="atlas-xai",
        )
        if response.status >= 400:
            raise RuntimeError(f"HTTP {response.status} from xAI: {_why(response.body)}")
        try:
            parsed = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"unreadable reply from xAI: {exc}") from None
        text = str(parsed.get("text", "") or "") if isinstance(parsed, dict) else ""
        duration = parsed.get("duration") if isinstance(parsed, dict) else None
        cost = f"{duration:.0f}s" if isinstance(duration, int | float) else ""
        return Understood(text, cost=cost)


def multipart(
    fields: list[tuple[str, str]], filename: str, media_type: str, data: bytes
) -> tuple[bytes, str]:
    """A `multipart/form-data` body with the file last, and its content type."""
    boundary = f"----atlas{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields:
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        ).encode()
    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\nContent-Type: {media_type}\r\n\r\n'
    ).encode()
    body += data
    body += f"\r\n--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def _why(body: bytes) -> str:
    """The vendor's own error message, if it sent one, without printing a page."""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])[:200]
            if isinstance(error, str) and error:
                return error[:200]
    except ValueError:
        pass
    return body[:200].decode("utf-8", "replace").strip()


class XAIPlugin(Plugin):
    """The xAI provider."""

    name = "xai"
    description = "The xAI model provider - Grok, at xAI."

    def register(self, ctx: PluginContext) -> None:
        ctx.register_provider("xai", XAIProvider)
        # The transcriber: xAI's speech-to-text under the key the provider uses,
        # which the core hands in because the reader is named for the vendor.
        transcription_model = str(ctx.setting("transcription_model", "") or "")
        ctx.register_media_reader(
            "xai/stt",
            lambda **kwargs: XAITranscriber(model=transcription_model, **kwargs),
        )
