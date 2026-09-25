---
name: deepseek
description: The DeepSeek model provider - DeepSeek's own models, at DeepSeek.
version: "1.0.0"
requires_ultron_sdk: ">=1.25,<2"
categories: [provider, models]
logo: logo.svg
contracts:
  providers: [deepseek]
providers:
  deepseek:
    api_key_env_vars: [DEEPSEEK_API_KEY]
    thinking_levels: [off, low, high, max]
    streaming: true
    catalog: live
python_dependencies:
  - "openai>=1.66"
---

# deepseek

DeepSeek's models - `deepseek-v4-pro`, `deepseek-flash` - at
[DeepSeek](https://platform.deepseek.com) itself, rather than through a reseller.

```
/plugins install deepseek
ultron auth add deepseek              # a key from platform.deepseek.com, or DEEPSEEK_API_KEY in ~/.ultron/.env
```

then `provider: deepseek` and a `model` in `config.json`, or `--provider deepseek --model
deepseek-v4-pro`. **There is no default model**; `ultron models list deepseek` shows what
DeepSeek serves. It needs Ultron's SDK 1.25 or later.

## The catalog is live

The manifest lists no models. `/model list --refresh` asks DeepSeek's `GET /models`, which
says each model's window, reply ceiling, whether it takes pictures, and which thinking
efforts it supports - so `/think` offers exactly those. Prices come from the catalog
Ultron publishes, hydrated from [models.dev](https://models.dev).

## Thinking

DeepSeek thinks by default. `/think off` sends `thinking: disabled`; `low`, `high` and
`max` send `thinking: enabled` with that `reasoning_effort`. There is no `medium`:
DeepSeek maps it onto `high` itself.

A model that thought keeps its reasoning between tool calls only if every earlier turn
sends its `reasoning_content` back, and DeepSeek refuses a request with tools that leaves
it out. This plugin sends it back - which is what Ultron's SDK 1.25 added. A turn another
provider wrote, before a `/model` switch, has no DeepSeek reasoning to send and sends
none.

Sampling (`temperature`, the penalties) is not forwarded: DeepSeek does not take it in
thinking mode, which is its default.

## Cache

DeepSeek caches on its own and reports the hits as `prompt_cache_hit_tokens`; `/status`
shows them as cache reads.

## What reaches DeepSeek

The conversation, the tool definitions, pictures for a model that takes them, and your
key. A PDF is not sent - DeepSeek does not take one - and the row says so.

## Not tested live

This plugin is built from DeepSeek's API documentation and tested against a fake client.
It has not been run against the real API.
