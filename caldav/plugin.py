"""CalDAV: iCloud, Fastmail, Yahoo or any CalDAV calendar (`docs/spec/caldav.md`).

Eight tools, the same shapes as `google-calendar`'s, named `caldav_calendar_*` so
they sit beside it and `microsoft`. Every tool is `trusted_only` - offered only in
a session an owner holds - and `untrusted`, because an event's title is whatever
the invite's sender wrote. Reading is free; every change is a card a person
answers in every mode (`Subject.confirm`), naming who the server will email.

Sign-in is the app password the `email` plugin already reads from `.env`, so an
iPhone user who set up mail has nothing new to do. That password goes to the
account's own server and nowhere else: every URL it is sent to - every redirect,
every address the server answers with - is checked against the server's domain
first (G2).

The server is spoken to in plain WebDAV through `atlas.sdk.web.request`, and
repeats are expanded here with `google-calendar`'s iCalendar reader.
"""

from __future__ import annotations

import asyncio
import base64
import calendar as month_lengths
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import unquote, urljoin, urlsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from dateutil.rrule import rruleset, rrulestr

from atlas.sdk.auth import SecretRef, read_dotenv, resolve
from atlas.sdk.plugin_entry import Plugin, PluginContext
from atlas.sdk.runtime import CredentialError, NetworkPolicyError, ToolError, assert_active
from atlas.sdk.tool_plugin import Subject, Tool, ToolResult
from atlas.sdk.web import WebError, check_redirect, request

PLUGIN = "caldav"
USER_AGENT = "atlas-caldav"
RESPONSE_BYTES = 20_000_000
RETRY_CAP = 10.0
"""R6.10: a read waits out a 429 or a 5xx once, for at most this long."""
DISCOVERY_SECONDS = 600.0
"""R4.5: how long an account's principal, home and calendars are kept."""
REDIRECTS = frozenset({301, 302, 307, 308})
FOLLOWED = frozenset({"PROPFIND", "REPORT", "GET"})
"""R4.5: the verbs a redirect is followed for - reads, whose body may be re-sent."""
MAX_HOPS = 3
TITLE_MAX = 120
DESCRIPTION_MAX = 2000
DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
CLOCK_TIME = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
XML = "application/xml; charset=utf-8"
ICS = "text/calendar; charset=utf-8"
DAV = "{DAV:}"
CAL = "{urn:ietf:params:xml:ns:caldav}"
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

Moment = datetime | date
"""A time, or - for an all-day event - a date."""


# -- servers (R4.2-R4.4) -------------------------------------------------------------


class Server(NamedTuple):
    """Where one account's calendars are, and the domain its password may go to.

    A `NamedTuple` and not a dataclass: Atlas imports a plugin without putting
    its module in `sys.modules`, which a dataclass needs to exist."""

    key: str
    name: str
    url: str
    domain: str
    passwords: str = "your provider's account security settings"
    """Where an app password is made, for the sentence that says to make one."""


PRESETS: dict[str, Server] = {
    "icloud": Server(
        "icloud",
        "iCloud",
        "https://caldav.icloud.com/",
        "icloud.com",
        "account.apple.com, Sign-In and Security, App-Specific Passwords",
    ),
    "fastmail": Server(
        "fastmail",
        "Fastmail",
        "https://caldav.fastmail.com/dav/",
        "fastmail.com",
        "Fastmail's Settings, Privacy & Security, App passwords - with Calendars (CalDAV) "
        "ticked; one made for mail alone does not open calendars",
    ),
    "yahoo": Server(
        "yahoo",
        "Yahoo",
        "https://caldav.calendar.yahoo.com/",
        "yahoo.com",
        "Yahoo's Account Security, Generate app password",
    ),
}
DOMAINS = {
    "icloud.com": "icloud",
    "me.com": "icloud",
    "mac.com": "icloud",
    "fastmail.com": "fastmail",
    "fastmail.fm": "fastmail",
    "yahoo.com": "yahoo",
    "ymail.com": "yahoo",
    "rocketmail.com": "yahoo",
}
GOOGLE = frozenset({"gmail.com", "googlemail.com"})
MICROSOFT = re.compile(r"^(outlook|hotmail|live|msn|passport)(\.[a-z]{2,})+$")
HOST = re.compile(r"[A-Za-z0-9.-]+")


def server_for(label: str, address: str, servers: Mapping[str, Any]) -> Server:
    """R4.2-R4.4: what `servers` names for the label, else the domain's preset."""
    named = servers.get(label)
    if isinstance(named, Mapping):
        named = named.get("caldav")
    if isinstance(named, str) and named.strip():
        text = named.strip()
        preset = PRESETS.get(text.lower())
        if preset is not None:
            return preset
        return _custom(label, text)
    domain = address.rpartition("@")[2].strip().lower()
    preset_name = DOMAINS.get(domain) or ("yahoo" if domain.startswith("yahoo.") else "")
    if preset_name:
        return PRESETS[preset_name]
    if domain in GOOGLE:
        raise CredentialError(
            f"{address} is a Google address - Google's calendar takes no app password over "
            "CalDAV; use the google-calendar plugin for it (/plugins install google-calendar)"
        )
    if MICROSOFT.match(domain):
        raise CredentialError(
            f"{address} is a Microsoft address - use the microsoft plugin for its calendar "
            "(/plugins install microsoft)"
        )
    raise CredentialError(
        f"Atlas does not know where {domain}'s calendars are: set plugins_settings.caldav."
        f"servers.{label} to one of {', '.join(PRESETS)} (an iCloud address on your own domain "
        'is "icloud"), or to the server\'s https address, such as '
        '"https://cloud.example.com/remote.php/dav/"'
    )


def _custom(label: str, url: str) -> Server:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https":
        raise CredentialError(
            f"servers.{label} is not an https:// address - the app password would cross the "
            "network in the clear"
        )
    if not host or not HOST.fullmatch(host):
        raise CredentialError(f"servers.{label} is not a server's address: {url!r}")
    return Server("custom", host, url if url.endswith("/") else url + "/", host)


def allowed(server: Server, url: str) -> None:
    """G2: the password goes over https to the server's own domain, or not at all."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    domain = server.domain.lower()
    if parts.scheme.lower() != "https" or not (host == domain or host.endswith("." + domain)):
        raise ToolError(
            f"refused: {host or 'an address'} is not {server.name}'s ({domain}) - the app "
            "password goes nowhere else, and nothing was sent there"
        )


# -- accounts (R4.1) -----------------------------------------------------------------


class Calendar:
    """One calendar collection on the server."""

    def __init__(self, label: str, url: str, name: str, writable: bool) -> None:
        self.label = label
        self.url = url
        self.name = name
        self.writable = writable
        self.default = False


class Found:
    """R4.5: what discovery found for one account."""

    def __init__(
        self, principal: str, addresses: Sequence[str], calendars: Sequence[Calendar]
    ) -> None:
        self.principal = principal
        self.addresses = tuple(addresses)
        """The person's own addresses, lower-cased and without `mailto:`."""
        self.calendars = list(calendars)


class Account:
    """One address and app password, its server, and what discovery found."""

    def __init__(
        self, label: str, address: str, password: str, server: Server | None, problem: str = ""
    ) -> None:
        self.label = label
        self.address = address
        self.password = password
        self.server = server
        self.problem = problem
        """Why this account cannot be reached at all (R4.4), said by any tool that tries."""
        self.found: Found | None = None
        self.found_at = float("-inf")

    def __repr__(self) -> str:  # never the password
        return f"<Account {self.label} {self.address}>"

    @property
    def provider(self) -> str:
        return self.server.name if self.server else "the calendar server"

    def authorization(self) -> str:
        pair = f"{self.address}:{self.password}".encode()
        return "Basic " + base64.b64encode(pair).decode("ascii")

    def me(self) -> set[str]:
        """Every address that is this person: the principal's, and the account's own."""
        found = set(self.found.addresses) if self.found else set()
        return found | {self.address.lower()}


def not_set_up(address_env: str, password_env: str) -> str:
    return (
        f"CalDAV is not set up: store your address as {address_env} and an app password as "
        f'{password_env} - the same two the email plugin reads (atlas "store my email app '
        'password"). For iCloud, make the password at account.apple.com, Sign-In and '
        "Security, App-Specific Passwords; two-step sign-in must be on"
    )


class Accounts:
    """R4.1: the accounts in `.env`, as `email` reads them, read fresh on every call."""

    def __init__(self, workspace: Path, settings: Mapping[str, Any]) -> None:
        self.workspace = workspace
        self.settings = settings
        self._kept: dict[str, Account] = {}

    def envs(self) -> tuple[str, str]:
        return (
            str(self.settings.get("address_env") or "EMAIL_ADDRESS"),
            str(self.settings.get("password_env") or "EMAIL_APP_PASSWORD"),
        )

    def all(self) -> list[Account]:
        address_env, password_env = self.envs()
        labels = [
            str(label).strip().lower() for label in self.settings.get("accounts") or ["personal"]
        ]
        servers = self.settings.get("servers") or {}
        servers = servers if isinstance(servers, Mapping) else {}
        dotenv = read_dotenv(self.workspace)
        found: list[Account] = []
        for index, label in enumerate(labels):
            suffix = "" if index == 0 else f"_{label.upper()}"
            address = _read(f"{address_env}{suffix}", dotenv)
            password = _read(f"{password_env}{suffix}", dotenv).replace(" ", "")
            if not address or not password:
                continue
            try:
                server: Server | None = server_for(label, address, servers)
                problem = ""
            except CredentialError as exc:
                server, problem = None, str(exc)
            kept = self._kept.get(label)
            if kept is None or (kept.address, kept.password, kept.server, kept.problem) != (
                address,
                password,
                server,
                problem,
            ):
                kept = self._kept[label] = Account(label, address, password, server, problem)
            found.append(kept)
        return found

    def named(self, label: str) -> Account:
        known = self.all()
        if not known:
            raise CredentialError(not_set_up(*self.envs()))
        chosen = [a for a in known if a.label == label.strip().lower()]
        if not chosen:
            raise ToolError(f"no account {label!r} - set up: {', '.join(a.label for a in known)}")
        if chosen[0].problem:
            raise CredentialError(chosen[0].problem)
        return chosen[0]

    def reading(self, label: str) -> list[Account]:
        """R4.4: the named account, or every one that can be reached."""
        if label:
            return [self.named(label)]
        known = self.all()
        if not known:
            raise CredentialError(not_set_up(*self.envs()))
        usable = [a for a in known if not a.problem]
        if not usable:
            raise CredentialError(known[0].problem)
        return usable

    def writer(self, label: str) -> Account:
        """R5.1: a change goes to one account, never a guessed one."""
        usable = self.reading(label)
        if len(usable) > 1:
            raise ToolError(
                "more than one account is set up; say which with account: "
                + ", ".join(a.label for a in usable)
            )
        return usable[0]


def _read(variable: str, dotenv: Mapping[str, str]) -> str:
    try:
        return resolve(SecretRef("env", variable), dotenv=dotenv).strip()
    except CredentialError:
        return ""


# -- talking to the server (R4.5, R6.10) --------------------------------------------------


class DavError(ToolError):
    """A status the server answered with, as a sentence the model can repeat."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _retry_after(response: Any) -> float:
    try:
        return min(max(float(response.header("retry-after") or 1.0), 0.0), RETRY_CAP)
    except ValueError:
        return 1.0


def explain(account: Account, status: int) -> Exception:
    if status == 401:
        where = account.server.passwords if account.server else "your provider"
        return CredentialError(
            f"{account.provider} ({account.label}) refused the app password - it was revoked, "
            f"or the account's password changed. Make a new one at {where}"
        )
    if status == 403:
        return DavError(status, "the server refused this (403) - the calendar may be read-only")
    if status == 404:
        return DavError(status, "not found (404) - it was deleted or moved; list again")
    if status == 412:
        return DavError(
            status, "the event changed since it was shown - nothing was changed; look again"
        )
    if status == 429:
        return DavError(status, f"{account.provider} is limiting requests (429); try again soon")
    return DavError(status, f"HTTP {status} from {account.provider}")


class Answer(NamedTuple):
    status: int
    body: bytes
    url: str
    """Where the answer came from, after any redirect - what an `href` resolves against."""
    etag: str


class Dav:
    """One install's way to the servers: checked addresses, one request at a time."""

    def __init__(self, policy: Any = None) -> None:
        self.allow_private = bool(getattr(policy, "allow_private", False))
        hosts = getattr(policy, "hosts", None)
        self.hosts = hosts
        self.clock: Callable[[], float] = time.monotonic

    def _rules(self) -> dict[str, Any]:
        return {
            "allow_private": self.allow_private,
            **({"hosts": self.hosts} if self.hosts else {}),
        }

    async def send(
        self,
        account: Account,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        content_type: str = "",
        depth: str | None = None,
        headers: Mapping[str, str] | None = None,
        ok: Iterable[int] = (200, 207),
    ) -> Answer:
        """One request, its redirects followed for a read and checked at every hop.
        A status not in `ok` is an error in words."""
        assert account.server is not None
        allowed(account.server, url)
        sent = {
            "Authorization": account.authorization(),
            **({"Depth": depth} if depth is not None else {}),
            **(headers or {}),
        }
        reading = method in FOLLOWED
        current = url
        for _hop in range(MAX_HOPS + 1):
            for attempt in range(2):
                try:
                    response = await request(
                        method,
                        current,
                        data=body,
                        content_type=content_type,
                        headers=sent,
                        max_bytes=RESPONSE_BYTES,
                        timeout=60.0,
                        user_agent=USER_AGENT,
                        **self._rules(),
                    )
                except NetworkPolicyError as exc:
                    raise ToolError(f"refused: {exc}") from None
                transient = response.status == 429 or response.status >= 500
                if transient and reading and attempt == 0:
                    await asyncio.sleep(_retry_after(response))
                    continue
                break
            location = response.header("location")
            if response.status in REDIRECTS and location and reading:
                following = urljoin(current, location)
                allowed(account.server, following)
                try:
                    current = check_redirect(current, location, **self._rules())
                except NetworkPolicyError as exc:
                    raise ToolError(f"refused: {exc}") from None
                continue
            if response.status in REDIRECTS:
                raise DavError(
                    response.status,
                    f"{account.provider} moved it ({response.status}) - nothing was changed; "
                    "list again",
                )
            if response.status not in set(ok):
                raise explain(account, response.status)
            return Answer(response.status, response.body, current, response.header("etag"))
        raise ToolError(f"{account.provider} redirected more than {MAX_HOPS} times")

    def resolve(self, account: Account, base: str, href: str) -> str:
        """R4.5: an address the server answered with, made absolute and checked."""
        assert account.server is not None
        url = urljoin(base, href.strip())
        allowed(account.server, url)
        return url


def multistatus(body: bytes) -> list[tuple[str, dict[str, ElementTree.Element]]]:
    """Each response's `href`, and its properties that were found, by Clark name."""
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise ToolError("the calendar server sent something unreadable") from None
    found: list[tuple[str, dict[str, ElementTree.Element]]] = []
    for response in root.iter(f"{DAV}response"):
        href = (response.findtext(f"{DAV}href") or "").strip()
        props: dict[str, ElementTree.Element] = {}
        for propstat in response.findall(f"{DAV}propstat"):
            status = propstat.findtext(f"{DAV}status") or "HTTP/1.1 200 OK"
            prop = propstat.find(f"{DAV}prop")
            if " 200" not in status or prop is None:
                continue
            for child in prop:
                props[child.tag] = child
        found.append((href, props))
    return found


def _text(element: ElementTree.Element | None) -> str:
    """An element's text. Not `element or ...`: an element with no children is falsy."""
    return (element.text or "").strip() if element is not None else ""


def _children(element: ElementTree.Element | None) -> list[ElementTree.Element]:
    return list(element) if element is not None else []


def _href(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    return (element.findtext(f"{DAV}href") or "").strip()


PRINCIPAL_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>'
)
HOME_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>'
    b"<c:calendar-home-set/><c:calendar-user-address-set/><c:schedule-default-calendar-URL/>"
    b"</d:prop></d:propfind>"
)
CALENDARS_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>'
    b"<d:displayname/><d:resourcetype/><c:supported-calendar-component-set/>"
    b"<d:current-user-privilege-set/></d:prop></d:propfind>"
)
QUERY_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
    '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
    '<c:time-range start="{start}" end="{end}"/>'
    "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"
)
WRITE_PRIVILEGES = frozenset({f"{DAV}write", f"{DAV}all", f"{DAV}write-content"})


def utc_stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def address_of(value: str) -> str:
    """`mailto:Sam@Example.com` -> `sam@example.com`."""
    text = value.strip()
    if text.lower().startswith("mailto:"):
        text = text[len("mailto:") :]
    return unquote(text).strip().lower()


# -- time (R5.3) --------------------------------------------------------------------


def local() -> tzinfo:
    return datetime.now().astimezone().tzinfo or UTC


def parse_when(value: str, zone: tzinfo) -> Moment:
    """A local time - `2026-10-02T15:00` - or a date, for an all-day event."""
    text = str(value or "").strip()
    if DATE_ONLY.fullmatch(text):
        return date.fromisoformat(text)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(
            f"{value!r} is not a time - write 2026-10-02T15:00 (local time), or 2026-10-02 "
            "for a whole day"
        ) from None
    return moment if moment.tzinfo else moment.replace(tzinfo=zone)


def as_instant(value: Moment, zone: tzinfo) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day, tzinfo=zone)


def shown(value: Moment, zone: tzinfo) -> Moment:
    return value.astimezone(zone) if isinstance(value, datetime) else value


def day_name(day: date) -> str:
    """`Fri 2 Oct` - built by hand because `%-d` is not portable."""
    return f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"


def span_of(start: Moment, end: Moment, zone: tzinfo) -> str:
    """R5.3: an all-day event's dates (end exclusive), or local times with the day named."""
    if not isinstance(start, datetime):
        last = end - timedelta(days=1) if not isinstance(end, datetime) else start
        if last <= start:
            return f"{day_name(start)} (all day)"
        return f"{day_name(start)} - {day_name(last)} (all day)"
    first, final = start.astimezone(zone), as_instant(end, zone).astimezone(zone)
    if final.date() == first.date():
        return f"{day_name(first.date())} {first:%H:%M}-{final:%H:%M}"
    return f"{day_name(first.date())} {first:%H:%M} - {day_name(final.date())} {final:%H:%M}"


def window(start: str, end: str, zone: tzinfo, *, days: int) -> tuple[datetime, datetime]:
    """A listing's span: given times; a date's local midnight - for an end, the whole of
    that day; with neither, now and `days` on."""
    first = datetime.now(zone)
    if start:
        given = parse_when(start, zone)
        first = as_instant(given, zone)
    last = first + timedelta(days=days)
    if end:
        given = parse_when(end, zone)
        last = (
            given
            if isinstance(given, datetime)
            else datetime.combine(given + timedelta(days=1), datetime.min.time(), tzinfo=zone)
        )
    if last <= first:
        raise ToolError("the end is not after the start")
    return first, last


def stretch(first: datetime, last: datetime) -> str:
    return f"{day_name(first.date())} {first:%H:%M} to {day_name(last.date())} {last:%H:%M}"


def clean(text: Any, limit: int = TITLE_MAX) -> str:
    """One line of someone else's words, capped."""
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


# -- iCalendar: reading (R5.2) ------------------------------------------------------------
#
# `google-calendar`'s reader (`google-calendar.md` R5.7), with parameters split
# respecting quotes, and a writer beside it (R6.2).

Prop = tuple[dict[str, str], str]
ICAL_DURATION = re.compile(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")


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

    def put(self, name: str, value: str, params: Mapping[str, str] | None = None) -> None:
        self.props[name] = [(dict(params or {}), value)]

    def put_text(self, name: str, value: str) -> None:
        self.put(name, escape(value))

    def drop(self, name: str) -> None:
        self.props.pop(name, None)

    def copy(self) -> Component:
        twin = Component(self.kind)
        twin.props = {k: [(dict(p), v) for p, v in values] for k, values in self.props.items()}
        twin.children = [child.copy() for child in self.children]
        return twin


def unfold(text: str) -> list[str]:
    """RFC 5545 §3.1: a line that starts with a space or a tab continues the last."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _outside_quotes(text: str, separator: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    for char in text:
        if char == '"':
            quoted = not quoted
        if char == separator and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def split_line(line: str) -> tuple[str, dict[str, str], str]:
    """`DTSTART;TZID=Australia/Sydney:20261001T090000` -> name, params, value. A colon
    or a semicolon inside a quoted parameter is not a separator."""
    quoted = False
    for i, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ":" and not quoted:
            head, value = line[:i], line[i + 1 :]
            break
    else:
        return line.upper(), {}, ""
    name, *parts = _outside_quotes(head, ";")
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


def ical_moment(params: Mapping[str, str], value: str, zone: tzinfo) -> Moment:
    """A DATE, a UTC time, a time in its TZID, or - floating - a time in `zone`."""
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


def occurrence_key(moment: Moment) -> str:
    if isinstance(moment, datetime):
        return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return moment.strftime("%Y%m%d")


def _until(rule: str, timed: bool) -> str:
    """dateutil wants UNTIL in UTC when DTSTART has a zone, and without one when it
    has none; servers write it either way."""

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


# -- iCalendar: writing (R6.2, R6.3) --------------------------------------------------


def escape(text: str) -> str:
    """RFC 5545 §3.3.11: a TEXT value's backslashes, semicolons, commas and newlines."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\n", "\\n")
    )


def _param(value: str) -> str:
    return f'"{value}"' if any(c in value for c in ":;,") else value


def fold(line: str) -> list[str]:
    """RFC 5545 §3.1: lines of at most 75 octets, never splitting a character."""
    out: list[str] = []
    current, size, limit = "", 0, 75
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            out.append(current)
            current, size, limit = " ", 1, 75
        current += char
        size += width
    out.append(current)
    return out


def serialize(component: Component) -> str:
    lines: list[str] = []

    def emit(block: Component) -> None:
        if block.kind != "ROOT":
            lines.append(f"BEGIN:{block.kind}")
        for name, values in block.props.items():
            for params, value in values:
                head = name + "".join(f";{k}={_param(v)}" for k, v in params.items())
                lines.extend(fold(f"{head}:{value}"))
        for child in block.children:
            emit(child)
        if block.kind != "ROOT":
            lines.append(f"END:{block.kind}")

    emit(component)
    return "\r\n".join(lines) + "\r\n"


def ical_value(moment: Moment, like: Prop | None, zone: tzinfo) -> Prop:
    """A time written the way `like` was: its TZID, UTC, or floating (R6.3). With
    no `like`, a date is a DATE and a time is UTC."""
    if not isinstance(moment, datetime):
        return {"VALUE": "DATE"}, moment.strftime("%Y%m%d")
    params, value = like if like is not None else ({}, "Z")
    tzid = params.get("TZID", "")
    if tzid and params.get("VALUE", "").upper() != "DATE":
        try:
            there = ZoneInfo(tzid)
        except (ValueError, KeyError, OSError):
            there = None
        if there is not None:
            return {"TZID": tzid}, moment.astimezone(there).strftime("%Y%m%dT%H%M%S")
    if (
        like is not None
        and not tzid
        and not value.strip().upper().endswith("Z")
        and len(value.strip()) > 8
    ):
        return {}, moment.astimezone(zone).strftime("%Y%m%dT%H%M%S")  # floating stays floating
    return {}, utc_stamp(moment)


def _written(moment: Moment, like: Prop | None, zone: tzinfo) -> tuple[str, dict[str, str]]:
    """`ical_value` in the order `Component.put` takes: the value, then its parameters."""
    params, value = ical_value(moment, like, zone)
    return value, params


def _offset(delta: timedelta | None) -> str:
    seconds = int((delta or timedelta(0)).total_seconds())
    sign = "-" if seconds < 0 else "+"
    hours, rest = divmod(abs(seconds), 3600)
    return f"{sign}{hours:02d}{rest // 60:02d}"


def vtimezone(zone: ZoneInfo, year: int) -> Component:
    """R6.3: a `VTIMEZONE` for `zone`, built from the tz database's transitions in
    `year` - each one a yearly rule on its weekday of the month."""
    block = Component("VTIMEZONE")
    block.put("TZID", zone.key)
    moment = datetime(year, 1, 1, tzinfo=UTC)
    last = datetime(year + 1, 1, 1, tzinfo=UTC)
    before = moment.astimezone(zone).utcoffset()
    changes: list[tuple[datetime, timedelta | None]] = []
    while moment < last:
        moment += timedelta(hours=1)
        after = moment.astimezone(zone).utcoffset()
        if after != before:
            changes.append((moment, before))
            before = after
    if not changes:
        here = datetime(year, 1, 1, tzinfo=UTC).astimezone(zone)
        part = Component("STANDARD")
        part.put("DTSTART", "19700101T000000")
        part.put("TZOFFSETFROM", _offset(here.utcoffset()))
        part.put("TZOFFSETTO", _offset(here.utcoffset()))
        part.put("TZNAME", here.tzname() or zone.key)
        block.children.append(part)
        return block
    for instant, was in changes:
        there = instant.astimezone(zone)
        wall = (instant + (was or timedelta(0))).replace(tzinfo=None)  # the clock before it moved
        nth = (wall.day - 1) // 7 + 1
        if wall.day + 7 > month_lengths.monthrange(wall.year, wall.month)[1]:
            nth = -1
        part = Component("DAYLIGHT" if there.dst() else "STANDARD")
        part.put("DTSTART", wall.strftime("%Y%m%dT%H%M%S"))
        part.put("RRULE", f"FREQ=YEARLY;BYMONTH={wall.month};BYDAY={nth}{WEEKDAYS[wall.weekday()]}")
        part.put("TZOFFSETFROM", _offset(was))
        part.put("TZOFFSETTO", _offset(there.utcoffset()))
        part.put("TZNAME", there.tzname() or zone.key)
        block.children.append(part)
    return block


def stamp_now() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def bump(event: Component, *, sequence: bool = True) -> None:
    """R6.2: a change says it is newer - `SEQUENCE` for an organiser's, `DTSTAMP` always."""
    if sequence:
        try:
            number = int(event.text("SEQUENCE") or "0")
        except ValueError:
            number = 0
        event.put("SEQUENCE", str(number + 1))
    event.put("DTSTAMP", stamp_now())
    event.put("LAST-MODIFIED", stamp_now())


# -- resources and occurrences --------------------------------------------------------


class Occurrence:
    """One time an event happens: the block it comes from, and when."""

    def __init__(
        self, resource: Resource, event: Component, start: Moment, end: Moment, key: str
    ) -> None:
        self.resource = resource
        self.event = event
        self.start = start
        self.end = end
        self.key = key
        """Which repeat, for one that repeats (`occurrence_key` of its original start)."""

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)

    @property
    def repeats(self) -> bool:
        return bool(self.key)

    @property
    def title(self) -> str:
        return clean(self.event.text("SUMMARY")) or "(no title)"

    @property
    def place(self) -> str:
        return clean(self.event.text("LOCATION"))

    @property
    def cancelled(self) -> bool:
        return self.event.text("STATUS").upper() == "CANCELLED"

    @property
    def transparent(self) -> bool:
        return self.event.text("TRANSP").upper() == "TRANSPARENT"

    def organiser(self) -> str:
        found = self.event.first("ORGANIZER")
        return address_of(found[1]) if found else ""

    def organiser_named(self) -> str:
        found = self.event.first("ORGANIZER")
        if found is None:
            return ""
        name, address = found[0].get("CN", ""), address_of(found[1])
        return f"{name} <{address}>" if name and name.lower() != address else address

    def attendees(self) -> list[tuple[str, str, str]]:
        """Each attendee's address, name and answer."""
        return [
            (address_of(value), params.get("CN", ""), params.get("PARTSTAT", "NEEDS-ACTION"))
            for params, value in self.event.props.get("ATTENDEE", [])
        ]

    def answer(self, me: set[str]) -> str:
        for address, _, partstat in self.attendees():
            if address in me:
                return partstat.upper()
        return ""

    def others(self, me: set[str]) -> list[str]:
        return [address for address, _, _ in self.attendees() if address and address not in me]


class Resource:
    """One `.ics` on the server: one event, or a series and its changed occurrences."""

    def __init__(self, calendar: Calendar, url: str, etag: str, text: str, zone: tzinfo) -> None:
        self.calendar = calendar
        self.url = url
        self.etag = etag
        self.zone = zone
        """Where a floating time is, and where times are shown."""
        self.root = parse_ics(text)
        found = next((c for c in self.root.children if c.kind == "VCALENDAR"), None)
        if found is None:
            raise ToolError("the calendar server sent an event that is not iCalendar")
        self.vcal = found

    @property
    def events(self) -> list[Component]:
        return [c for c in self.vcal.children if c.kind == "VEVENT"]

    @property
    def master(self) -> Component | None:
        return next((e for e in self.events if e.first("RECURRENCE-ID") is None), None)

    def overrides(self) -> dict[str, Component]:
        found: dict[str, Component] = {}
        for event in self.events:
            recurrence = event.first("RECURRENCE-ID")
            if recurrence is not None:
                found[occurrence_key(ical_moment(recurrence[0], recurrence[1], self.zone))] = event
        return found

    def recurring(self) -> bool:
        master = self.master
        return master is not None and (
            master.first("RRULE") is not None or master.first("RDATE") is not None
        )

    def text(self) -> str:
        return serialize(self.root)

    def length(self, event: Component, begins: Moment) -> timedelta:
        ends = event.first("DTEND")
        if ends is not None:
            finish = ical_moment(ends[0], ends[1], self.zone)
            if type(finish) is type(begins):
                return finish - begins  # type: ignore[operator]
        duration = ical_duration(event.text("DURATION"))
        if duration is not None:
            return duration
        return timedelta(days=1) if not isinstance(begins, datetime) else timedelta(0)

    def occurrences(self, start: datetime, end: datetime) -> list[Occurrence]:
        """Every occurrence overlapping `start`..`end`: repeats expanded, EXDATEs dropped,
        moved ones moved, cancelled ones left out - `google-calendar`'s `Feed`."""
        found: list[Occurrence] = []
        master = self.master
        instead = self.overrides()
        if master is not None and master.first("DTSTART") is not None:
            first = master.first("DTSTART")
            assert first is not None
            begins = ical_moment(first[0], first[1], self.zone)
            length = self.length(master, begins)
            if not self.recurring():
                if master.text("STATUS").upper() != "CANCELLED" and self._overlaps(
                    begins, length, start, end
                ):
                    found.append(Occurrence(self, master, begins, begins + length, ""))
                return found
            if master.text("STATUS").upper() != "CANCELLED":
                for moment in self._expand(master, begins, start - length, end):
                    key = occurrence_key(moment)
                    if key not in instead:
                        found.append(Occurrence(self, master, moment, moment + length, key))
        for key, change in instead.items():
            changed = change.first("DTSTART")
            if changed is None or change.text("STATUS").upper() == "CANCELLED":
                continue  # one occurrence cancelled: gone
            at = ical_moment(changed[0], changed[1], self.zone)
            span = self.length(change, at)
            if self._overlaps(at, span, start, end):
                found.append(Occurrence(self, change, at, at + span, key))
        return found

    def occurrence(self, key: str) -> Occurrence:
        """The occurrence a handle names - the event itself when it does not repeat."""
        if not key:
            master = self.master
            first = master.first("DTSTART") if master is not None else None
            if master is None or first is None:
                raise ToolError("that event is gone - list again")
            begins = ical_moment(first[0], first[1], self.zone)
            return Occurrence(self, master, begins, begins + self.length(master, begins), "")
        if len(key) == 8:
            around = datetime(int(key[:4]), int(key[4:6]), int(key[6:8]), tzinfo=self.zone)
        else:
            around = datetime.strptime(key, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        for found in self.occurrences(around - timedelta(days=2), around + timedelta(days=2)):
            if found.key == key:
                return found
        raise ToolError("that occurrence is gone - list again")

    def _overlaps(self, begins: Moment, length: timedelta, start: datetime, end: datetime) -> bool:
        first = as_instant(begins, self.zone)
        last = as_instant(begins + length, self.zone)
        return first < end and (last > start or (length == timedelta(0) and first >= start))

    def _expand(
        self, event: Component, begins: Moment, after: datetime, before: datetime
    ) -> list[Moment]:
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
        moments: list[Moment] = []
        for moment in rules.between(low, high, inc=True):
            moments.append(moment if timed else moment.date())
            if len(moments) >= 1000:
                break
        return moments


# -- handles (R5.4) --------------------------------------------------------------------


class Handles:
    """`e4` for one occurrence, standing for its account, calendar, resource and repeat."""

    def __init__(self) -> None:
        self._by_handle: dict[str, tuple[str, str, str, str]] = {}
        self._by_key: dict[tuple[str, str, str, str], str] = {}

    def give(self, occurrence: Occurrence) -> str:
        resource = occurrence.resource
        key = (resource.calendar.label, resource.calendar.url, resource.url, occurrence.key)
        if key not in self._by_key:
            handle = f"e{len(self._by_key) + 1}"
            self._by_key[key] = handle
            self._by_handle[handle] = key
        return self._by_key[key]

    def take(self, handle: str) -> tuple[str, str, str, str]:
        found = self._by_handle.get(str(handle or "").strip().lower())
        if found is None:
            raise ToolError(f"no event {handle!r} - list again for a fresh id")
        return found


# -- what every tool shares ---------------------------------------------------------------


class Target(NamedTuple):
    """The event a change or a read is about, read fresh."""

    account: Account
    calendar: Calendar
    resource: Resource
    occurrence: Occurrence


class Caldav:
    """The accounts, the server client, the handles and the settings."""

    def __init__(self, ctx: PluginContext, dav: Dav | None = None) -> None:
        self.ctx = ctx
        self.settings = dict(ctx.settings)
        self.accounts = Accounts(Path(ctx.workspace), self.settings)
        self.dav = dav or Dav(getattr(ctx, "web_policy", None))
        self.handles = Handles()

    def number(self, key: str, default: int) -> int:
        try:
            return int(self.settings.get(key) or default)
        except (TypeError, ValueError):
            return default

    def zone(self) -> tzinfo:
        """R5.3: the `time_zone` setting's zone, or this machine's."""
        named = str(self.settings.get("time_zone") or "").strip()
        if named:
            try:
                return ZoneInfo(named)
            except (ValueError, KeyError, OSError):
                raise ToolError(
                    f"time_zone {named!r} is not a zone - write Australia/Sydney"
                ) from None
        return local()

    def zone_key(self) -> str:
        """R6.3: the IANA name a repeating event is written with, or "" for none."""
        zone = self.zone()
        return str(getattr(zone, "key", "") or "")

    async def discover(self, account: Account) -> Found:
        """R4.5: principal, home and calendars, kept for ten minutes."""
        if account.found is not None and self.dav.clock() - account.found_at < DISCOVERY_SECONDS:
            return account.found
        assert account.server is not None
        principal, at = await self._principal(account, account.server.url)
        if not principal and account.server.key == "custom":
            principal, at = await self._principal(
                account, urljoin(account.server.url, "/.well-known/caldav")
            )
        if not principal:
            raise ToolError(
                f"{account.provider} did not say where {account.address}'s calendars are"
            )
        principal_url = self.dav.resolve(account, at, principal)
        answer = await self.dav.send(
            account, "PROPFIND", principal_url, body=HOME_BODY, content_type=XML, depth="0"
        )
        props = multistatus(answer.body)[0][1] if multistatus(answer.body) else {}
        home = _href(props.get(f"{CAL}calendar-home-set"))
        if not home:
            raise ToolError(f"{account.provider} has no calendars for {account.address}")
        addresses = [
            address_of(h.text or "")
            for h in _children(props.get(f"{CAL}calendar-user-address-set"))
            if h.tag == f"{DAV}href" and (h.text or "").lower().startswith("mailto:")
        ]
        default_href = _href(props.get(f"{CAL}schedule-default-calendar-URL"))
        home_url = self.dav.resolve(account, answer.url, home)
        listing = await self.dav.send(
            account, "PROPFIND", home_url, body=CALENDARS_BODY, content_type=XML, depth="1"
        )
        default_url = self.dav.resolve(account, answer.url, default_href) if default_href else ""
        calendars: list[Calendar] = []
        for href, found in multistatus(listing.body):
            kind = found.get(f"{DAV}resourcetype")
            if kind is None or kind.find(f"{CAL}calendar") is None:
                continue
            parts = found.get(f"{CAL}supported-calendar-component-set")
            names = [c.get("name", "").upper() for c in parts] if parts is not None else []
            if names and "VEVENT" not in names:
                continue  # a task list, not a calendar
            privileges = found.get(f"{DAV}current-user-privilege-set")
            writable = privileges is None or any(
                element.tag in WRITE_PRIVILEGES for element in privileges.iter()
            )
            url = self.dav.resolve(account, listing.url, href)
            name = clean(_text(found.get(f"{DAV}displayname")))
            if not name:
                name = unquote(url.rstrip("/").rpartition("/")[2]) or "calendar"
            calendar = Calendar(account.label, url, name, writable)
            # By path: iCloud names its default on the address the principal came
            # from, and the calendars on the per-person host.
            calendar.default = bool(default_url) and (
                urlsplit(url).path.rstrip("/") == urlsplit(default_url).path.rstrip("/")
            )
            calendars.append(calendar)
        account.found = Found(principal_url, addresses, calendars)
        account.found_at = self.dav.clock()
        return account.found

    async def _principal(self, account: Account, url: str) -> tuple[str, str]:
        try:
            answer = await self.dav.send(
                account,
                "PROPFIND",
                url,
                body=PRINCIPAL_BODY,
                content_type=XML,
                depth="0",
            )
        except DavError as exc:
            if exc.status in (400, 404, 405):
                return "", url
            raise
        for _, props in multistatus(answer.body):
            href = _href(props.get(f"{DAV}current-user-principal"))
            if href:
                return href, answer.url
        return "", answer.url

    async def resources(
        self, account: Account, calendar: Calendar, start: datetime, end: datetime
    ) -> list[Resource]:
        """R5.2: one `calendar-query` with a time range."""
        body = QUERY_BODY.format(start=utc_stamp(start), end=utc_stamp(end)).encode()
        answer = await self.dav.send(
            account, "REPORT", calendar.url, body=body, content_type=XML, depth="1"
        )
        found: list[Resource] = []
        for href, props in multistatus(answer.body):
            data = props.get(f"{CAL}calendar-data")
            if data is None or not (data.text or "").strip():
                continue
            etag = _text(props.get(f"{DAV}getetag"))
            url = self.dav.resolve(account, answer.url, href)
            found.append(Resource(calendar, url, etag.strip(), data.text or "", self.zone()))
        return found

    async def fetch(self, account: Account, calendar: Calendar, url: str) -> Resource:
        answer = await self.dav.send(account, "GET", url, ok=(200,))
        return Resource(
            calendar, answer.url, answer.etag, answer.body.decode("utf-8", "replace"), self.zone()
        )

    async def target(self, handle: str) -> Target:
        """R5.4: the occurrence a handle names, read fresh from the server."""
        label, calendar_url, url, key = self.handles.take(handle)
        account = self.accounts.named(label)
        found = await self.discover(account)
        calendar = next((c for c in found.calendars if c.url == calendar_url), None)
        if calendar is None:
            raise ToolError(f"the calendar of {handle} is gone - list again")
        resource = await self.fetch(account, calendar, url)
        return Target(account, calendar, resource, resource.occurrence(key))

    def chosen(self, found: Found, name: str) -> list[Calendar]:
        """A calendar by its name, or every one for `all` or nothing."""
        wanted = name.strip().lower()
        if not wanted or wanted in ("all", "*"):
            return found.calendars
        for calendar in found.calendars:
            if calendar.name.lower() == wanted:
                return [calendar]
        names = ", ".join(c.name for c in found.calendars)
        raise ToolError(f"no calendar {name!r} - this account has: {names}")

    def home_for(self, found: Found, name: str) -> Calendar:
        """§10: where a new event goes - the one named, `default_calendar`, the
        server's default, or the only writable one; never a guess."""
        wanted = name.strip() or str(self.settings.get("default_calendar") or "").strip()
        writable = [c for c in found.calendars if c.writable]
        if wanted:
            calendar = self.chosen(found, wanted)[0]
            if not calendar.writable:
                raise ToolError(f"{calendar.name} is read-only - choose another calendar")
            return calendar
        for calendar in writable:
            if calendar.default:
                return calendar
        if len(writable) == 1:
            return writable[0]
        if not writable:
            raise ToolError("every calendar on this account is read-only")
        raise ToolError("say which calendar with calendar: " + ", ".join(c.name for c in writable))


def addresses(value: Any) -> list[str]:
    """Attendees as addresses, each checked to look like one."""
    found: list[str] = []
    for item in value or ():
        text = str(item or "").strip()
        address = address_of(text.rpartition("<")[2].rstrip(">") if "<" in text else text)
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
            raise ToolError(f"{item!r} is not an email address")
        if address not in found:
            found.append(address)
    return found


ACCOUNT = {
    "type": "string",
    "description": "Which account, by its label. Needed for a change when several are set up.",
}
EVENT_ID = {"type": "string", "description": "An event's id, as a listing showed it (e4)."}
SCOPE = {
    "type": "string",
    "enum": ["this", "series"],
    "description": "For a repeating event: this occurrence, or the whole series. Required then.",
}


class CaldavTool(Tool):
    """Owner-only, marked untrusted, failures as sentences."""

    trusted_only = True
    untrusted = True

    def __init__(self, caldav: Caldav) -> None:
        self.caldav = caldav

    async def run(self, **arguments: Any) -> ToolResult:
        try:
            assert_active()
            return ToolResult.ok(await self.act(**arguments))
        except (CredentialError, ToolError, WebError) as exc:
            return ToolResult.error(str(exc))

    async def act(self, *args: Any, **arguments: Any) -> str:
        raise NotImplementedError


def marks_of(occurrence: Occurrence, me: set[str]) -> str:
    marks: list[str] = []
    organiser = occurrence.organiser()
    others = occurrence.others(me)
    if others and (not organiser or organiser in me):
        marks.append(f"you organise, {len(others)} invited")
    answer = occurrence.answer(me)
    if organiser and organiser not in me:
        marks.append(
            {
                "ACCEPTED": "accepted",
                "DECLINED": "you declined",
                "TENTATIVE": "tentative",
            }.get(answer, "not answered")
        )
    if occurrence.repeats:
        marks.append("repeats")
    if occurrence.transparent:
        marks.append("shown as free")
    return ", ".join(marks)


# -- reading (R5.1) -------------------------------------------------------------------------


class ListCalendars(CaldavTool):
    name = "caldav_calendar_list_calendars"
    description = (
        "The calendars on the person's CalDAV accounts (iCloud, Fastmail, Yahoo, Nextcloud), "
        "by name, the default and read-only ones marked. Use a name with the other "
        "caldav_calendar tools."
    )
    parameters = {"type": "object", "properties": {"account": ACCOUNT}, "required": []}

    async def act(self, account: str = "") -> str:
        accounts = self.caldav.accounts
        known = [accounts.named(account)] if account else accounts.all()
        if not known:
            raise CredentialError(not_set_up(*accounts.envs()))
        lines: list[str] = []
        for each in known:
            if each.problem:
                lines.append(f"{each.label}: not reachable here - {each.problem}")
                continue
            found = await self.caldav.discover(each)
            for calendar in found.calendars:
                marks = [
                    m
                    for m, on in (
                        ("default", calendar.default),
                        ("read-only", not calendar.writable),
                    )
                    if on
                ]
                lines.append(
                    f"{each.label}: {calendar.name}"
                    + (f" ({', '.join(marks)})" if marks else "")
                    + f" · {each.provider}"
                )
        return "\n".join(lines) or "No calendars."


class ListEvents(CaldavTool):
    name = "caldav_calendar_list_events"
    description = (
        "What is on the person's CalDAV calendars (iCloud and others) between two times - "
        "default now to seven days on - repeats expanded, in order, each with an id. Times "
        "are local: 2026-10-02T09:00, or a date. calendar is a name, or all (default); query "
        "finds an event by words in it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "A local time or a date. Default: now."},
            "end": {"type": "string", "description": "A local time or a date (that whole day)."},
            "calendar": {"type": "string", "description": "A calendar's name, or all."},
            "query": {"type": "string", "description": "Only events containing these words."},
            "account": ACCOUNT,
        },
        "required": [],
    }

    async def act(
        self, start: str = "", end: str = "", calendar: str = "", query: str = "", account: str = ""
    ) -> str:
        zone = self.caldav.zone()
        first, last = window(start, end, zone, days=7)
        limit = max(1, self.caldav.number("max_results", 50))
        accounts = self.caldav.accounts.reading(account)
        words = query.lower().split()
        found: list[tuple[datetime, str]] = []
        for each in accounts:
            discovered = await self.caldav.discover(each)
            chosen = self.caldav.chosen(discovered, calendar)
            me = each.me()
            for cal in chosen:
                for resource in await self.caldav.resources(each, cal, first, last):
                    for occurrence in resource.occurrences(first, last):
                        event = occurrence.event
                        text = " ".join(
                            event.text(k) for k in ("SUMMARY", "LOCATION", "DESCRIPTION")
                        ).lower()
                        if not all(word in text for word in words):
                            continue
                        where = (f" · {cal.name}" if len(chosen) > 1 else "") + (
                            f" · {each.label}" if len(accounts) > 1 else ""
                        )
                        found.append(
                            (as_instant(occurrence.start, zone), self._line(occurrence, me, where))
                        )
        found.sort(key=lambda pair: pair[0])
        if not found:
            return f"Nothing on, {stretch(first, last)}."
        text = f"{len(found[:limit])} event(s), {stretch(first, last)}:\n" + "\n".join(
            line for _, line in found[:limit]
        )
        if len(found) > limit:
            text += f"\n(showing the first {limit} - narrow the time to see the rest)"
        return text

    def _line(self, occurrence: Occurrence, me: set[str], where: str) -> str:
        handle = self.caldav.handles.give(occurrence)
        marks = marks_of(occurrence, me)
        span = span_of(occurrence.start, occurrence.end, self.caldav.zone())
        return (
            f"{span} · {occurrence.title}"
            + (f" · at {occurrence.place}" if occurrence.place else "")
            + (f" · {marks}" if marks else "")
            + f"  [id: {handle}{where}]"
        )


PARTSTATS = {
    "ACCEPTED": "accepted",
    "DECLINED": "declined",
    "TENTATIVE": "tentative",
    "NEEDS-ACTION": "not answered",
    "DELEGATED": "delegated",
}


class GetEvent(CaldavTool):
    name = "caldav_calendar_get_event"
    description = (
        "One CalDAV event in full: times, place, organiser, attendees and their answers, how "
        "it repeats, and its description."
    )
    parameters = {"type": "object", "properties": {"event_id": EVENT_ID}, "required": ["event_id"]}

    async def act(self, event_id: str) -> str:
        target = await self.caldav.target(event_id)
        occurrence, me = target.occurrence, target.account.me()
        zone = self.caldav.zone()
        lines = [
            f"Title: {occurrence.title}",
            f"When: {span_of(occurrence.start, occurrence.end, zone)}",
            f"Calendar: {target.calendar.name} ({target.account.label})",
        ]
        if occurrence.place:
            lines.append(f"Where: {occurrence.place}")
        organiser = occurrence.organiser_named()
        if organiser:
            lines.append(
                f"Organiser: {organiser}" + (" (you)" if occurrence.organiser() in me else "")
            )
        attendees = occurrence.attendees()
        if attendees:
            lines.append("Attendees:")
            for address, name, partstat in attendees:
                who = f"{name} <{address}>" if name and name.lower() != address else address
                you = " (you)" if address in me else ""
                lines.append(f"  {who}{you} - {PARTSTATS.get(partstat.upper(), partstat.lower())}")
        master = target.resource.master
        if master is not None and target.resource.recurring():
            rules = [value for _, value in master.props.get("RRULE", [])]
            lines.append("Repeats: " + ("; ".join(rules) if rules else "on set dates"))
            if occurrence.event is not master:
                lines.append("This occurrence was changed on its own.")
        url = occurrence.event.text("URL")
        if url:
            lines.append(f"Link: {clean(url, 300)}")
        description = occurrence.event.text("DESCRIPTION").strip()
        if description:
            cut = " [...]" if len(description) > DESCRIPTION_MAX else ""
            lines += ["", description[:DESCRIPTION_MAX] + cut]
        lines.append(f"[id: {self.caldav.handles.give(occurrence)}]")
        return "\n".join(lines)


def _clock(value: Any) -> timedelta:
    match = CLOCK_TIME.fullmatch(str(value or "").strip())
    if not match:
        raise ToolError(f"{value!r} is not a time of day - write HH:MM")
    return timedelta(hours=int(match.group(1)), minutes=int(match.group(2)))


def _merge(blocks: Sequence[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(blocks):
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
        day = (day + timedelta(days=1, hours=2)).replace(hour=0)  # safe across a 23-hour day


class FreeBusy(CaldavTool):
    name = "caldav_calendar_free_busy"
    description = (
        "When the person is busy between two times on their CalDAV calendars, and the free gaps "
        "of at least min_minutes inside working hours each day. For 'when am I free Thursday'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "A local time or a date."},
            "end": {"type": "string", "description": "A local time or a date (that whole day)."},
            "calendars": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Calendar names. Default: all of them.",
            },
            "min_minutes": {
                "type": "integer",
                "minimum": 5,
                "description": "Shortest gap worth listing. Default 30.",
            },
            "day_start": {"type": "string", "description": "HH:MM. Default 09:00."},
            "day_end": {"type": "string", "description": "HH:MM. Default 17:00."},
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
        zone = self.caldav.zone()
        begin, finish = window(start, end, zone, days=1)
        busy: list[tuple[datetime, datetime]] = []
        for each in self.caldav.accounts.reading(account):
            found = await self.caldav.discover(each)
            chosen = (
                [c for name in calendars for c in self.caldav.chosen(found, name)]
                if calendars
                else found.calendars
            )
            me = each.me()
            for cal in chosen:
                for resource in await self.caldav.resources(each, cal, begin, finish):
                    for occurrence in resource.occurrences(begin, finish):
                        if (
                            occurrence.all_day
                            or occurrence.transparent
                            or occurrence.answer(me) == "DECLINED"
                        ):
                            continue
                        busy.append(
                            (
                                as_instant(occurrence.start, zone).astimezone(zone),
                                as_instant(occurrence.end, zone).astimezone(zone),
                            )
                        )
        merged = _merge(busy)
        lines = [f"Busy {day_name(begin.date())} to {day_name(finish.date())}:"]
        lines += [f"  {span_of(a, b, zone)}" for a, b in merged if b > begin and a < finish] or [
            "  nothing"
        ]
        minimum = max(5, int(min_minutes or 30))
        gaps = list(_gaps(merged, begin, finish, opens, closes, minimum))
        lines.append(f"Free for at least {minimum} min between {day_start} and {day_end}:")
        lines += [f"  {span_of(a, b, zone)}" for a, b in gaps] or ["  none"]
        return "\n".join(lines)


# -- changing (§6) ----------------------------------------------------------------------------


class Change:
    """A change worked out for its card, and the one request that makes it."""

    def __init__(self, card: str, run: Callable[[], Awaitable[str]]) -> None:
        self.card = card
        self.run = run


class CalendarChange(CaldavTool):
    """R6.1: every change is a confirm card drawn from what is really there, and done
    exactly as it was shown."""

    gated = True
    verb = "change"

    def __init__(self, caldav: Caldav) -> None:
        super().__init__(caldav)
        self._looked: dict[str, tuple[float, Change]] = {}

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        raise NotImplementedError

    async def subject(self, arguments: Mapping[str, Any]) -> Subject | None:
        try:
            change = await self.prepare(arguments)
        except (CredentialError, ToolError, WebError):
            return None  # `run` fails the same way, before anything changes
        key = json.dumps(arguments, sort_keys=True, default=str)
        self._looked[key] = (time.monotonic(), change)
        return Subject(tool=self.name, action=self.verb, summary=change.card, confirm=True)

    async def act(self, **arguments: Any) -> str:
        seen = self._looked.pop(json.dumps(arguments, sort_keys=True, default=str), None)
        if seen is None or time.monotonic() - seen[0] > 900:
            await self.prepare(arguments)  # says why no card could be drawn, when one could not
            raise ToolError(
                "a calendar change has to be shown to the person first; nothing was changed"
            )
        return await seen[1].run()

    def scoped(self, target: Target, scope: Any) -> str:
        """R6.5: `this` or `series` for an event that repeats, with no default."""
        given = str(scope or "")
        if not target.resource.recurring():
            return ""
        if given in ("this", "series"):
            return given
        raise ToolError(
            "this event repeats - say scope: this (only this occurrence) or scope: series "
            "(every occurrence)"
        )

    async def put(self, target: Target, extra: Mapping[str, str] | None = None) -> None:
        """R6.6: the resource as changed, against the version the card read."""
        headers = {"If-Match": target.resource.etag} if target.resource.etag else {}
        await self.caldav.dav.send(
            target.account,
            "PUT",
            target.resource.url,
            body=target.resource.text().encode("utf-8"),
            content_type=ICS,
            headers={**headers, **(extra or {})},
            ok=(200, 201, 204),
        )


def emailed(account: Account, people: Sequence[str], what: str) -> list[str]:
    """R6.4: the line naming who the server will email."""
    return [f"{account.provider} will email {what} to: {', '.join(people)}"] if people else []


def override_for(resource: Resource, occurrence: Occurrence) -> Component:
    """R6.5: the block that is this one occurrence - its override, made from the series
    when there is none yet."""
    if occurrence.event is not resource.master:
        return occurrence.event
    master = resource.master
    assert master is not None
    made = master.copy()
    for name in ("RRULE", "RDATE", "EXDATE"):
        made.drop(name)
    first = master.first("DTSTART")
    made.put("RECURRENCE-ID", *_written(occurrence.start, first, resource.zone))
    made.put("DTSTART", *_written(occurrence.start, first, resource.zone))
    ends = master.first("DTEND") or first
    made.drop("DURATION")
    made.put("DTEND", *_written(occurrence.end, ends, resource.zone))
    resource.vcal.children.append(made)
    return made


class CreateEvent(CalendarChange):
    name = "caldav_calendar_create_event"
    verb = "create"
    description = (
        "Create an event on a CalDAV calendar (iCloud and others). start and end are local "
        "times - 2026-10-02T15:00 - or dates for all day (end is the day after the last). "
        "The calendar server emails invitations to attendees. The person sees everything, and "
        "who will be emailed, and must say yes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "The title."},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "location": {"type": "string"},
            "description": {"type": "string"},
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Email addresses to invite.",
            },
            "recurrence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "RFC 5545 lines, e.g. RRULE:FREQ=WEEKLY;BYDAY=MO.",
            },
            "reminder_minutes": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Alerts, minutes before.",
            },
            "calendar": {"type": "string", "description": "A calendar's name."},
            "account": ACCOUNT,
        },
        "required": ["summary", "start", "end"],
    }

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        account = self.caldav.accounts.writer(str(arguments.get("account", "") or ""))
        found = await self.caldav.discover(account)
        calendar = self.caldav.home_for(found, str(arguments.get("calendar", "") or ""))
        zone = self.caldav.zone()
        title = clean(arguments.get("summary"))
        if not title:
            raise ToolError("say what the event is called")
        start = parse_when(str(arguments.get("start", "")), zone)
        end = parse_when(str(arguments.get("end", "")), zone)
        if isinstance(start, datetime) != isinstance(end, datetime):
            raise ToolError("start and end are both times, or both dates for an all-day event")
        if as_instant(end, zone) <= as_instant(start, zone):
            raise ToolError("the end is not after the start")
        me = account.me()
        people = [p for p in addresses(arguments.get("attendees")) if p not in me]
        repeats = [str(r).strip() for r in arguments.get("recurrence") or () if str(r).strip()]
        for line in repeats:
            name, _, rule = line.partition(":")
            if name.upper() not in ("RRULE", "RDATE", "EXDATE") or not rule:
                raise ToolError(f"{line!r} is not an RRULE:, RDATE: or EXDATE: line")
            if name.upper() == "RRULE":
                try:
                    rrulestr(rule, dtstart=datetime(2026, 1, 1))
                except (ValueError, TypeError):
                    raise ToolError(f"{line!r} is not a rule iCalendar understands") from None
        tzid = ""
        if repeats and isinstance(start, datetime):
            tzid = self.caldav.zone_key()
            if not tzid:
                raise ToolError(
                    "a repeating event needs a time zone, and this machine does not name one - "
                    "set plugins_settings.caldav.time_zone, such as Australia/Sydney"
                )
        uid = f"{uuid.uuid4()}@atlas"
        vcal = Component("VCALENDAR")
        vcal.put("VERSION", "2.0")
        vcal.put("PRODID", "-//Atlas//caldav plugin//EN")
        vcal.put("CALSCALE", "GREGORIAN")
        like: Prop | None = None
        if tzid:
            vcal.children.append(vtimezone(ZoneInfo(tzid), as_instant(start, zone).year))
            like = ({"TZID": tzid}, "")
        event = Component("VEVENT")
        event.put("UID", uid)
        event.put("DTSTAMP", stamp_now())
        event.put("SEQUENCE", "0")
        event.put("DTSTART", *_written(start, like, zone))
        event.put("DTEND", *_written(end, like, zone))
        event.put_text("SUMMARY", title)
        place = str(arguments.get("location") or "").strip()
        notes = str(arguments.get("description") or "").strip()
        if place:
            event.put_text("LOCATION", place)
        if notes:
            event.put_text("DESCRIPTION", notes)
        if people:
            mine = found.addresses[0] if found.addresses else account.address.lower()
            event.put("ORGANIZER", f"mailto:{mine}")
            event.props["ATTENDEE"] = [
                ({"PARTSTAT": "ACCEPTED", "ROLE": "CHAIR"}, f"mailto:{mine}")
            ] + [
                (
                    {"PARTSTAT": "NEEDS-ACTION", "RSVP": "TRUE", "ROLE": "REQ-PARTICIPANT"},
                    f"mailto:{p}",
                )
                for p in people
            ]
        for line in repeats:
            name, _, rule = line.partition(":")
            event.props.setdefault(name.upper(), []).append(({}, rule.strip()))
        minutes = sorted({int(m) for m in arguments.get("reminder_minutes") or ()})
        for before in minutes:
            alarm = Component("VALARM")
            alarm.put("ACTION", "DISPLAY")
            alarm.put_text("DESCRIPTION", title)
            alarm.put("TRIGGER", f"-PT{max(0, before)}M")
            event.children.append(alarm)
        vcal.children.append(event)
        root = Component("ROOT")
        root.children.append(vcal)
        text = serialize(root)
        url = self.caldav.dav.resolve(account, calendar.url.rstrip("/") + "/", f"{uid}.ics")
        when = span_of(start, end, zone)
        lines = [f"Create in {calendar.name} ({account.label})", f"Title: {title}", f"When: {when}"]
        lines += [f"Where: {place}"] if place else []
        lines += [f"Repeats: {'; '.join(repeats)}"] if repeats else []
        lines += [f"Alerts: {', '.join(f'{m} min before' for m in minutes)}"] if minutes else []
        lines += emailed(account, people, "invitations")
        lines += ["", clean(notes, DESCRIPTION_MAX)] if notes else []

        async def run() -> str:
            await self.caldav.dav.send(
                account,
                "PUT",
                url,
                body=text.encode("utf-8"),
                content_type=ICS,
                headers={"If-None-Match": "*"},
                ok=(200, 201, 204),
            )
            invited = (
                f"; {account.provider} emails invitations to {', '.join(people)}" if people else ""
            )
            return f"Created {title}, {when} in {calendar.name}{invited}."

        return Change("\n".join(lines), run)


class UpdateEvent(CalendarChange):
    name = "caldav_calendar_update_event"
    verb = "update"
    description = (
        "Change a CalDAV event - title, times, place, description or attendees (the list given "
        "replaces the old one). Moving start alone keeps the length. For a repeating event, "
        "scope says this occurrence or the whole series. The person sees each change from and "
        "to, and who will be emailed, and must say yes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "event_id": EVENT_ID,
            "summary": {"type": "string", "description": "The title."},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "location": {"type": "string"},
            "description": {"type": "string"},
            "attendees": {"type": "array", "items": {"type": "string"}},
            "scope": SCOPE,
        },
        "required": ["event_id"],
    }

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        target = await self.caldav.target(str(arguments.get("event_id", "")))
        account, resource, occurrence = target.account, target.resource, target.occurrence
        scope = self.scoped(target, arguments.get("scope"))
        me = account.me()
        organiser = occurrence.organiser()
        before = occurrence.others(me)
        if before and organiser and organiser not in me:
            raise ToolError(
                "you are not the organiser of this one - answer it with caldav_calendar_respond"
            )
        zone = self.caldav.zone()
        series = scope == "series"
        master = resource.master
        edit = (
            master
            if series and master is not None
            else (override_for(resource, occurrence) if scope == "this" else occurrence.event)
        )
        title = occurrence.title
        lines = [f"Change {title} ({account.label})"]
        changed = False
        if "summary" in arguments and clean(arguments.get("summary")) != title:
            new_title = clean(arguments.get("summary"))
            if not new_title:
                raise ToolError("an event needs a title")
            edit.put_text("SUMMARY", new_title)
            lines.append(f"Title: {title} -> {new_title}")
            changed = True
        if arguments.get("start") or arguments.get("end"):
            old_start, old_end = occurrence.start, occurrence.end
            start = (
                parse_when(str(arguments["start"]), zone) if arguments.get("start") else old_start
            )
            if arguments.get("end"):
                end = parse_when(str(arguments["end"]), zone)
            elif isinstance(start, datetime) == isinstance(old_start, datetime):
                end = start + (old_end - old_start)  # type: ignore[operator]
            else:
                raise ToolError("say the new end too")
            if isinstance(start, datetime) != isinstance(end, datetime):
                raise ToolError("start and end are both times, or both dates for an all-day event")
            if as_instant(end, zone) <= as_instant(start, zone):
                raise ToolError("the end is not after the start")
            if series:
                if resource.overrides() or (master is not None and master.first("EXDATE")):
                    raise ToolError(
                        "some occurrences of this series were changed or removed one by one, so "
                        "moving the whole series would strand them - change this occurrence "
                        "(scope: this), or move the series in your calendar app"
                    )
                assert master is not None
                first = master.first("DTSTART")
                assert first is not None
                origin = ical_moment(first[0], first[1], resource.zone)
                if isinstance(origin, datetime) != isinstance(start, datetime):
                    raise ToolError(
                        "a series cannot change between all-day and timed - make a new one"
                    )
                # The series moves by what this occurrence moves by, on the wall clock:
                # an aware time plus a timedelta keeps its zone's hour across a change
                # of daylight saving.
                shift = start - shown(old_start, zone)  # type: ignore[operator]
                new_first: Moment = origin + shift
                edit.put("DTSTART", *_written(new_first, first, zone))
                edit.drop("DURATION")
                edit.put(
                    "DTEND",
                    *_written(new_first + (end - start), master.first("DTEND") or first, zone),  # type: ignore[operator]
                )
            else:
                like_start = edit.first("DTSTART")
                like_end = edit.first("DTEND") or like_start
                if (
                    like_start is not None
                    and isinstance(start, datetime)
                    and like_start[0].get("VALUE", "").upper() == "DATE"
                ):
                    like_start = like_end = None  # all day becomes timed: written in UTC
                edit.put("DTSTART", *_written(start, like_start, zone))
                edit.drop("DURATION")
                edit.put("DTEND", *_written(end, like_end, zone))
            lines.append(
                f"When: {span_of(old_start, old_end, zone)} -> {span_of(start, end, zone)}"
            )
            changed = True
        place = arguments.get("location")
        if place is not None and clean(place) != occurrence.place:
            if clean(place):
                edit.put_text("LOCATION", str(place).strip())
            else:
                edit.drop("LOCATION")
            lines.append(f"Where: {occurrence.place or '(none)'} -> {clean(place) or '(none)'}")
            changed = True
        notes = arguments.get("description")
        if notes is not None:
            if str(notes).strip():
                edit.put_text("DESCRIPTION", str(notes).strip())
            else:
                edit.drop("DESCRIPTION")
            lines.append(f"Description -> {clean(notes, DESCRIPTION_MAX) or '(none)'}")
            changed = True
        after = before
        if "attendees" in arguments:
            after = [p for p in addresses(arguments.get("attendees")) if p not in me]
            if sorted(after) != sorted(before):
                kept = {
                    address_of(value): (params, value)
                    for params, value in edit.props.get("ATTENDEE", [])
                }
                mine = next((kept[a] for a in kept if a in me), None)
                rows = [mine] if mine else []
                rows += [
                    kept.get(
                        p,
                        (
                            {"PARTSTAT": "NEEDS-ACTION", "RSVP": "TRUE", "ROLE": "REQ-PARTICIPANT"},
                            f"mailto:{p}",
                        ),
                    )
                    for p in after
                ]
                if after and not organiser:
                    own = (
                        target.account.found.addresses[0]
                        if target.account.found and target.account.found.addresses
                        else account.address.lower()
                    )
                    edit.put("ORGANIZER", f"mailto:{own}")
                    if mine is None:
                        rows.insert(0, ({"PARTSTAT": "ACCEPTED", "ROLE": "CHAIR"}, f"mailto:{own}"))
                if after:
                    edit.props["ATTENDEE"] = [r for r in rows if r is not None]
                else:
                    edit.drop("ATTENDEE")
                    edit.drop("ORGANIZER")
                added = [p for p in after if p not in before]
                gone = [p for p in before if p not in after]
                lines += [f"Invite: {', '.join(added)}"] if added else []
                lines += [f"Uninvite: {', '.join(gone)}"] if gone else []
                changed = True
        if not changed:
            raise ToolError("nothing to change - say what is different")
        bump(edit)
        lines.append("(the whole series)" if series else "(this occurrence)" if scope else "")
        told = sorted(set(before) | set(after))
        lines += emailed(account, told, "the change")
        card = "\n".join(line for line in lines if line)

        async def run() -> str:
            await self.put(target)
            return (
                f"Changed {title}"
                + (f"; {account.provider} emails {', '.join(told)}" if told else "")
                + "."
            )

        return Change(card, run)


class DeleteEvent(CalendarChange):
    name = "caldav_calendar_delete_event"
    verb = "delete"
    description = (
        "Remove a CalDAV event, or one occurrence of a repeating one (scope). As its organiser "
        "with attendees, it is cancelled and the server emails them. The person sees the event "
        "and who is told, and must say yes."
    )
    parameters = {
        "type": "object",
        "properties": {"event_id": EVENT_ID, "scope": SCOPE},
        "required": ["event_id"],
    }

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        target = await self.caldav.target(str(arguments.get("event_id", "")))
        account, resource, occurrence = target.account, target.resource, target.occurrence
        scope = self.scoped(target, arguments.get("scope"))
        me = account.me()
        organiser = occurrence.organiser()
        people = occurrence.others(me)
        organising = bool(people) and (not organiser or organiser in me)
        guest = bool(organiser) and organiser not in me
        zone = self.caldav.zone()
        verb = "Cancel" if organising else "Delete"
        which = " (this occurrence)" if scope == "this" else " (the whole series)" if scope else ""
        lines = [
            f"{verb} {occurrence.title}{which} ({account.label})",
            f"When: {span_of(occurrence.start, occurrence.end, zone)}",
            f"Calendar: {target.calendar.name}",
        ]
        if organising:
            lines += emailed(account, people, "the cancellation")
        elif guest:
            lines.append(
                f"The organiser, {occurrence.organiser_named()}, is not told - decline it "
                "instead to tell them"
            )
        quiet = {"Schedule-Reply": "F"} if guest else {}
        if scope == "this":
            master = resource.master
            assert master is not None
            params, value = ical_value(_original_start(occurrence), master.first("DTSTART"), zone)
            master.props.setdefault("EXDATE", []).append((params, value))
            resource.vcal.children = [
                c
                for c in resource.vcal.children
                if not (
                    c.kind == "VEVENT"
                    and c is not master
                    and _key_of(c, resource) == occurrence.key
                )
            ]
            bump(master, sequence=not guest)

            async def run() -> str:
                await self.put(target, quiet)
                return (
                    f"Removed {occurrence.title} on "
                    f"{span_of(occurrence.start, occurrence.end, zone)}"
                    + (f"; {account.provider} emails {', '.join(people)}" if organising else "")
                    + "."
                )

            return Change("\n".join(lines), run)

        async def remove() -> str:
            headers = {"If-Match": resource.etag} if resource.etag else {}
            await self.caldav.dav.send(
                account,
                "DELETE",
                resource.url,
                headers={**headers, **quiet},
                ok=(200, 204),
            )
            return (
                f"{'Cancelled' if organising else 'Deleted'} {occurrence.title}"
                + (f"; {account.provider} emails {', '.join(people)}" if organising else "")
                + "."
            )

        return Change("\n".join(lines), remove)


def _key_of(event: Component, resource: Resource) -> str:
    recurrence = event.first("RECURRENCE-ID")
    if recurrence is None:
        return ""
    return occurrence_key(ical_moment(recurrence[0], recurrence[1], resource.zone))


def _original_start(occurrence: Occurrence) -> Moment:
    """When an occurrence was due before anyone moved it - what its key records."""
    key = occurrence.key
    if len(key) == 8:
        return date(int(key[:4]), int(key[4:6]), int(key[6:8]))
    return datetime.strptime(key, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


RESPONSES = {
    "accepted": ("ACCEPTED", "Accept", "Accepted"),
    "declined": ("DECLINED", "Decline", "Declined"),
    "tentative": ("TENTATIVE", "Tentatively accept", "Tentatively accepted"),
}


class Respond(CalendarChange):
    name = "caldav_calendar_respond"
    verb = "respond"
    description = (
        "Answer a CalDAV invitation: accepted, declined or tentative - for a repeating one, "
        "this occurrence or the series. The server tells the organiser. The person must say yes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "event_id": EVENT_ID,
            "response": {"type": "string", "enum": sorted(RESPONSES)},
            "scope": SCOPE,
        },
        "required": ["event_id", "response"],
    }

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        response = str(arguments.get("response") or "")
        if response not in RESPONSES:
            raise ToolError(f"response is one of {', '.join(sorted(RESPONSES))}")
        target = await self.caldav.target(str(arguments.get("event_id", "")))
        account, resource, occurrence = target.account, target.resource, target.occurrence
        me = account.me()
        organiser = occurrence.organiser()
        if not organiser or organiser in me:
            raise ToolError("you organise this one - there is no invitation to answer")
        if occurrence.answer(me) == "":
            raise ToolError("you are not invited to this event, so there is nothing to answer")
        scope = self.scoped(target, arguments.get("scope"))
        master = resource.master
        edit = (
            master
            if scope == "series" and master is not None
            else override_for(resource, occurrence)
            if scope == "this"
            else occurrence.event
        )
        partstat, verb, done = RESPONSES[response]
        rows = []
        for params, value in edit.props.get("ATTENDEE", []):
            if address_of(value) in me:
                params = {k: v for k, v in params.items() if k != "RSVP"}
                params["PARTSTAT"] = partstat
            rows.append((params, value))
        edit.props["ATTENDEE"] = rows
        bump(edit, sequence=False)
        named = occurrence.organiser_named()
        which = " (this occurrence)" if scope == "this" else " (the whole series)" if scope else ""
        lines = [
            f"{verb}: {occurrence.title}{which} ({account.label})",
            f"When: {span_of(occurrence.start, occurrence.end, self.caldav.zone())}",
            f"From: {named}",
            f"{account.provider} will email your answer to the organiser, {named}",
        ]

        async def run() -> str:
            await self.put(target)
            return f"{done} {occurrence.title}; {account.provider} tells {named}."

        return Change("\n".join(lines), run)


TOOLS = (
    ListCalendars,
    ListEvents,
    GetEvent,
    FreeBusy,
    CreateEvent,
    UpdateEvent,
    DeleteEvent,
    Respond,
)


class CaldavPlugin(Plugin):
    name = PLUGIN
    description = "CalDAV calendars - iCloud, Fastmail, Yahoo, Nextcloud: read, change with a yes."

    def register(self, ctx: PluginContext) -> None:
        caldav = Caldav(ctx)
        for tool in TOOLS:
            ctx.register_tool(tool(caldav), toolset="Calendar (CalDAV)")
