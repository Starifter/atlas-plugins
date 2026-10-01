---
name: spotify
description: Spotify - what's playing, search, your playlists, and playback on any of your devices. Needs Spotify Premium to control playback.
categories: [music, media]
logo: logo.svg
version: "1.0.0"
requires_atlas_sdk: ">=1.34,<2"
contracts:
  tools:
    - spotify_now_playing
    - spotify_devices
    - spotify_search
    - spotify_playlists
    - spotify_play
    - spotify_pause
    - spotify_skip
    - spotify_volume
    - spotify_queue
    - spotify_transfer
    - spotify_playlist_add
    - spotify_playlist_remove
    - spotify_playlist_create
    - spotify_playlist_delete
  logins: [spotify]
config_schema:
  client_id:
    type: str
    default: ""
    description: The Client ID of your Spotify app, from developer.spotify.com/dashboard.
  redirect_port:
    type: int
    default: 0
    description: A fixed port for the sign-in redirect, only if Spotify insists on one; then register http://127.0.0.1:<port>/callback. 0 is any free port.
  search_results:
    type: int
    default: 10
    description: How many results one search returns (Spotify allows at most 10).
  max_results:
    type: int
    default: 50
    description: The most playlists, or items of one playlist, a listing shows (at most 50).
---

# spotify

**Read this when:** you want Atlas to tell you what's playing, find music, play it on your
phone or speaker, or tidy your playlists - or you want to know why a command did nothing, or
exactly what Atlas can reach in your Spotify account.

**See also:** [`docs/spec/spotify.md`](https://github.com/Starifter/atlas/blob/main/docs/spec/spotify.md)
is the specification and wins where they disagree.

---

`spotify` is in Atlas's official marketplace. Install it once:

```
/plugins install spotify
```

It does nothing until you sign in: no connection to Spotify, and the tools say how.

## Two things to know first

- **Playing, pausing, skipping and the rest need Spotify Premium.** Spotify lets other apps
  control playback only on Premium accounts. On a free account Atlas can still tell you what's
  playing, search, and read and change your playlists.
- **The device has to have Spotify open.** Atlas plays on your phone, computer or speaker
  through Spotify Connect, with nothing of Atlas's installed there - but a device only shows up
  while the Spotify app is open on it. A phone that has closed the app drops off the list; open
  Spotify on it and ask again.

## Signing in

Spotify doesn't let one app serve everyone any more, so you make your own - free, five
minutes:

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard), sign in,
   and *Create app*. Name it "Atlas".
2. Under *Redirect URIs* add exactly `http://127.0.0.1/callback`. Not `localhost` - Spotify
   refuses it.
3. Tick **Web API**, agree, and *Save*.
4. In the app's *Settings* → *User Management*, add the Spotify account that will sign in -
   your own, and anyone else's in the house. **At most five people**, and **the app's owner
   needs Premium** (Spotify's rules since February 2026).
5. Copy the **Client ID** into `config.json`:

```jsonc
{ "plugins_settings": { "spotify": { "client_id": "0123456789abcdef0123456789abcdef" } } }
```

Then:

```
atlas auth login spotify:spotify
```

A browser opens on Spotify; agree to what Atlas asks for. A second person's account is a
second sign-in with a name - `atlas auth login spotify:spotify --id sam` - and then you say
whose account to control.

**Spotify ends a sign-in after six months**, however often it is used. When that happens the
tools say "sign in again"; run the same command.

If signing in fails with *INVALID_CLIENT: Invalid redirect URI*, Spotify wants the port fixed:
set `"redirect_port": 8888`, and register `http://127.0.0.1:8888/callback` instead.

## What you can ask

| You say | Atlas uses | Asks you first? |
|---|---|---|
| "What's playing?" | `spotify_now_playing` | no |
| "What can I play on?" | `spotify_devices` | no |
| "Find Bonobo's Kerala" | `spotify_search` | no |
| "What's in my Focus playlist?" | `spotify_playlists` | no |
| "Play my focus playlist on my phone" | `spotify_playlists`, `spotify_play` | as your permission mode says |
| "Pause", "next song", "volume 30" | `spotify_pause`, `spotify_skip`, `spotify_volume` | as your permission mode says |
| "Queue that one next" | `spotify_queue` | as your permission mode says |
| "Move it to the kitchen speaker" | `spotify_transfer` | as your permission mode says |
| "Add this to Road Trip" | `spotify_playlist_add` | as your permission mode says |
| "Take the Christmas songs out of Road Trip" | `spotify_playlist_remove` | as your permission mode says |
| "Make a playlist called Running" | `spotify_playlist_create` | **yes, always** |
| "Delete the Party 2019 playlist" | `spotify_playlist_delete` | **yes, always** |

Playing, pausing and adding to a playlist cost nothing and reach nobody, so they follow your
permission mode like any ordinary action. Making or deleting a playlist always shows you a
card:

```
Remove the playlist Party 2019 (48 items)
You made it: it goes from your library and profile. Anyone who follows it keeps their copy,
and you can follow it again from its link.
```

**Spotify has no true delete.** Deleting a playlist you made takes it out of your library, as
Spotify's own app does.

Search results and playlists carry short ids - `t3` for a track, `p2` for a playlist - that
Atlas uses for the next step. They last while Atlas runs; after a restart, search again. You
can also paste a `spotify:` URI or an `open.spotify.com` link.

**Only you can use it.** The tools are offered in your own conversations and nowhere else.

## Settings

All optional, under `plugins_settings.spotify`:

| Key | Default | What |
|---|---|---|
| `client_id` | none | Your Spotify app's Client ID (above). |
| `redirect_port` | `0` | A fixed sign-in port, only if Spotify insists (above). |
| `search_results` | `10` | Results per search; Spotify's own maximum is 10. |
| `max_results` | `50` | Playlists, or items of one playlist, per listing. |

## What reaches Spotify

Requests to `api.spotify.com` and sign-in at `accounts.spotify.com`, over TLS, with the access
Spotify's consent screen showed you: your playback, your devices, your playlists, and taking a
playlist out of your library. Nothing else.

What you ask Atlas about your music goes to the model you chose, as everything you ask it does.

## Troubleshooting

**"not signed in to Spotify"** - run `atlas auth login spotify:spotify`.

**"needs an app of your own"** - make one (above) and set `client_id`.

**"this account is not Premium"** - playback control is Premium-only; Spotify's rule.

**"no Spotify device is active"** - open Spotify on the phone, computer or speaker, then ask
again.

**"refused the sign-in (401)"** or a sign-in that stopped working - sign in again; Spotify ends
sign-ins after six months.

**"Spotify refused this (403)"** on sign-in or every call - the account isn't in your app's
*User Management* list.

**"request quota is used up"** - Spotify limits each developer's apps together; wait and try
later.

**"no item 't3' - search again"** - ids last while Atlas runs; search again for fresh ones.
