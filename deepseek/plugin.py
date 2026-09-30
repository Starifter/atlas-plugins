"""DeepSeek provider - DeepSeek's own models, at DeepSeek.

DeepSeek speaks OpenAI's Chat Completions at `https://api.deepseek.com`, so this is
the SDK's OpenAI-compatible base with DeepSeek's own parts declared on it
(`plugin-sdk.md` §6.7): thinking switched by `thinking.type` with an effort beside
it, every earlier turn's `reasoning_content` sent back (DeepSeek refuses a request
with tools that leaves it out), and a cache DeepSeek reports in its own words.

Requires the `openai` package (`pip install openai`) and Atlas's SDK 1.25, for
`replay_reasoning_as`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from atlas.sdk.openai_compat import OpenAICompatProvider
from atlas.sdk.plugin_entry import Plugin, PluginContext
from atlas.sdk.provider import ModelEntry, ThinkingLevel, Usage
from atlas.sdk.runtime import ProviderError

BASE_URL = "https://api.deepseek.com"

LEVELS: tuple[ThinkingLevel, ...] = ("off", "low", "high", "max")
"""DeepSeek's efforts are `low`, `high` and `max`; `off` is `thinking.type: disabled`.
It maps `medium` onto `high` itself, so offering `medium` would be a level that buys
nothing a neighbour does not."""

FROM_EFFORT: dict[str, ThinkingLevel] = {"none": "off", "low": "low", "high": "high", "max": "max"}
"""A listing's `effort.supported_levels` in Atlas's words; anything else is dropped."""


class DeepSeekProvider(OpenAICompatProvider):
    """Chat Completions at DeepSeek, on the SDK's OpenAI-compatible base."""

    name = "deepseek"
    label = "DeepSeek"
    api_key_env_vars = ("DEEPSEEK_API_KEY",)
    base_url = BASE_URL
    thinking_levels = LEVELS
    streaming = True
    sampling = False
    """Not declared: in thinking mode DeepSeek does not take `temperature` or the
    penalties, and thinking is on by default. A `Sampling` is dropped, as the core
    drops it for any provider that did not declare."""
    replay_reasoning_as = "reasoning_content"
    """With tools - and Atlas always sends them - every earlier assistant turn has
    to carry its own `reasoning_content` back, or DeepSeek answers 400."""

    def thinking_request(self, level: ThinkingLevel) -> dict[str, Any]:
        if level not in self.thinking_levels:
            return {}
        if level == "off":
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        return {"reasoning_effort": level, "extra_body": {"thinking": {"type": "enabled"}}}

    def entry_of(self, row: Mapping[str, Any]) -> ModelEntry | None:
        """One row of `GET /models`: the window, the reply ceiling, what it takes,
        and the efforts it supports."""
        model_id = str(row.get("id", "") or "")
        if not model_id:
            return None
        window = row.get("context_window")
        ceiling = row.get("max_output_tokens")
        inputs = row.get("input_modalities")
        taken = tuple(m for m in ("text", "image") if isinstance(inputs, list) and m in inputs)
        effort = row.get("effort")
        supported = effort.get("supported_levels") if isinstance(effort, Mapping) else None
        levels: tuple[ThinkingLevel, ...] | None = None
        if isinstance(supported, list):
            named = {FROM_EFFORT[str(s)] for s in supported if str(s) in FROM_EFFORT}
            levels = tuple(level for level in LEVELS if level in named)
        return ModelEntry(
            id=model_id,
            context_window=window if isinstance(window, int) and window > 0 else 0,
            max_output=ceiling if isinstance(ceiling, int) and ceiling > 0 else 0,
            thinking_levels=levels,
            modalities=taken,
        )

    def usage_of(self, usage: Mapping[str, Any]) -> Usage | None:
        """DeepSeek's cache: `prompt_cache_hit_tokens` of `prompt_tokens`, which is
        hits plus misses. A reply that reports no hit field reports no cache."""
        if not usage:
            return None
        if "prompt_cache_hit_tokens" not in usage:
            return super().usage_of(usage)
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        return Usage(
            input_tokens=max(prompt - hit, 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            cache_read_tokens=hit,
        )

    def describe_failure(self, exc: Exception) -> Exception | None:
        if getattr(exc, "status_code", None) == 402:
            return ProviderError(
                "DeepSeek says this account has run out of balance - top it up at "
                f"platform.deepseek.com ({exc})"
            )
        return None


class DeepSeekPlugin(Plugin):
    """The DeepSeek provider."""

    name = "deepseek"
    description = "The DeepSeek model provider - DeepSeek's own models, at DeepSeek."

    def register(self, ctx: PluginContext) -> None:
        ctx.register_provider("deepseek", DeepSeekProvider)
