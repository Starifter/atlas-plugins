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
/plugins install dice                 copy one into ~/.atlas/plugins/ and enable it
```

Installing is two things said out loud - a copy into `~/.atlas/plugins/<name>/`, then a
line in `plugins_enabled` - and the plugin loads on the next session. A marketplace is
where a plugin comes from and never where one runs from: forgetting this marketplace
(`/plugins market remove official`) leaves every plugin installed from it in place.

## What is here

| plugin | what it adds |
|---|---|
| `openrouter` | A model provider: one `OPENROUTER_API_KEY`, every model OpenRouter routes to, with a live catalog of what each costs. |
| `llama-cpp` | A model provider for a `llama-server` on this machine: GGUF models, no key, no bill, with an embedder for memory search. |
| `ollama` | Two model providers: `ollama` for a local Ollama (no key, no bill, context checked against what Ollama loaded) and `ollama-cloud` for ollama.com with `OLLAMA_API_KEY`, plus an embedder. |
| `lmstudio` | A model provider for LM Studio on this machine: loads the model at the context you choose, no key, no bill, with an embedder. |
| `groq` | A model provider for Groq (`GROQ_API_KEY`): open models served fast, with `/think` per model family, and `groq/whisper` to transcribe voice notes on the same key. |
| `deepseek` | A model provider for DeepSeek (`DEEPSEEK_API_KEY`), its thinking carried through tool loops the way DeepSeek requires. Needs SDK 1.25. |
| `xai` | A model provider for xAI's Grok (`XAI_API_KEY`), priced from xAI's own listing, long-context rate included, and `xai/stt` to transcribe voice notes on the same key. |
| `deepgram` | A media reader: voice notes and recordings transcribed by Deepgram Nova (`DEEPGRAM_API_KEY`). |
| `together` | A model provider for Together AI (`TOGETHER_API_KEY`): its hosted open models, priced from its listing. |
| `fireworks` | A model provider for Fireworks AI (`FIREWORKS_API_KEY`): its hosted open models, reasoning carried through tool loops. Needs SDK 1.25. |
| `firecrawl` | A `web_fetch` backend tried after the built-in `reader`: PDFs, bot walls and JavaScript-rendered pages, read by Firecrawl's browser. Works without a key at a low rate limit; `FIRECRAWL_API_KEY` raises it. |
| `google-calendar` | Google Calendar: read it, change it with your yes (every change a confirm card), and reminders on the chat you last wrote from. Sign in with `atlas auth login google-calendar:google`, or read-only from a calendar's secret iCal address. |
| `email` | Email over IMAP and SMTP with an app password - Gmail, iCloud, Fastmail, Yahoo, or any IMAP server you name: search, read, draft, send with your yes, tidy, and a one-line notice for new mail. Formerly `gmail`, whose setup it still reads. |
| `microsoft` | Outlook.com, Hotmail and Microsoft 365 through Microsoft Graph, one sign-in: mail - search, read, draft, send with your yes, tidy, and a notice for new Focused mail - and the calendar, every change a card naming who will be emailed. Tools are `outlook_*`, so it sits beside `email` and `google-calendar`. |
| `caldav` | iCloud Calendar - and Fastmail, Yahoo, Nextcloud or any CalDAV server - with the app password `email` already reads: list, find free time, and change with your yes, every change a card naming who the server will email. Tools are `caldav_calendar_*`; the password goes to the server's own domain and nowhere else. |
| `google-drive` | Google Drive: find, read and download files; upload, create, organise and share them, every change a card, and a confirm card naming everyone whenever somebody new could see a file. Sign in with `atlas auth login google-drive:google`. |
| `dice` | A `roll_dice` tool - the example a new plugin is copied from. |

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

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: copy `dice/`, rename
everything, make `scripts/validate.py` pass, open a pull request.
