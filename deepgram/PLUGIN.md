---
name: deepgram
description: Deepgram speech-to-text - voice notes and recordings transcribed by Nova.
version: "1.0.0"
requires_ultron_sdk: ">=1.23,<2"
categories: [audio]
logo: logo.svg
contracts:
  media_readers: [deepgram]
config_schema:
  api_key_env:
    type: str
    default: DEEPGRAM_API_KEY
    description: The variable the key is read from, in the environment or ~/.ultron/.env.
  model:
    type: str
    default: ""
    description: The Deepgram model to transcribe with. Empty is nova-3.
  smart_format:
    type: bool
    default: true
    description: Let Deepgram format numbers, dates, currency and punctuation for reading.
---

# deepgram

[Deepgram](https://deepgram.com)'s pre-recorded speech-to-text as a media reader: a voice
note from Telegram, a recording you attach, or a file `read_media` is pointed at is
transcribed by Nova (`nova-3` unless `model` says otherwise).

```
/plugins install deepgram
```

then put `DEEPGRAM_API_KEY=...` in `~/.ultron/.env`. `ultron "store my deepgram api key"`
is the shortest way to do that. The key is the plugin's own, not an auth profile:
Deepgram sells transcription and no model you could chat with, and profiles are for
model vendors. The plugin loads without a key and reports itself not ready, so until
there is one the next reader down the ladder answers.

## Where it sits

The reader is named `deepgram`, with priority 48. It comes after `groq/whisper` (40) and
`xai/stt` (45) and before `openai/whisper` (50), if those are installed and have keys.
`audio_reader: deepgram` pins it with no fallback. The reference line on the message
names the reader that answered.

With `audio_language` set, that language is sent to Deepgram. Without it, Deepgram is
asked to detect the language rather than assume English.

## What reaches Deepgram

The audio, the model name, the language hint and your key. Never the conversation, and
never anything else from Ultron.

## Not tested live

This plugin is built from Deepgram's API documentation and tested against a fake
transport. It has not been run against the real API. If Deepgram refuses a request, the
error is shown as Deepgram worded it.
