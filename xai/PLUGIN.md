---
name: xai
description: The xAI model provider - Grok, at xAI - and Grok speech-to-text for voice notes.
version: "1.1.0"
requires_ultron_sdk: ">=1.23,<2"
categories: [provider, models, audio]
logo: logo.svg
contracts:
  providers: [xai]
  media_readers: [xai/stt]
config_schema:
  transcription_model:
    type: str
    default: ""
    description: The model xai/stt transcribes with. Empty is grok-voice-transcribe-2.0.
providers:
  xai:
    api_key_env_vars: [XAI_API_KEY]
    thinking_levels: [low, medium, high, max]
    streaming: true
    catalog: live
python_dependencies:
  - "openai>=1.66"
---

# xai

Grok, at [xAI](https://x.ai).

```
/plugins install xai
ultron auth add xai                   # a key from console.x.ai, or XAI_API_KEY in ~/.ultron/.env
```

then `provider: xai` and a `model` in `config.json`, or `--provider xai --model
grok-4.7`. **There is no default model**; `ultron models list xai` shows what xAI serves.

## The catalog is live

The manifest lists no models. `/model list --refresh` asks xAI's `GET /models`, which
says each model's window, its price - with the higher rate xAI charges above a model's
long-context threshold as a second tier, so a long prompt is priced as xAI bills it - and
which reasoning efforts it takes.

## Thinking

Grok's reasoning models cannot be told not to reason, so there is no `/think off`:
`low`, `medium` and `high`, and `max` where xAI offers `xhigh`. Each model's menu is the
listing's; before a listing has been fetched, a `grok-4` model is offered low to high
and a model whose id says `non-reasoning` is offered none - and is sent no reasoning
field. The reasoning comes back as `reasoning_content` and is shown as thinking.

Sampling (`temperature`, the penalties, `stop`) is not forwarded: xAI's reasoning models
refuse some of it outright.

## Voice notes

The plugin also registers `xai/stt`, a media reader that transcribes a voice note or a
recording with xAI's speech-to-text (`grok-voice-transcribe-2.0` unless
`transcription_model` says otherwise). It uses the same key as the provider, so once
`ultron auth add xai` is done a voice note is transcribed whether or not you chat through
Grok. Its priority is 45: after `groq/whisper` (40), before `openai/whisper` (50).
`audio_reader: xai/stt` pins it. xAI does not take WebM audio, so a WebM voice note goes
to the next reader.

## What reaches xAI

The conversation, the tool definitions, pictures, and your key. A PDF is not sent over
this API, and the row says so. A voice note reaches xAI only when `xai/stt` is the reader
that transcribes it, and then only the audio and the `audio_language` hint - never the
conversation.

## Not tested live

This plugin is built from xAI's API documentation and tested against a fake client. It
has not been run against the real API.
