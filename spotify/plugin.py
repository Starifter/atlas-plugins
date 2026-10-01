"""Spotify: playback and library through the Spotify Web API (`docs/spec/spotify.md`).

One login and fourteen tools, no service. Every tool is `trusted_only` - offered
only in a session an owner holds - and `untrusted`, because titles, playlist
names and descriptions are words other people chose. Reading is free.
Controlling playback and changing a playlist's items are gated and take the
permission mode's ordinary answer: they cost nothing and reach nobody. Creating
a playlist or removing one from the library is a card a person answers in every
mode (`Subject.confirm`).

Spotify Connect is what makes "play my focus playlist on my phone" work with no
node: the Web API drives whichever device has Spotify open. Two limits are
Spotify's and are said in words whenever they bite - playback control needs
Premium, and a device is only reachable while Spotify is open on it.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from atlas.sdk.oauth import LoginContext, OAuthClient, Tokens, connections, grant
from atlas.sdk.plugin_entry import Plugin, PluginContext
from atlas.sdk.runtime import CredentialError, ToolError, assert_active
from atlas.sdk.tool_plugin import Subject, Tool, ToolResult
from atlas.sdk.web import WebError, request

PLUGIN = "spotify"
LOGIN = "spotify"
LOGIN_NAME = f"{PLUGIN}:{LOGIN}"
"""What a person types: `atlas auth login spotify:spotify`."""

DEFAULT_CLIENT_ID = ""
"""Atlas's own Spotify app (`spotify.md` R4.1). Empty: a Development-mode app reaches
five people, so every install brings its own `client_id` until Spotify says otherwise."""

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
REDIRECT_HOST = "127.0.0.1"
"""R4.2: Spotify refuses `localhost` in a redirect URI; a loopback IP literal is allowed."""
SCOPES = (
    "user-read-playback-state",
    "user-read-currently-playing",
    "user-modify-playback-state",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-public",
    "playlist-modify-private",
    "user-library-modify",
)
"""R4.3: playback, the person's playlists, and taking a playlist out of the library."""

USER_AGENT = "atlas-spotify"
RESPONSE_BYTES = 5_000_000
RETRY_CAP = 10.0
"""R5.2: a read waits out a 429 or a 5xx once, for at most this long."""
SEARCH_MAX = 10
"""Spotify's own cap on `limit` since February 2026."""
ITEMS_MAX = 50
TITLE_MAX = 120
KINDS = {"track": "t", "episode": "e", "album": "a", "artist": "r", "playlist": "p"}
"""R5.3: the letter a handle starts with, by what it names."""
SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{1,64}")
URI = re.compile(r"spotify:(track|episode|album|artist|playlist):([A-Za-z0-9]{1,64})")
HANDLE = re.compile(r"([tearp])(\d{1,6})")
NOT_SIGNED_IN = f"not signed in to Spotify: atlas auth login {LOGIN_NAME}"
DEVICE_HINT = (
    "a device is listed only while Spotify is open on it - open the app on the phone or "
    "speaker first"
)


# -- talking to Spotify ------------------------------------------------------------------


class SpotifyError(ToolError):
    """A Spotify status that was not 2xx, said in words (`spotify.md` §9)."""

    def __init__(self, status: int, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


def label_of(connection_id: str) -> str:
    """`spotify:family` -> `family`."""
    return connection_id.partition(":")[2] or connection_id


def _error_of(response: Any) -> tuple[str, str]:
    """Spotify's `{"error": {"message", "reason"}}`, as (message, reason)."""
    try:
        data = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "", ""
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or "")[:300], str(error.get("reason") or "")
    if isinstance(error, str):  # the accounts service's shape: {"error": "invalid_grant"}
        return error[:300], ""
    return "", ""


def _retry_after(response: Any) -> float:
    try:
        return min(max(float(response.header("retry-after") or 1), 0.0), RETRY_CAP)
    except ValueError:
        return 1.0


def _explain(status: int, text: str, reason: str) -> str:
    if status == 401:
        return (
            f"Spotify refused the sign-in (401): sign in again with atlas auth login {LOGIN_NAME}"
        )
    if reason == "PREMIUM_REQUIRED" or (status == 403 and "premium" in text.lower()):
        return (
            "Spotify only lets Premium accounts be controlled from other apps - this account "
            "is not Premium, so Atlas can read but not play, pause or skip"
        )
    if reason == "NO_ACTIVE_DEVICE" or (status == 404 and "device" in text.lower()):
        return f"no Spotify device is active - {DEVICE_HINT}"
    if status == 403:
        return (
            "Spotify refused this (403)"
            + (f": {text}" if text else "")
            + " - a playlist can be changed only by its owner or a collaborator, and a "
            "Development-mode app only works for the people added to it"
        )
    if status == 404:
        return "Spotify has no such thing (404) - search again"
    if status == 429 and (reason == "QUOTA_EXCEEDED" or "quota" in text.lower()):
        return (
            "the Spotify app's request quota is used up for now (it is shared by every app of "
            "its developer account) - try again later"
        )
    if status == 429:
        return "Spotify is limiting requests for now (429); try again in a minute"
    return f"HTTP {status} from Spotify{f': {text}' if text else ''}"


class Spotify:
    """One install's way to the Web API: which connection, which verb, what came back."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self._me: dict[str, Mapping[str, Any]] = {}

    def accounts(self) -> tuple[str, ...]:
        return connections(PLUGIN, workspace=self.workspace)

    def pick(self, account: str, *, write: bool) -> list[str]:
        """R4.4: the only account, the named one, or - for a read - all of them."""
        known = self.accounts()
        if not known:
            raise CredentialError(NOT_SIGNED_IN)
        if account:
            wanted = account if ":" in account else f"{PLUGIN}:{account}"
            if wanted not in known:
                names = ", ".join(label_of(k) for k in known)
                raise ToolError(f"no account {account!r} - signed in: {names}")
            return [wanted]
        if len(known) > 1 and write:
            names = ", ".join(label_of(k) for k in known)
            raise ToolError(f"more than one account is signed in; say which with account: {names}")
        return list(known)

    async def bearer(self, account: str) -> str:
        # `bearer` refreshes over the network when due, synchronously (`oauth.md` §5.4).
        return await asyncio.to_thread(lambda: grant(account, workspace=self.workspace).bearer())

    async def call(
        self,
        account: str,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        retry: bool = True,
    ) -> Any:
        """One Web API call: the decoded JSON, `{}` for an empty body (`204`). A
        status that is not 2xx is a `SpotifyError` in words. `retry` is for reads
        only (R5.2): a control call is never sent twice."""
        query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        url = f"{API}{path}" + (f"?{urlencode(query, quote_via=quote)}" if query else "")
        for attempt in range(2):
            token = await self.bearer(account)
            try:
                response = await request(
                    method,
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    max_bytes=RESPONSE_BYTES,
                    timeout=30.0,
                    user_agent=USER_AGENT,
                )
            except WebError as exc:
                raise ToolError(f"could not reach Spotify: {exc}") from None
            if 200 <= response.status < 300:
                if not response.body:
                    return {}
                try:
                    return json.loads(response.body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    return {}  # some control calls answer 200 with a bare snapshot id
            text, reason = _error_of(response)
            transient = response.status == 429 or response.status >= 500
            if transient and retry and attempt == 0 and reason != "QUOTA_EXCEEDED":
                await asyncio.sleep(_retry_after(response))
                continue
            raise SpotifyError(response.status, _explain(response.status, text, reason), reason)
        raise AssertionError("unreachable")  # pragma: no cover

    async def me(self, account: str) -> Mapping[str, Any]:
        """R4.5: the account's Spotify id and display name, asked once."""
        if account not in self._me:
            data = await self.call(account, "GET", "/me")
            self._me[account] = data if isinstance(data, dict) else {}
        return self._me[account]

    async def name(self, account: str) -> str:
        me = await self.me(account)
        return str(me.get("display_name") or me.get("id") or label_of(account))


# -- what the tools show -----------------------------------------------------------------


class Handles:
    """R5.3: `t3` for a track, `p2` for a playlist, standing for Spotify's URIs."""

    def __init__(self) -> None:
        self._by_handle: dict[str, tuple[str, str, str]] = {}
        self._by_uri: dict[tuple[str, str], str] = {}
        self._count: dict[str, int] = {}

    def give(self, account: str, uri: str, label: str) -> str:
        kind = uri.split(":")[1] if uri.count(":") == 2 else ""
        letter = KINDS.get(kind, "")
        if not letter:
            return uri
        key = (account, uri)
        if key not in self._by_uri:
            self._count[letter] = self._count.get(letter, 0) + 1
            handle = f"{letter}{self._count[letter]}"
            self._by_uri[key] = handle
        handle = self._by_uri[key]
        self._by_handle[handle] = (account, uri, label)
        return handle

    def take(self, handle: str) -> tuple[str, str, str]:
        found = self._by_handle.get(handle.strip().lower())
        if found is None:
            raise ToolError(f"no item {handle!r} - search again for a fresh id")
        return found


def clean(text: Any, limit: int = TITLE_MAX) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def minutes(ms: Any) -> str:
    try:
        seconds = int(ms) // 1000
    except (TypeError, ValueError):
        return ""
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def artists_of(item: Mapping[str, Any]) -> str:
    names = [clean(a.get("name"), 60) for a in item.get("artists") or () if isinstance(a, dict)]
    return ", ".join(n for n in names if n)


def title_of(item: Mapping[str, Any]) -> str:
    """One thing Spotify has, in a few words: what it is called and whose it is."""
    kind = str(item.get("type") or "")
    name = clean(item.get("name")) or "(untitled)"
    if kind == "track":
        by = artists_of(item)
        return f'"{name}"' + (f" by {by}" if by else "")
    if kind == "episode":
        show = clean((item.get("show") or {}).get("name"), 80)
        return f'"{name}"' + (f" from {show}" if show else "")
    if kind == "album":
        by = artists_of(item)
        return f"the album {name}" + (f" by {by}" if by else "")
    if kind == "playlist":
        return f"the playlist {name}"
    if kind == "artist":
        return name
    return name


def line_of(item: Mapping[str, Any], handle: str) -> str:
    kind = str(item.get("type") or "")
    name = clean(item.get("name")) or "(untitled)"
    parts = [name]
    if kind == "track":
        album = clean((item.get("album") or {}).get("name"), 80)
        parts.append(artists_of(item) + (f" ({album})" if album else ""))
        parts.append(minutes(item.get("duration_ms")))
    elif kind == "episode":
        parts.append(clean((item.get("show") or {}).get("name"), 80))
        parts.append(minutes(item.get("duration_ms")))
    elif kind == "album":
        parts.append(artists_of(item))
        parts.append(str(item.get("release_date") or "")[:4])
    elif kind == "playlist":
        owner = item.get("owner") or {}
        parts.append(f"by {clean(owner.get('display_name') or owner.get('id'), 60)}")
        total = _total(item)
        if total is not None:
            parts.append(f"{total} items")
    elif kind == "artist":
        parts.append("artist")
    return " · ".join(p for p in parts if p) + f"  [id: {handle}]"


def _total(playlist: Mapping[str, Any]) -> int | None:
    """A playlist's length - under `items` since February 2026, `tracks` before."""
    for key in ("items", "tracks"):
        block = playlist.get(key)
        if isinstance(block, dict) and isinstance(block.get("total"), int):
            return int(block["total"])
    return None


def entry_item(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    """A playlist entry's track or episode - `item` since February 2026, `track` before."""
    found = entry.get("item") or entry.get("track")
    return found if isinstance(found, dict) else {}


def parse_target(text: str) -> tuple[str, str]:
    """R5.4: a `spotify:` URI or an open.spotify.com link, as (kind, uri)."""
    given = str(text or "").strip()
    match = URI.fullmatch(given)
    if match:
        return match.group(1), given
    parts = urlsplit(given)
    if parts.scheme in ("http", "https") and (parts.hostname or "").lower() == "open.spotify.com":
        pieces = [p for p in parts.path.split("/") if p]
        if pieces and pieces[0].startswith("intl-"):
            pieces = pieces[1:]
        if len(pieces) >= 2 and pieces[0] in KINDS and SPOTIFY_ID.fullmatch(pieces[1]):
            return pieces[0], f"spotify:{pieces[0]}:{pieces[1]}"
    raise ToolError(
        f"{given!r} is not something Spotify has - give an id from spotify_search (t3, p2), "
        "a spotify: URI, or an open.spotify.com link"
    )


def spotify_id(uri: str) -> str:
    return uri.rpartition(":")[2]


# -- the shared state --------------------------------------------------------------------

ACCOUNT = {
    "type": "string",
    "description": "Which account, by its label. Needed for a change when several are signed in.",
}
DEVICE = {
    "type": "string",
    "description": "A device's name from spotify_devices. Default: the one playing now.",
}
WHAT = {
    "type": "string",
    "description": "An id from spotify_search or spotify_playlists (t3, p2), a spotify: URI, "
    "or an open.spotify.com link.",
}


class Player:
    """What every tool shares: the API, the handles, the settings."""

    def __init__(self, ctx: PluginContext, api: Spotify | None = None) -> None:
        self.ctx = ctx
        self.settings = dict(ctx.settings)
        self.api = api or Spotify(Path(ctx.workspace))
        self.handles = Handles()

    def number(self, key: str, default: int) -> int:
        try:
            return int(self.settings.get(key, default))
        except (TypeError, ValueError):
            return default

    async def resolve(self, given: str, account: str) -> tuple[str, str, str, str]:
        """What a call names, as (connection, kind, uri, label). A handle carries its
        account; a URI or link is on the account `account` picks."""
        text = str(given or "").strip()
        if not text:
            raise ToolError("say what - an id from spotify_search, a spotify: URI or a link")
        if HANDLE.fullmatch(text.lower()):
            connection, uri, label = self.handles.take(text)
            if account:
                connection = self.api.pick(account, write=True)[0]
            return connection, uri.split(":")[1], uri, label
        kind, uri = parse_target(text)
        connection = self.api.pick(account, write=True)[0]
        label = await self.label(connection, kind, uri)
        return connection, kind, uri, label

    async def label(self, connection: str, kind: str, uri: str) -> str:
        """A name for the card, asked of Spotify; the URI itself when it will not say."""
        try:
            item = await self.api.call(connection, "GET", f"/{kind}s/{quote(spotify_id(uri))}")
        except (SpotifyError, ToolError):
            return uri
        return title_of(item) if isinstance(item, dict) and item.get("name") else uri

    async def devices(self, connection: str) -> list[Mapping[str, Any]]:
        data = await self.api.call(connection, "GET", "/me/player/devices")
        return [d for d in (data.get("devices") or []) if isinstance(d, dict)]

    async def device(self, connection: str, name: str) -> tuple[str, str]:
        """R6.4: a device by its name, as (id, name) - exact, then the one whose name
        starts with it, then the one containing it. Empty `name` is the active one."""
        wanted = " ".join(name.split()).lower()
        if not wanted:
            return "", ""
        found = await self.devices(connection)
        for test in (
            lambda d: d == wanted,
            lambda d: d.startswith(wanted),
            lambda d: wanted in d,
        ):
            hits = [d for d in found if test(" ".join(str(d.get("name") or "").split()).lower())]
            if len(hits) == 1 and hits[0].get("id"):
                return str(hits[0]["id"]), str(hits[0].get("name"))
            if len(hits) > 1:
                names = ", ".join(str(d.get("name")) for d in hits)
                raise ToolError(f"{name!r} could be any of: {names} - say which")
        listed = ", ".join(str(d.get("name")) for d in found) or "none"
        raise ToolError(f"no Spotify device called {name!r} - open now: {listed}; {DEVICE_HINT}")

    async def playlist(self, connection: str, uri: str) -> Mapping[str, Any]:
        data = await self.api.call(connection, "GET", f"/playlists/{quote(spotify_id(uri))}")
        if not isinstance(data, dict) or not data.get("id"):
            raise ToolError("Spotify has no such playlist - search again")
        return data


class SpotifyTool(Tool):
    """Owner-only, marked untrusted, failures as sentences."""

    trusted_only = True
    untrusted = True

    def __init__(self, player: Player) -> None:
        self.player = player

    @property
    def api(self) -> Spotify:
        return self.player.api

    async def run(self, **arguments: Any) -> ToolResult:
        try:
            assert_active()
            return ToolResult.ok(await self.act(**arguments))
        except (CredentialError, ToolError) as exc:
            return ToolResult.error(str(exc))

    async def act(self, *args: Any, **arguments: Any) -> str:
        raise NotImplementedError


# -- reading (R6.1) ----------------------------------------------------------------------


class NowPlaying(SpotifyTool):
    name = "spotify_now_playing"
    description = "What Spotify is playing now, on which device, and what is queued next."
    parameters = {"type": "object", "properties": {"account": ACCOUNT}, "required": []}

    async def act(self, account: str = "") -> str:
        accounts = self.api.pick(account, write=False)
        lines: list[str] = []
        for connection in accounts:
            who = f" ({label_of(connection)})" if len(accounts) > 1 else ""
            state = await self.api.call(
                connection, "GET", "/me/player", params={"additional_types": "episode"}
            )
            item = state.get("item") if isinstance(state, dict) else None
            device = (state.get("device") or {}) if isinstance(state, dict) else {}
            if not isinstance(item, dict):
                lines.append(f"Nothing is playing{who}.")
                continue
            handle = self.player.handles.give(
                connection, str(item.get("uri") or ""), title_of(item)
            )
            verb = "Playing" if state.get("is_playing") else "Paused on"
            where = clean(device.get("name"), 60)
            progress = minutes(state.get("progress_ms"))
            length = minutes(item.get("duration_ms"))
            lines.append(
                f"{verb} {title_of(item)}{who}"
                + (f" on {where}" if where else "")
                + (f" · {progress} of {length}" if progress and length else "")
                + (
                    f" · volume {device.get('volume_percent')}%"
                    if device.get("volume_percent") is not None
                    else ""
                )
                + ("" if not state.get("shuffle_state") else " · shuffle")
                + (
                    f" · repeat {state.get('repeat_state')}"
                    if state.get("repeat_state") not in (None, "off")
                    else ""
                )
                + f"  [id: {handle}]"
            )
            queue = await self.api.call(connection, "GET", "/me/player/queue")
            upcoming = [q for q in (queue.get("queue") or []) if isinstance(q, dict)][:5]
            if upcoming:
                lines.append("Next:")
                lines += [
                    "  "
                    + line_of(
                        q, self.player.handles.give(connection, str(q.get("uri")), title_of(q))
                    )
                    for q in upcoming
                ]
        return "\n".join(lines)


class Devices(SpotifyTool):
    name = "spotify_devices"
    description = (
        "The devices Spotify can play on now - phones, computers, speakers - and which is "
        "active. A device is listed only while Spotify is open on it."
    )
    parameters = {"type": "object", "properties": {"account": ACCOUNT}, "required": []}

    async def act(self, account: str = "") -> str:
        accounts = self.api.pick(account, write=False)
        lines: list[str] = []
        for connection in accounts:
            for device in await self.player.devices(connection):
                marks = [str(device.get("type") or "").lower()]
                if device.get("is_active"):
                    marks.append("active")
                if device.get("volume_percent") is not None:
                    marks.append(f"volume {device.get('volume_percent')}%")
                if device.get("is_restricted"):
                    marks.append("cannot be controlled from other apps")
                lines.append(
                    f"{clean(device.get('name'), 60)} · {', '.join(m for m in marks if m)}"
                    + (f" ({label_of(connection)})" if len(accounts) > 1 else "")
                )
        if not lines:
            return f"No devices. {DEVICE_HINT[0].upper()}{DEVICE_HINT[1:]}."
        return "\n".join(lines)


class Search(SpotifyTool):
    name = "spotify_search"
    description = (
        "Search Spotify for tracks, albums, artists, playlists or podcast episodes. Each "
        "result has an id (t3, p2) to play, queue or add to a playlist."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Words; Spotify's filters work too: artist:, album:, year:.",
            },
            "type": {
                "type": "string",
                "enum": ["track", "album", "artist", "playlist", "episode"],
                "description": "What to look for. Default: track.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": SEARCH_MAX},
            "account": ACCOUNT,
        },
        "required": ["query"],
    }

    async def act(self, query: str, type: str = "track", limit: int = 0, account: str = "") -> str:
        words = " ".join(str(query or "").split())
        if not words:
            raise ToolError("say what to search for")
        kind = type if type in KINDS else "track"
        size = max(
            1, min(int(limit or self.player.number("search_results", SEARCH_MAX)), SEARCH_MAX)
        )
        connection = self.api.pick(account, write=False)[0]
        data = await self.api.call(
            connection, "GET", "/search", params={"q": words, "type": kind, "limit": size}
        )
        found = [
            i for i in ((data.get(f"{kind}s") or {}).get("items") or []) if isinstance(i, dict)
        ]
        if not found:
            return f"No {kind}s for {words!r}."
        lines = [f"{len(found)} {kind}(s) for {words!r}:"]
        for item in found:
            handle = self.player.handles.give(
                connection, str(item.get("uri") or ""), title_of(item)
            )
            lines.append(line_of(item, handle))
        return "\n".join(lines)


class Playlists(SpotifyTool):
    name = "spotify_playlists"
    description = (
        "The person's playlists, or - given a playlist's id - what is in it. Spotify shows a "
        "playlist's contents only for playlists the person owns or collaborates on."
    )
    parameters = {
        "type": "object",
        "properties": {
            "playlist": {**WHAT, "description": "A playlist (p2, URI or link). Omit to list them."},
            "account": ACCOUNT,
        },
        "required": [],
    }

    async def act(self, playlist: str = "", account: str = "") -> str:
        most = max(1, min(self.player.number("max_results", 50), ITEMS_MAX))
        if playlist:
            connection, kind, uri, _ = await self.player.resolve(playlist, account)
            if kind != "playlist":
                raise ToolError(f"{playlist!r} is a {kind}, not a playlist")
            meta = await self.player.playlist(connection, uri)
            data = await self.api.call(
                connection,
                "GET",
                f"/playlists/{quote(spotify_id(uri))}/items",
                params={"limit": most, "additional_types": "episode"},
            )
            entries = [entry_item(e) for e in (data.get("items") or []) if isinstance(e, dict)]
            entries = [e for e in entries if e.get("uri")]
            total = data.get("total")
            head = (
                f"{clean(meta.get('name'))} · {total if total is not None else len(entries)} items:"
            )
            lines = [head]
            for item in entries:
                handle = self.player.handles.give(connection, str(item["uri"]), title_of(item))
                lines.append("  " + line_of(item, handle))
            if isinstance(total, int) and total > len(entries):
                lines.append(f"  (the first {len(entries)} of {total})")
            return "\n".join(lines)
        accounts = self.api.pick(account, write=False)
        lines = []
        for connection in accounts:
            data = await self.api.call(connection, "GET", "/me/playlists", params={"limit": most})
            for item in data.get("items") or []:
                if not isinstance(item, dict) or not item.get("uri"):
                    continue
                handle = self.player.handles.give(connection, str(item["uri"]), title_of(item))
                lines.append(
                    line_of(item, handle)
                    + (f" ({label_of(connection)})" if len(accounts) > 1 else "")
                )
        return "\n".join(lines) or "No playlists."


# -- changing things (R6.3-R6.9) ---------------------------------------------------------


class Change:
    """A call worked out for its card, and the one request that makes it."""

    def __init__(self, card: str, run: Callable[[], Awaitable[str]]) -> None:
        self.card = card
        self.run = run


class Carded(SpotifyTool):
    """Gated: worked out once for the card and done as shown. With `confirm`, a card
    must have been shown; without, the permission mode's answer stands, and a
    call the gate let through unasked is worked out again."""

    gated = True
    verb = "change"
    confirm = False

    def __init__(self, player: Player) -> None:
        super().__init__(player)
        self._looked: dict[str, tuple[float, Change]] = {}

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        raise NotImplementedError

    async def subject(self, arguments: Mapping[str, Any]) -> Subject | None:
        try:
            change = await self.prepare(arguments)
        except (CredentialError, ToolError):
            return None  # `run` fails the same way, before anything changes
        self._looked[_key(arguments)] = (time.monotonic(), change)
        return Subject(tool=self.name, action=self.verb, summary=change.card, confirm=self.confirm)

    async def act(self, **arguments: Any) -> str:
        seen = self._looked.pop(_key(arguments), None)
        change = seen[1] if seen is not None and time.monotonic() - seen[0] < 900 else None
        if change is None:
            if self.confirm:
                await self.prepare(arguments)  # says why no card could be drawn
                raise ToolError("this has to be shown to the person first; nothing was changed")
            change = await self.prepare(arguments)
        return await change.run()


def _key(arguments: Mapping[str, Any]) -> str:
    return json.dumps(arguments, sort_keys=True, default=str)


def _on(device: str) -> str:
    return f" on {device}" if device else " on the device playing now"


class Play(Carded):
    name = "spotify_play"
    verb = "play"
    description = (
        "Play a track, album, artist, playlist or episode - or, with nothing named, carry on "
        "where it paused - optionally on a named device. Needs Spotify Premium, and the "
        "device must have Spotify open."
    )
    parameters = {
        "type": "object",
        "properties": {
            "what": {**WHAT, "description": WHAT["description"] + " Omit to resume."},
            "device": DEVICE,
            "account": ACCOUNT,
        },
        "required": [],
    }

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        account = str(arguments.get("account") or "")
        what = str(arguments.get("what") or "").strip()
        body: dict[str, Any] | None = None
        if what:
            connection, kind, uri, label = await self.player.resolve(what, account)
            body = {"uris": [uri]} if kind in ("track", "episode") else {"context_uri": uri}
            card = f"Play {label}"
        else:
            connection = self.api.pick(account, write=True)[0]
            card = "Resume playing"
        device_id, device = await self.player.device(connection, str(arguments.get("device") or ""))
        card += _on(device)

        async def run() -> str:
            await self.api.call(
                connection,
                "PUT",
                "/me/player/play",
                params={"device_id": device_id},
                body=body,
                retry=False,
            )
            return f"Started: {card}."

        return Change(card, run)


class Pause(Carded):
    name = "spotify_pause"
    verb = "pause"
    description = "Pause Spotify, on the device playing now or a named one. Needs Premium."
    parameters = {
        "type": "object",
        "properties": {"device": DEVICE, "account": ACCOUNT},
        "required": [],
    }

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        connection = self.api.pick(str(arguments.get("account") or ""), write=True)[0]
        device_id, device = await self.player.device(connection, str(arguments.get("device") or ""))

        async def run() -> str:
            await self.api.call(
                connection, "PUT", "/me/player/pause", params={"device_id": device_id}, retry=False
            )
            return "Paused."

        return Change("Pause Spotify" + _on(device), run)


class Skip(Carded):
    name = "spotify_skip"
    verb = "skip"
    description = "Skip to the next track, or back to the previous one. Needs Premium."
    parameters = {
        "type": "object",
        "properties": {
            "direction": {"type": "string", "enum": ["next", "previous"]},
            "device": DEVICE,
            "account": ACCOUNT,
        },
        "required": [],
    }

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        direction = str(arguments.get("direction") or "next")
        if direction not in ("next", "previous"):
            raise ToolError("direction is next or previous")
        connection = self.api.pick(str(arguments.get("account") or ""), write=True)[0]
        device_id, device = await self.player.device(connection, str(arguments.get("device") or ""))

        async def run() -> str:
            await self.api.call(
                connection,
                "POST",
                f"/me/player/{direction}",
                params={"device_id": device_id},
                retry=False,
            )
            return (
                "Skipped to the next track."
                if direction == "next"
                else "Back to the previous track."
            )

        word = "Skip to the next track" if direction == "next" else "Go back to the previous track"
        return Change(word + _on(device), run)


class Volume(Carded):
    name = "spotify_volume"
    verb = "volume"
    description = (
        "Set Spotify's volume, 0 to 100, on the device playing now or a named one. Needs Premium."
    )
    parameters = {
        "type": "object",
        "properties": {
            "percent": {"type": "integer", "minimum": 0, "maximum": 100},
            "device": DEVICE,
            "account": ACCOUNT,
        },
        "required": ["percent"],
    }

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        try:
            percent = int(arguments.get("percent"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ToolError("percent is a whole number from 0 to 100") from None
        if not 0 <= percent <= 100:
            raise ToolError("percent is a whole number from 0 to 100")
        connection = self.api.pick(str(arguments.get("account") or ""), write=True)[0]
        device_id, device = await self.player.device(connection, str(arguments.get("device") or ""))

        async def run() -> str:
            await self.api.call(
                connection,
                "PUT",
                "/me/player/volume",
                params={"volume_percent": percent, "device_id": device_id},
                retry=False,
            )
            return f"Volume {percent}%."

        return Change(f"Set the volume to {percent}%" + _on(device), run)


class Queue(Carded):
    name = "spotify_queue"
    verb = "queue"
    description = "Add a track or episode to the end of what is queued to play. Needs Premium."
    parameters = {
        "type": "object",
        "properties": {"what": WHAT, "device": DEVICE, "account": ACCOUNT},
        "required": ["what"],
    }

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        connection, kind, uri, label = await self.player.resolve(
            str(arguments.get("what") or ""), str(arguments.get("account") or "")
        )
        if kind not in ("track", "episode"):
            raise ToolError(
                f"only a track or an episode can be queued - that is a {kind}; play it instead"
            )
        device_id, device = await self.player.device(connection, str(arguments.get("device") or ""))

        async def run() -> str:
            await self.api.call(
                connection,
                "POST",
                "/me/player/queue",
                params={"uri": uri, "device_id": device_id},
                retry=False,
            )
            return f"Queued {label}."

        return Change(f"Queue {label}" + _on(device), run)


class Transfer(Carded):
    name = "spotify_transfer"
    verb = "transfer"
    description = (
        "Move playback to another device - 'play this on the kitchen speaker'. The device "
        "must have Spotify open. Needs Premium."
    )
    parameters = {
        "type": "object",
        "properties": {
            "device": {"type": "string", "description": "The device's name, from spotify_devices."},
            "play": {"type": "boolean", "description": "Start playing there. Default true."},
            "account": ACCOUNT,
        },
        "required": ["device"],
    }

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        name = str(arguments.get("device") or "").strip()
        if not name:
            raise ToolError("say which device - spotify_devices lists them")
        connection = self.api.pick(str(arguments.get("account") or ""), write=True)[0]
        device_id, device = await self.player.device(connection, name)
        play = arguments.get("play", True) is not False

        async def run() -> str:
            await self.api.call(
                connection,
                "PUT",
                "/me/player",
                body={"device_ids": [device_id], "play": play},
                retry=False,
            )
            return f"Moved to {device}."

        return Change(f"Move playback to {device}" + ("" if play else " (paused)"), run)


def _items(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in value or () if str(v).strip()]


class PlaylistItems(Carded):
    """Adding to or removing from a playlist: gated, ordinary answer (R6.6)."""

    adding = True

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        account = str(arguments.get("account") or "")
        connection, kind, uri, _ = await self.player.resolve(
            str(arguments.get("playlist") or ""), account
        )
        if kind != "playlist":
            raise ToolError(f"that is a {kind}, not a playlist")
        given = _items(arguments.get("items"))
        if not given:
            raise ToolError("say which tracks or episodes")
        if len(given) > ITEMS_MAX:
            raise ToolError(f"{len(given)} items - at most {ITEMS_MAX} at once")
        found: list[tuple[str, str]] = []
        for one in given:
            _, item_kind, item_uri, label = await self.player.resolve(one, label_of(connection))
            if item_kind not in ("track", "episode"):
                raise ToolError(f"{one!r} is a {item_kind} - a playlist holds tracks and episodes")
            found.append((item_uri, label))
        meta = await self.player.playlist(connection, uri)
        name = clean(meta.get("name"))
        owner = meta.get("owner") or {}
        mine = str(owner.get("id") or "") == str((await self.api.me(connection)).get("id") or "")
        whose = (
            "" if mine else f" (owned by {clean(owner.get('display_name') or owner.get('id'), 60)})"
        )
        shown = [label for _, label in found]
        if self.adding:
            card = f"Add to {name}{whose}: " + "; ".join(shown)
        else:
            card = f"Remove from {name}{whose}: " + "; ".join(shown)
        path = f"/playlists/{quote(spotify_id(uri))}/items"
        snapshot = str(meta.get("snapshot_id") or "")
        uris = [u for u, _ in found]

        async def run() -> str:
            if self.adding:
                await self.api.call(connection, "POST", path, body={"uris": uris}, retry=False)
                return f"Added {len(uris)} to {name}."
            body: dict[str, Any] = {"items": [{"uri": u} for u in uris]}
            if snapshot:
                body["snapshot_id"] = snapshot
            await self.api.call(connection, "DELETE", path, body=body, retry=False)
            return f"Removed {len(uris)} from {name}."

        return Change(card, run)


class PlaylistAdd(PlaylistItems):
    name = "spotify_playlist_add"
    verb = "add"
    description = (
        "Add tracks or episodes to the end of a playlist the person owns or collaborates on."
    )
    parameters = {
        "type": "object",
        "properties": {
            "playlist": {**WHAT, "description": "The playlist (p2, URI or link)."},
            "items": {"type": "array", "items": WHAT, "description": "Tracks or episodes."},
            "account": ACCOUNT,
        },
        "required": ["playlist", "items"],
    }


class PlaylistRemove(PlaylistItems):
    name = "spotify_playlist_remove"
    verb = "remove"
    adding = False
    description = (
        "Remove tracks or episodes from a playlist the person owns or collaborates on - every "
        "time each appears."
    )
    parameters = PlaylistAdd.parameters


class PlaylistCreate(Carded):
    name = "spotify_playlist_create"
    verb = "create"
    confirm = True
    description = (
        "Make a new playlist, private unless public is true, optionally with tracks in it. "
        "The person sees it and must say yes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "public": {"type": "boolean", "description": "Show it on the profile. Default false."},
            "items": {
                "type": "array",
                "items": WHAT,
                "description": "Tracks or episodes to put in it.",
            },
            "account": ACCOUNT,
        },
        "required": ["name"],
    }

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        title = clean(arguments.get("name"), 100)
        if not title:
            raise ToolError("say what the playlist is called")
        connection = self.api.pick(str(arguments.get("account") or ""), write=True)[0]
        public = arguments.get("public") is True
        notes = clean(arguments.get("description"), 300)
        given = _items(arguments.get("items"))
        if len(given) > ITEMS_MAX:
            raise ToolError(f"{len(given)} items - at most {ITEMS_MAX} at once")
        found: list[tuple[str, str]] = []
        for one in given:
            _, kind, uri, label = await self.player.resolve(one, label_of(connection))
            if kind not in ("track", "episode"):
                raise ToolError(f"{one!r} is a {kind} - a playlist holds tracks and episodes")
            found.append((uri, label))
        owner = await self.api.name(connection)
        lines = [
            f"Create the playlist {title} for {owner}",
            "Public: anyone can see it on your profile" if public else "Private: only you see it",
        ]
        lines += [f"Description: {notes}"] if notes else []
        lines += [f"With: {'; '.join(label for _, label in found)}"] if found else []
        body: dict[str, Any] = {"name": title, "public": public}
        if notes:
            body["description"] = notes

        async def run() -> str:
            made = await self.api.call(connection, "POST", "/me/playlists", body=body, retry=False)
            uri = str(made.get("uri") or f"spotify:playlist:{made.get('id', '')}")
            handle = self.player.handles.give(connection, uri, f"the playlist {title}")
            if found:
                await self.api.call(
                    connection,
                    "POST",
                    f"/playlists/{quote(spotify_id(uri))}/items",
                    body={"uris": [u for u, _ in found]},
                    retry=False,
                )
            return (
                f"Created {title}"
                + (f" with {len(found)} items" if found else "")
                + f" [id: {handle}]."
            )

        return Change("\n".join(lines), run)


class PlaylistDelete(Carded):
    name = "spotify_playlist_delete"
    verb = "delete"
    confirm = True
    description = (
        "Take a playlist out of the person's library. Spotify has no true delete: for one the "
        "person made, this is how it is deleted. The person sees it and must say yes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "playlist": {**WHAT, "description": "The playlist (p2, URI or link)."},
            "account": ACCOUNT,
        },
        "required": ["playlist"],
    }

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        connection, kind, uri, _ = await self.player.resolve(
            str(arguments.get("playlist") or ""), str(arguments.get("account") or "")
        )
        if kind != "playlist":
            raise ToolError(f"that is a {kind}, not a playlist")
        meta = await self.player.playlist(connection, uri)
        name = clean(meta.get("name"))
        owner = meta.get("owner") or {}
        mine = str(owner.get("id") or "") == str((await self.api.me(connection)).get("id") or "")
        total = _total(meta)
        lines = [f"Remove the playlist {name}" + (f" ({total} items)" if total is not None else "")]
        if mine:
            lines.append(
                "You made it: it goes from your library and profile. Anyone who follows it keeps "
                "their copy, and you can follow it again from its link."
            )
        else:
            whose = clean(owner.get("display_name") or owner.get("id"), 60)
            lines.append(
                f"It is {whose}'s: you stop following it; it is not deleted for anyone else."
            )

        async def run() -> str:
            await self.api.call(
                connection, "DELETE", "/me/library", params={"uris": uri}, retry=False
            )
            return f"Removed {name} from your library."

        return Change("\n".join(lines), run)


# -- signing in (§4) ---------------------------------------------------------------------


def _port(value: Any) -> int | None:
    try:
        port = int(value or 0)
    except (TypeError, ValueError):
        return None
    return port if 1024 <= port <= 65535 else None


def client(client_id: str, port: int | None = None) -> OAuthClient:
    return OAuthClient(
        authorize_url=AUTHORIZE_URL,
        token_url=TOKEN_URL,
        client_id=client_id,
        scopes=SCOPES,
        redirect_host=REDIRECT_HOST,
        redirect_port=port,
        label="Spotify",
    )


def no_client(context: LoginContext) -> Tokens:
    """What signing in does before any client id exists: say how to make one, and stop."""
    raise CredentialError(
        "Spotify sign-in needs an app of your own. At developer.spotify.com/dashboard: Create "
        "app; name it Atlas; Redirect URI http://127.0.0.1/callback (not localhost - Spotify "
        "refuses it); tick Web API; save. Under Settings, User Management, add the Spotify "
        "account(s) that will sign in (at most five, and the app's owner needs Premium). Then "
        "copy the Client ID into plugins_settings.spotify.client_id in config.json."
    )


READ_TOOLS = (NowPlaying, Devices, Search, Playlists)
CONTROL_TOOLS = (Play, Pause, Skip, Volume, Queue, Transfer)
PLAYLIST_TOOLS = (PlaylistAdd, PlaylistRemove, PlaylistCreate, PlaylistDelete)
TOOLS: Sequence[type[SpotifyTool]] = (*READ_TOOLS, *CONTROL_TOOLS, *PLAYLIST_TOOLS)


class SpotifyPlugin(Plugin):
    name = PLUGIN
    description = "Spotify: what's playing, search, playlists, and playback on any device."

    def register(self, ctx: PluginContext) -> None:
        client_id = str(ctx.setting("client_id", "") or "").strip() or DEFAULT_CLIENT_ID
        port = _port(ctx.setting("redirect_port", 0))
        ctx.register_login(LOGIN, client(client_id, port) if client_id else no_client)
        player = Player(ctx)
        for tool in TOOLS:
            ctx.register_tool(tool(player), toolset="Spotify")
