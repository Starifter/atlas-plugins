"""Together AI provider - open models, hosted at Together.

Together speaks OpenAI's Chat Completions at `https://api.together.ai/v1`, so this is
the SDK's OpenAI-compatible base with Together's own parts declared on it
(`plugin-sdk.md` §6.7): a request that would not fit is refused rather than cut, a
listing that is a bare array with a price on each row, and a cache figure reported
in either of two places.

Requires the `openai` package (`pip install openai`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from atlas.sdk.openai_compat import OpenAICompatProvider
from atlas.sdk.plugin_entry import Plugin, PluginContext
from atlas.sdk.provider import ModelEntry, Pricing, Usage
from atlas.sdk.runtime import ProviderError

BASE_URL = "https://api.together.ai/v1"


def _rate(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return float(value)


def pricing_of(row: Mapping[str, Any]) -> Pricing | None:
    """USD per million, as Together lists it. A row priced at zero for both input and
    output is *unknown*, not free: Together lists models it only serves on dedicated
    hardware that way, and a free-looking turn would be a false one."""
    raw = row.get("pricing")
    if not isinstance(raw, Mapping):
        return None
    price = Pricing(
        input=_rate(raw.get("input")),
        output=_rate(raw.get("output")),
        cache_read=_rate(raw.get("cached_input")),
    )
    if price.input is None or price.output is None or (price.input == 0 and price.output == 0):
        return None
    return price


class TogetherProvider(OpenAICompatProvider):
    """Chat Completions at Together, on the SDK's OpenAI-compatible base.

    There is no default model and no `/think`: Together hosts a hundred models whose
    reasoning switches differ - `reasoning.enabled` on some, `reasoning_effort` on
    others, a chat-template flag on a third - and its listing does not say which. A
    model's own reasoning, whichever field it arrives in, is still shown as thinking.
    """

    name = "together"
    label = "Together"
    api_key_env_vars = ("TOGETHER_API_KEY",)
    base_url = BASE_URL
    streaming = True
    sampling = True
    extra_body = {"context_length_exceeded_behavior": "error"}
    """Refuse a request that does not fit, rather than let Together shorten it - an
    overflow is what Atlas's compaction answers, and it can only answer one it hears."""
    overflow_markers = ("must be less than the context length",)
    """Together's words: "Input token count + `max_tokens` parameter must be less than
    the context length"."""

    async def listing(self) -> list[Mapping[str, Any]]:
        """`GET /models`, which at Together is a bare array rather than OpenAI's
        `{data: [...]}` - so it is read raw, not through `models.list()`."""
        await self.prepare()
        try:
            rows = await self._client.get("/models", cast_to=object)
        except Exception as exc:  # the SDK raises its own hierarchy; keep ours at the seam
            raise ProviderError(f"Together's model list failed: {exc}") from exc
        if isinstance(rows, Mapping):
            rows = rows.get("data")
        return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []

    def entry_of(self, row: Mapping[str, Any]) -> ModelEntry | None:
        """One chat model: the window, the price and the date. Embedding, image,
        rerank and moderation models are not models a session can talk to."""
        model_id = str(row.get("id", "") or "")
        if not model_id or row.get("type") != "chat":
            return None
        window = row.get("context_length")
        base = super().entry_of(row)
        return ModelEntry(
            id=model_id,
            context_window=window if isinstance(window, int) and window > 0 else 0,
            cost=pricing_of(row),
            released=base.released if base is not None else "",
        )

    def usage_of(self, usage: Mapping[str, Any]) -> Usage | None:
        """OpenAI's `prompt_tokens_details.cached_tokens`, or the flat
        `cached_tokens` some of Together's models report instead."""
        found = super().usage_of(usage)
        flat = usage.get("cached_tokens") if usage else None
        if found is None or found.cache_read_tokens or not isinstance(flat, int) or flat <= 0:
            return found
        return Usage(
            input_tokens=max(found.input_tokens - flat, 0),
            output_tokens=found.output_tokens,
            cache_read_tokens=flat,
        )


class TogetherPlugin(Plugin):
    """The Together provider."""

    name = "together"
    description = "The Together AI model provider - open models, hosted at Together."

    def register(self, ctx: PluginContext) -> None:
        ctx.register_provider("together", TogetherProvider)
