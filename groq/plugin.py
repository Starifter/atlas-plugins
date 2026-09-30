"""Groq provider - open models on Groq's LPUs, fast.

Groq speaks OpenAI's Chat Completions at `https://api.groq.com/openai/v1`, so this
is the SDK's OpenAI-compatible base with Groq's own parts declared on it
(`plugin-sdk.md` §6.7): the reply ceiling under `max_completion_tokens`, a thinking
switch that differs by model family, and a listing that says each model's window
and reply ceiling.

Requires the `openai` package (`pip install openai`).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from atlas.sdk.openai_compat import OpenAICompatProvider
from atlas.sdk.plugin_entry import Plugin, PluginContext
from atlas.sdk.provider import ModelEntry, ThinkingLevel

BASE_URL = "https://api.groq.com/openai/v1"
TRANSCRIPTIONS_URL = f"{BASE_URL}/audio/transcriptions"
DEFAULT_TRANSCRIPTION_MODEL = "whisper-large-v3-turbo"
"""Groq's fastest Whisper, and its cheapest; `whisper-large-v3` is the accurate one."""

AUDIO_EXTENSIONS: dict[str, str] = {
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/wav": "wav",
    "audio/webm": "webm",
    "audio/flac": "flac",
}
"""Every audio type Atlas stores, each of which Groq documents taking."""

EFFORT_ONLY: tuple[ThinkingLevel, ...] = ("low", "medium", "high")
"""GPT-OSS: `reasoning_effort` low to high, and no off - `include_reasoning: false`
only hides the reasoning, it does not stop it."""

SWITCHED: tuple[ThinkingLevel, ...] = ("off", "low", "medium", "high")
"""Qwen: the same efforts, and `none` turns reasoning off."""


def family_levels(model: str) -> tuple[ThinkingLevel, ...]:
    """The menu a model's family takes, by its id. Empty for a model Groq serves
    without a thinking control - Llama, and anything not named here."""
    name = model.lower()
    if "gpt-oss" in name:
        return EFFORT_ONLY
    if "qwen3" in name:
        return SWITCHED
    return ()


class GroqProvider(OpenAICompatProvider):
    """Chat Completions at Groq, on the SDK's OpenAI-compatible base.

    There is no default model: Groq's line-up changes month to month, and which
    one to run is the user's decision. `/model list --refresh` fetches it.
    """

    name = "groq"
    label = "Groq"
    api_key_env_vars = ("GROQ_API_KEY",)
    base_url = BASE_URL
    thinking_levels = SWITCHED
    streaming = True
    sampling = True
    max_tokens_field = "max_completion_tokens"
    """`max_tokens` is deprecated at Groq in favour of this."""
    reasoning_fields = ("reasoning", "reasoning_content")
    """`reasoning` is Groq's field, with `reasoning_format: parsed`."""
    overflow_markers = ("context_length_exceeded", "reduce the length of the messages")
    """Groq's own words for a prompt that does not fit."""

    @classmethod
    def levels_for(cls, model: str) -> tuple[ThinkingLevel, ...]:
        """What `model_catalog` or the listing says, else the family's menu."""
        declared = cls.declared_entry(model).thinking_levels
        return family_levels(model) if declared is None else super().levels_for(model)

    def thinking_request(self, level: ThinkingLevel) -> dict[str, Any]:
        """`reasoning_effort` where the model has a menu, and nothing where it
        does not - a Llama model is never sent a reasoning field."""
        if level not in self.thinking_levels:
            return {}
        if "gpt-oss" in self.model.lower():
            return {"reasoning_effort": level}
        # Parsed, because `raw` puts the reasoning inside the reply's text as
        # <think> tags, and Groq refuses `raw` alongside tools anyway.
        effort = "none" if level == "off" else level
        return {"reasoning_effort": effort, "extra_body": {"reasoning_format": "parsed"}}

    def entry_of(self, row: Mapping[str, Any]) -> ModelEntry | None:
        """One row of `GET /models`: the id, the window, the reply ceiling and the
        date. A model Groq marks inactive is not offered."""
        model_id = str(row.get("id", "") or "")
        if not model_id or row.get("active") is False:
            return None
        window = row.get("context_window")
        ceiling = row.get("max_completion_tokens")
        base = super().entry_of(row)
        return ModelEntry(
            id=model_id,
            context_window=window if isinstance(window, int) and window > 0 else 0,
            max_output=ceiling if isinstance(ceiling, int) and ceiling > 0 else 0,
            thinking_levels=family_levels(model_id),
            released=base.released if base is not None else "",
        )


class GroqWhisper:
    """Groq's transcriptions endpoint as a media reader (`media.md` §8.3).

    Named `groq/whisper`, so the core hands it the key the `groq` provider would
    use - a profile, or `GROQ_API_KEY` in `~/.atlas/.env` - and never reads it on
    the reader's behalf. It sees the bytes and a language hint, never the
    conversation. Priority 40: ahead of `openai/whisper` at 50, because the same
    model costs a tenth as much here; `audio_reader` pins either.
    """

    name = "groq/whisper"
    kinds: tuple[str, ...] = ("audio",)
    accepts: tuple[str, ...] = tuple(AUDIO_EXTENSIONS)
    priority = 40

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
        self._url = (
            f"{base_url.rstrip('/')}/audio/transcriptions" if base_url else TRANSCRIPTIONS_URL
        )

    def ready(self) -> str:
        if not self._key:
            return "no groq key (atlas auth add groq, or GROQ_API_KEY in ~/.atlas/.env)"
        return ""

    async def read(self, reading: Any) -> Any:
        from atlas.sdk.media import Understood
        from atlas.sdk.web import post

        fields = [("model", self.model), ("response_format", "json")]
        if reading.language:
            fields.append(("language", reading.language))
        ext = AUDIO_EXTENSIONS.get(reading.media_type, "bin")
        body, content_type = multipart(fields, f"audio.{ext}", reading.media_type, reading.data)
        response = await post(
            self._url,
            data=body,
            content_type=content_type,
            headers={"Authorization": f"Bearer {self._key}"},
            timeout=reading.timeout,
            max_bytes=4 * 1024 * 1024,
            user_agent="atlas-groq",
        )
        if response.status >= 400:
            raise RuntimeError(f"HTTP {response.status} from Groq: {_why(response.body)}")
        try:
            parsed = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"unreadable reply from Groq: {exc}") from None
        text = str(parsed.get("text", "") or "") if isinstance(parsed, dict) else ""
        return Understood(text)


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
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:200]
    except ValueError:
        pass
    return body[:200].decode("utf-8", "replace").strip()


class GroqPlugin(Plugin):
    """The Groq provider."""

    name = "groq"
    description = "The Groq model provider - open models, served fast."

    def register(self, ctx: PluginContext) -> None:
        ctx.register_provider("groq", GroqProvider)
        # The transcriber: Groq's Whisper under the key the provider uses, which
        # the core hands in because the reader is named for the vendor.
        transcription_model = str(ctx.setting("transcription_model", "") or "")
        ctx.register_media_reader(
            "groq/whisper",
            lambda **kwargs: GroqWhisper(model=transcription_model, **kwargs),
        )
