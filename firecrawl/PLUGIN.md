---
name: firecrawl
description: Firecrawl as a web_fetch backend - the pages the built-in fetcher cannot read, with or without a key.
version: "1.0.0"
requires_atlas_sdk: ">=1.15,<2"
categories: [web, fetch]
logo: logo.svg
contracts:
  fetchers: [firecrawl]
config_schema:
  api_key_env:
    type: str
    default: FIRECRAWL_API_KEY
    description: The variable the key is read from, in the environment or ~/.atlas/.env.
  base_url:
    type: str
    default: https://api.firecrawl.dev
    description: Where the scrape API is. A self-hosted one on a private address also needs web_allowed_hosts or web_allow_private_addresses.
  keyless:
    type: bool
    default: true
    description: Without a key, still send a URL to Firecrawl's starter tier. false makes this fetcher wait for a key.
  only_main_content:
    type: bool
    default: true
    description: Ask Firecrawl to drop headers, navigation and footers. Off when web_readability is off.
  max_age_ms:
    type: int
    default: 0
    description: Accept a page Firecrawl cached this recently, in milliseconds. 0 leaves it to Firecrawl.
  timeout_seconds:
    type: float
    default: 30
    description: How long one scrape may take. The tool's own web_timeout_seconds still applies over it.
---

# firecrawl

The fetcher after the built-in one. `web_fetch` tries `reader` first - it is part of
Atlas, costs nothing and reads most pages - and this one when `reader` declined: a PDF, a
page a bot wall answered, a page rendered by JavaScript that came back as a title and a
cookie notice. [Firecrawl](https://firecrawl.dev) runs a browser on its side, reads PDFs,
and returns markdown, and this plugin is the whole of that over `atlas.sdk.web.post`. It is
the plugin to copy if you are writing a fetcher of your own: it reads its key through
`atlas.sdk.auth`, sends its one request through the shipped client, and uses nothing a
plugin could not.

## Setup

```
/plugins install firecrawl
```

That copies it into `~/.atlas/plugins/firecrawl/` and enables it; it loads on the next
session. `/plugins` then lists it beside `reader`:

```
firecrawl  via user   +fetcher firecrawl
```

**Once installed it is ready without a key.** Firecrawl's scrape API answers an
unauthenticated request at a low rate limit, and by default this plugin uses that. What it
means is worth stating plainly: a URL the built-in fetcher could not read is sent to
`api.firecrawl.dev`, a service you did not sign up for. Only the URL goes - no headers you
configured, no cookie, nothing from the conversation - and it went through Atlas's own
address policy before either fetcher was built, so a private or cloud-metadata address is
refused before it could be sent anywhere. The result comes back in the same envelope every
fetched page does.

If that is not what you want, either line does it:

```jsonc
{ "plugins_settings": { "firecrawl": { "keyless": false } } }   // wait for a key
{ "plugins_disabled": ["firecrawl"] }                           // not loaded at all
```

With `keyless: false` the plugin loads, registers and reports itself not ready until a key
resolves, exactly as `brave` does; a call that names it (`backend: firecrawl`) is refused
with a sentence rather than sent.

**With a key** the same requests carry a bearer and the rate limit is your plan's. Get one
at [firecrawl.dev](https://firecrawl.dev) and store it without typing it where the model can
see it:

```console
$ atlas "store my firecrawl api key"
```

or write the line yourself:

```
# ~/.atlas/.env
FIRECRAWL_API_KEY=fc-...
```

The key is read fresh on every fetch, never cached at install, so one stored mid-session is
used on the next call. It lives in `~/.atlas/.env` and not in `auth-profiles.json`: that
file is for model providers, keyed by vendor name, and it rotates - none of which means
anything for a scrape key.

## Settings

All optional, all under `plugins_settings.firecrawl` in `config.json`:

| Key | Default | What |
|---|---|---|
| `api_key_env` | `FIRECRAWL_API_KEY` | The variable the key is read from, in the environment or `~/.atlas/.env`. |
| `base_url` | `https://api.firecrawl.dev` | Where `/v2/scrape` is. A self-hosted instance on a private address also needs `web_allowed_hosts` or `web_allow_private_addresses` - see below. |
| `keyless` | `true` | Without a key, still send. `false` makes the fetcher wait for one. |
| `only_main_content` | `true` | Sent as Firecrawl's `onlyMainContent`. Off whenever `web_readability` is off, whatever this says. |
| `max_age_ms` | `0` | Sent as `maxAge` when set: Firecrawl may answer from its own cache of the page if it is this fresh. `0` sends nothing and leaves it to Firecrawl's default. |
| `timeout_seconds` | `30` | Sent as `timeout`, and the client's own limit. `web_timeout_seconds` still applies over the whole `web_fetch` call. |

The caps are `web_fetch`'s, not this plugin's - `web_max_chars`, `web_max_bytes` and
`web_cache_minutes`. `web_max_bytes` bounds the page the model reads; the plugin caps
Firecrawl's JSON reply, which carries the page and is larger than it, at 10 MB on the wire.

## What reaches Firecrawl

One `POST` to `<base_url>/v2/scrape`, with the key - when there is one - in the
`Authorization: Bearer` header and this body:

```json
{
  "url": "https://example.tld/report.pdf",
  "formats": ["markdown"],
  "onlyMainContent": true,
  "timeout": 30000
}
```

plus `maxAge` when `max_age_ms` is set. Nothing else: not `web_headers`, not
`web_user_agent`, not a cookie. Those shape the request *Atlas* makes, and this request is
to Firecrawl, whose browser makes its own.

**The key goes to `base_url` and nowhere else.** It is not on the `Fetch`, not in the
result, not in the audit record, and the client never follows a redirect from a `POST`, so
it cannot be carried to an address Firecrawl's server chose.

**`base_url` is checked under your policy.** The request goes through the same client as
every fetch, and that client refuses a private, loopback or link-local address unless the
operator allowed it. A self-hosted Firecrawl on `localhost:3002` is therefore refused until
you say so, exactly as any other private address is:

```jsonc
{
  "plugins_settings": { "firecrawl": { "base_url": "http://localhost:3002" } },
  "web_allowed_hosts": ["localhost"]
}
```

## What comes back

Firecrawl's markdown as the page's text, its `metadata.title` as the title, its final URL
after redirects as the page's URL, and its `contentType` on the type line - so a PDF says
`application/pdf` and the model knows why the page has no links. A page Firecrawl scraped
that was itself a 404 is an error naming the status, not the error page's furniture.
`mode: text` comes back as markdown and says so, because Firecrawl produces nothing plainer.

A Firecrawl-side failure - a rate limit, a page it could not reach - is a sentence with
Firecrawl's own error text in it, and it is the end of the fetch: `reader` already declined
before this one was asked, and nothing is tried after it unless you installed a third.

## Readiness and order

`ready()` is `keyless`, or whether the key resolves; `priority` is 50, after the built-in
`reader` at 10. With nothing configured the order is what decides: `reader`, then
`firecrawl`, and the line under the result says which answered and, when it was not the
first, why the first did not:

```
Fetched https://example.tld/report.pdf via firecrawl; type: application/pdf; 41000 characters,
showing 1-15000 and the last 5000. (after reader could not fetch https://example.tld/report.pdf:
RuntimeError: https://example.tld/report.pdf is application/pdf, which reader cannot turn into text)
```

To have every fetch go here rather than after `reader`, pin it: `{ "web_fetcher": "firecrawl" }`
or `backend: firecrawl` on the call. A pinned backend never falls back.

## Troubleshooting

**"HTTP 429 from Firecrawl: Rate limit exceeded"** - the keyless tier's limit. A key raises
it; so does `web_cache_minutes`, which keeps a repeated fetch from reaching Firecrawl at all.

**"installed but not ready"** - `keyless` is `false` and no key resolved from `api_key_env`.
Check `~/.atlas/.env` for the line. `atlas auth` does not list it, because it is not a
provider credential.

**"refused: ... private address"** on a self-hosted `base_url` - the client applied your
address policy to Firecrawl's address. Add the host to `web_allowed_hosts`.

**A page came through `firecrawl` that you expected from `reader`** - `reader` declined it,
and the parenthesis on the result line says why: the type it could not read, the status it
got, or that the text came back thin.
