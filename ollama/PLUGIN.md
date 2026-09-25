---
name: ollama
description: Models through a local Ollama - no key, no bill - or Ollama's cloud with a key.
version: "1.0.0"
requires_ultron_sdk: ">=1.24,<2"
categories: [provider, models, local]
logo: logo.svg
contracts:
  providers: [ollama, ollama-cloud]
  embedders: [ollama]
providers:
  ollama:
    thinking_levels: [off, low, medium, high, max]
    streaming: true
    sampling: true
    local: true
    catalog: live
  ollama-cloud:
    api_key_env_vars: [OLLAMA_API_KEY]
    thinking_levels: [off, low, medium, high, max]
    streaming: true
    sampling: true
    local: false
    catalog: live
config_schema:
  base_url:
    type: str
    default: http://127.0.0.1:11434/v1
    description: "Where your Ollama listens, up to and including /v1. Another machine on your network works too."
  context_length:
    type: int
    default: 0
    description: "The context your Ollama loads models with - the value of OLLAMA_CONTEXT_LENGTH. Used before a model is loaded; the loaded size wins once there is one."
  embedding_dimensions:
    type: int
    default: 0
    description: "The embedding model's vector width. Zero reads it from Ollama; set it only if Ollama says nothing."
python_dependencies:
  - "openai>=1.66"
---

# ollama

Two providers in one plugin:

- **`ollama`**: the Ollama on this machine, or on your network. No key, no bill, nothing
  leaves the box.
- **`ollama-cloud`**: ollama.com directly, with an `OLLAMA_API_KEY`. No Ollama install
  needed.

```
/plugins install ollama
```

## Local

Install Ollama, pull a model that can call tools, and name it:

```
ollama pull qwen3
```

```jsonc
{ "provider": "ollama", "model": "qwen3" }
```

If Ollama holds exactly one model, `model` can be left out and that one is used. With
several, Ultron asks you to pick; `ultron models list ollama` shows them.

**Set the context length.** This is the one setting that matters. Ollama loads a model
with a context that depends on your GPU's memory - **4k under 24 GB**, 32k up to 48 GB,
256k above - unless `OLLAMA_CONTEXT_LENGTH` says otherwise. Its OpenAI endpoint has no way
to ask for more per request, and a prompt that does not fit is cut from the front
without an error. Ultron's own instructions and tools come to several thousand tokens,
so 4k is not enough for an agent. Raise it (at least 32k; 64k is Ollama's own advice for
agents) and tell the plugin the same number:

```
OLLAMA_CONTEXT_LENGTH=65536 ollama serve        # or the context slider in the Ollama app
```

```jsonc
{ "plugins_settings": { "ollama": { "context_length": 65536 } } }
```

What the plugin does with it:

- **Before each request**, it loads the model if it is not in memory - exactly as the
  request would have - and reads the context Ollama actually gave it from `/api/ps`.
- **The session follows that size.** From the first reply on, Ultron compacts against
  the context Ollama loaded, not the one it started with. A session starts on the loaded
  size if the model is already in memory, otherwise on your `context_length`, otherwise
  on 4096 - and corrects itself after one turn either way.
- **It refuses a request that would not fit** instead of letting Ollama cut it. Ultron
  compacts the conversation against the real size and tries once more; only if that still
  does not fit does the error reach you, saying how many tokens the turn needs and how to
  raise the limit. The size is estimated - Ollama cannot count tokens for a client - and
  the estimate is calibrated from what Ollama reports after each reply, so it is only
  deliberately pessimistic on the first turn.

`context_length` is your word, and it is checked: the loaded size wins the moment there
is one.

A `-cloud` model run through your local Ollama (after `ollama signin`) is Ollama's to
size, not this server's. It is not checked.

## Cloud

```
ultron auth add ollama-cloud          # or OLLAMA_API_KEY in ~/.ultron/.env
```

```jsonc
{ "provider": "ollama-cloud", "model": "gpt-oss:120b" }
```

Cloud models run at their full context, so the window is the model's own. The model ids
are the ones `ultron models list ollama-cloud` shows, which are what ollama.com's
`/api/tags` returns. The plan is a subscription rather than a per-token price, so
`/status` reports cost as unknown, never as zero.

## The catalog is live

`/model list --refresh` (or `ultron models refresh ollama`) lists models from `/api/tags`
and asks `/api/show` about each one:

- **The window.** For local, the loaded size or your `context_length`, never the model's
  trained maximum. For cloud, the model's own.
- **Pictures**: whether the model can see them (`vision`).
- **Thinking**: the model's menu.

Embedding-only models are left out of the list. A listing never loads a model.

## Thinking

`/think` sends `reasoning_effort`, which Ollama maps onto what each model has:

- **Named levels** (gpt-oss's `low`/`medium`/`high`) are offered as named.
- **An on/off switch** is offered as `off` and `high`.
- **A model that cannot think** has no `/think` at all, and is sent nothing: `think` on
  such a model is an error rather than a no-op. If the listing has not run yet and a
  level goes to a model that cannot think, the plugin notices the refusal, drops the
  control and asks once more.

What a model reasons arrives as `reasoning`, and is shown as it streams.

## Memory search

```jsonc
{ "memory_embedder": "ollama", "memory_embedder_model": "nomic-embed-text" }
```

After `ollama pull nomic-embed-text`. The vector width is read from `/api/show`.

## Settings

Under `plugins_settings.ollama`:

```jsonc
{
  "base_url": "http://127.0.0.1:11434/v1",  // another machine: http://box.lan:11434/v1
  "context_length": 65536,                   // what OLLAMA_CONTEXT_LENGTH is
  "embedding_dimensions": 0                  // 0 reads it from Ollama
}
```

Nothing here is a credential. A `base_url` with a username or password in it is refused.

## What is not here

- **No server management.** The plugin never starts Ollama and never pulls a model; it
  says exactly what to run when either is missing.
- **No fast mode and no cache breakpoints.** Ollama reuses its cache on a matching prefix
  by itself, and reports the reuse as cached tokens on `/status`.
- **Tools are required.** A model that cannot call tools is refused with a pointer to
  ones that can, because every Ultron turn offers tools.
