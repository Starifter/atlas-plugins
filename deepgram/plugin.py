"""Deepgram - speech-to-text for voice notes and recordings.

A media reader and nothing else (`media.md` §8.3): Deepgram sells transcription and
no model Atlas could chat with, so its key is the plugin's own, read from
`DEEPGRAM_API_KEY` in the environment or `~/.atlas/.env` - never an auth profile,
which is for model vendors and rotates with them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from atlas.sdk.auth import SecretRef, read_dotenv, resolve
from atlas.sdk.media import Reading, Understood
from atlas.sdk.plugin_entry import Plugin, PluginContext
from atlas.sdk.runtime import CredentialError
from atlas.sdk.web import post

ENDPOINT = "https://api.deepgram.com/v1/listen"
DEFAULT_MODEL = "nova-3"

AUDIO_TYPES: tuple[str, ...] = (
    "audio/ogg",
    "audio/mpeg",
    "audio/mp4",
    "audio/wav",
    "audio/webm",
    "audio/flac",
)
"""Every audio type Atlas stores. Deepgram reads the container itself; the type
is sent as the body's `Content-Type`."""


class Deepgram:
    """Deepgram's pre-recorded endpoint as a media reader.

    The bytes go up as the request body, as Deepgram documents, with the model
    and the formatting in the query. Without an `audio_language` hint Deepgram
    is asked to detect the language rather than assume English. Priority 48:
    after `groq/whisper` and `xai/stt`, before `openai/whisper`.
    """

    name = "deepgram"
    kinds: tuple[str, ...] = ("audio",)
    accepts: tuple[str, ...] = AUDIO_TYPES
    priority = 48

    def __init__(
        self,
        *,
        workspace: Path | str = ".",
        api_key_env: str = "DEEPGRAM_API_KEY",
        model: str = "",
        smart_format: bool = True,
    ) -> None:
        self.workspace = Path(workspace)
        self.ref = SecretRef("env", api_key_env or "DEEPGRAM_API_KEY")
        self.model = (model or DEFAULT_MODEL).strip()
        self.smart_format = smart_format

    def ready(self) -> str:
        """Whether a key can be had. Read fresh, never cached at install, so a
        key stored mid-session is used on the next voice note."""
        try:
            if self._key():
                return ""
        except CredentialError:
            pass
        return f"no {self.ref.id} (store a Deepgram key in ~/.atlas/.env)"

    def _key(self) -> str:
        return resolve(self.ref, dotenv=read_dotenv(self.workspace))

    def query(self, reading: Reading) -> dict[str, str]:
        params = {"model": self.model}
        if self.smart_format:
            params["smart_format"] = "true"
        if reading.language:
            params["language"] = reading.language
        else:
            params["detect_language"] = "true"
        return params

    async def read(self, reading: Reading) -> Understood:
        response = await post(
            f"{ENDPOINT}?{urlencode(self.query(reading))}",
            data=reading.data,
            content_type=reading.media_type,
            headers={"Authorization": f"Token {self._key()}"},
            timeout=reading.timeout,
            max_bytes=4 * 1024 * 1024,
            user_agent="atlas-deepgram",
        )
        if response.status >= 400:
            raise RuntimeError(f"HTTP {response.status} from Deepgram: {_why(response.body)}")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"unreadable reply from Deepgram: {exc}") from None
        return Understood(transcript_of(payload), cost=_duration(payload))


def transcript_of(payload: Any) -> str:
    """`results.channels[0].alternatives[0].transcript`, or empty - which the pass
    reports as *nothing came back*."""
    try:
        channel = payload["results"]["channels"][0]
        return str(channel["alternatives"][0].get("transcript", "") or "")
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


def _duration(payload: Any) -> str:
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    seconds = metadata.get("duration") if isinstance(metadata, dict) else None
    return f"{seconds:.0f}s" if isinstance(seconds, int | float) else ""


def _why(body: bytes) -> str:
    """Deepgram's own error text, if it sent one, without printing a page."""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            for field in ("err_msg", "message", "reason"):
                if data.get(field):
                    return str(data[field])[:200]
    except ValueError:
        pass
    return body[:200].decode("utf-8", "replace").strip()


class DeepgramPlugin(Plugin):
    name = "deepgram"
    description = "Deepgram speech-to-text as a media reader for voice notes."

    def register(self, ctx: PluginContext) -> None:
        workspace = ctx.workspace
        api_key_env = str(ctx.setting("api_key_env", "DEEPGRAM_API_KEY") or "DEEPGRAM_API_KEY")
        model = str(ctx.setting("model", "") or "")
        smart_format = bool(ctx.setting("smart_format", True))
        ctx.register_media_reader(
            "deepgram",
            lambda **_: Deepgram(
                workspace=workspace,
                api_key_env=api_key_env,
                model=model,
                smart_format=smart_format,
            ),
        )
