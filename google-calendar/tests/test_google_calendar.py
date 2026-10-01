"""The Google Calendar plugin (`docs/spec/google-calendar.md`), driven the way Atlas
drives it with Google replaced: `request`, `grant` and `connections` are the
plugin's module-level names, and a fake answers the Calendar API's shapes. The
reminder service runs against a context whose clock moves when it sleeps.

Run from a checkout of Atlas (`uv run --project ../Atlas pytest google-calendar/tests`).
"""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

import pytest

from atlas.web.client import Response

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "atlas_plugin_google_calendar", HERE.parent / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gc = _load()

ZONE = "Australia/Sydney"
TZ = ZoneInfo(ZONE)
ME = "google-calendar:google"
PRIMARY = {
    "id": "me@example.com",
    "summary": "Personal",
    "timeZone": ZONE,
    "accessRole": "owner",
    "primary": True,
    "defaultReminders": [{"method": "popup", "minutes": 10}],
}


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def event(
    eid: str, start: datetime, minutes: int = 30, title: str = "Dentist", **extra: Any
) -> dict[str, Any]:
    return {
        "id": eid,
        "etag": f'"{eid}-v1"',
        "status": "confirmed",
        "summary": title,
        "start": {"dateTime": start.isoformat(), "timeZone": ZONE},
        "end": {"dateTime": (start + timedelta(minutes=minutes)).isoformat(), "timeZone": ZONE},
        "reminders": {"useDefault": True},
        **extra,
    }


class Call(SimpleNamespace):
    method: str
    path: str
    query: dict[str, str]
    body: Any
    headers: dict[str, str]


Answer = Callable[[Call], tuple[int, Any]]


class Fake:
    """The Calendar API, as far as these tests need it."""

    def __init__(self) -> None:
        self.accounts: tuple[str, ...] = (ME,)
        self.calls: list[Call] = []
        self.events: dict[str, dict[str, Any]] = {}
        self.listed: list[dict[str, Any]] = []
        self.routes: list[tuple[str, str, Answer]] = []

    def on(self, method: str, pattern: str, answer: Answer) -> None:
        self.routes.insert(0, (method, pattern, answer))

    def default(self, call: Call) -> tuple[int, Any]:
        if call.method == "GET" and call.path.startswith("/users/me/calendarList/"):
            return 200, PRIMARY
        if call.method == "GET" and call.path == "/users/me/calendarList":
            return 200, {"items": [PRIMARY]}
        if call.method == "GET" and re.fullmatch(r"/calendars/[^/]+/events", call.path):
            return 200, {"items": self.listed}
        match = re.fullmatch(r"/calendars/[^/]+/events/([^/]+)", call.path)
        if call.method == "GET" and match:
            found = self.events.get(match.group(1))
            return (200, found) if found else (404, {"error": {"message": "Not Found"}})
        if call.method == "POST" and re.fullmatch(r"/calendars/[^/]+/events", call.path):
            return 200, {"id": "new1", **call.body}
        if call.method == "PATCH" and match:
            return 200, {**self.events.get(match.group(1), {}), **call.body}
        if call.method == "DELETE" and match:
            return 204, None
        return 404, {"error": {"message": f"no route for {call.method} {call.path}"}}

    async def request(self, method: str, url: str, **kwargs: Any) -> Response:
        parts = urlsplit(url)
        call = Call(
            method=method,
            path=parts.path.removeprefix("/calendar/v3"),
            query=dict(parse_qsl(parts.query)),
            body=kwargs.get("json"),
            headers=dict(kwargs.get("headers") or {}),
        )
        self.calls.append(call)
        answer: Answer = self.default
        for verb, pattern, handler in self.routes:
            if verb == method and re.fullmatch(pattern, call.path):
                answer = handler
                break
        status, body = answer(call)
        return Response(
            url=url,
            status=status,
            headers=(("retry-after", "0"),),
            body=b"" if body is None else json.dumps(body).encode(),
            truncated=False,
        )

    def writes(self) -> list[Call]:
        return [c for c in self.calls if c.method != "GET" and c.path != "/freeBusy"]


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> Fake:
    fake = Fake()
    monkeypatch.setattr(gc, "request", fake.request)
    monkeypatch.setattr(gc, "connections", lambda plugin, workspace=None: fake.accounts)
    monkeypatch.setattr(
        gc, "grant", lambda name, workspace=None: SimpleNamespace(bearer=lambda: f"tok-{name}")
    )
    return fake


SETTINGS: dict[str, Any] = {"calendars": ["primary"], "max_results": 50}


def tool(cls: Any, tmp_path: Path, **settings: Any) -> Any:
    return cls(gc.Google(tmp_path), {**SETTINGS, **settings})


# -- reading ----------------------------------------------------------------------


async def test_list_events_reads_in_the_calendars_zone(google: Fake, tmp_path: Path) -> None:
    google.listed = [
        event("e1", at(1, 9), location="12 Smith St"),
        event(
            "e2",
            at(2, 14),
            60,
            "Review",
            attendees=[{"email": "x", "self": True, "responseStatus": "declined"}],
        ),
        {
            "id": "e3",
            "summary": "Mum's birthday",
            "start": {"date": "2026-10-03"},
            "end": {"date": "2026-10-04"},
        },
        event("e4_20261005", at(5, 10), title="Standup", recurringEventId="e4"),
    ]
    result = await tool(gc.ListEvents, tmp_path).run(start="2026-10-01", end="2026-10-08")

    assert not result.is_error, result.content
    text = result.content
    assert "Thu 1 Oct 09:00-09:30 · Dentist · 12 Smith St  [id: e1 · Personal]" in text
    assert "Review · (you declined)" in text  # R6.10: declined is listed, not hidden
    assert "Sat 3 Oct (all day) · Mum's birthday" in text
    assert "Standup · (repeats)" in text
    assert text.startswith("4 event(s) Thu 1 Oct 00:00 to Thu 8 Oct 00:00 (Australia/Sydney)")
    listing = next(c for c in google.calls if c.path.endswith("/events"))
    assert listing.query["singleEvents"] == "true" and listing.query["timeZone"] == ZONE
    assert listing.query["timeMin"] == "2026-10-01T00:00:00+10:00"
    assert listing.headers["Authorization"] == f"Bearer tok-{ME}"


async def test_nothing_reaches_google_until_someone_signs_in(google: Fake, tmp_path: Path) -> None:
    google.accounts = ()
    result = await tool(gc.ListEvents, tmp_path).run()
    assert result.is_error and "atlas auth login google-calendar:google" in result.content
    assert google.calls == []


async def test_get_event_shows_attendees_and_repeats(google: Fake, tmp_path: Path) -> None:
    google.events["e1"] = event(
        "e1",
        at(1, 9),
        attendees=[
            {"email": "me@example.com", "self": True, "responseStatus": "accepted"},
            {"email": "sam@example.com", "responseStatus": "needsAction"},
        ],
        recurrence=["RRULE:FREQ=WEEKLY"],
        description="Bring the forms\n\nand the card",
    )
    result = await tool(gc.GetEvent, tmp_path).run(event_id="e1")
    assert "When: Thu 1 Oct 09:00-09:30 (Australia/Sydney)" in result.content
    assert "sam@example.com (needsAction)" in result.content
    assert "me@example.com (accepted) (you)" in result.content
    assert "Repeats: RRULE:FREQ=WEEKLY" in result.content
    assert "Description: Bring the forms and the card" in result.content


async def test_free_busy_lists_the_gaps_inside_the_day(google: Fake, tmp_path: Path) -> None:
    busy = [
        {"start": at(1, 10).isoformat(), "end": at(1, 11).isoformat()},
        {"start": at(1, 13).isoformat(), "end": at(1, 13, 30).isoformat()},
        {"start": at(1, 10, 30).isoformat(), "end": at(1, 11, 15).isoformat()},
    ]
    google.on("POST", "/freeBusy", lambda call: (200, {"calendars": {"primary": {"busy": busy}}}))
    result = await tool(gc.FreeBusy, tmp_path).run(start="2026-10-01", end="2026-10-02")

    text = result.content
    assert "Thu 1 Oct 10:00-11:15" in text and "Thu 1 Oct 13:00-13:30" in text
    free = text.split("Free for at least 30 min")[1]
    assert "09:00-10:00" in free and "11:15-13:00" in free and "13:30-17:00" in free


async def test_a_rate_limit_is_waited_out_once(google: Fake, tmp_path: Path) -> None:
    answers = iter([(429, {"error": {"message": "slow down"}}), (200, {"items": [PRIMARY]})])
    google.on("GET", "/users/me/calendarList", lambda call: next(answers))
    result = await tool(gc.ListCalendars, tmp_path).run()
    assert (
        not result.is_error
        and "Personal · Australia/Sydney · owner, primary, reminders on" in result.content
    )
    assert len([c for c in google.calls if c.path == "/users/me/calendarList"]) == 2


async def test_a_second_account_labels_every_line(google: Fake, tmp_path: Path) -> None:
    google.accounts = (ME, "google-calendar:work")
    google.listed = [event("e1", at(1, 9))]
    result = await tool(gc.ListEvents, tmp_path).run(start="2026-10-01", end="2026-10-02")
    assert "[id: e1 · Personal · google]" in result.content
    assert "[id: e1 · Personal · work]" in result.content


# -- changing: G2, R6.4-R6.8 ----------------------------------------------------------


async def test_create_is_a_confirm_card_and_emails_nobody_unless_asked(
    google: Fake, tmp_path: Path
) -> None:
    create = tool(gc.CreateEvent, tmp_path)
    arguments = {"summary": "Dentist", "start": "2026-10-01T09:00", "end": "2026-10-01T09:30"}

    subject = await create.subject(arguments)
    assert subject.confirm is True and subject.tool == "calendar_create_event"
    assert subject.summary == 'Create "Dentist" · Thu 1 Oct 09:00-09:30 · Personal'
    result = await create.run(**arguments)

    assert not result.is_error, result.content
    [post] = google.writes()
    assert post.query == {"sendUpdates": "none"}
    assert post.body["start"] == {"dateTime": "2026-10-01T09:00:00+10:00", "timeZone": ZONE}
    assert "attendees" not in post.body


async def test_inviting_people_says_who_is_emailed(google: Fake, tmp_path: Path) -> None:
    create = tool(gc.CreateEvent, tmp_path)
    arguments = {
        "summary": "Lunch",
        "start": "2026-10-01T12:00",
        "end": "2026-10-01T13:00",
        "attendees": ["sam@example.com", "alex@example.com"],
        "notify": True,
        "recurrence": ["RRULE:FREQ=WEEKLY"],
    }
    subject = await create.subject(arguments)
    assert subject.summary.endswith(
        "· repeats RRULE:FREQ=WEEKLY · Personal"
        " · emails invitations to alex@example.com, sam@example.com"
    )
    await create.run(**arguments)
    [post] = google.writes()
    assert post.query == {"sendUpdates": "all"}
    assert post.body["attendees"] == [{"email": "sam@example.com"}, {"email": "alex@example.com"}]


async def test_an_all_day_event_takes_dates(google: Fake, tmp_path: Path) -> None:
    create = tool(gc.CreateEvent, tmp_path)
    arguments = {"summary": "Leave", "start": "2026-10-05", "end": "2026-10-08"}
    assert (
        await create.subject(arguments)
    ).summary == 'Create "Leave" · Mon 5 Oct - Wed 7 Oct (all day) · Personal'
    await create.run(**arguments)
    assert google.writes()[0].body["start"] == {"date": "2026-10-05"}
    mixed = await create.run(summary="x", start="2026-10-05", end="2026-10-05T10:00")
    assert mixed.is_error and "both be times, or both be dates" in mixed.content


async def test_a_move_keeps_the_length_and_writes_against_what_was_shown(
    google: Fake, tmp_path: Path
) -> None:
    google.events["e1"] = event("e1", at(1, 9))
    move = tool(gc.UpdateEvent, tmp_path)
    arguments = {"event_id": "e1", "start": "2026-10-02T10:00"}

    subject = await move.subject(arguments)
    assert (
        subject.summary
        == 'Move "Dentist" · Thu 1 Oct 09:00-09:30 → Fri 2 Oct 10:00-10:30 · Personal'
    )
    google.events["e1"] = {**google.events["e1"], "etag": '"e1-v2"'}  # changed after the card
    google.on(
        "PATCH",
        r"/calendars/[^/]+/events/e1",
        lambda call: (412, {"error": {"message": "Precondition Failed"}}),
    )
    result = await move.run(**arguments)

    [patch] = google.writes()
    assert patch.headers["If-Match"] == '"e1-v1"'  # the version the person saw
    assert patch.body["end"]["dateTime"] == "2026-10-02T10:30:00+10:00"
    assert result.is_error and "changed since it was read" in result.content


async def test_a_recurring_event_needs_a_scope(google: Fake, tmp_path: Path) -> None:
    google.events["e4_20261005"] = event(
        "e4_20261005", at(5, 10), title="Standup", recurringEventId="e4"
    )
    google.events["e4"] = event("e4", at(5, 10), title="Standup", recurrence=["RRULE:FREQ=DAILY"])
    delete = tool(gc.DeleteEvent, tmp_path)

    assert (
        await delete.subject({"event_id": "e4_20261005"}) is None
    )  # no card for a call that cannot run
    refused = await delete.run(event_id="e4_20261005")
    assert refused.is_error and "scope: this" in refused.content and google.writes() == []

    subject = await delete.subject({"event_id": "e4_20261005", "scope": "series"})
    assert (
        subject.summary == 'Delete "Standup" · every occurrence from Mon 5 Oct 10:00-10:30'
        " (the whole series) · Personal"
    )
    await delete.run(event_id="e4_20261005", scope="series")
    [gone] = google.writes()
    assert gone.method == "DELETE" and gone.path.endswith("/events/e4")
    assert gone.headers["If-Match"] == '"e4-v1"'  # the series' etag, not the occurrence's

    master = await delete.run(event_id="e4", scope="this")
    assert master.is_error and "that id is the whole series" in master.content


async def test_answering_an_invitation_tells_the_organiser(google: Fake, tmp_path: Path) -> None:
    google.events["e2"] = event(
        "e2",
        at(3, 14),
        title="Quarterly review",
        attendees=[
            {"email": "me@example.com", "self": True, "responseStatus": "needsAction"},
            {"email": "boss@example.com", "organizer": True, "responseStatus": "accepted"},
        ],
    )
    respond = tool(gc.Respond, tmp_path)
    subject = await respond.subject({"event_id": "e2", "response": "declined"})
    assert (
        subject.summary
        == 'Decline "Quarterly review" · Sat 3 Oct 14:00-14:30 · Personal · the organiser is told'
    )
    await respond.run(event_id="e2", response="declined")
    [patch] = google.writes()
    assert patch.query == {"sendUpdates": "all"}
    assert patch.body["attendees"][0] == {
        "email": "me@example.com",
        "self": True,
        "responseStatus": "declined",
    }

    google.events["e3"] = event("e3", at(4, 9))
    alone = await respond.run(event_id="e3", response="accepted")
    assert alone.is_error and "not invited" in alone.content


async def test_a_write_with_two_accounts_and_none_named_is_refused(
    google: Fake, tmp_path: Path
) -> None:
    google.accounts = (ME, "google-calendar:work")
    create = tool(gc.CreateEvent, tmp_path)
    arguments = {"summary": "x", "start": "2026-10-01T09:00", "end": "2026-10-01T10:00"}
    assert await create.subject(arguments) is None
    result = await create.run(**arguments)
    assert result.is_error and "google, work" in result.content and google.writes() == []
    named = await create.run(**arguments, account="work")
    assert (
        not named.is_error
        and google.writes()[0].headers["Authorization"] == "Bearer tok-google-calendar:work"
    )


def test_every_tool_is_owner_only_and_untrusted_and_every_write_is_gated(tmp_path: Path) -> None:
    google = gc.Google(tmp_path)
    reads = (gc.ListCalendars, gc.ListEvents, gc.GetEvent, gc.FreeBusy)
    writes = (gc.CreateEvent, gc.UpdateEvent, gc.DeleteEvent, gc.Respond)
    for cls in reads + writes:
        made = cls(google, SETTINGS)
        assert made.trusted_only and made.untrusted, cls.name
        assert made.gated == (cls in writes), cls.name


# -- signing in: R5.1, R5.2 --------------------------------------------------------------


def test_the_client_asks_for_offline_access_and_reads_the_email() -> None:
    client = gc.client("123.apps.googleusercontent.com")
    assert client.authorize_params == {"access_type": "offline", "prompt": "consent"}
    assert "https://www.googleapis.com/auth/calendar.events" in client.scopes
    claims = (
        base64.urlsafe_b64encode(json.dumps({"email": "me@example.com"}).encode())
        .decode()
        .rstrip("=")
    )
    assert gc._label({"id_token": f"h.{claims}.s"}) == {
        "email": "me@example.com",
        "label": "me@example.com",
    }
    assert gc._label({"id_token": "garbage"}) == {}


def test_signing_in_with_no_client_id_says_what_to_do(tmp_path: Path) -> None:
    from atlas.errors import CredentialError

    with pytest.raises(CredentialError, match=r"plugins_settings\.google-calendar\.client_id"):
        gc.no_client(None)


# -- reminders: R7 -----------------------------------------------------------------------


def test_which_leads_an_event_is_reminded_at() -> None:
    defaults = [{"method": "popup", "minutes": 10}, {"method": "email", "minutes": 60}]
    on = {"use_event_reminders": True, "lead_minutes": [15]}
    assert gc.leads(event("a", at(1, 9)), defaults, on) == [10]
    popup = {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]}
    assert gc.leads(event("a", at(1, 9), reminders=popup), defaults, on) == [30]
    email = {"useDefault": False, "overrides": [{"method": "email", "minutes": 30}]}
    assert gc.leads(event("a", at(1, 9), reminders=email), defaults, on) == []  # they chose email
    assert gc.leads(event("a", at(1, 9)), [], on) == [15]  # nothing set anywhere
    declined = event("a", at(1, 9), attendees=[{"self": True, "responseStatus": "declined"}])
    assert gc.leads(declined, defaults, on) == []
    assert gc.leads({**event("a", at(1, 9)), "status": "cancelled"}, defaults, on) == []
    assert gc.leads(event("a", at(1, 9)), defaults, {**on, "use_event_reminders": False}) == [15]


class Context:
    """A `ServiceContext` whose clock jumps to wherever the service sleeps until.

    It stops after `sleeps` sleeps, or once the clock reaches `until`. A time in
    `wakes` is a tool waking the service: a sleep past it ends there instead,
    and `woken()` says so once - what `PluginContext.wake_service` does."""

    def __init__(
        self,
        tmp_path: Path,
        now: datetime,
        *,
        sleeps: int = 6,
        until: datetime | None = None,
        wakes: tuple[datetime, ...] = (),
        **settings: Any,
    ) -> None:
        self.plugin = "google-calendar"
        self.workspace = tmp_path
        self.settings: Mapping[str, Any] = {
            "calendars": ["primary"],
            "use_event_reminders": True,
            "lead_minutes": [10],
            "poll_minutes": 5,
            "idle_poll_minutes": 30,
            "horizon_hours": 24,
            "late_grace_minutes": 5,
            "all_day_at": "",
            "reminders": True,
            **settings,
        }
        self.clock = now.timestamp()
        self.left = sleeps if until is None else 10_000
        self.until = until.timestamp() if until is not None else None
        self.wakes = sorted(w.timestamp() for w in wakes)
        self.was_woken = False
        self.on_wake: Callable[[], None] | None = None
        self.sent: list[tuple[float, str]] = []
        self.state_dir = tmp_path / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.accounts: tuple[str, ...] = (ME,)

    @property
    def stopping(self) -> bool:
        return self.left < 0 or (self.until is not None and self.clock >= self.until)

    def setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock, UTC)

    def connections(self) -> tuple[str, ...]:
        return self.accounts

    async def sleep_until(self, when: datetime | float) -> bool:
        target = when.timestamp() if isinstance(when, datetime) else float(when)
        return await self.sleep_for(target - self.clock)

    async def sleep_for(self, seconds: float) -> bool:
        self.left -= 1
        target = self.clock + max(seconds, 0)
        if self.wakes and self.wakes[0] <= target:
            self.clock = max(self.clock, self.wakes.pop(0))
            self.was_woken = True
            if self.on_wake is not None:
                self.on_wake()
        else:
            self.clock = target
        if self.until is not None:
            self.clock = min(self.clock, self.until)
        return self.stopping

    def woken(self) -> bool:
        was, self.was_woken = self.was_woken, False
        return was

    async def notify(self, text: str) -> str:
        self.sent.append((self.clock, text))
        return "telegram:dm:1"


async def test_a_reminder_is_sent_at_its_lead_and_only_once(google: Fake, tmp_path: Path) -> None:
    google.listed = [
        event("e1", at(1, 9), location="12 Smith St"),
        event("gone", at(1, 8, 30)),  # already started: never reminded
    ]
    ctx = Context(tmp_path, at(1, 8, 44))
    await gc.Reminders().run(ctx)  # type: ignore[arg-type]

    assert [text for _, text in ctx.sent] == ["⏰ Dentist at 9:00 (in 10 min) · 12 Smith St"]
    assert ctx.sent[0][0] == at(1, 8, 50).timestamp()
    saved = json.loads((ctx.state_dir / "reminded.json").read_text(encoding="utf-8"))
    assert next(iter(saved.values()))["sent"] == "telegram:dm:1"

    again = Context(tmp_path, at(1, 8, 49))  # a restart, before and past the due time
    await gc.Reminders().run(again)  # type: ignore[arg-type]
    assert again.sent == []


async def test_a_moved_event_is_reminded_again(google: Fake, tmp_path: Path) -> None:
    google.listed = [event("e1", at(1, 9))]
    first = Context(tmp_path, at(1, 8, 45))
    await gc.Reminders().run(first)  # type: ignore[arg-type]
    google.listed = [event("e1", at(1, 11))]
    later = Context(tmp_path, at(1, 10, 45))
    await gc.Reminders().run(later)  # type: ignore[arg-type]
    assert [text for _, text in later.sent] == ["⏰ Dentist at 11:00 (in 10 min)"]


async def test_a_reminder_missed_while_asleep_goes_late_within_the_grace(
    google: Fake, tmp_path: Path
) -> None:
    google.listed = [event("e1", at(1, 9)), event("e2", at(1, 9, 20), title="Call")]
    ctx = Context(tmp_path, at(1, 8, 53), sleeps=1)  # e1 was due 8:50: 3 min late; e2 not yet
    await gc.Reminders().run(ctx)  # type: ignore[arg-type]
    assert [text for _, text in ctx.sent] == ["⏰ Dentist at 9:00 (in 7 min)"]

    stale = Context(tmp_path / "b", at(1, 8, 57), sleeps=1)  # 7 min late: past the grace
    await gc.Reminders().run(stale)  # type: ignore[arg-type]
    assert stale.sent == []


async def test_all_day_events_are_reminded_at_the_hour_asked(google: Fake, tmp_path: Path) -> None:
    google.listed = [
        {
            "id": "b",
            "summary": "Mum's birthday",
            "start": {"date": "2026-10-01"},
            "end": {"date": "2026-10-02"},
        }
    ]
    ctx = Context(tmp_path, at(1, 7, 55), all_day_at="08:00")
    await gc.Reminders().run(ctx)  # type: ignore[arg-type]
    assert ctx.sent == [(at(1, 8).timestamp(), "📅 Today: Mum's birthday")]

    none = Context(tmp_path / "c", at(1, 7, 55))
    await gc.Reminders().run(none)  # type: ignore[arg-type]
    assert none.sent == []  # all_day_at empty: none


async def test_an_expired_sign_in_is_said_once(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atlas.errors import CredentialError

    def expired(name: str, workspace: Any = None) -> Any:
        def bearer() -> str:
            raise CredentialError(
                "the sign-in has expired: atlas auth login google-calendar:google"
            )

        return SimpleNamespace(bearer=bearer)

    monkeypatch.setattr(gc, "grant", expired)
    ctx = Context(tmp_path, at(1, 8), sleeps=4)
    await gc.Reminders().run(ctx)  # type: ignore[arg-type]
    assert [text for _, text in ctx.sent] == [
        "Google Calendar (google): the sign-in has expired: atlas auth login google-calendar:google"
    ]


async def test_reminders_off_sends_nothing_and_asks_google_nothing(
    google: Fake, tmp_path: Path
) -> None:
    google.listed = [event("e1", at(1, 9))]
    ctx = Context(tmp_path, at(1, 8, 45), reminders=False)
    await gc.Reminders().run(ctx)  # type: ignore[arg-type]
    assert ctx.sent == [] and google.calls == []


# -- the plugin, installed ----------------------------------------------------------------


def test_the_plugin_installs_whole_with_no_one_signed_in(tmp_path: Path) -> None:
    from atlas.auth.oauth import known_logins, unregister_login
    from atlas.plugins.install import install_one
    from atlas.plugins.services import unregister_service
    from atlas.tools.registry import ToolRegistry

    registry = ToolRegistry()
    provision = install_one(gc.GoogleCalendarPlugin(), workspace=tmp_path, tools=registry)
    try:
        assert provision.ok, provision.error
        assert provision.logins == ("google-calendar:google",)
        assert provision.services == ("google-calendar:reminders",)
        assert len(provision.tools) == 8
        login = known_logins()["google-calendar:google"]
        assert login.run is gc.no_client  # no client id yet: signing in says what is missing
    finally:
        unregister_login("google-calendar:google")
        unregister_service("google-calendar:reminders")

    provision = install_one(
        gc.GoogleCalendarPlugin(),
        workspace=tmp_path,
        tools=ToolRegistry(),
        settings={"client_id": "cid"},
    )
    try:
        client = known_logins()["google-calendar:google"].client
        assert client is not None and client.client_id == "cid"
    finally:
        unregister_login("google-calendar:google")
        unregister_service("google-calendar:reminders")


# -- R7.1, R7.7: what a day costs --------------------------------------------------------

DAILY_BUDGET = 100
"""Requests one watched calendar may cost the project in a day with a few events.
Every install signed in through Atlas's client shares Google's 1,000,000 a day,
which cannot be raised: at this, that is ten thousand always-on installs."""


async def test_an_idle_day_polls_every_half_hour(google: Fake, tmp_path: Path) -> None:
    ctx = Context(tmp_path, at(1, 0), until=at(2, 0))
    await gc.Reminders().run(ctx)  # type: ignore[arg-type]
    listings = [c for c in google.calls if c.path.endswith("/events")]
    assert 47 <= len(listings) <= 49
    assert len(google.calls) <= 55  # the calendar's entry, cached for hours, is the rest


async def test_a_day_with_events_stays_inside_the_budget(google: Fake, tmp_path: Path) -> None:
    google.listed = [
        event("a", at(1, 9), title="Standup"),
        event("b", at(1, 13), title="Lunch"),
        event("c", at(1, 17), title="Gym"),
    ]
    ctx = Context(tmp_path, at(1, 0), until=at(2, 0))
    await gc.Reminders().run(ctx)  # type: ignore[arg-type]

    assert [text for _, text in ctx.sent] == [
        "⏰ Standup at 9:00 (in 10 min)",
        "⏰ Lunch at 13:00 (in 10 min)",
        "⏰ Gym at 17:00 (in 10 min)",
    ]
    assert len(google.calls) <= DAILY_BUDGET
    # Often only in the hour before a reminder: none between 10:00 and 11:50.
    quiet = [
        c
        for c in google.calls
        if c.path.endswith("/events")
        and at(1, 10).isoformat() < c.query["timeMin"] < at(1, 11, 50).isoformat()
    ]
    assert len(quiet) <= 5


async def test_a_change_made_through_atlas_is_seen_at_once(google: Fake, tmp_path: Path) -> None:
    """Idle, the next look is 8:30; a call booked at 8:02 for 8:20 is due at 8:10.
    The tool that booked it wakes the service, and the reminder is not missed."""
    ctx = Context(tmp_path, at(1, 8), until=at(1, 9), wakes=(at(1, 8, 2),))
    ctx.on_wake = lambda: google.listed.append(event("new", at(1, 8, 20), title="Call"))
    await gc.Reminders().run(ctx)  # type: ignore[arg-type]
    assert ctx.sent == [(at(1, 8, 10).timestamp(), "⏰ Call at 8:20 (in 10 min)")]


async def test_a_change_wakes_the_reminder_service_and_a_failure_does_not(
    google: Fake, tmp_path: Path
) -> None:
    woken: list[int] = []
    create = gc.CreateEvent(gc.Google(tmp_path), SETTINGS, lambda: woken.append(1))
    done = await create.run(summary="Call", start="2026-10-01T08:20", end="2026-10-01T08:40")
    assert not done.is_error and woken == [1]
    failed = await create.run(summary="Call", start="soon", end="later")
    assert failed.is_error and woken == [1]


# -- R5.6-R5.9: the secret iCal address ----------------------------------------------------

SECRET = "https://calendar.google.com/calendar/ical/me%40example.com/private-5ecret/basic.ics"

FEED = "\r\n".join(
    [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "X-WR-CALNAME:Family",
        "X-WR-TIMEZONE:Australia/Sydney",
        "BEGIN:VEVENT",
        "UID:dentist@google.com",
        "DTSTART;TZID=Australia/Sydney:20261001T090000",
        "DTEND;TZID=Australia/Sydney:20261001T093000",
        "SUMMARY:Dentist",
        "LOCATION:12 Smith St\\, Level 2",
        "DESCRIPTION:Bring the forms and the card and anything else they asked for in th",
        " e letter",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "TRIGGER:-PT30M",
        "END:VALARM",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:birthday@google.com",
        "DTSTART;VALUE=DATE:20261003",
        "DTEND;VALUE=DATE:20261004",
        "SUMMARY:Mum's birthday",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:standup@google.com",
        "DTSTART;TZID=Australia/Sydney:20260928T100000",
        "DURATION:PT15M",
        "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20261031T000000Z",
        "EXDATE;TZID=Australia/Sydney:20261002T100000",
        "SUMMARY:Standup",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:standup@google.com",
        "RECURRENCE-ID;TZID=Australia/Sydney:20261001T100000",
        "DTSTART;TZID=Australia/Sydney:20261001T110000",
        "DTEND;TZID=Australia/Sydney:20261001T111500",
        "SUMMARY:Standup (late)",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:standup@google.com",
        "RECURRENCE-ID;TZID=Australia/Sydney:20260930T100000",
        "DTSTART;TZID=Australia/Sydney:20260930T100000",
        "STATUS:CANCELLED",
        "SUMMARY:Standup",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:lunch@google.com",
        "DTSTART:20261001T030000Z",
        "DTEND:20261001T040000Z",
        "SUMMARY:Lunch",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
)


@pytest.fixture
def feed(google: Fake, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A calendar read only through its secret address: no account signed in.
    Returns the addresses fetched, in order."""
    google.accounts = ()
    fetched: list[str] = []

    async def fake_get(url: str, **kwargs: Any) -> Response:
        fetched.append(url)
        return Response(url=url, status=200, headers=(), body=FEED.encode(), truncated=False)

    monkeypatch.setattr(gc, "get", fake_get)
    monkeypatch.setenv("GOOGLE_CALENDAR_ICAL_URL", SECRET)
    return fetched


async def test_a_feed_is_read_without_signing_in(
    google: Fake, feed: list[str], tmp_path: Path
) -> None:
    result = await tool(gc.ListEvents, tmp_path).run(start="2026-09-30", end="2026-10-04")

    assert not result.is_error, result.content
    text = result.content
    assert (
        "Thu 1 Oct 09:00-09:30 · Dentist · 12 Smith St, Level 2"
        "  [id: ical1/dentist@google.com · Family]"
    ) in text
    assert "Thu 1 Oct 11:00-11:15 · Standup (late) · (repeats)" in text  # moved
    assert "Thu 1 Oct 13:00-14:00 · Lunch" in text  # a UTC time, in the calendar's zone
    assert "Sat 3 Oct (all day) · Mum's birthday" in text
    assert "Fri 2 Oct" not in text  # EXDATE
    assert "Wed 30 Sep 10:00" not in text  # that occurrence was cancelled
    assert text.count("Standup") == 1
    assert feed == [SECRET] and google.calls == []  # one download, no Calendar API at all
    assert "private-5ecret" not in text


async def test_the_secret_address_never_reaches_the_model(
    google: Fake, feed: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def gone(url: str, **kwargs: Any) -> Response:
        return Response(url=url, status=404, headers=(), body=b"Not Found", truncated=False)

    monkeypatch.setattr(gc, "get", gone)
    result = await tool(gc.ListEvents, tmp_path).run()
    assert result.is_error and "ical1 answered HTTP 404" in result.content
    assert "private-5ecret" not in result.content and "calendar.google.com" not in result.content

    from atlas.sdk.web import WebError

    async def unreachable(url: str, **kwargs: Any) -> Response:
        raise WebError(f"could not connect to {url}")

    monkeypatch.setattr(gc, "get", unreachable)
    result = await tool(gc.ListEvents, tmp_path).run()
    assert result.is_error and "could not reach ical1" in result.content
    assert "private-5ecret" not in result.content


async def test_a_feed_is_read_only(google: Fake, feed: list[str], tmp_path: Path) -> None:
    create = await tool(gc.CreateEvent, tmp_path).run(
        summary="x", start="2026-10-01T09:00", end="2026-10-01T10:00"
    )
    assert create.is_error and "read-only" in create.content
    assert "atlas auth login" in create.content

    google.accounts = (ME,)  # signed in as well: the feed's own events still cannot change
    move = await tool(gc.UpdateEvent, tmp_path).run(
        event_id="ical1/dentist@google.com", start="2026-10-02T09:00"
    )
    assert move.is_error and "read-only" in move.content and google.writes() == []


async def test_an_occurrence_from_a_feed_can_be_read_in_full(
    google: Fake, feed: list[str], tmp_path: Path
) -> None:
    listed = await tool(gc.ListEvents, tmp_path).run(start="2026-10-05", end="2026-10-06")
    occurrence = re.search(r"id: (ical1/standup@google\.com/\S+) ·", listed.content)
    assert occurrence is not None, listed.content
    result = await tool(gc.GetEvent, tmp_path).run(event_id=occurrence.group(1))
    assert "When: Mon 5 Oct 10:00-10:15 (Australia/Sydney)" in result.content
    assert "Read-only" in result.content

    dentist = await tool(gc.GetEvent, tmp_path).run(event_id="ical1/dentist@google.com")
    assert (
        "Description: Bring the forms and the card and anything else they asked for in the letter"
        in dentist.content
    )


async def test_a_feed_is_listed_and_counted_for_free_busy(
    google: Fake, feed: list[str], tmp_path: Path
) -> None:
    calendars = await tool(gc.ListCalendars, tmp_path).run()
    assert (
        "Family · Australia/Sydney · read-only (its secret iCal address), reminders on  [id: ical1]"
        in calendars.content
    )
    free = await tool(gc.FreeBusy, tmp_path).run(start="2026-10-01", end="2026-10-02")
    assert "Thu 1 Oct 09:00-09:30" in free.content and "Thu 1 Oct 13:00-14:00" in free.content
    gaps = free.content.split("Free for at least")[1]
    assert "09:30-11:00" in gaps and "11:15-13:00" in gaps and "14:00-17:00" in gaps


async def test_a_feed_reminds_by_its_own_alarms(
    google: Fake, feed: list[str], tmp_path: Path
) -> None:
    ctx = Context(tmp_path, at(1, 8), until=at(1, 11, 30))
    ctx.accounts = ()
    await gc.Reminders().run(ctx)  # type: ignore[arg-type]
    assert ctx.sent == [
        (at(1, 8, 30).timestamp(), "⏰ Dentist at 9:00 (in 30 min) · 12 Smith St, Level 2"),
        (at(1, 10, 50).timestamp(), "⏰ Standup (late) at 11:00 (in 10 min)"),
    ]


def test_ical_lines_unfold_and_unescape() -> None:
    assert gc.unfold("A:one\r\n two\r\nB:x\n\tthree") == ["A:onetwo", "B:xthree"]
    assert gc.unescape("a\\, b\\; c\\nd\\\\e") == "a, b; c\nd\\e"
    assert gc.split_line('ATTENDEE;CN="Smith: J":mailto:j@x') == (
        "ATTENDEE",
        {"CN": "Smith: J"},
        "mailto:j@x",
    )
    assert gc._until("FREQ=DAILY;UNTIL=20261031", True) == "FREQ=DAILY;UNTIL=20261031T235959Z"
    assert gc._until("FREQ=DAILY;UNTIL=20261031T000000Z", False) == "FREQ=DAILY;UNTIL=20261031"
