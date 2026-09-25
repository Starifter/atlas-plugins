---
name: fireworks
description: The Fireworks AI model provider - open models, hosted at Fireworks.
version: "1.0.0"
requires_ultron_sdk: ">=1.25,<2"
categories: [provider, models]
logo: logo.svg
contracts:
  providers: [fireworks]
providers:
  fireworks:
    api_key_env_vars: [FIREWORKS_API_KEY]
    streaming: true
    sampling: true
    catalog: live
python_dependencies:
  - "openai>=1.66"
---

# fireworks

Open models - Kimi, DeepSeek, Qwen, GLM, GPT-OSS - hosted at
[Fireworks AI](https://fireworks.ai).

```
/plugins install fireworks
ultron auth add fireworks             # a key from fireworks.ai, or FIREWORKS_API_KEY in ~/.ultron/.env
```

then `provider: fireworks` and a `model` in `config.json`, with the id as Fireworks
writes it: `accounts/fireworks/models/kimi-k2p6`. **There is no default model**;
`ultron models list fireworks` shows what Fireworks serves. It needs Ultron's SDK 1.25
or later.

## The catalog is live

The manifest lists no models. `/model list --refresh` asks Fireworks' `GET /models`.
Prices come from the catalog Ultron publishes, hydrated from
[models.dev](https://models.dev), so `/status` knows what a turn cost - and says
*unknown*, never zero, for a model it has no price for.

## A request that does not fit is refused

By default Fireworks lowers a request's reply ceiling to make an overflowing prompt fit.
This plugin asks it not to (`context_length_exceeded_behavior: error`), so an overflow is
an error Ultron hears and answers by compacting and trying again.

## Thinking

There is no `/think`: `reasoning_effort` is taken by some of Fireworks' models and not
others, and neither its documentation nor its listing says which, so a menu here would
be a guess. A model that reasons does so at its own default. Its reasoning is shown as
thinking and - since a Fireworks model loses its thinking between tool calls otherwise -
sent back with each earlier turn, which is what Ultron's SDK 1.25 added.

## What reaches Fireworks

The conversation, the tool definitions, pictures for a model that takes them, and your
key. A PDF is not sent - Fireworks asks for pages as pictures - and the row says so.

## Not tested live

This plugin is built from Fireworks' API documentation and tested against a fake client.
It has not been run against the real API.
