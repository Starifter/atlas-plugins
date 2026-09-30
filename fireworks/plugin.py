"""Fireworks AI provider - open models, hosted at Fireworks.

Fireworks speaks OpenAI's Chat Completions at `https://api.fireworks.ai/inference/v1`,
so this is the SDK's OpenAI-compatible base with Fireworks' own parts declared on it
(`plugin-sdk.md` §6.7): every earlier turn's `reasoning_content` sent back, since a
model loses its thinking across a tool loop without it, and a request that would not
fit refused rather than shortened.

Requires the `openai` package (`pip install openai`) and Atlas's SDK 1.25, for
`replay_reasoning_as`.
"""

from __future__ import annotations

from atlas.sdk.openai_compat import OpenAICompatProvider
from atlas.sdk.plugin_entry import Plugin, PluginContext

BASE_URL = "https://api.fireworks.ai/inference/v1"


class FireworksProvider(OpenAICompatProvider):
    """Chat Completions at Fireworks, on the SDK's OpenAI-compatible base.

    There is no default model and no `/think`: `reasoning_effort` is accepted by some
    of Fireworks' models and not others, and neither its docs nor its listing say
    which. A model that reasons does so at its own default, and its reasoning is shown
    as thinking and sent back.
    """

    name = "fireworks"
    label = "Fireworks"
    api_key_env_vars = ("FIREWORKS_API_KEY",)
    base_url = BASE_URL
    streaming = True
    sampling = True
    replay_reasoning_as = "reasoning_content"
    """Fireworks' reasoning models keep their thinking between tool calls only if each
    earlier assistant turn sends its `reasoning_content` back."""
    extra_body = {"context_length_exceeded_behavior": "error"}
    """By default Fireworks lowers `max_tokens` to make an overflowing request fit.
    Refused instead, so Atlas's compaction hears the overflow and answers it."""


class FireworksPlugin(Plugin):
    """The Fireworks provider."""

    name = "fireworks"
    description = "The Fireworks AI model provider - open models, hosted at Fireworks."

    def register(self, ctx: PluginContext) -> None:
        ctx.register_provider("fireworks", FireworksProvider)
