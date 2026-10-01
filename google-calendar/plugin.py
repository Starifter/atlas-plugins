"""Google Calendar: a login, eight tools, and reminders (`docs/spec/google-calendar.md`).

The calendar is the person's, so every tool is `trusted_only` - offered only in
a session an owner holds - and every result is `untrusted`, because an event's
title and description are whatever the invite's sender wrote. Reading is free;
every change is a card a person answers in every mode (`Subject.confirm`).

Reminders are a service (§8): no model runs to send one. It polls every few
minutes, sleeps until something is due, and sends one line to the owner.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from dateutil.rrule import rruleset, rrulestr

from atlas.sdk.auth import SecretRef, read_dotenv, resolve
from atlas.sdk.oauth import LoginContext, OAuthClient, Tokens, connections, grant
from atlas.sdk.plugin_entry import Plugin, PluginContext
from atlas.sdk.runtime import CredentialError, ToolError, assert_active
from atlas.sdk.service import NotifyError, Service, ServiceContext
from atlas.sdk.tool_plugin import Subject, Tool, ToolResult
from atlas.sdk.web import WebError, get, request

PLUGIN = "google-calendar"
LOGIN = "google"
LOGIN_NAME = f"{PLUGIN}:{LOGIN}"
"""What a person types: `atlas auth login google-calendar:google`."""

DEFAULT_CLIENT_ID = ""
"""Atlas's own Google OAuth client (a Desktop-app client, `google-calendar.md` R4.1).
Empty until the Google Cloud project exists; `client_id` in settings is used
instead, and with neither, signing in says what is missing."""

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "openid",
    "email",
)
"""R4.2: sensitive, not restricted, plus the address to label a connection with."""

USER_AGENT = "atlas-google-calendar"
RESPONSE_BYTES = 5_000_000
RETRY_CAP = 10.0
"""R11: a tool waits out a 429 or a 5xx once, for at most this long."""

NEAR = 3600.0
"""R7.1: a reminder due within this long makes the service poll at `poll_minutes`;
with none, it polls at `idle_poll_minutes`. What keeps one install's requests a
small share of the project's daily cap, which every install signed in through
Atlas's client shares and which Google does not raise."""

SERVICE_CACHE = 6 * 3600.0
"""How long the service keeps a calendar's list entry - its zone and default
reminders, which change rarely and are not worth a request every poll."""

TITLE_MAX = 120
DESCRIPTION_MAX = 2000
DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
CLOCK_TIME = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
ICAL_ENV = "GOOGLE_CALENDAR_ICAL_URL"
ICAL_CACHE = 60.0
"""How long a tool uses a downloaded feed, so three calls in one answer fetch it once."""
NOT_SIGNED_IN = (
    f"not signed in to Google Calendar: atlas auth login {LOGIN_NAME} - or, to read and be "
    f"reminded without signing in, store the calendar's secret iCal address as {ICAL_ENV}"
)
READ_ONLY = (
    "the calendar is read through its secret iCal address, which is read-only - to change "
    f"events, sign in: atlas auth login {LOGIN_NAME}"
)


# -- talking to Google ---------------------------------------------------------


class GoogleError(Exception):
    """A status Google answered with, as a sentence the model can repeat."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _error_text(response: Any) -> str:
    try:
        data = json.loads(response.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("status") or "")
    return str(error or "")


def _retry_after(response: Any) -> float:
    try:
        return min(max(float(response.header("retry-after") or 1.0), 0.0), RETRY_CAP)
    except ValueError:
        return 1.0


def label_of(connection_id: str) -> str:
    """`google-calendar:work` -> `work`."""
    return connection_id.partition(":")[2] or connection_id


class Google:
    """One plugin's way to Google: which connection, which verb, what came back."""

    def __init__(
        self,
        workspace: Path,
        *,
        cache_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
        ical_env: str = ICAL_ENV,
        feed_age: float = ICAL_CACHE,
    ) -> None:
        self.workspace = workspace
        self.ical_env = ical_env or ICAL_ENV
        self.feed_age = feed_age
        """How long a downloaded feed is used before it is fetched again: a
        minute for the tools, none for the service, which fetches once a look."""
        self._feeds: dict[str, Feed] = {}
        self.cache_seconds = cache_seconds
        self.clock = clock
        """What the cache is timed by: the service passes its own clock, so a
        test that moves the service's time moves the cache's too."""
        self._zones: dict[tuple[str, str], tuple[float, Mapping[str, Any]]] = {}
        self._lists: dict[str, tuple[float, list[Mapping[str, Any]]]] = {}

    # -- accounts -------------------------------------------------------------

    def accounts(self) -> tuple[str, ...]:
        return connections(PLUGIN, workspace=self.workspace)

    def feeds(self) -> list[Feed]:
        """R5.6: the secret iCal addresses in `ical_url_env`, from the environment or
        `.env`, read fresh so one stored mid-session is used on the next call."""
        try:
            value = resolve(SecretRef("env", self.ical_env), dotenv=read_dotenv(self.workspace))
        except CredentialError:
            return []
        urls = [u for u in re.split(r"[\s,]+", value or "") if u]
        found: list[Feed] = []
        for index, url in enumerate(urls, start=1):
            url = "https://" + url[len("webcal://") :] if url.startswith("webcal://") else url
            if not url.startswith("https://"):
                continue
            feed = self._feeds.get(url)
            if feed is None or feed.index != index:
                feed = self._feeds[url] = Feed(index, url)
            found.append(feed)
        return found

    def feed(self, feed_id: str) -> Feed:
        for feed in self.feeds():
            if feed.id == feed_id:
                return feed
        raise ToolError(f"no iCal calendar {feed_id!r} - calendar_list_calendars shows them")

    def reading(self, account: str) -> tuple[list[str], list[Feed]]:
        """What a read covers: the signed-in accounts and the iCal feeds, or the one
        named. Neither is `NOT_SIGNED_IN`."""
        if account.startswith("ical"):
            return [], [self.feed(account)]
        if account:
            return self.pick(account, write=False), []
        known, feeds = self.accounts(), self.feeds()
        if not known and not feeds:
            raise CredentialError(NOT_SIGNED_IN)
        return list(known), feeds

    def pick(self, account: str, *, write: bool) -> list[str]:
        """R5.3: the only account, the named one, or - for a read - all of them.
        A write with more than one account and none named is refused."""
        known = self.accounts()
        if not known:
            raise CredentialError(READ_ONLY if write and self.feeds() else NOT_SIGNED_IN)
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
        # `bearer` refreshes over the network when the token is due, and the
        # engine under it is synchronous (`oauth.md` §5.4), so it runs off the loop.
        return await asyncio.to_thread(lambda: grant(account, workspace=self.workspace).bearer())

    # -- one request ----------------------------------------------------------

    async def call(
        self,
        account: str,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = True,
    ) -> Any:
        """One API call. Returns the decoded JSON (or `{}` for an empty body);
        raises `GoogleError` for a status that is not 2xx."""
        query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        url = f"{API}{path}" + (f"?{urlencode(query, doseq=True)}" if query else "")
        for attempt in range(2):
            token = await self.bearer(account)
            response = await request(
                method,
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}", **(headers or {})},
                max_bytes=RESPONSE_BYTES,
                user_agent=USER_AGENT,
            )
            if 200 <= response.status < 300:
                if not response.body:
                    return {}
                try:
                    return json.loads(response.body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise GoogleError(
                        response.status, f"Google sent something unreadable: {exc}"
                    ) from None
            transient = response.status == 429 or response.status >= 500
            if transient and retry and attempt == 0:
                await asyncio.sleep(_retry_after(response))
                continue
            raise GoogleError(response.status, _explain(response.status, _error_text(response)))
        raise AssertionError("unreachable")  # pragma: no cover

    # -- what the tools and the service share ---------------------------------

    async def calendars(self, account: str) -> list[Mapping[str, Any]]:
        cached = self._lists.get(account)
        if cached is not None and self.clock() - cached[0] < self.cache_seconds:
            return cached[1]
        data = await self.call(account, "GET", "/users/me/calendarList", params={"maxResults": 250})
        items = data.get("items", []) if isinstance(data, dict) else []
        found: list[Mapping[str, Any]] = [item for item in items if isinstance(item, dict)]
        self._lists[account] = (self.clock(), found)
        return found

    async def calendar(self, account: str, calendar: str) -> Mapping[str, Any]:
        """One calendar-list entry - its name, zone and default reminders -
        cached, because every listing needs the zone."""
        key = (account, calendar)
        cached = self._zones.get(key)
        if cached is not None and self.clock() - cached[0] < self.cache_seconds:
            return cached[1]
        entry = await self.call(
            account, "GET", f"/users/me/calendarList/{quote(calendar, safe='')}"
        )
        if not isinstance(entry, dict):
            entry = {}
        self._zones[key] = (self.clock(), entry)
        return entry

    async def zone(self, account: str, calendar: str) -> tuple[str, tzinfo]:
        """The calendar's own zone - R6.3's "local" - or this machine's."""
        name = str((await self.calendar(account, calendar)).get("timeZone") or "")
        if name:
            try:
                return name, ZoneInfo(name)
            except (ValueError, KeyError, OSError):  # a zone the tz database lacks
                pass
        return local_zone()

    async def events(
        self,
        account: str,
        calendar: str,
        start: datetime,
        end: datetime,
        *,
        zone_name: str,
        query: str = "",
        limit: int = 250,
        retry: bool = True,
    ) -> tuple[list[Mapping[str, Any]], bool]:
        """Occurrences between `start` and `end`, recurring events expanded,
        in start order. The flag says whether `limit` cut the list."""
        found: list[Mapping[str, Any]] = []
        token = ""
        while True:
            data = await self.call(
                account,
                "GET",
                f"/calendars/{quote(calendar, safe='')}/events",
                params={
                    "timeMin": start.isoformat(),
                    "timeMax": end.isoformat(),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": min(250, limit - len(found) + 1),
                    "timeZone": zone_name,
                    "q": query,
                    "pageToken": token,
                },
                retry=retry,
            )
            items = data.get("items", []) if isinstance(data, dict) else []
            found.extend(item for item in items if isinstance(item, dict))
            token = str(data.get("nextPageToken") or "") if isinstance(data, dict) else ""
            if len(found) > limit:
                return found[:limit], True
            if not token:
                return found, False

    async def event(self, account: str, calendar: str, event_id: str) -> Mapping[str, Any]:
        data = await self.call(
            account,
            "GET",
            f"/calendars/{quote(calendar, safe='')}/events/{quote(event_id, safe='')}",
        )
        return data if isinstance(data, dict) else {}


def _explain(status: int, text: str) -> str:
    if status == 401:
        return f"Google refused the sign-in (401): sign in again with atlas auth login {LOGIN_NAME}"
    if status == 403 and "insufficient" in text.lower():
        return (
            f"a permission was not granted (403: {text}) - sign in again with "
            f"atlas auth login {LOGIN_NAME} and tick every box"
        )
    if status == 404:
        return f"not found (404){f': {text}' if text else ''} - check the event or calendar id"
    if status == 412:
        return "the event changed since it was read - look again before changing it"
    return f"HTTP {status} from Google Calendar{f': {text}' if text else ''}"


# -- time ----------------------------------------------------------------------


def local_zone() -> tuple[str, tzinfo]:
    here = datetime.now().astimezone()
    return here.tzname() or "local", here.tzinfo or UTC


def parse_moment(value: str, zone: tzinfo) -> datetime | date:
    """R6.3: RFC 3339; no offset means the calendar's zone; a bare date is all day."""
    text = str(value or "").strip()
    if DATE_ONLY.fullmatch(text):
        return date.fromisoformat(text)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(
            f"{value!r} is not a time - write 2026-10-01T09:00 (the calendar's own zone), "
            "2026-10-01T09:00+10:00, or 2026-10-01 for a whole day"
        ) from None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=zone)


def as_instant(value: datetime | date, zone: tzinfo) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day, tzinfo=zone)


def api_time(value: datetime | date, zone_name: str) -> dict[str, str]:
    if isinstance(value, datetime):
        return {"dateTime": value.isoformat(), "timeZone": zone_name}
    return {"date": value.isoformat()}


def moment_of(part: Any, zone: tzinfo) -> tuple[datetime, bool]:
    """An event's `start` or `end`, as an instant in `zone`, and whether it is a date."""
    part = part if isinstance(part, dict) else {}
    if part.get("dateTime"):
        moment = datetime.fromisoformat(str(part["dateTime"]).replace("Z", "+00:00"))
        return moment.astimezone(zone), False
    if part.get("date"):
        day = date.fromisoformat(str(part["date"]))
        return datetime(day.year, day.month, day.day, tzinfo=zone), True
    return datetime.now(zone), False


def day_name(moment: datetime) -> str:
    """`Tue 1 Oct` - R6.4. Built by hand because `%-d` is not portable."""
    return f"{moment.strftime('%a')} {moment.day} {moment.strftime('%b')}"


def hm(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def span(event: Mapping[str, Any], zone: tzinfo) -> str:
    start, all_day = moment_of(event.get("start"), zone)
    end, _ = moment_of(event.get("end"), zone)
    if all_day:
        last = end - timedelta(days=1)
        if last.date() <= start.date():
            return f"{day_name(start)} (all day)"
        return f"{day_name(start)} - {day_name(last)} (all day)"
    if end.date() == start.date():
        return f"{day_name(start)} {hm(start)}-{hm(end)}"
    return f"{day_name(start)} {hm(start)} - {day_name(end)} {hm(end)}"


def clean(text: Any, limit: int = TITLE_MAX) -> str:
    """One line of someone else's words, capped."""
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def title_of(event: Mapping[str, Any]) -> str:
    return clean(event.get("summary")) or "(no title)"


def my_response(event: Mapping[str, Any]) -> str:
    for attendee in event.get("attendees") or ():
        if isinstance(attendee, dict) and attendee.get("self"):
            return str(attendee.get("responseStatus") or "")
    return ""


def where_of(event: Mapping[str, Any]) -> str:
    return clean(event.get("location")) or str(event.get("hangoutLink") or "")


# -- the secret iCal address -----------------------------------------------------
#
# R5.6-R5.9: a calendar read from its "Secret address in iCal format" - no Google
# Cloud project, no sign-in, no quota - and turned into the shape the Calendar API
# answers with, so every listing, card and reminder above works on it unchanged.

ICAL_BYTES = 20_000_000
ICAL_TIMEOUT = 30.0
ICAL_DURATION = re.compile(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")

Prop = tuple[dict[str, str], str]


class Component:
    """One `BEGIN:`...`END:` block: its properties, and the blocks inside it."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.props: dict[str, list[Prop]] = {}
        self.children: list[Component] = []

    def first(self, name: str) -> Prop | None:
        found = self.props.get(name)
        return found[0] if found else None

    def text(self, name: str) -> str:
        found = self.first(name)
        return unescape(found[1]) if found else ""


def unfold(text: str) -> list[str]:
    """RFC 5545 §3.1: a line that starts with a space or a tab continues the last."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def split_line(line: str) -> tuple[str, dict[str, str], str]:
    """`DTSTART;TZID=Australia/Sydney:20261001T090000` -> name, params, value. A
    colon inside a quoted parameter is not the separator."""
    quoted = False
    for i, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ":" and not quoted:
            head, value = line[:i], line[i + 1 :]
            break
    else:
        return line.upper(), {}, ""
    name, *parts = head.split(";")
    params: dict[str, str] = {}
    for part in parts:
        key, _, found = part.partition("=")
        params[key.upper()] = found.strip('"')
    return name.upper(), params, value


def parse_ics(text: str) -> Component:
    root = Component("ROOT")
    stack = [root]
    for line in unfold(text):
        name, params, value = split_line(line)
        if name == "BEGIN":
            child = Component(value.strip().upper())
            stack[-1].children.append(child)
            stack.append(child)
        elif name == "END":
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1].props.setdefault(name, []).append((params, value))
    return root


def unescape(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        char = value[i]
        if char == "\\" and i + 1 < len(value):
            following = value[i + 1]
            out.append("\n" if following in "nN" else following)
            i += 2
        else:
            out.append(char)
            i += 1
    return "".join(out)


def ical_moment(params: Mapping[str, str], value: str, zone: tzinfo) -> datetime | date:
    """A DATE, a UTC time, a time in its TZID, or - floating - a time in the feed's zone."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or len(value) == 8:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    moment = datetime.strptime(value.rstrip("Zz")[:15], "%Y%m%dT%H%M%S")
    if value[-1:] in ("Z", "z"):
        return moment.replace(tzinfo=UTC)
    tzid = params.get("TZID", "")
    if tzid:
        try:
            return moment.replace(tzinfo=ZoneInfo(tzid))
        except (ValueError, KeyError, OSError):
            pass  # a Windows zone name, or one only its VTIMEZONE defines
    return moment.replace(tzinfo=zone)


def ical_duration(value: str) -> timedelta | None:
    match = ICAL_DURATION.fullmatch(value.strip().upper())
    if not match or not any(match.groups()[1:]):
        return None
    sign, weeks, days, hours, minutes, seconds = match.groups()
    length = timedelta(
        weeks=int(weeks or 0),
        days=int(days or 0),
        hours=int(hours or 0),
        minutes=int(minutes or 0),
        seconds=int(seconds or 0),
    )
    return -length if sign == "-" else length


def occurrence_key(moment: datetime | date) -> str:
    if isinstance(moment, datetime):
        return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return moment.strftime("%Y%m%d")


def api_part(moment: datetime | date) -> dict[str, str]:
    if isinstance(moment, datetime):
        return {"dateTime": moment.isoformat()}
    return {"date": moment.isoformat()}


class Feed:
    """One secret iCal address. The address is a credential: anyone holding it
    reads the calendar, so it is never put in anything the model or a log sees."""

    def __init__(self, index: int, url: str) -> None:
        self.index = index
        self.url = url
        self.id = f"ical{index}"
        self.name = f"iCal calendar {index}"
        self.zone_name, self.zone = local_zone()
        self.events: list[Component] = []
        self.fetched = float("-inf")

    async def load(self, clock: Callable[[], float], max_age: float) -> None:
        if clock() - self.fetched < max_age:
            return
        try:
            response = await get(
                self.url, max_bytes=ICAL_BYTES, timeout=ICAL_TIMEOUT, user_agent=USER_AGENT
            )
        except WebError as exc:
            # Not `str(exc)`: a client error may quote the address it could not reach.
            raise GoogleError(0, f"could not reach {self.id}: {type(exc).__name__}") from None
        if response.status != 200:
            raise GoogleError(
                response.status,
                f"{self.id} answered HTTP {response.status} - is it still the calendar's "
                "current secret address? Google changes it when the address is reset.",
            )
        calendar = next(
            (
                c
                for c in parse_ics(response.body.decode("utf-8", "replace")).children
                if c.kind == "VCALENDAR"
            ),
            None,
        )
        if calendar is None:
            raise GoogleError(response.status, f"{self.id} is not an iCal calendar")
        self.name = clean(calendar.text("X-WR-CALNAME")) or self.name
        zone_name = calendar.text("X-WR-TIMEZONE").strip()
        if zone_name:
            with contextlib.suppress(ValueError, KeyError, OSError):  # a zone tzdata lacks
                self.zone_name, self.zone = zone_name, ZoneInfo(zone_name)
        self.events = [c for c in calendar.children if c.kind == "VEVENT"]
        self.fetched = clock()

    def occurrences(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Every occurrence overlapping `start`..`end`, as the Calendar API shapes an
        event: recurring events expanded, EXDATEs dropped, moved ones moved."""
        masters: dict[str, Component] = {}
        moved: dict[str, dict[str, Component]] = {}
        for number, event in enumerate(self.events):
            uid = event.text("UID") or f"event{number}"
            recurrence_id = event.first("RECURRENCE-ID")
            if recurrence_id is not None:
                key = occurrence_key(ical_moment(recurrence_id[0], recurrence_id[1], self.zone))
                moved.setdefault(uid, {})[key] = event
            else:
                masters[uid] = event
        found: list[dict[str, Any]] = []
        for uid, event in masters.items():
            first = event.first("DTSTART")
            if first is None or event.text("STATUS").upper() == "CANCELLED":
                continue  # what the Calendar API leaves out of a listing, so does this
            begins = ical_moment(first[0], first[1], self.zone)
            length = self._length(event, begins)
            if event.first("RRULE") is None and event.first("RDATE") is None:
                if self._overlaps(begins, length, start, end):
                    found.append(self._as_event(event, uid, begins, length, None))
                continue
            instead = moved.get(uid, {})
            for moment in self._expand(event, begins, start - length, end):
                key = occurrence_key(moment)
                if key not in instead:
                    found.append(self._as_event(event, uid, moment, length, key))
            for key, change in instead.items():
                changed = change.first("DTSTART")
                if changed is None or change.text("STATUS").upper() == "CANCELLED":
                    continue  # one occurrence cancelled: gone, as the Calendar API has it
                at = ical_moment(changed[0], changed[1], self.zone)
                span_of = self._length(change, at)
                if self._overlaps(at, span_of, start, end):
                    found.append(self._as_event(change, uid, at, span_of, key))
        found.sort(key=lambda e: moment_of(e.get("start"), self.zone)[0])
        return found

    def find(self, event_id: str, around: datetime) -> dict[str, Any]:
        """The event `event_id` names - an id this feed made - near `around`."""
        _, _, rest = event_id.partition("/")
        key = rest.partition("/")[2]
        window = timedelta(days=2) if key else timedelta(days=800)
        if key:
            day = key[:8]
            around = datetime(int(day[:4]), int(day[4:6]), int(day[6:8]), tzinfo=self.zone)
        for event in self.occurrences(around - window, around + window):
            if event["id"] == event_id:
                return event
        raise ToolError(f"no event {event_id!r} in {self.id}")

    def _length(self, event: Component, begins: datetime | date) -> timedelta:
        ends = event.first("DTEND")
        if ends is not None:
            finish = ical_moment(ends[0], ends[1], self.zone)
            if type(finish) is type(begins):
                return finish - begins  # type: ignore[operator]
        duration = ical_duration(event.text("DURATION"))
        if duration is not None:
            return duration
        return timedelta(days=1) if not isinstance(begins, datetime) else timedelta(0)

    def _overlaps(
        self, begins: datetime | date, length: timedelta, start: datetime, end: datetime
    ) -> bool:
        first = as_instant(begins, self.zone)
        last = as_instant(begins + length, self.zone)
        return first < end and (last > start or (length == timedelta(0) and first >= start))

    def _expand(
        self, event: Component, begins: datetime | date, after: datetime, before: datetime
    ) -> list[datetime | date]:
        timed = isinstance(begins, datetime)
        anchor = begins if timed else datetime(begins.year, begins.month, begins.day)
        rules = rruleset()
        for _, value in event.props.get("RRULE", ()):
            rules.rrule(rrulestr(_until(value, timed), dtstart=anchor))  # type: ignore[arg-type]
        for name, add in (("RDATE", rules.rdate), ("EXDATE", rules.exdate)):
            for params, value in event.props.get(name, ()):
                for part in value.split(","):
                    moment = ical_moment(params, part, self.zone)
                    if timed:
                        add(as_instant(moment, self.zone))
                    else:
                        add(datetime(moment.year, moment.month, moment.day))
        if timed:
            low, high = after, before
        else:
            low = datetime.combine(after.astimezone(self.zone).date(), datetime.min.time())
            high = datetime.combine(before.astimezone(self.zone).date(), datetime.min.time())
        moments: list[datetime | date] = []
        for moment in rules.between(low, high, inc=True):
            moments.append(moment if timed else moment.date())
            if len(moments) >= 1000:
                break
        return moments

    def _as_event(
        self,
        event: Component,
        uid: str,
        begins: datetime | date,
        length: timedelta,
        key: str | None,
    ) -> dict[str, Any]:
        shaped: dict[str, Any] = {
            "id": f"{self.id}/{uid}" + (f"/{key}" if key else ""),
            "summary": event.text("SUMMARY"),
            "location": event.text("LOCATION"),
            "description": event.text("DESCRIPTION"),
            "status": (event.text("STATUS") or "confirmed").lower(),
            "transparency": "transparent"
            if event.text("TRANSP").upper() == "TRANSPARENT"
            else "opaque",
            "start": api_part(begins),
            "end": api_part(begins + length),
            "readOnly": True,
        }
        if key is not None:
            shaped["recurringEventId"] = f"{self.id}/{uid}"
        alarms = _alarms(event)
        shaped["reminders"] = (
            {"useDefault": False, "overrides": [{"method": "popup", "minutes": m} for m in alarms]}
            if alarms
            else {"useDefault": True}
        )
        return shaped


def _until(rule: str, timed: bool) -> str:
    """dateutil wants UNTIL in UTC when DTSTART has a zone, and without one when it
    has none; feeds write it either way."""

    def fix(match: re.Match[str]) -> str:
        value = match.group(1)
        if timed:
            if len(value) == 8:
                value += "T235959Z"
            elif not value.upper().endswith("Z"):
                value += "Z"
        else:
            value = value[:8]
        return f"UNTIL={value}"

    return re.sub(r"UNTIL=([0-9TZz]+)", fix, rule)


def _alarms(event: Component) -> list[int]:
    """The minutes-before of the event's own display or sound alarms."""
    found: list[int] = []
    for alarm in event.children:
        if alarm.kind != "VALARM" or alarm.text("ACTION").upper() not in ("DISPLAY", "AUDIO"):
            continue
        trigger = alarm.first("TRIGGER")
        if trigger is None or trigger[0].get("RELATED", "START").upper() != "START":
            continue
        before = ical_duration(trigger[1])
        if before is not None and before <= timedelta(0):
            found.append(int(-before.total_seconds() // 60))
    return sorted(set(found))


# -- the tools -----------------------------------------------------------------


ACCOUNT = {
    "type": "string",
    "description": (
        "Which signed-in Google account, by its label. Needed for a change when more than "
        "one is signed in."
    ),
}
CALENDAR = {
    "type": "string",
    "description": "A calendar id from calendar_list_calendars. Default: primary.",
}
EVENT_ID = {"type": "string", "description": "The event's id, as a listing showed it."}
SCOPE = {
    "type": "string",
    "enum": ["this", "series"],
    "description": (
        "For a recurring event: this occurrence, or the whole series. Required when the "
        "event repeats."
    ),
}
NOTIFY = {
    "type": "boolean",
    "description": "Email the other attendees about this. Default false: nobody is emailed.",
}


class CalendarTool(Tool):
    """What all eight share: owner-only, marked untrusted, and failures as sentences."""

    trusted_only = True
    untrusted = True

    def __init__(
        self,
        google: Google,
        settings: Mapping[str, Any],
        wake: Callable[[], object] | None = None,
    ) -> None:
        self.google = google
        self.settings = settings
        self.wake = wake
        """Tells the reminder service to look now (R7.7): set for the writes."""

    async def run(self, **arguments: Any) -> ToolResult:
        try:
            assert_active()
            return ToolResult.ok(await self.act(**arguments))
        except (CredentialError, ToolError, GoogleError, WebError) as exc:
            return ToolResult.error(str(exc))

    async def act(self, *args: Any, **arguments: Any) -> str:
        raise NotImplementedError


class ListCalendars(CalendarTool):
    name = "calendar_list_calendars"
    description = (
        "List the Google calendars the person can see: id, name, time zone, and whether "
        "reminders are sent for it. Use the id with the other calendar tools."
    )
    parameters = {"type": "object", "properties": {"account": ACCOUNT}, "required": []}

    async def act(self, account: str = "") -> str:
        watched = set(self.settings.get("calendars") or ["primary"])
        accounts, feeds = self.google.reading(account)
        lines: list[str] = []
        for who in accounts:
            for entry in await self.google.calendars(who):
                cid = str(entry.get("id", ""))
                marks = [str(entry.get("accessRole", ""))]
                if entry.get("primary"):
                    marks.append("primary")
                if (
                    "*" in watched
                    or cid in watched
                    or (entry.get("primary") and "primary" in watched)
                ):
                    marks.append("reminders on")
                lines.append(
                    f"{clean(entry.get('summary'))} · {entry.get('timeZone', '')} · "
                    f"{', '.join(m for m in marks if m)}  [id: {cid}]"
                    + (f"  ({label_of(who)})" if len(accounts) > 1 else "")
                )
        for feed in feeds:
            await feed.load(self.google.clock, self.google.feed_age)
            lines.append(
                f"{feed.name} · {feed.zone_name} · read-only (its secret iCal address), "
                f"reminders on  [id: {feed.id}]"
            )
        return "\n".join(lines) or "no calendars"


class ListEvents(CalendarTool):
    name = "calendar_list_events"
    description = (
        "List events between two times, recurring ones expanded, in order. Default: from now "
        "to 7 days ahead, on the person's watched calendars. Times without an offset are the "
        "calendar's own local time. Use it for 'what's on tomorrow', or with `query` to find "
        "an event by words in it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "start": {
                "type": "string",
                "description": "RFC 3339 time or YYYY-MM-DD. Default: now.",
            },
            "end": {
                "type": "string",
                "description": "RFC 3339 time or YYYY-MM-DD. Default: 7 days after start.",
            },
            "calendar": {
                "type": "string",
                "description": (
                    "A calendar id, or 'all' for every calendar. Default: the watched calendars."
                ),
            },
            "query": {"type": "string", "description": "Only events containing these words."},
            "account": ACCOUNT,
        },
        "required": [],
    }

    async def act(
        self, start: str = "", end: str = "", calendar: str = "", query: str = "", account: str = ""
    ) -> str:
        limit = int(self.settings.get("max_results", 50) or 50)
        accounts, feeds = self.google.reading(account)
        if calendar.startswith("ical"):
            accounts, feeds = (
                [],
                [f for f in feeds if f.id == calendar] or [self.google.feed(calendar)],
            )
        elif calendar and calendar != "all":
            feeds = []  # a Google calendar id names that calendar and no feed
        several = len(accounts) > 1
        lines: list[tuple[datetime, str]] = []
        cut = False
        header_zone = ""
        begin = finish = None

        def bounds(zone: tzinfo) -> tuple[datetime, datetime]:
            low = as_instant(parse_moment(start, zone), zone) if start else datetime.now(zone)
            high = as_instant(parse_moment(end, zone), zone) if end else low + timedelta(days=7)
            if high <= low:
                raise ToolError("end is not after start")
            return low, high

        for who in accounts:
            ids = await self._calendars(who, calendar)
            for cid in ids:
                zone_name, zone = await self.google.zone(who, cid)
                header_zone = header_zone or zone_name
                begin, finish = bounds(zone)
                name = clean((await self.google.calendar(who, cid)).get("summary")) or cid
                found, more = await self.google.events(
                    who, cid, begin, finish, zone_name=zone_name, query=query, limit=limit
                )
                cut = cut or more
                for event in found:
                    moment, _ = moment_of(event.get("start"), zone)
                    lines.append((moment, _event_line(event, zone, name, who if several else "")))
        words = query.lower().split()
        for feed in feeds:
            await feed.load(self.google.clock, self.google.feed_age)
            header_zone = header_zone or feed.zone_name
            begin, finish = bounds(feed.zone)
            for event in feed.occurrences(begin, finish):
                text = " ".join(
                    str(event.get(k, "")) for k in ("summary", "location", "description")
                )
                if all(word in text.lower() for word in words):
                    moment, _ = moment_of(event.get("start"), feed.zone)
                    lines.append((moment, _event_line(event, feed.zone, feed.name, "")))
        lines.sort(key=lambda pair: pair[0])
        if begin is None or finish is None:
            return "no calendars to read"
        head = f"{day_name(begin)} {hm(begin)} to {day_name(finish)} {hm(finish)} ({header_zone})"
        if not lines:
            return f"No events {head}."
        body = [text for _, text in lines[:limit]]
        tail = (
            f"\n(showing the first {limit} - ask for a narrower range to see the rest)"
            if cut or len(lines) > limit
            else ""
        )
        return f"{len(body)} event(s) {head}:\n" + "\n".join(body) + tail

    async def _calendars(self, who: str, calendar: str) -> list[str]:
        if calendar == "all":
            return [str(c.get("id")) for c in await self.google.calendars(who) if c.get("id")]
        if calendar:
            return [calendar]
        watched = [str(c) for c in (self.settings.get("calendars") or ["primary"])]
        if "*" in watched:
            return [str(c.get("id")) for c in await self.google.calendars(who) if c.get("id")]
        return watched


def _event_line(event: Mapping[str, Any], zone: tzinfo, calendar: str, account: str) -> str:
    parts = [span(event, zone), title_of(event)]
    where = where_of(event)
    if where:
        parts.append(where)
    if my_response(event) == "declined":
        parts.append("(you declined)")
    if event.get("recurringEventId"):
        parts.append("(repeats)")
    tags = f"id: {event.get('id', '')} · {calendar}" + (
        f" · {label_of(account)}" if account else ""
    )
    return " · ".join(parts) + f"  [{tags}]"


class GetEvent(CalendarTool):
    name = "calendar_get_event"
    description = (
        "One event in full: time, place, description, attendees and their answers, "
        "conference link, and how it repeats."
    )
    parameters = {
        "type": "object",
        "properties": {"event_id": EVENT_ID, "calendar": CALENDAR, "account": ACCOUNT},
        "required": ["event_id"],
    }

    async def act(self, event_id: str, calendar: str = "primary", account: str = "") -> str:
        if event_id.startswith("ical"):
            feed = self.google.feed(event_id.partition("/")[0])
            await feed.load(self.google.clock, self.google.feed_age)
            event: Mapping[str, Any] = feed.find(event_id, datetime.now(feed.zone))
            zone_name, zone = feed.zone_name, feed.zone
        else:
            who = self.google.pick(account, write=True)[0]  # an event is on one account
            zone_name, zone = await self.google.zone(who, calendar)
            event = await self.google.event(who, calendar, event_id)
        lines = [f"{title_of(event)}", f"When: {span(event, zone)} ({zone_name})"]
        if where_of(event):
            lines.append(f"Where: {where_of(event)}")
        if event.get("hangoutLink"):
            lines.append(f"Video: {event['hangoutLink']}")
        organizer = event.get("organizer") or {}
        if isinstance(organizer, dict) and organizer.get("email"):
            lines.append(f"Organiser: {organizer.get('email')}")
        attendees = [a for a in event.get("attendees") or () if isinstance(a, dict)]
        if attendees:
            lines.append(
                "Attendees: "
                + ", ".join(
                    f"{a.get('email', '?')} ({a.get('responseStatus', 'needsAction')})"
                    + (" (you)" if a.get("self") else "")
                    for a in attendees
                )
            )
        if event.get("recurrence"):
            lines.append("Repeats: " + "; ".join(str(r) for r in event["recurrence"]))
        if event.get("recurringEventId"):
            lines.append(f"An occurrence of the series {event['recurringEventId']}")
        if event.get("readOnly"):
            lines.append("Read-only: from the calendar's secret iCal address.")
        description = clean(event.get("description"), DESCRIPTION_MAX)
        if description:
            lines.append(f"Description: {description}")
        lines.append(f"[id: {event.get('id', event_id)} · status: {event.get('status', '')}]")
        return "\n".join(lines)


class FreeBusy(CalendarTool):
    name = "calendar_free_busy"
    description = (
        "When the person is busy between two times, and the free gaps of at least "
        "`min_minutes` inside working hours each day. For 'when am I free on Thursday'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "RFC 3339 time or YYYY-MM-DD."},
            "end": {"type": "string", "description": "RFC 3339 time or YYYY-MM-DD (exclusive)."},
            "calendars": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Calendar ids. Default: the watched calendars.",
            },
            "min_minutes": {
                "type": "integer",
                "minimum": 5,
                "description": "Shortest gap worth listing. Default 30.",
            },
            "day_start": {
                "type": "string",
                "description": "HH:MM, the start of each day's window. Default 09:00.",
            },
            "day_end": {
                "type": "string",
                "description": "HH:MM, the end of each day's window. Default 17:00.",
            },
            "account": ACCOUNT,
        },
        "required": ["start", "end"],
    }

    async def act(
        self,
        start: str,
        end: str,
        calendars: Sequence[str] = (),
        min_minutes: int = 30,
        day_start: str = "09:00",
        day_end: str = "17:00",
        account: str = "",
    ) -> str:
        opens, closes = _clock(day_start), _clock(day_end)
        if closes <= opens:
            raise ToolError("day_end is not after day_start")
        ids = list(calendars) or [str(c) for c in (self.settings.get("calendars") or ["primary"])]
        busy: list[tuple[datetime, datetime]] = []
        zone_name, zone = local_zone()
        begin = finish = datetime.now(zone)
        accounts, feeds = self.google.reading(account)
        if calendars:
            feeds = [f for f in feeds if f.id in ids]
            ids = [c for c in ids if not c.startswith("ical")]
            accounts = accounts if ids else []
        for feed in feeds:
            await feed.load(self.google.clock, self.google.feed_age)
            zone_name, zone = feed.zone_name, feed.zone
            begin = as_instant(parse_moment(start, zone), zone)
            finish = as_instant(parse_moment(end, zone), zone)
            if finish <= begin:
                raise ToolError("end is not after start")
            for event in feed.occurrences(begin, finish):
                first, all_day = moment_of(event.get("start"), zone)
                last, _ = moment_of(event.get("end"), zone)
                if (
                    all_day
                    or event["status"] == "cancelled"
                    or event["transparency"] == "transparent"
                ):
                    continue
                busy.append((first, last))
        for who in accounts:
            zone_name, zone = await self.google.zone(who, ids[0])
            begin = as_instant(parse_moment(start, zone), zone)
            finish = as_instant(parse_moment(end, zone), zone)
            if finish <= begin:
                raise ToolError("end is not after start")
            data = await self.google.call(
                who,
                "POST",
                "/freeBusy",
                body={
                    "timeMin": begin.isoformat(),
                    "timeMax": finish.isoformat(),
                    "timeZone": zone_name,
                    "items": [{"id": cid} for cid in ids],
                },
            )
            for cid, entry in (data.get("calendars") or {}).items():
                for error in entry.get("errors") or ():
                    raise ToolError(f"cannot read {cid}: {error.get('reason', 'unknown')}")
                for block in entry.get("busy") or ():
                    busy.append(
                        (
                            moment_of({"dateTime": block["start"]}, zone)[0],
                            moment_of({"dateTime": block["end"]}, zone)[0],
                        )
                    )
        merged = _merge(sorted(busy))
        lines = [f"Busy {day_name(begin)} to {day_name(finish)} ({zone_name}):"]
        lines += [f"  {day_name(a)} {hm(a)}-{hm(b)}" for a, b in merged] or ["  nothing"]
        gaps = list(_gaps(merged, begin, finish, opens, closes, int(min_minutes)))
        lines.append(f"Free for at least {int(min_minutes)} min between {day_start} and {day_end}:")
        lines += [f"  {day_name(a)} {hm(a)}-{hm(b)}" for a, b in gaps] or ["  none"]
        return "\n".join(lines)


def _clock(value: str) -> timedelta:
    match = CLOCK_TIME.fullmatch(str(value).strip())
    if not match:
        raise ToolError(f"{value!r} is not a time of day - write HH:MM")
    return timedelta(hours=int(match.group(1)), minutes=int(match.group(2)))


def _merge(blocks: Sequence[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in blocks:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _gaps(
    busy: Sequence[tuple[datetime, datetime]],
    begin: datetime,
    finish: datetime,
    opens: timedelta,
    closes: timedelta,
    minimum: int,
) -> Iterable[tuple[datetime, datetime]]:
    day = begin.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < finish:
        window_start = max(day + opens, begin)
        window_end = min(day + closes, finish)
        cursor = window_start
        for start, end in busy:
            if end <= cursor or start >= window_end:
                continue
            if start - cursor >= timedelta(minutes=minimum):
                yield cursor, start
            cursor = max(cursor, end)
        if window_end - cursor >= timedelta(minutes=minimum):
            yield cursor, window_end
        day += timedelta(days=1)


# -- changing things -----------------------------------------------------------


class Target:
    """What a change is aimed at, worked out once for the card and used again to run."""

    def __init__(
        self, account: str, calendar: str, calendar_name: str, zone_name: str, zone: tzinfo
    ) -> None:
        self.account = account
        self.calendar = calendar
        self.calendar_name = calendar_name
        self.zone_name = zone_name
        self.zone = zone
        self.event: Mapping[str, Any] = {}
        self.event_id = ""
        """The id the write goes to: the occurrence's or the series'."""
        self.series = False


class WriteTool(CalendarTool):
    """A change: gated, confirmed in every mode (G2), carded from what is really there."""

    gated = True
    action = ""

    def __init__(
        self,
        google: Google,
        settings: Mapping[str, Any],
        wake: Callable[[], object] | None = None,
    ) -> None:
        super().__init__(google, settings, wake)
        self._looked: dict[str, tuple[float, Target]] = {}
        """What `subject` read, keyed by the call's arguments, so `run` writes
        against the version the person saw (its etag) and not a later one."""

    async def subject(self, arguments: Mapping[str, Any]) -> Subject | None:
        try:
            target = await self.aim(arguments)
            summary = self.card(arguments, target)
        except (CredentialError, ToolError):
            return None  # `run` fails the same way, before it writes anything
        except (GoogleError, WebError) as exc:
            # Still a card: a read that failed is no reason to skip the question.
            summary = (
                f"{self.action} event {arguments.get('event_id', '')} (could not read it: {exc})"
            )
        else:
            self._looked[_key(arguments)] = (time.monotonic(), target)
        return Subject(tool=self.name, action=self.action, summary=summary, confirm=True)

    async def act(self, **arguments: Any) -> str:
        seen = self._looked.pop(_key(arguments), None)
        target = seen[1] if seen is not None and time.monotonic() - seen[0] < 600 else None
        if target is None:
            target = await self.aim(arguments)
        said = await self.write(arguments, target)
        if self.wake is not None:
            # R7.7: an event made or moved to within the idle poll would
            # otherwise wait up to that long for the service to see it.
            self.wake()
        return said

    async def aim(self, arguments: Mapping[str, Any]) -> Target:
        if str(arguments.get("event_id", "") or "").startswith("ical") or str(
            arguments.get("calendar", "") or ""
        ).startswith("ical"):
            raise ToolError(READ_ONLY)
        account = self.google.pick(str(arguments.get("account", "") or ""), write=True)[0]
        calendar = str(arguments.get("calendar", "") or "primary")
        zone_name, zone = await self.google.zone(account, calendar)
        name = clean((await self.google.calendar(account, calendar)).get("summary")) or calendar
        target = Target(account, calendar, name, zone_name, zone)
        event_id = str(arguments.get("event_id", "") or "")
        if event_id:
            target.event = await self.google.event(account, calendar, event_id)
            target.event_id, target.series = _aim_at(target.event, event_id, arguments.get("scope"))
        return target

    def card(self, arguments: Mapping[str, Any], target: Target) -> str:
        raise NotImplementedError

    async def write(self, arguments: Mapping[str, Any], target: Target) -> str:
        raise NotImplementedError

    def path(self, target: Target) -> str:
        return (
            f"/calendars/{quote(target.calendar, safe='')}/events/{quote(target.event_id, safe='')}"
        )

    def when(self, target: Target) -> str:
        text = span(target.event, target.zone)
        if target.series:
            return f"every occurrence from {text} (the whole series)"
        if target.event.get("recurringEventId"):
            return f"{text} (this occurrence)"
        return text


def _key(arguments: Mapping[str, Any]) -> str:
    return json.dumps(arguments, sort_keys=True, default=str)


def _aim_at(event: Mapping[str, Any], event_id: str, scope: Any) -> tuple[str, bool]:
    """R6.5: which id a change goes to. No default for a recurring event."""
    scope = str(scope or "")
    series_id = str(event.get("recurringEventId") or "")
    if series_id:
        if scope == "this":
            return event_id, False
        if scope == "series":
            return series_id, True
        raise ToolError(
            "this event repeats - say scope: this (only this occurrence) or scope: series "
            "(every occurrence)"
        )
    if event.get("recurrence"):
        if scope == "series":
            return event_id, True
        raise ToolError(
            "that id is the whole series - use scope: series to change every occurrence, "
            "or name one occurrence's id (from calendar_list_events) with scope: this"
        )
    return event_id, False


def _mailed(arguments: Mapping[str, Any], people: Iterable[str], verb: str) -> str:
    """R6.7: the line naming everybody Google will email, or nothing."""
    if not arguments.get("notify"):
        return ""
    names = sorted({p for p in people if p})
    return f" · emails {verb} to {', '.join(names)}" if names else ""


def _people(event: Mapping[str, Any]) -> list[str]:
    return [
        str(a.get("email"))
        for a in event.get("attendees") or ()
        if isinstance(a, dict) and a.get("email") and not a.get("self")
    ]


def _send_updates(arguments: Mapping[str, Any]) -> str:
    return "all" if arguments.get("notify") else "none"


def _etag(target: Target) -> dict[str, str]:
    etag = str(target.event.get("etag") or "")
    return {"If-Match": etag} if etag else {}


EVENT_FIELDS = {
    "summary": {"type": "string", "description": "The title."},
    "start": {
        "type": "string",
        "description": (
            "RFC 3339 (2026-10-01T09:00 is the calendar's local time) or YYYY-MM-DD for all day."
        ),
    },
    "end": {
        "type": "string",
        "description": "RFC 3339, or YYYY-MM-DD - the day after the last day, for all day.",
    },
    "location": {"type": "string"},
    "description": {"type": "string"},
    "attendees": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Email addresses to invite.",
    },
}


class CreateEvent(WriteTool):
    name = "calendar_create_event"
    action = "create"
    description = (
        "Create an event. The person is shown what will be created and must confirm. "
        "Nobody is emailed unless notify is true."
    )
    parameters = {
        "type": "object",
        "properties": {
            **EVENT_FIELDS,
            "recurrence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "RFC 5545 lines, e.g. RRULE:FREQ=WEEKLY;BYDAY=MO.",
            },
            "reminder_minutes": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Popup reminders, minutes before. Default: the calendar's.",
            },
            "calendar": CALENDAR,
            "account": ACCOUNT,
            "notify": NOTIFY,
        },
        "required": ["summary", "start", "end"],
    }

    def _times(self, arguments: Mapping[str, Any], target: Target) -> tuple[Any, Any]:
        start = parse_moment(str(arguments.get("start", "")), target.zone)
        end = parse_moment(str(arguments.get("end", "")), target.zone)
        if isinstance(start, datetime) != isinstance(end, datetime):
            raise ToolError("start and end must both be times, or both be dates")
        if as_instant(end, target.zone) <= as_instant(start, target.zone):
            raise ToolError("end is not after start")
        return start, end

    def card(self, arguments: Mapping[str, Any], target: Target) -> str:
        start, end = self._times(arguments, target)
        shown = span(
            {"start": api_time(start, target.zone_name), "end": api_time(end, target.zone_name)},
            target.zone,
        )
        repeats = arguments.get("recurrence") or ()
        text = f'Create "{clean(arguments.get("summary"))}" · {shown}'
        if repeats:
            text += f" · repeats {'; '.join(str(r) for r in repeats)}"
        return (
            text
            + f" · {target.calendar_name}"
            + _mailed(arguments, arguments.get("attendees") or (), "invitations")
        )

    async def write(self, arguments: Mapping[str, Any], target: Target) -> str:
        start, end = self._times(arguments, target)
        body: dict[str, Any] = {
            "summary": str(arguments.get("summary", "")),
            "start": api_time(start, target.zone_name),
            "end": api_time(end, target.zone_name),
        }
        for key in ("location", "description"):
            if arguments.get(key):
                body[key] = str(arguments[key])
        if arguments.get("attendees"):
            body["attendees"] = [{"email": str(a)} for a in arguments["attendees"]]
        if arguments.get("recurrence"):
            body["recurrence"] = [str(r) for r in arguments["recurrence"]]
        if arguments.get("reminder_minutes") is not None:
            body["reminders"] = {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": int(m)} for m in arguments["reminder_minutes"]
                ],
            }
        made = await self.google.call(
            target.account,
            "POST",
            f"/calendars/{quote(target.calendar, safe='')}/events",
            params={"sendUpdates": _send_updates(arguments)},
            body=body,
            retry=False,
        )
        return f"Created: {_event_line(made, target.zone, target.calendar_name, '')}"


class UpdateEvent(WriteTool):
    name = "calendar_update_event"
    action = "update"
    description = (
        "Change an event: title, time, place, description or attendees. Only the fields "
        "given change. For a recurring event, scope says this occurrence or the whole series. "
        "The person is shown the change and must confirm."
    )
    parameters = {
        "type": "object",
        "properties": {
            "event_id": EVENT_ID,
            **EVENT_FIELDS,
            "scope": SCOPE,
            "calendar": CALENDAR,
            "account": ACCOUNT,
            "notify": NOTIFY,
        },
        "required": ["event_id"],
    }

    def _changes(self, arguments: Mapping[str, Any], target: Target) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        for key in ("summary", "location", "description"):
            if key in arguments:
                changes[key] = str(arguments[key] or "")
        if "attendees" in arguments:
            kept = {
                str(a.get("email")): a
                for a in target.event.get("attendees") or ()
                if isinstance(a, dict) and a.get("email")
            }
            changes["attendees"] = [
                kept.get(str(e), {"email": str(e)}) for e in arguments["attendees"]
            ]
            for a in kept.values():
                if a.get("self") and a not in changes["attendees"]:
                    changes["attendees"].append(a)
        if "start" in arguments or "end" in arguments:
            old_start, all_day = moment_of(target.event.get("start"), target.zone)
            old_end, _ = moment_of(target.event.get("end"), target.zone)
            start = (
                parse_moment(str(arguments["start"]), target.zone)
                if "start" in arguments
                else (old_start.date() if all_day else old_start)
            )
            if "end" in arguments:
                end = parse_moment(str(arguments["end"]), target.zone)
            else:  # a move keeps the length
                end = start + (old_end - old_start)
            if isinstance(start, datetime) != isinstance(end, datetime):
                raise ToolError("start and end must both be times, or both be dates")
            if as_instant(end, target.zone) <= as_instant(start, target.zone):
                raise ToolError("end is not after start")
            changes["start"] = api_time(start, target.zone_name)
            changes["end"] = api_time(end, target.zone_name)
        if not changes:
            raise ToolError("nothing to change - give at least one field")
        return changes

    def card(self, arguments: Mapping[str, Any], target: Target) -> str:
        changes = self._changes(arguments, target)
        title = title_of(target.event)
        parts: list[str] = []
        if "start" in changes:
            after = {**target.event, "start": changes["start"], "end": changes["end"]}
            verb = f'Move "{title}" · {self.when(target)} → {span(after, target.zone)}'
        else:
            verb = f'Change "{title}" · {self.when(target)}'
        if "summary" in changes:
            parts.append(f'title → "{clean(changes["summary"])}"')
        if "location" in changes:
            parts.append(f"place → {clean(changes['location']) or '(none)'}")
        if "description" in changes:
            parts.append("new description")
        if "attendees" in changes:
            parts.append(
                "attendees → "
                + (
                    ", ".join(
                        str(a.get("email")) for a in changes["attendees"] if not a.get("self")
                    )
                    or "(none)"
                )
            )
        detail = f" · {', '.join(parts)}" if parts else ""
        people = set(_people(target.event)) | {
            str(a.get("email")) for a in changes.get("attendees", ()) if not a.get("self")
        }
        return f"{verb}{detail} · {target.calendar_name}" + _mailed(arguments, people, "updates")

    async def write(self, arguments: Mapping[str, Any], target: Target) -> str:
        changes = self._changes(arguments, target)
        if target.series and target.event_id != str(target.event.get("id", "")):
            # The card was drawn from the occurrence; the etag must be the series'.
            target.event = await self.google.event(target.account, target.calendar, target.event_id)
        done = await self.google.call(
            target.account,
            "PATCH",
            self.path(target),
            params={"sendUpdates": _send_updates(arguments)},
            body=changes,
            headers=_etag(target),
            retry=False,
        )
        return f"Updated: {_event_line(done, target.zone, target.calendar_name, '')}"


class DeleteEvent(WriteTool):
    name = "calendar_delete_event"
    action = "delete"
    description = (
        "Delete an event. For a recurring event, scope says this occurrence or the whole "
        "series. The person is shown what will be deleted and must confirm."
    )
    parameters = {
        "type": "object",
        "properties": {
            "event_id": EVENT_ID,
            "scope": SCOPE,
            "calendar": CALENDAR,
            "account": ACCOUNT,
            "notify": NOTIFY,
        },
        "required": ["event_id"],
    }

    def card(self, arguments: Mapping[str, Any], target: Target) -> str:
        return (
            f'Delete "{title_of(target.event)}" · {self.when(target)} · {target.calendar_name}'
            + _mailed(arguments, _people(target.event), "a cancellation")
        )

    async def write(self, arguments: Mapping[str, Any], target: Target) -> str:
        if target.series and target.event_id != str(target.event.get("id", "")):
            target.event = await self.google.event(target.account, target.calendar, target.event_id)
        await self.google.call(
            target.account,
            "DELETE",
            self.path(target),
            params={"sendUpdates": _send_updates(arguments)},
            headers=_etag(target),
            retry=False,
        )
        which = "the whole series" if target.series else span(target.event, target.zone)
        return f'Deleted "{title_of(target.event)}" ({which}).'


RESPONSES = {"accepted": "Accept", "declined": "Decline", "tentative": "Tentatively accept"}


class Respond(WriteTool):
    name = "calendar_respond"
    action = "respond"
    description = (
        "Answer an invitation: accepted, declined or tentative. The organiser is told. "
        "The person is shown the answer and must confirm."
    )
    parameters = {
        "type": "object",
        "properties": {
            "event_id": EVENT_ID,
            "response": {"type": "string", "enum": sorted(RESPONSES)},
            "scope": SCOPE,
            "calendar": CALENDAR,
            "account": ACCOUNT,
        },
        "required": ["event_id", "response"],
    }

    def _attendees(self, arguments: Mapping[str, Any], target: Target) -> list[dict[str, Any]]:
        response = str(arguments.get("response", ""))
        if response not in RESPONSES:
            raise ToolError(f"response is one of {', '.join(sorted(RESPONSES))}")
        attendees = [dict(a) for a in target.event.get("attendees") or () if isinstance(a, dict)]
        mine = [a for a in attendees if a.get("self")]
        if not mine:
            raise ToolError("you are not invited to this event, so there is nothing to answer")
        for a in mine:
            a["responseStatus"] = response
        return attendees

    def card(self, arguments: Mapping[str, Any], target: Target) -> str:
        self._attendees(arguments, target)
        verb = RESPONSES[str(arguments.get("response"))]
        return (
            f'{verb} "{title_of(target.event)}" · {self.when(target)} · {target.calendar_name}'
            " · the organiser is told"
        )

    async def write(self, arguments: Mapping[str, Any], target: Target) -> str:
        if target.series and target.event_id != str(target.event.get("id", "")):
            target.event = await self.google.event(target.account, target.calendar, target.event_id)
        attendees = self._attendees(arguments, target)
        await self.google.call(
            target.account,
            "PATCH",
            self.path(target),
            params={"sendUpdates": "all"},
            body={"attendees": attendees},
            headers=_etag(target),
            retry=False,
        )
        title = title_of(target.event)
        return f'Answered {arguments["response"]} to "{title}"; the organiser is told.'


# -- reminders -----------------------------------------------------------------


class Due:
    """One reminder to send: when, which, and the line itself."""

    def __init__(
        self,
        at: float,
        key: str,
        starts: float,
        ends: float,
        event: Mapping[str, Any],
        zone: tzinfo,
        all_day: bool = False,
    ) -> None:
        self.at = at
        self.key = key
        self.starts = starts
        self.ends = ends
        self.event = event
        self.zone = zone
        self.all_day = all_day


class Reminded:
    """R7.5: what has been sent, kept across restarts. A key is written before
    the send and marked after it, so a crash between the two sends nothing twice."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.rows: dict[str, dict[str, Any]] = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            self.rows = {}

    def __contains__(self, key: str) -> bool:
        return key in self.rows

    def claim(self, key: str, ends: float) -> None:
        self.rows[key] = {"ends": ends, "sent": False}
        self.save()

    def mark(self, key: str, result: str) -> None:
        if key in self.rows:
            self.rows[key]["sent"] = result
            self.save()

    def prune(self, now: float) -> None:
        stale = [k for k, row in self.rows.items() if float(row.get("ends", 0)) < now - 86400]
        for key in stale:
            del self.rows[key]
        if stale:
            self.save()

    def save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.rows, indent=1, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)


def leads(
    event: Mapping[str, Any], defaults: Sequence[Mapping[str, Any]], settings: Mapping[str, Any]
) -> list[int]:
    """R7.2-R7.3: the minutes-before this event is reminded at; empty for none."""
    if event.get("status") == "cancelled" or my_response(event) == "declined":
        return []
    fallback = [int(m) for m in settings.get("lead_minutes") or [10]]
    if not settings.get("use_event_reminders", True):
        return fallback
    reminders = event.get("reminders") or {}
    methods = defaults if reminders.get("useDefault", True) else reminders.get("overrides") or ()
    methods = [m for m in methods if isinstance(m, dict)]
    if not methods:
        return fallback
    popups = [int(m.get("minutes", 0)) for m in methods if m.get("method") == "popup"]
    return sorted(set(popups))  # only email reminders: the person chose email


def reminder_text(due: Due, now: float) -> str:
    """R7.4: one line, from the event and nothing else."""
    title = title_of(due.event)
    if due.all_day:
        return f"📅 Today: {title}"
    start = datetime.fromtimestamp(due.starts, due.zone)
    minutes = max(0, round((due.starts - now) / 60))
    line = f"⏰ {title} at {start.hour}:{start.minute:02d} (in {minutes} min)"
    where = where_of(due.event)
    return f"{line} · {where}" if where else line


class Reminders(Service):
    """The service: look ahead, sleep until something is due, send it (§7)."""

    async def run(self, ctx: ServiceContext) -> None:
        google = Google(
            Path(ctx.workspace),
            cache_seconds=SERVICE_CACHE,
            clock=lambda: ctx.now().timestamp(),
            ical_env=str(ctx.setting("ical_url_env", ICAL_ENV) or ICAL_ENV),
            feed_age=0.0,
        )
        reminded = Reminded(ctx.state_dir / "reminded.json")
        warned: dict[str, str] = {}
        backoff = 0.0
        while not ctx.stopping:
            poll = max(1, int(ctx.setting("poll_minutes", 5) or 5)) * 60
            idle = max(poll, int(ctx.setting("idle_poll_minutes", 30) or 30) * 60)
            if not ctx.setting("reminders", True):
                if await ctx.sleep_for(idle):
                    return
                continue
            now = ctx.now().timestamp()
            due: list[Due] = []
            failed = False
            for account in ctx.connections():
                try:
                    due += await self.upcoming(google, account, ctx, now)
                    warned.pop(account, None)
                except CredentialError as exc:
                    # R5.5: said once, then quiet until the sign-in changes.
                    if warned.get(account) != str(exc):
                        warned[account] = str(exc)
                        await _tell(ctx, f"Google Calendar ({label_of(account)}): {exc}")
                except (GoogleError, WebError, OSError):
                    failed = True
            for feed in google.feeds():
                try:
                    await feed.load(google.clock, google.feed_age)
                    due += self.dues(
                        feed.occurrences(*self.window(ctx, now, feed.zone)),
                        feed.zone,
                        [],
                        ctx,
                        now,
                        feed.id,
                    )
                    warned.pop(feed.id, None)
                except GoogleError as exc:
                    if 400 <= exc.status < 500:
                        # R5.9: a reset address is said once, like an expired sign-in.
                        if warned.get(feed.id) != str(exc):
                            warned[feed.id] = str(exc)
                            await _tell(ctx, f"Google Calendar: {exc}")
                    else:
                        failed = True
                except OSError:
                    failed = True
            reminded.prune(now)
            backoff = min(max(backoff * 2, poll), 1800.0) if failed else 0.0
            # R7.1: often while something is close, rarely while nothing is.
            near = any(d.at <= now + NEAR and d.key not in reminded for d in due)
            next_poll = now + (backoff or (poll if near else idle))
            await self.fire(ctx, reminded, due, next_poll)

    @staticmethod
    def window(ctx: ServiceContext, now: float, zone: tzinfo) -> tuple[datetime, datetime]:
        """From a day back - an all-day event began at midnight - to the horizon."""
        horizon = max(1, int(ctx.setting("horizon_hours", 24) or 24)) * 3600
        return datetime.fromtimestamp(now - 86400, zone), datetime.fromtimestamp(
            now + horizon, zone
        )

    async def upcoming(
        self, google: Google, account: str, ctx: ServiceContext, now: float
    ) -> list[Due]:
        watched = [str(c) for c in (ctx.setting("calendars") or ["primary"])]
        if "*" in watched:
            watched = [str(c.get("id")) for c in await google.calendars(account) if c.get("id")]
        found: list[Due] = []
        for calendar in watched:
            entry = await google.calendar(account, calendar)
            zone_name, zone = await google.zone(account, calendar)
            defaults = [d for d in entry.get("defaultReminders") or () if isinstance(d, dict)]
            begin, until = self.window(ctx, now, zone)
            events, _ = await google.events(
                account, calendar, begin, until, zone_name=zone_name, limit=500, retry=False
            )
            found += self.dues(events, zone, defaults, ctx, now, account)
        return found

    def dues(
        self,
        events: Iterable[Mapping[str, Any]],
        zone: tzinfo,
        defaults: Sequence[Mapping[str, Any]],
        ctx: ServiceContext,
        now: float,
        source: str,
    ) -> list[Due]:
        """R7.2-R7.3: the reminders these events are due, from an account or a feed."""
        horizon = max(1, int(ctx.setting("horizon_hours", 24) or 24)) * 3600
        grace = max(0, int(ctx.setting("late_grace_minutes", 5) or 0)) * 60
        at = _clock_or_none(str(ctx.setting("all_day_at", "") or ""))
        found: list[Due] = []
        for event in events:
            start, all_day = moment_of(event.get("start"), zone)
            end, _ = moment_of(event.get("end"), zone)
            occurrence = f"{source}|{event.get('id', '')}|{start.isoformat()}"
            if all_day:
                if (
                    at is None
                    or event.get("status") == "cancelled"
                    or my_response(event) == "declined"
                ):
                    continue
                when = (start + at).timestamp()
                if now - grace <= when <= now + horizon:
                    found.append(
                        Due(
                            when,
                            f"{occurrence}|all-day",
                            start.timestamp(),
                            end.timestamp(),
                            event,
                            zone,
                            True,
                        )
                    )
                continue
            for minutes in leads(event, defaults, ctx.settings):
                when = start.timestamp() - minutes * 60
                if when < now - grace or start.timestamp() <= now:
                    continue  # R7.3: a machine that was asleep, or it already began
                found.append(
                    Due(
                        when,
                        f"{occurrence}|{minutes}",
                        start.timestamp(),
                        end.timestamp(),
                        event,
                        zone,
                    )
                )
        return found

    async def fire(
        self, ctx: ServiceContext, reminded: Reminded, due: list[Due], next_poll: float
    ) -> None:
        pending = sorted((d for d in due if d.key not in reminded), key=lambda d: d.at)
        while not ctx.stopping:
            now = ctx.now().timestamp()
            while pending and pending[0].at <= now:
                item = pending.pop(0)
                if not item.all_day and item.starts <= now:
                    continue  # R8.9: after the event began, it is dropped, not sent late
                reminded.claim(item.key, item.ends)
                reminded.mark(item.key, await _tell(ctx, reminder_text(item, now)))
            wake = min([next_poll, *(d.at for d in pending[:1])])
            if wake <= now and not pending:
                return
            if await ctx.sleep_until(wake) or ctx.woken():
                return  # stopped, or a tool changed something: look again now
            if ctx.now().timestamp() >= next_poll:
                return


def _clock_or_none(value: str) -> timedelta | None:
    match = CLOCK_TIME.fullmatch(value.strip())
    if not match:
        return None
    return timedelta(hours=int(match.group(1)), minutes=int(match.group(2)))


async def _tell(ctx: ServiceContext, text: str) -> str:
    try:
        return await ctx.notify(text)
    except NotifyError as exc:
        return f"failed: {exc}"


# -- signing in ----------------------------------------------------------------


def _label(response: Mapping[str, Any]) -> Mapping[str, str]:
    """R5.2: the account's address, from the `id_token` the token endpoint sent.
    Decoded, not verified - it came back over TLS from the endpoint the flow
    itself called."""
    token = str(response.get("id_token") or "")
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}
    email = str(claims.get("email") or "") if isinstance(claims, dict) else ""
    return {"email": email, "label": email} if email else {}


def client(client_id: str) -> OAuthClient:
    return OAuthClient(
        authorize_url=AUTHORIZE_URL,
        token_url=TOKEN_URL,
        client_id=client_id,
        scopes=SCOPES,
        authorize_params={"access_type": "offline", "prompt": "consent"},
        parse=_label,
        label="Google Calendar",
    )


def no_client(context: LoginContext) -> Tokens:
    """What signing in does before any client id exists: say so, and stop."""
    raise CredentialError(
        "Google Calendar has no OAuth client id yet. Create a Desktop-app client in a "
        "Google Cloud project with the Calendar API enabled, then set "
        "plugins_settings.google-calendar.client_id to its id in config.json."
    )


class GoogleCalendarPlugin(Plugin):
    name = PLUGIN
    description = "Google Calendar: read, change with a yes, and reminders."

    def register(self, ctx: PluginContext) -> None:
        client_id = str(ctx.setting("client_id", "") or "").strip() or DEFAULT_CLIENT_ID
        ctx.register_login(LOGIN, client(client_id) if client_id else no_client)
        google = Google(
            Path(ctx.workspace),
            ical_env=str(ctx.setting("ical_url_env", ICAL_ENV) or ICAL_ENV),
        )
        settings = dict(ctx.settings)

        def wake() -> bool:
            return ctx.wake_service("reminders")

        for read in (ListCalendars, ListEvents, GetEvent, FreeBusy):
            ctx.register_tool(read(google, settings), toolset="Google Calendar")
        for write in (CreateEvent, UpdateEvent, DeleteEvent, Respond):
            ctx.register_tool(write(google, settings, wake), toolset="Google Calendar")
        ctx.register_service("reminders", Reminders)
