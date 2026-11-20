# Atlas plugins

The official plugin marketplace for [Atlas](https://github.com/Starifter/atlas).
Every top-level directory here is one plugin: a `PLUGIN.md` manifest and a `plugin.py`
beside it, written against `atlas.sdk` and nothing else - exactly what the plugins
Atlas itself ships are made of.

Every Atlas install has this marketplace connected under the name `official`. Nothing is
fetched until you ask:

```
/plugins market refresh official      fetch or update the copy on this machine
/plugins                              Discover tab: what it offers
/plugins install email                copy one into ~/.atlas/plugins/ and enable it
```

Installing is two things said out loud - a copy into `~/.atlas/plugins/<name>/`, then a
line in `plugins_enabled` - and the plugin loads on the next session. A marketplace is
where a plugin comes from and never where one runs from: forgetting this marketplace
(`/plugins market remove official`) leaves every plugin installed from it in place.

## What is here

Hosted model providers are not here: Atlas's core carries them (OpenRouter, Groq, DeepSeek,
xAI, Together, Fireworks and the rest of its provider catalog), so there is nothing to
install. The local ones are, because each asks its server what it loaded.

| plugin | what it adds |
|---|---|
| `ollama` | Two model providers: `ollama` for a local Ollama (no key, no bill, context checked against what Ollama loaded) and `ollama-cloud` for ollama.com with `OLLAMA_API_KEY`, plus an embedder. |
| `lmstudio` | A model provider for LM Studio on this machine: loads the model at the context you choose, no key, no bill, with an embedder. |
| `firecrawl` | A `web_fetch` backend tried after the built-in `reader`: PDFs, bot walls and JavaScript-rendered pages, read by Firecrawl's browser. Works without a key at a low rate limit; `FIRECRAWL_API_KEY` raises it. |
| `email` | Email over IMAP and SMTP with an app password - Gmail, iCloud, Fastmail or any IMAP server, several accounts: read, search, draft, send with your yes, file, and new mail in the folders you watch wakes the agent with your own instruction. Ported from Ultron; its libraries (`aioimaplib`, `aiosmtplib`) are installed with it. |

## Connectors

`connectors/` holds MCP servers described once, for `/mcp discover` and the MCP page's
Discover tab: `/mcp install <name>` adds one to `config.json`, asks for any key it needs, and
signs in when it signs people in (Atlas's `docs/spec/connectors.md`).

| connector | what it reaches |
|---|---|
| `linear` | Linear issues and projects - signs in with OAuth. |
| `notion` | Notion pages and databases - signs in with OAuth. |
| `sentry` | Sentry issues and releases - signs in with OAuth. |
| `atlassian` | Jira and Confluence Cloud - signs in with OAuth. |
| `github` | GitHub's remote server - asks for a personal access token. |
| `context7` | Current library documentation - no sign-in. |
| `huggingface` | The Hugging Face Hub - no sign-in. |
| `cloudflare-docs` | Cloudflare's developer docs - no sign-in. |
| `memory` | The reference memory-graph server, run with `npx`. |
| `brave-search` | Brave Search, run with `npx` - asks for `BRAVE_API_KEY`. |

## What "official" means

Only that Atlas knows this repository's address. A plugin here goes through the same
refresh, the same copy and the same consent line as one from any other marketplace, and
nothing is allowed or refused because it came from here. What the marketplace promises is
the check in [`scripts/validate.py`](scripts/validate.py), run before every merge: each entry
has a manifest that parses, a name that matches its directory, a version, an SDK range that
the current SDK satisfies, and no `autoload` - which Atlas refuses from anything it did
not ship, so a plugin here that said it would be a plugin lying about itself.

CI runs the same check, but cannot yet: it installs Atlas from its repository, which is
private for now, so the `validate` job fails before it reaches a plugin. Until that changes,
the check is run against a local Atlas checkout, and a red `validate` says nothing about
the change it is on.

## Pointing an install elsewhere

`plugin_marketplace_official` in `config.json` is the address. A path to a checkout of this
repository reads it in place, which is how it is developed; `""` disconnects it.

```json
{ "plugin_marketplace_official": "~/dev/atlas-plugins" }
```

## Adding a plugin

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: a directory with a `PLUGIN.md`
and a `plugin.py`, laid out as it shows; make `scripts/validate.py` pass, open a pull request.
