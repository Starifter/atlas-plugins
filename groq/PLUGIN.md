---
name: groq
description: The Groq model provider - open models, served fast - and Whisper for voice notes.
version: "1.1.0"
requires_ultron_sdk: ">=1.23,<2"
categories: [provider, models, audio]
logo: logo.svg
contracts:
  providers: [groq]
  media_readers: [groq/whisper]
config_schema:
  transcription_model:
    type: str
    default: ""
    description: The Whisper model groq/whisper transcribes with. Empty is whisper-large-v3-turbo; whisper-large-v3 is slower and more accurate.
providers:
  groq:
    api_key_env_vars: [GROQ_API_KEY]
    thinking_levels: [off, low, medium, high]
    streaming: true
    sampling: true
    catalog: live
python_dependencies:
  - "openai>=1.66"
---

# groq

Open models - GPT-OSS, Qwen, Llama - on [Groq](https://groq.com)'s hardware, which is
built to answer fast.

```
/plugins install groq
ultron auth add groq                  # a key from console.groq.com, or GROQ_API_KEY in ~/.ultron/.env
```

then `provider: groq` and a `model` in `config.json`, or `--provider groq --model
openai/gpt-oss-120b`. **There is no default model**: Groq's line-up changes month to
month, and `ultron models list groq` shows what it serves today.

## The catalog is live

The manifest lists no models. `/model list --refresh` (or `ultron models refresh groq`)
asks Groq's `GET /models`, which says each model's window and reply ceiling; a model Groq
marks inactive is not offered. Prices come from the catalog Ultron publishes, hydrated
from [models.dev](https://models.dev), so `/status` knows what a turn cost - and says
*unknown*, never zero, for a model it has no price for.

## Thinking

`/think` depends on the model, because Groq's control does:

| Model | `/think` | Sent as |
|---|---|---|
| GPT-OSS (`openai/gpt-oss-120b`, `-20b`) | `low`, `medium`, `high` - no `off`, it always reasons | `reasoning_effort` |
| Qwen 3 (`qwen/qwen3...`) | `off`, `low`, `medium`, `high` | `reasoning_effort` (`none` is off), `reasoning_format: parsed` |
| Llama and the rest | none | nothing |

The reasoning comes back in Groq's `reasoning` field and is shown as thinking, never as
part of the reply.

## Voice notes

The plugin also registers `groq/whisper`, a media reader that transcribes a voice note
or a recording with Groq's Whisper (`whisper-large-v3-turbo` unless
`transcription_model` says otherwise). It uses the same key as the provider, so once
`ultron auth add groq` is done a voice note sent from Telegram is transcribed, whether or
not you chat through Groq. Its priority is 40, ahead of `openai/whisper` at 50, because
Groq charges less for the same model. `audio_reader: groq/whisper` pins it, and
`audio_reader: openai/whisper` pins the other. The reference line on the message names
the reader that answered.

## What reaches Groq

The conversation, the tool definitions, pictures for a model that takes them, and your
key. Nothing else: no Ultron setting, no file you did not attach. A voice note reaches
Groq only when `groq/whisper` is the reader that transcribes it, and then only the
audio and the `audio_language` hint - never the conversation.

## Not tested live

This plugin is built from Groq's API documentation and tested against a fake client. It
has not been run against the real API - if something Groq says is not handled, the error
is shown as Groq worded it.
