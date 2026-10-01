"""The Spotify plugin (`docs/spec/spotify.md`), driven the way Atlas drives it with Spotify
replaced: `request`, `grant` and `connections` are the plugin's module-level names, and a
fake answers the Web API's shapes.

The module is loaded as Atlas loads a plugin - never put in `sys.modules`.

Run from a checkout of Atlas (`uv run --project ../Atlas pytest spotify/tests`).
"""

from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from atlas.web.client import Response

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("atlas_plugin_spotify", HERE.parent / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sp = _load()

MAIN = "spotify:spotify"
TRACK_ID = "4uLU6hMCjMI75M1A2tKUQC"
PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"


def track(
    spotify_id: str = TRACK_ID, name: str = "Kerala", artist: str = "Bonobo"
) -> dict[str, Any]:
    return {
        "type": "track",
        "id": spotify_id,
        "uri": f"spotify:track:{spotify_id}",
        "name": name,
        "artists": [{"name": artist}],
        "album": {"name": "Migration"},
        "duration_ms": 242000,
    }


def playlist(
    spotify_id: str = PLAYLIST_ID, name: str = "Focus", owner: str = "me1", total: int = 2
) -> dict[str, Any]:
    return {
        "type": "playlist",
        "id": spotify_id,
        "uri": f"spotify:playlist:{spotify_id}",
        "name": name,
        "owner": {"id": owner, "display_name": "Me" if owner == "me1" else owner.title()},
        "items": {"total": total},
        "snapshot_id": "snap-1",
    }


class Call(SimpleNamespace):
    method: str
    path: str
    query: dict[str, str]
    body: Any


class Fake:
    """The Spotify Web API, as far as these tests need it."""

    def __init__(self) -> None:
        self.accounts: tuple[str, ...] = (MAIN,)
        self.calls: list[Call] = []
        self.routes: list[tuple[str, str, Callable[[Call], tuple[int, Any]]]] = []
        self.devices = [
            {
                "id": "dev-phone",
                "name": "Sam's iPhone",
                "type": "Smartphone",
                "is_active": True,
                "volume_percent": 60,
            },
            {
                "id": "dev-kitchen",
                "name": "Kitchen speaker",
                "type": "Speaker",
                "is_active": False,
                "volume_percent": 40,
            },
        ]
        self.state: dict[str, Any] | None = {
            "device": self.devices[0],
            "is_playing": True,
            "progress_ms": 61000,
            "shuffle_state": False,
            "repeat_state": "off",
            "item": track(),
        }
        self.playlists = {
            PLAYLIST_ID: playlist(),
            "otherList0000000000000": playlist("otherList0000000000000", "Road Trip", owner="alex"),
        }

    def on(self, method: str, pattern: str, answer: Callable[[Call], tuple[int, Any]]) -> None:
        self.routes.insert(0, (method, pattern, answer))

    def default(self, call: Call) -> tuple[int, Any]:
        path, method = call.path, call.method
        if path == "/me":
            return 200, {"id": "me1", "display_name": "Me"}
        if path == "/me/player" and method == "GET":
            return (204, None) if self.state is None else (200, self.state)
        if path == "/me/player/queue":
            return 200, {"queue": [track("1" * 22, "Cirrus"), track("2" * 22, "Kong")]}
        if path == "/me/player/devices":
            return 200, {"devices": self.devices}
        if path == "/search":
            kind = call.query["type"]
            table: dict[str, list[Any]] = {
                "track": [track(), track("3" * 22, "Kerala (Live)")],
                "playlist": [playlist(), None],
            }
            found = table.get(kind, [])
            return 200, {f"{kind}s": {"items": found[: int(call.query["limit"])]}}
        if path == "/me/playlists" and method == "GET":
            return 200, {"items": list(self.playlists.values())}
        if path == "/me/playlists" and method == "POST":
            made = playlist("newList00000000000000a", call.body["name"], total=0)
            self.playlists[made["id"]] = made
            return 201, made
        match = re.fullmatch(r"/playlists/([^/]+)(/items)?", path)
        if match:
            stored = self.playlists.get(match.group(1))
            if stored is None:
                return 404, {"error": {"status": 404, "message": "Not found."}}
            if not match.group(2):
                return 200, stored
            if method == "GET":
                return 200, {
                    "total": 2,
                    "items": [{"item": track()}, {"track": track("4" * 22, "Black Sands")}],
                }
            return 201, {"snapshot_id": "snap-2"}
        match = re.fullmatch(r"/(track|album|artist|episode|playlist)s/([^/]+)", path)
        if match:
            if match.group(1) == "track":
                return 200, track(match.group(2), "Linked song", "Someone")
            return 404, {"error": {"status": 404, "message": "Not found."}}
        if path.startswith("/me/player") or path == "/me/library":
            return 204, None
        return 404, {"error": {"status": 404, "message": f"no route for {method} {path}"}}

    async def request(self, method: str, url: str, **kwargs: Any) -> Response:
        parts = urlsplit(url)
        call = Call(
            method=method,
            path=parts.path.removeprefix("/v1"),
            query=dict(parse_qsl(parts.query)),
            body=kwargs.get("json"),
        )
        self.calls.append(call)
        answer: Callable[[Call], tuple[int, Any]] = self.default
        for verb, pattern, handler in self.routes:
            if verb == method and re.fullmatch(pattern, call.path):
                answer = handler
                break
        status, body = answer(call)
        raw = b"" if body is None else json.dumps(body).encode()
        return Response(
            url=url, status=status, headers=(("retry-after", "0"),), body=raw, truncated=False
        )

    def made(self, method: str, path: str) -> list[Call]:
        return [c for c in self.calls if c.method == method and c.path == path]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    made = Fake()
    monkeypatch.setattr(sp, "request", made.request)
    monkeypatch.setattr(sp, "connections", lambda plugin, workspace=None: made.accounts)
    monkeypatch.setattr(
        sp, "grant", lambda account, workspace=None: SimpleNamespace(bearer=lambda: "token-abc")
    )
    return made


class Context:
    def __init__(self, workspace: Path, **settings: Any) -> None:
        self.workspace = workspace
        self.settings = settings


def tools(tmp_path: Path, **settings: Any) -> dict[str, Any]:
    player = sp.Player(Context(tmp_path, **settings))
    return {cls.name: cls(player) for cls in sp.TOOLS}


async def carded(tool: Any, **arguments: Any) -> tuple[Any, Any]:
    subject = await tool.subject(arguments)
    return subject, await tool.run(**arguments)


def handles(text: str) -> list[str]:
    return re.findall(r"\[id: ([tearp]\d+)\]", text)


# -- §4: signing in ------------------------------------------------------------------------


async def test_nothing_is_reached_until_someone_signs_in(fake: Fake, tmp_path: Path) -> None:
    fake.accounts = ()
    for name, tool in tools(tmp_path).items():
        result = await tool.run(
            **(
                {"query": "x"}
                if name == "spotify_search"
                else {"percent": 10}
                if name == "spotify_volume"
                else {"device": "x"}
                if name == "spotify_transfer"
                else {"name": "x"}
                if name == "spotify_playlist_create"
                else {"what": "spotify:track:abc"}
                if name == "spotify_queue"
                else {"playlist": "spotify:playlist:abc", "items": ["spotify:track:abc"]}
                if "playlist_" in name
                else {}
            )
        )
        assert result.is_error, name
    assert fake.calls == []
    result = await tools(tmp_path)["spotify_now_playing"].run()
    assert "atlas auth login spotify:spotify" in result.content


def test_the_login_is_pkce_on_a_loopback_ip_and_asks_for_what_it_needs() -> None:
    client = sp.client("client-123")
    assert client.authorize_url == "https://accounts.spotify.com/authorize"
    assert client.token_url == "https://accounts.spotify.com/api/token"
    assert client.redirect_host == "127.0.0.1" and client.redirect_port is None
    assert client.redirect_uri(54321) == "http://127.0.0.1:54321/callback"
    assert {
        "user-read-playback-state",
        "user-modify-playback-state",
        "playlist-read-private",
        "playlist-modify-private",
        "user-library-modify",
    } <= set(client.scopes)
    assert sp.client("x", sp._port("8888")).redirect_port == 8888
    assert sp._port("0") is None and sp._port("80") is None and sp._port("x") is None


def test_without_a_client_id_signing_in_says_how_to_make_one() -> None:
    from atlas.sdk.runtime import CredentialError

    with pytest.raises(CredentialError) as raised:
        sp.no_client(SimpleNamespace())
    said = str(raised.value)
    assert "developer.spotify.com" in said and "http://127.0.0.1/callback" in said
    assert "User Management" in said and "Premium" in said


def test_every_tool_is_owner_only_untrusted_and_gated_as_decided(tmp_path: Path) -> None:
    made = tools(tmp_path)
    assert len(made) == 14
    for tool in made.values():
        assert tool.trusted_only and tool.untrusted, tool.name
        assert tool.name.startswith("spotify_")
    gated = {n for n, t in made.items() if getattr(t, "gated", False)}
    assert gated == {
        "spotify_play",
        "spotify_pause",
        "spotify_skip",
        "spotify_volume",
        "spotify_queue",
        "spotify_transfer",
        "spotify_playlist_add",
        "spotify_playlist_remove",
        "spotify_playlist_create",
        "spotify_playlist_delete",
    }
    confirmed = {n for n, t in made.items() if getattr(t, "confirm", False)}
    assert confirmed == {"spotify_playlist_create", "spotify_playlist_delete"}


async def test_a_change_with_two_accounts_says_which(fake: Fake, tmp_path: Path) -> None:
    fake.accounts = (MAIN, "spotify:sam")
    result = await tools(tmp_path)["spotify_pause"].run()
    assert result.is_error and "say which with account: spotify, sam" in result.content
    assert not fake.calls


# -- §6.1: reading -------------------------------------------------------------------------


async def test_now_playing_says_what_where_and_what_is_next(fake: Fake, tmp_path: Path) -> None:
    result = await tools(tmp_path)["spotify_now_playing"].run()
    assert not result.is_error, result.content
    first, *rest = result.content.splitlines()
    assert first == (
        'Playing "Kerala" by Bonobo on Sam\'s iPhone · 1:01 of 4:02 · volume 60%  [id: t1]'
    )
    assert rest[0] == "Next:" and "Cirrus" in rest[1] and "[id: t2]" in rest[1]
    fake.state = None
    assert (await tools(tmp_path)["spotify_now_playing"].run()).content == "Nothing is playing."


async def test_devices_are_listed_and_none_says_open_the_app(fake: Fake, tmp_path: Path) -> None:
    result = await tools(tmp_path)["spotify_devices"].run()
    assert result.content.splitlines() == [
        "Sam's iPhone · smartphone, active, volume 60%",
        "Kitchen speaker · speaker, volume 40%",
    ]
    fake.devices = []
    result = await tools(tmp_path)["spotify_devices"].run()
    assert "open the app on the phone" in result.content


async def test_search_gives_short_ids_and_asks_at_most_ten(fake: Fake, tmp_path: Path) -> None:
    made = tools(tmp_path)
    result = await made["spotify_search"].run(query="kerala bonobo", limit=50)
    assert fake.calls[-1].query == {"q": "kerala bonobo", "type": "track", "limit": "10"}
    assert handles(result.content) == ["t1", "t2"]
    assert "Kerala · Bonobo (Migration) · 4:02  [id: t1]" in result.content
    again = await made["spotify_search"].run(query="kerala")
    assert handles(again.content) == ["t1", "t2"]  # the same thing keeps its id
    lists = await made["spotify_search"].run(query="focus", type="playlist")
    assert handles(lists.content) == ["p1"]  # Spotify's null entries are skipped


async def test_playlists_and_what_is_in_one(fake: Fake, tmp_path: Path) -> None:
    made = tools(tmp_path)
    mine = await made["spotify_playlists"].run()
    assert "Focus · by Me · 2 items  [id: p1]" in mine.content
    assert "Road Trip · by Alex" in mine.content
    inside = await made["spotify_playlists"].run(playlist="p1")
    lines = inside.content.splitlines()
    assert lines[0] == "Focus · 2 items:"
    assert "Kerala" in lines[1] and "Black Sands" in lines[2]  # `item`, and the older `track`
    assert fake.made("GET", f"/playlists/{PLAYLIST_ID}/items")


async def test_an_unknown_id_says_search_again(fake: Fake, tmp_path: Path) -> None:
    result = await tools(tmp_path)["spotify_queue"].run(what="t9")
    assert result.is_error and "search again" in result.content


def test_links_and_uris_are_read_and_anything_else_refused() -> None:
    assert sp.parse_target(f"https://open.spotify.com/intl-de/playlist/{PLAYLIST_ID}?si=x") == (
        "playlist",
        f"spotify:playlist:{PLAYLIST_ID}",
    )
    assert sp.parse_target(f"spotify:track:{TRACK_ID}") == ("track", f"spotify:track:{TRACK_ID}")
    for bad in ("https://evil.example/track/abc", "spotify:user:me", "kerala"):
        with pytest.raises(sp.ToolError):
            sp.parse_target(bad)


# -- §6.2: playback -----------------------------------------------------------------------


async def test_play_a_playlist_on_a_named_device(fake: Fake, tmp_path: Path) -> None:
    made = tools(tmp_path)
    await made["spotify_playlists"].run()
    subject, result = await carded(made["spotify_play"], what="p1", device="kitchen")
    assert subject.confirm is False
    assert subject.summary == "Play the playlist Focus on Kitchen speaker"
    assert not result.is_error, result.content
    [call] = fake.made("PUT", "/me/player/play")
    assert call.query == {"device_id": "dev-kitchen"}
    assert call.body == {"context_uri": f"spotify:playlist:{PLAYLIST_ID}"}


async def test_a_track_by_link_plays_alone_and_nothing_named_resumes(
    fake: Fake, tmp_path: Path
) -> None:
    made = tools(tmp_path)
    subject, _ = await carded(
        made["spotify_play"], what=f"https://open.spotify.com/track/{'9' * 22}"
    )
    assert subject.summary == 'Play "Linked song" by Someone on the device playing now'
    assert fake.made("PUT", "/me/player/play")[-1].body == {"uris": [f"spotify:track:{'9' * 22}"]}
    subject, result = await carded(made["spotify_play"])
    assert subject.summary == "Resume playing on the device playing now"
    assert fake.made("PUT", "/me/player/play")[-1].body is None and not result.is_error


async def test_a_call_the_gate_let_through_unasked_still_runs(fake: Fake, tmp_path: Path) -> None:
    """Gated without confirm: in a mode that allows it, `subject` may never be asked."""
    result = await tools(tmp_path)["spotify_skip"].run(direction="previous")
    assert not result.is_error and fake.made("POST", "/me/player/previous")


async def test_pause_volume_queue_and_transfer_go_where_they_should(
    fake: Fake, tmp_path: Path
) -> None:
    made = tools(tmp_path)
    await made["spotify_search"].run(query="kerala")
    _, paused = await carded(made["spotify_pause"])
    assert paused.content == "Paused." and fake.made("PUT", "/me/player/pause")
    subject, _ = await carded(made["spotify_volume"], percent=30, device="Sam's iPhone")
    assert subject.summary == "Set the volume to 30% on Sam's iPhone"
    assert fake.made("PUT", "/me/player/volume")[0].query == {
        "volume_percent": "30",
        "device_id": "dev-phone",
    }
    subject, _ = await carded(made["spotify_queue"], what="t2")
    assert subject.summary == 'Queue "Kerala (Live)" by Bonobo on the device playing now'
    assert fake.made("POST", "/me/player/queue")[0].query["uri"] == f"spotify:track:{'3' * 22}"
    subject, _ = await carded(made["spotify_transfer"], device="kitchen", play=False)
    assert subject.summary == "Move playback to Kitchen speaker (paused)"
    assert fake.made("PUT", "/me/player")[0].body == {"device_ids": ["dev-kitchen"], "play": False}
    bad = await made["spotify_volume"].run(percent=130)
    assert bad.is_error and "0 to 100" in bad.content


async def test_only_a_track_or_episode_can_be_queued(fake: Fake, tmp_path: Path) -> None:
    made = tools(tmp_path)
    await made["spotify_playlists"].run()
    result = await made["spotify_queue"].run(what="p1")
    assert result.is_error and "play it instead" in result.content


async def test_a_device_that_is_not_open_names_the_ones_that_are(
    fake: Fake, tmp_path: Path
) -> None:
    result = await tools(tmp_path)["spotify_pause"].run(device="bedroom")
    assert result.is_error
    assert "open now: Sam's iPhone, Kitchen speaker" in result.content
    assert "open the app" in result.content


async def test_premium_and_no_active_device_are_said_plainly(fake: Fake, tmp_path: Path) -> None:
    fake.on(
        "PUT",
        "/me/player/pause",
        lambda call: (
            403,
            {
                "error": {
                    "status": 403,
                    "message": "Player command failed: Premium required",
                    "reason": "PREMIUM_REQUIRED",
                }
            },
        ),
    )
    result = await tools(tmp_path)["spotify_pause"].run()
    assert result.is_error and "Premium" in result.content and "can read" in result.content
    fake.on(
        "POST",
        "/me/player/next",
        lambda call: (
            404,
            {
                "error": {
                    "status": 404,
                    "message": "Player command failed: No active device found",
                    "reason": "NO_ACTIVE_DEVICE",
                }
            },
        ),
    )
    result = await tools(tmp_path)["spotify_skip"].run()
    assert result.is_error and "open the app on the phone" in result.content


async def test_a_read_waits_once_and_a_control_call_is_never_sent_twice(
    fake: Fake, tmp_path: Path
) -> None:
    answers = iter([(503, {}), (200, {"devices": fake.devices})])
    fake.on("GET", "/me/player/devices", lambda call: next(answers))
    assert not (await tools(tmp_path)["spotify_devices"].run()).is_error
    assert len(fake.made("GET", "/me/player/devices")) == 2
    fake.on("POST", "/me/player/next", lambda call: (503, {}))
    result = await tools(tmp_path)["spotify_skip"].run()
    assert result.is_error and len(fake.made("POST", "/me/player/next")) == 1


async def test_a_spent_quota_is_said_and_not_retried(fake: Fake, tmp_path: Path) -> None:
    fake.on(
        "GET",
        "/me/player/devices",
        lambda call: (
            429,
            {"error": {"status": 429, "message": "Too many requests", "reason": "QUOTA_EXCEEDED"}},
        ),
    )
    result = await tools(tmp_path)["spotify_devices"].run()
    assert result.is_error and "quota is used up" in result.content
    assert len(fake.made("GET", "/me/player/devices")) == 1


async def test_a_refused_sign_in_says_sign_in_again(fake: Fake, tmp_path: Path) -> None:
    fake.on("GET", "/me/player", lambda call: (401, {"error": {"status": 401}}))
    result = await tools(tmp_path)["spotify_now_playing"].run()
    assert "atlas auth login spotify:spotify" in result.content


# -- §6.3: playlists ----------------------------------------------------------------------


async def test_adding_and_removing_name_the_playlist_and_the_tracks(
    fake: Fake, tmp_path: Path
) -> None:
    made = tools(tmp_path)
    await made["spotify_playlists"].run()
    await made["spotify_search"].run(query="kerala")
    subject, result = await carded(made["spotify_playlist_add"], playlist="p1", items=["t1", "t2"])
    assert subject.confirm is False
    assert subject.summary == 'Add to Focus: "Kerala" by Bonobo; "Kerala (Live)" by Bonobo'
    assert result.content == "Added 2 to Focus."
    assert fake.made("POST", f"/playlists/{PLAYLIST_ID}/items")[0].body == {
        "uris": [f"spotify:track:{TRACK_ID}", f"spotify:track:{'3' * 22}"]
    }
    subject, _ = await carded(made["spotify_playlist_remove"], playlist="p2", items=["t1"])
    assert subject.summary == 'Remove from Road Trip (owned by Alex): "Kerala" by Bonobo'
    assert fake.made("DELETE", "/playlists/otherList0000000000000/items")[0].body == {
        "items": [{"uri": f"spotify:track:{TRACK_ID}"}],
        "snapshot_id": "snap-1",
    }
    refused = await made["spotify_playlist_add"].run(playlist="p1", items=["p2"])
    assert refused.is_error and "holds tracks and episodes" in refused.content


async def test_creating_a_playlist_is_a_confirm_card_shown_first(
    fake: Fake, tmp_path: Path
) -> None:
    made = tools(tmp_path)
    await made["spotify_search"].run(query="kerala")
    unshown = await made["spotify_playlist_create"].run(name="Running")
    assert unshown.is_error and "shown to the person first" in unshown.content
    assert not fake.made("POST", "/me/playlists")
    subject, result = await carded(made["spotify_playlist_create"], name="Running", items=["t1"])
    assert subject.confirm is True
    assert subject.summary.splitlines() == [
        "Create the playlist Running for Me",
        "Private: only you see it",
        'With: "Kerala" by Bonobo',
    ]
    assert fake.made("POST", "/me/playlists")[0].body == {"name": "Running", "public": False}
    assert fake.made("POST", "/playlists/newList00000000000000a/items")
    assert result.content.startswith("Created Running with 1 items [id: p")


async def test_deleting_a_playlist_takes_it_out_of_the_library(fake: Fake, tmp_path: Path) -> None:
    made = tools(tmp_path)
    await made["spotify_playlists"].run()
    subject, result = await carded(made["spotify_playlist_delete"], playlist="p1")
    assert subject.confirm is True
    assert subject.summary.splitlines()[0] == "Remove the playlist Focus (2 items)"
    assert "Anyone who follows it keeps their copy" in subject.summary
    [call] = fake.made("DELETE", "/me/library")
    assert call.query == {"uris": f"spotify:playlist:{PLAYLIST_ID}"}
    assert result.content == "Removed Focus from your library."
    subject, _ = await carded(made["spotify_playlist_delete"], playlist="p2")
    assert "It is Alex's: you stop following it" in subject.summary
