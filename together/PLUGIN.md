---
name: together
description: The Together AI model provider - open models, hosted at Together.
version: "1.0.0"
requires_atlas_sdk: ">=1.23,<2"
categories: [provider, models]
logo: logo.svg
contracts:
  providers: [together]
providers:
  together:
    api_key_env_vars: [TOGETHER_API_KEY]
    streaming: true
    sampling: true
    catalog: live
python_dependencies:
  - "openai>=1.66"
---

# together

Open models - DeepSeek, Qwen, Kimi, GLM, Llama, GPT-OSS - hosted at
[Together AI](https://together.ai).

```
/plugins install together
atlas auth add together              # a key from api.together.ai, or TOGETHER_API_KEY in ~/.atlas/.env
```

then `provider: together` and a `model` in `config.json`, or `--provider together --model`
with an id as Together lists it (`deepseek-ai/DeepSeek-V4-Pro`, say). **There is no
default model**; `atlas models list together` shows the chat models Together serves.

## The catalog is live

The manifest lists no models. `/model list --refresh` asks Together's `GET /models`, which
says each chat model's window and price - so `/status` knows what a turn cost. A model
Together lists at zero for both input and output is priced *unknown*, not free: that is
how it lists models it serves only on dedicated hardware.

## A request that does not fit is refused

Together can quietly shorten a request that would overflow a model's context. This
plugin asks it not to (`context_length_exceeded_behavior: error`), so an overflow is an
error Atlas hears and answers the way it answers any other - by compacting and trying
again - rather than a reply built on a conversation Together cut.

## Thinking

There is no `/think`. Together's models switch reasoning three different ways, and its
listing does not say which model takes which, so a menu here would be a guess. A model
that reasons does so at its own default, and its reasoning is shown as thinking whichever
field it arrives in.

## What reaches Together

The conversation, the tool definitions, pictures for a model that takes them, and your
key. A PDF is not sent, and the row says so.

## Not tested live

This plugin is built from Together's API documentation and tested against a fake client.
It has not been run against the real API.
