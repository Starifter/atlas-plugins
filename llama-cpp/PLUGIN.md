---
name: llama-cpp
description: A model provider for a llama.cpp server on this machine - GGUF models, no key, no bill.
version: "1.1.0"
requires_atlas_sdk: ">=1.24,<2"
categories: [provider, models, local]
logo: logo.svg
contracts:
  providers: [llama-cpp]
  embedders: [llama-cpp]
providers:
  llama-cpp:
    api_key_env_vars: [LLAMA_SERVER_API_KEY]
    thinking_levels: [off, low, medium, high, max]
    streaming: true
    sampling: true
    local: true
    catalog: live
config_schema:
  base_url:
    type: str
    default: http://127.0.0.1:8080/v1
    description: "Where a llama-server you started listens, up to and including /v1. Ignored when server_model is set."
  server_model:
    type: str
    description: "A model for the plugin to serve itself: a .gguf path, or org/repo[:quant] / hf:org/repo for llama-server -hf to download. Setting it makes the plugin start and own llama-server."
  server_binary:
    type: str
    description: "Path to llama-server. Empty finds it on PATH."
  server_port:
    type: int
    default: 8080
    description: "The port a managed server listens on, on loopback only."
  server_context:
    type: int
    default: 0
    description: "The context to start the managed server with (-c). Zero leaves it to the server."
  server_gpu_layers:
    type: int
    description: "Layers to offload to the GPU (-ngl). Unset leaves it to the server."
  server_mmproj:
    type: str
    description: "A multimodal projector (.gguf) for the managed server, so it takes pictures (--mmproj)."
  server_args:
    type: list
    description: "Extra arguments appended to the managed chat server's command line, e.g. [--flash-attn, on]."
  server_startup_timeout:
    type: int
    default: 600
    description: "Seconds to wait for a managed server to answer /health. A first -hf download can take longer."
  embedding_model:
    type: str
    description: "With embedding_url empty: an embedding model for the plugin to serve (--embedding --pooling mean). With embedding_url set: the id sent on requests."
  embedding_port:
    type: int
    default: 8081
    description: "The port a managed embedding server listens on."
  embedding_url:
    type: str
    description: "An embedding llama-server you started, up to and including /v1. Empty means the plugin serves embedding_model itself, or base_url when that is empty too."
  embedding_dimensions:
    type: int
    default: 0
    description: "The embedding model's vector width. Zero reads it from the server (n_embd); set it only for a server that lists nothing."
python_dependencies:
  - "openai>=1.66"
---

# llama-cpp

A GGUF model on your own machine, behind [`llama-server`](https://github.com/ggml-org/llama.cpp/tree/master/tools/server):
no key, no bill, nothing leaves the box. Two ways in.

**Let the plugin run the server.** Install llama.cpp (`winget install ggml.llamacpp`,
`brew install llama.cpp`, or a release from its GitHub page - the plugin finds `llama-server`
on `PATH`, or `server_binary` names it), then name a model:

```jsonc
{
  "provider": "llama-cpp",
  "plugins_enabled": ["llama-cpp"],
  "plugins_settings": {
    "llama-cpp": { "server_model": "ggml-org/Qwen3-8B-GGUF", "server_context": 32768 }
  }
}
```

`server_model` is a `.gguf` path, or `org/repo[:quant]` (or `hf:org/repo`) for the server's
own `-hf` download into its own cache. The server is started on the first request - never at
install, so `atlas --tools` leaves no model loaded - on loopback, on a credential-scrubbed
environment, and stopped when the Atlas process that started it exits: the gateway's exit
for a detached gateway, the REPL's for `--local`. Every lane in a gateway shares it. A server
already answering on the port is used, not replaced. Its output goes to
`<workspace>/.atlas/llama-cpp/llama-server-chat.log`, and an error that stops it comes back
with the log's last lines. The plugin runs the program you installed; it does not fetch one.

**Or point at a server you started.** `llama-server -m model.gguf` (with `--mmproj` for
pictures, `--api-key` if it is not on loopback), and `base_url` where it listens:

```jsonc
{ "provider": "llama-cpp", "plugins_settings": { "llama-cpp": { "base_url": "http://127.0.0.1:8080/v1" } } }
```

Either way, **a model name is optional.** A `llama-server` serves the one model it was started
with and answers under any name, so an empty `model` asks the server what it has and uses
that; a server that serves several (`--models-dir`, the router) needs `model` set to one of
them and says which. The server speaks OpenAI's Chat Completions dialect, so this rides the
`openai` package with the base URL moved - `pip install openai` if `/plugins llama-cpp` says
it is missing.

## The catalog is live

`/model list --refresh` (or `atlas models refresh llama-cpp`) asks `GET /v1/models` and
`GET /props`, and keeps what they say about each id: the context the server was **started
with** (`-c`, split across `--parallel`), which is the window a request actually gets and
is usually well under the model's own; whether a projector is loaded for pictures; and when
the model file was made. `/props` is asked with `autoload=false`, so a router never loads
a model to answer.

The same `/props` answer is asked before every request. From the first reply on, the
session compacts against the context the server was started with, whatever it believed
when it started. A request that would not fit is refused before it is sent, with how to
start the server larger; Atlas compacts and tries once more first.

The size checked is **exact**, not estimated: the request goes to `/apply-template`, which
renders it through the same chat template, tool schemas and thinking switch a chat
completion would, and `/tokenize` counts the result - two round trips to your own server.
A request carrying a picture or audio is estimated instead, because the rendered prompt
holds a marker rather than the tokens the projector adds; so is every request to a build
too old to have the two endpoints, which is asked once and then left alone.

Every model is priced at zero. That is a declaration and not a gap: the machine is yours,
and `/status` says `$0.00` rather than *at least*. The server's prompt-cache reuse is not
reported as a cache figure, because the OpenAI-shaped `usage` does not carry it and a zero
would claim there was none.

## Thinking

`/think off|low|medium|high|max` is one menu for every model, because the switch is one
request: `reasoning_effort` as `llama-server` takes it, and `chat_template_kwargs:
{enable_thinking: false}` beside it for `off` - the two spellings a chat template may read
(Qwen3, GLM, gpt-oss, DeepSeek). A template that reads neither ignores both, so a level on
a model that does not reason costs nothing. Where you know a model's real menu,
`model_catalog` in `config.json` narrows it and wins over this plugin's word.

What a model reasons arrives as `reasoning_content` (the server's default
`--reasoning-format deepseek`) and is shown as it streams. It is **not** sent back on the
next turn: a template that thinks does not want last turn's thoughts, and a local context
has no room for them.

## Pictures

A server with a projector takes images - `server_mmproj` on a managed server, `--mmproj` on
yours - and `/props` says so; the listing records `image` and Atlas sends the picture.
Without one, the catalog entry says `text` and Atlas describes the picture with
`vision_model` instead, or says it could not.

## A key

None, by default. A managed server is loopback-only and gets none. A server you started with
`--api-key` wants one: put it in `~/.atlas/.env` as `LLAMA_SERVER_API_KEY` and it becomes a
`llama-cpp` auth profile like any provider's. `base_url` never carries a username or
password - a URL with one in it is refused, because it would reach every error message.

## Memory search

An embedding model is its own server. Name one and the plugin runs it beside the chat server:

```jsonc
{
  "memory_embedder": "llama-cpp",
  "plugins_settings": {
    "llama-cpp": { "embedding_model": "ggml-org/embeddinggemma-300m-qat-q8_0-GGUF" }
  }
}
```

That is `llama-server -hf ... --embedding --pooling mean` on `embedding_port` (8081), started
on first need like the chat server. Or run it yourself and set `embedding_url`.

The vector width is read from the server (`n_embd` in `GET /v1/models`), so nobody has to
know it. The core asks for the width from synchronous code before it ever embeds, and a
managed server that is not yet running is started on that first ask and answers a later one -
so the first memory refresh of a fresh process runs on keyword, and the next embed pass has
its vectors. `embedding_dimensions` overrides for a server that lists nothing; a wrong number
is caught on the first embed and the message says the right one. `atlas memory status`
reports what is missing.

## Settings

Under `plugins_settings.llama-cpp`:

```jsonc
{
  "server_model": "ggml-org/Qwen3-8B-GGUF", // a model for the plugin to serve; unset = use base_url
  "server_binary": "",                      // llama-server, if not on PATH
  "server_port": 8080,
  "server_context": 32768,                  // -c
  "server_gpu_layers": 99,                  // -ngl; unset leaves it to the server
  "server_mmproj": "",                      // --mmproj, for pictures
  "server_args": ["--flash-attn", "on"],    // anything else, verbatim
  "server_startup_timeout": 600,            // seconds to wait for /health
  "base_url": "http://127.0.0.1:8080/v1",  // a server you started, when server_model is unset
  "embedding_model": "",                    // an embedding model to serve (or the id to send, with embedding_url)
  "embedding_port": 8081,
  "embedding_url": "",                      // an embedding server you started
  "embedding_dimensions": 0                 // 0 reads the width from the server
}
```

Nothing here is a credential.

## What is not here

No binary download: the plugin runs the `llama-server` you installed and never fetches an
executable. No hardware survey or model recommendation. No fast mode and no cache
breakpoints: the server has neither. A `Sampling` is forwarded whole - `llama-server` takes
every field.
