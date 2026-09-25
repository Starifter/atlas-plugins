---
name: lmstudio
description: Models loaded in LM Studio on this machine - no key, no bill.
version: "1.0.0"
requires_ultron_sdk: ">=1.24,<2"
categories: [provider, models, local]
logo: logo.svg
contracts:
  providers: [lmstudio]
  embedders: [lmstudio]
providers:
  lmstudio:
    api_key_env_vars: [LM_API_TOKEN]
    streaming: true
    sampling: true
    local: true
    catalog: live
config_schema:
  base_url:
    type: str
    default: http://127.0.0.1:1234/v1
    description: "Where LM Studio's server listens, up to and including /v1."
  context_length:
    type: int
    default: 0
    description: "The context to load a model with when it is not loaded yet. Zero leaves it to LM Studio, which usually means 4096 - too small for an agent."
  embedding_dimensions:
    type: int
    default: 0
    description: "The embedding model's vector width. Zero asks LM Studio by embedding one word."
python_dependencies:
  - "openai>=1.66"
---

# lmstudio

Models you downloaded in [LM Studio](https://lmstudio.ai), served by its local server:
no key, no bill, nothing leaves the box.

```
/plugins install lmstudio
```

Start the server from the app's **Developer** tab, or with `lms server start`, then name
a model by its key:

```jsonc
{
  "provider": "lmstudio",
  "model": "qwen/qwen3-8b",
  "plugins_settings": { "lmstudio": { "context_length": 32768 } }
}
```

`model` can be left out when only one model is loaded, or only one is downloaded.
`ultron models list lmstudio` shows the keys.

## Context length

LM Studio fixes a model's context when it loads it, and a model loaded on demand usually
gets 4096 tokens. Ultron's own instructions and tools take several thousand, so that is
not enough for an agent. So the plugin loads the model itself before the first request:

- **The size.** It loads at `context_length` when you set one; the model's
  `max_context_length` in LM Studio is the ceiling.
- **Reading it back.** It reads the size LM Studio actually loaded, and from the first
  reply on the session compacts against that size. A session starts on the loaded size
  if the model is already loaded, otherwise on your `context_length`, or 4096, and
  corrects itself after one turn either way.
- **Refusing.** It refuses a request that would not fit instead of sending it. Ultron
  compacts against the real size and tries once more; only if that still does not fit
  does the error reach you, with how to fix it. The size is an estimate calibrated from
  what LM Studio reports after each reply.

A model that is already loaded is used at the size it has. To change it, change
`context_length` and unload the model in LM Studio (or `lms unload`); the next request
loads it again at the new size.

## The catalog is live

`/model list --refresh` (or `ultron models refresh lmstudio`) reads LM Studio's
`/api/v1/models`. For every chat model downloaded, it keeps:

- **The context a request gets**: the loaded size, or your `context_length`. Never the
  model's maximum.
- **Pictures**: whether it can see them.
- **The price**: zero.

Embedding models are left out of the list, and a listing loads nothing.

## Thinking

`/think` offers each model's own menu, read from what LM Studio lists about it
(`capabilities.reasoning`):

- **A switch** (`off`/`on`, like Qwen3) is offered as `off` and `high`.
- **Named levels** (`low`/`medium`/`high`, like gpt-oss) are offered as named.
- **A model with no reasoning entry** has no `/think`, and is sent nothing.

The level goes out as `reasoning_effort`, with `off` sent as `none`. That is the one field
LM Studio's OpenAI endpoint honours, measured against a running server:
`reasoning.effort`, `chat_template_kwargs` and the rest are ignored. The on/off case was
measured against Qwen3; the named-level case follows LM Studio's own listing and has not
been run.

What a model reasons is shown as it streams.

## A token

None, by default. If you switch on **Require authentication** in LM Studio's server
settings, create a token there and put it in `~/.ultron/.env` as `LM_API_TOKEN`. It
becomes a `lmstudio` auth profile like any provider's. The plugin never sends your
`OPENAI_API_KEY` to LM Studio.

## Memory search

```jsonc
{
  "memory_embedder": "lmstudio",
  "memory_embedder_model": "text-embedding-nomic-embed-text-v1.5"
}
```

The vector width is found by embedding one word. Set `embedding_dimensions` if you would
rather say it yourself.

## Settings

Under `plugins_settings.lmstudio`:

```jsonc
{
  "base_url": "http://127.0.0.1:1234/v1",
  "context_length": 32768,       // 0 leaves it to LM Studio
  "embedding_dimensions": 0      // 0 asks LM Studio
}
```

Nothing here is a credential.

## What is not here

- **No server management.** The plugin never starts LM Studio's server and never
  downloads a model; it says what to do when either is missing. It does load a model
  you named, at the context you chose, because that is the only way to control the
  context.
- **No fast mode and no cache breakpoints.**
