"""Google Contacts: a login and six tools, over the People API v1.

Every tool is `trusted_only` - offered only in a session an owner holds - and
`untrusted`, because a contact's name and notes are whatever whoever saved it
wrote, and an "other contact" is whatever a stranger put in a From line.
Looking someone up is free. Making a contact, changing one and labelling one
are ordinary cards: a new contact's card shows every field, a change's card
shows each field before and after.

Nothing is deleted. Deleting a contact is done at contacts.google.com.
"""

from __future__ import annotations

import asyncio
import base64
import calendar
import contextlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from atlas.sdk.auth import credentials_in
from atlas.sdk.oauth import LoginContext, OAuthClient, Tokens, connections, grant
from atlas.sdk.plugin_entry import Plugin, PluginContext
from atlas.sdk.runtime import CredentialError, ToolError, assert_active
from atlas.sdk.tool_plugin import Subject, Tool, ToolResult
from atlas.sdk.web import Response, WebError, request

PLUGIN = "google-contacts"
LOGIN = "google"
LOGIN_NAME = f"{PLUGIN}:{LOGIN}"
"""What a person types: `atlas auth login google-contacts:google`."""

DEFAULT_CLIENT_ID = ""
"""Atlas's own Google OAuth client (a Desktop-app client), with the People API
enabled. Empty until it exists; `client_id` in settings is used instead, and with
neither, signing in says what is missing."""

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://people.googleapis.com/v1"
GOOGLE_HOST = "people.googleapis.com"
"""The only host the token is ever sent to."""
SCOPES = (
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/contacts.other.readonly",
    "openid",
    "email",
)
"""Contacts to read and change, "other contacts" - people the person has emailed
but never saved - to find an address by, and the address that labels a connection."""

USER_AGENT = "atlas-google-contacts"
RESPONSE_BYTES = 5_000_000
RETRY_CAP = 10.0
"""A read waits out a 429, a rate-limit 403 or a 5xx once, for at most this long."""

SEARCH_MAX = 30
"""Google caps a search at 30 results a page, and a search has no second page."""
WARM_FOR = 300.0
"""How long a warm-up keeps search's cache fresh enough, by this plugin's reckoning.
Google asks for an empty-query request before searching, to refresh a cache the
search reads from; a contact changed elsewhere since may take a few minutes to show."""
WARM_WAIT = 2.0
"""After a warm-up that found the cache cold, one empty result is asked again
after this long - Google's own example waits a few seconds."""
PAGE_SIZE = 1000
PAGES_MAX = 25
"""`connections.list` pages walked at most: 25,000 contacts."""
LABEL_MAX = 50
"""The most contacts one label change carries."""
NAME_MAX = 120
NOTE_MAX = 4000
DAYS_MAX = 366

EMAIL = re.compile(r"[^@\s,;<>()]+@[^@\s,;<>()]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"[+\d\s().\-/]{3,40}")
PHONE_QUERY = re.compile(r"[+\d\s().\-]{6,}")
PERSON_ID = re.compile(r"people/[A-Za-z0-9_\-]+")
OTHER_ID = re.compile(r"otherContacts/[A-Za-z0-9_\-]+")
GROUP_ID = re.compile(r"contactGroups/[A-Za-z0-9_\-]+")
BIRTHDAY = re.compile(r"(?:(\d{4})|-)?-(\d{1,2})-(\d{1,2})")
"""`1986-10-08`, or `--10-08` / `-10-08` for a birthday without its year."""

READ_FIELDS = (
    "names,nicknames,emailAddresses,phoneNumbers,addresses,birthdays,events,organizations,"
    "biographies,memberships,urls,relations"
)
SEARCH_FIELDS = "names,emailAddresses,phoneNumbers,organizations"
OTHER_FIELDS = "names,emailAddresses,phoneNumbers"
"""All that `otherContacts:search` may be asked for, but `metadata`."""
EDIT_FIELDS = (
    "names,emailAddresses,phoneNumbers,addresses,birthdays,organizations,biographies,metadata"
)
DATE_FIELDS = "names,birthdays,events"

ADDABLE_SYSTEM = ("contactGroups/myContacts", "contactGroups/starred")
"""The only system labels Google lets a contact be added to; the others are deprecated."""

NOT_SIGNED_IN = f"not signed in to Google Contacts: atlas auth login {LOGIN_NAME}"
OTHER_CONTACT = (
    "{id} is an other contact - someone the person has emailed but never saved. What "
    "contacts_search showed is all Google keeps about them; contacts_create saves them."
)


def today() -> date:
    """This machine's date - a function so the tests can say which day it is."""
    return date.today()


# -- talking to Google ---------------------------------------------------------


class GoogleError(Exception):
    """A status Google answered with, as a sentence the model can repeat."""

    def __init__(self, status: int, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


def _error_of(response: Response) -> tuple[str, str]:
    """Google's message and its reason: the old `errors[].reason`, else the newer
    `details[].reason` (`ACCESS_TOKEN_SCOPE_INSUFFICIENT`), else the status."""
    try:
        data = json.loads(response.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "", ""
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return str(error or ""), ""
    reasons = [e.get("reason", "") for e in error.get("errors") or () if isinstance(e, dict)]
    reasons += [d.get("reason", "") for d in error.get("details") or () if isinstance(d, dict)]
    reasons = [r for r in reasons if r]
    message = str(error.get("message") or error.get("status") or "")
    return message, str(reasons[0] if reasons else error.get("status") or "")


def _retry_after(response: Response) -> float:
    try:
        return min(max(float(response.header("retry-after") or 1.0), 0.0), RETRY_CAP)
    except ValueError:
        return 1.0


def _transient(status: int, reason: str) -> bool:
    return (
        status == 429
        or status >= 500
        or (
            status == 403
            and reason in ("rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED")
        )
    )


def _explain(status: int, text: str, reason: str) -> str:
    detail = f": {text}" if text else ""
    lowered = text.lower()
    if status == 401:
        return f"Google refused the sign-in (401): sign in again with atlas auth login {LOGIN_NAME}"
    if status == 403 and (
        reason in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT")
        or "insufficient authentication" in lowered
    ):
        return (
            f"a permission was not granted (403{detail}) - sign in again with "
            f"atlas auth login {LOGIN_NAME} and tick every box"
        )
    if status == 403 and (
        reason in ("SERVICE_DISABLED", "accessNotConfigured") or "has not been used" in lowered
    ):
        return (
            "the People API is not enabled in the Google Cloud project behind the client id - "
            "enable it at console.cloud.google.com/apis/library/people.googleapis.com"
        )
    if status == 400 and reason in ("failedPrecondition", "FAILED_PRECONDITION"):
        return (
            "the contact changed since it was read - nothing was changed; ask again to see it "
            "as it is now"
        )
    if status == 404:
        return (
            f"not found (404){detail} - check the id; the contact may have been deleted or merged"
        )
    return f"HTTP {status} from Google Contacts{detail}"


def label_of(connection_id: str) -> str:
    """`google-contacts:work` -> `work`."""
    return connection_id.partition(":")[2] or connection_id


def person_id(value: Any) -> str:
    """The contact a tool is about, as `people/c...` - never a path somebody chose."""
    given = str(value or "").strip()
    if OTHER_ID.fullmatch(given):
        raise ToolError(OTHER_CONTACT.format(id=given))
    if re.fullmatch(r"c?\d+", given):
        given = f"people/{given if given.startswith('c') else 'c' + given}"
    if not PERSON_ID.fullmatch(given) or given == "people/me":
        raise ToolError("say which contact, by the id contacts_search showed (people/c...)")
    return given


def digits(text: Any) -> str:
    return "".join(c for c in str(text or "") if c.isdigit())


def same_number(a: str, b: str) -> bool:
    """Two phone numbers, however written: the last eight digits agree, so a
    country code and a trunk zero do not stop `0412 345 678` matching `+61412345678`."""
    x, y = digits(a), digits(b)
    if len(x) >= 8 and len(y) >= 8:
        return x[-8:] == y[-8:]
    return bool(x) and x == y


class Contacts:
    """What the tools share: the accounts, the requests, search's cache."""

    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.workspace = Path(ctx.workspace)
        self.settings = dict(ctx.settings)
        self._warm: dict[tuple[str, str], float] = {}

    def setting(self, key: str, default: Any) -> Any:
        value = self.settings.get(key)
        return default if value is None else value

    # -- accounts -------------------------------------------------------------

    def accounts(self) -> tuple[str, ...]:
        return connections(PLUGIN, workspace=self.workspace)

    def pick(self, account: str, *, write: bool) -> list[str]:
        """The only account, the named one, or - for a read - all of them. A change
        with more than one account and none named is refused."""
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
        # `bearer` refreshes over the network when the token is due, and the
        # engine under it is synchronous (`oauth.md` §5.4), so it runs off the loop.
        return await asyncio.to_thread(lambda: grant(account, workspace=self.workspace).bearer())

    # -- one request ----------------------------------------------------------

    async def send(
        self,
        account: str,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        retry: bool = True,
    ) -> Response:
        """One request to Google. `path` is under the API. Raises `GoogleError` for a
        status that is not 2xx."""
        url = path if path.startswith("https://") else f"{API}{path}"
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != GOOGLE_HOST:
            # The token goes to the People API and nowhere else.
            raise GoogleError(0, "that is somewhere other than Google - nothing was sent")
        query = {k: v for k, v in (params or {}).items() if v is not None}
        if query:
            url += ("&" if parts.query else "?") + urlencode(query, doseq=True)
        for attempt in range(2):
            token = await self.bearer(account)
            response = await request(
                method,
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                max_bytes=RESPONSE_BYTES,
                timeout=30.0,
                user_agent=USER_AGENT,
            )
            if 200 <= response.status < 300:
                return response
            text, reason = _error_of(response)
            if retry and attempt == 0 and _transient(response.status, reason):
                await asyncio.sleep(_retry_after(response))
                continue
            raise GoogleError(response.status, _explain(response.status, text, reason), reason)
        raise AssertionError("unreachable")  # pragma: no cover

    async def call(self, account: str, method: str, path: str, **kwargs: Any) -> Any:
        """`send`, with the answer decoded as JSON (`{}` for an empty one)."""
        response = await self.send(account, method, path, **kwargs)
        if not response.body:
            return {}
        try:
            data = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoogleError(response.status, f"Google sent something unreadable: {exc}") from None
        return data if isinstance(data, dict) else {}

    # -- what the tools share -------------------------------------------------

    async def person(
        self, account: str, resource: str, fields: str = READ_FIELDS, *, contact_only: bool = False
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"personFields": fields}
        if contact_only:
            # What a change merges into is the saved contact alone - not the
            # profile the person's Google account shows, which is theirs.
            params["sources"] = "READ_SOURCE_TYPE_CONTACT"
        return await self.call(account, "GET", f"/{person_id(resource)}", params=params)

    async def locate(self, resource: str, account: str) -> tuple[str, dict[str, Any]]:
        """Which account has a contact - the named one, or the first that does."""
        resource = person_id(resource)
        found: GoogleError | None = None
        for who in self.pick(account, write=False):
            try:
                return who, await self.person(who, resource)
            except GoogleError as exc:
                if exc.status not in (400, 404):
                    raise
                found = exc
        raise found or ToolError(f"no contact {resource!r}")

    async def warm(self, account: str, endpoint: str, mask: str) -> bool:
        """Google's warm-up before a search: an empty query, which refreshes the
        cache the search reads. True when this call did it."""
        key = (account, endpoint)
        if time.monotonic() - self._warm.get(key, -WARM_FOR) < WARM_FOR:
            return False
        await self.call(
            account, "GET", f"/{endpoint}", params={"query": "", "readMask": mask, "pageSize": 1}
        )
        self._warm[key] = time.monotonic()
        return True

    def cold(self, account: str) -> None:
        """After a change from here, the next search warms up again first."""
        for key in [k for k in self._warm if k[0] == account]:
            del self._warm[key]

    async def search(
        self, account: str, endpoint: str, mask: str, query: str, limit: int
    ) -> list[dict[str, Any]]:
        """`people:searchContacts` or `otherContacts:search`: prefix matches on
        names, emails, phone numbers (and, for contacts, nicknames and organisations)."""
        warmed = await self.warm(account, endpoint, mask)
        params = {"query": query, "readMask": mask, "pageSize": min(limit, SEARCH_MAX)}
        for attempt in range(2):
            data = await self.call(account, "GET", f"/{endpoint}", params=params)
            found = [
                r["person"]
                for r in data.get("results") or ()
                if isinstance(r, dict) and isinstance(r.get("person"), dict)
            ]
            if found or not warmed or attempt:
                return found
            await asyncio.sleep(WARM_WAIT)  # a cache just refreshed may not have filled yet
        return []  # pragma: no cover

    async def everyone(self, account: str, fields: str) -> tuple[list[dict[str, Any]], bool]:
        """Every saved contact, `fields` of each, a page of a thousand at a time. The
        flag says whether `PAGES_MAX` cut it."""
        people: list[dict[str, Any]] = []
        token = ""
        for _ in range(PAGES_MAX):
            params: dict[str, Any] = {"personFields": fields, "pageSize": PAGE_SIZE}
            if token:
                params["pageToken"] = token
            data = await self.call(account, "GET", "/people/me/connections", params=params)
            people += [p for p in data.get("connections") or () if isinstance(p, dict)]
            token = str(data.get("nextPageToken") or "")
            if not token:
                return people, False
        return people, True

    async def groups(self, account: str) -> list[dict[str, Any]]:
        """The labels a person sees: their own, and My Contacts and Starred."""
        data = await self.call(
            account,
            "GET",
            "/contactGroups",
            params={"pageSize": 1000, "groupFields": "name,groupType,memberCount"},
        )
        return [
            g
            for g in data.get("contactGroups") or ()
            if isinstance(g, dict)
            and (
                g.get("groupType") == "USER_CONTACT_GROUP"
                or g.get("resourceName") in ADDABLE_SYSTEM
            )
        ]

    def record(self, event: str, detail: str, arguments: Mapping[str, Any]) -> None:
        """What changed, on the trail - which contact and which fields, never the values."""
        with contextlib.suppress(Exception):
            self.ctx.audit(event, detail, arguments=dict(arguments))


# -- showing things ------------------------------------------------------------


def clean(text: Any, limit: int = NAME_MAX) -> str:
    """One line of someone else's words, capped."""
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def name_of(person: Mapping[str, Any]) -> str:
    for name in person.get("names") or ():
        if isinstance(name, dict):
            shown = name.get("displayName") or name.get("unstructuredName")
            shown = (
                shown
                or " ".join(str(name.get(k) or "") for k in ("givenName", "familyName")).strip()
            )
            if shown:
                return clean(shown)
    for email in person.get("emailAddresses") or ():
        if isinstance(email, dict) and email.get("value"):
            return clean(email["value"])
    return "(no name)"


def _kind(item: Mapping[str, Any]) -> str:
    return clean(item.get("type") or item.get("formattedType"), 40)


def item_line(item: Mapping[str, Any], value: str) -> str:
    kind = _kind(item)
    return f"{value} ({kind})" if kind else value


def address_of(item: Mapping[str, Any]) -> str:
    if item.get("formattedValue"):
        return clean(item["formattedValue"], 300)
    parts = ("streetAddress", "extendedAddress", "city", "region", "postalCode", "country")
    return clean(", ".join(str(item[k]) for k in parts if item.get(k)), 300)


def items(person: Mapping[str, Any], field: str) -> list[dict[str, Any]]:
    return [i for i in person.get(field) or () if isinstance(i, dict)]


def emails_of(person: Mapping[str, Any]) -> list[str]:
    return [
        item_line(i, clean(i["value"])) for i in items(person, "emailAddresses") if i.get("value")
    ]


def phones_of(person: Mapping[str, Any]) -> list[str]:
    return [
        item_line(i, clean(i["value"], 40)) for i in items(person, "phoneNumbers") if i.get("value")
    ]


def addresses_of(person: Mapping[str, Any]) -> list[str]:
    return [item_line(i, address_of(i)) for i in items(person, "addresses") if address_of(i)]


def organisation_of(person: Mapping[str, Any]) -> str:
    for org in items(person, "organizations"):
        shown = " · ".join(clean(org[k]) for k in ("name", "title", "department") if org.get(k))
        if shown:
            return shown
    return ""


def notes_of(person: Mapping[str, Any]) -> str:
    for bio in items(person, "biographies"):
        if bio.get("value"):
            return str(bio["value"])
    return ""


def date_text(when: Mapping[str, Any] | None) -> str:
    """`8 Oct 1986`, or `8 Oct` when the year is not known."""
    if not isinstance(when, dict):
        return ""
    try:
        month, day = int(when.get("month") or 0), int(when.get("day") or 0)
    except (TypeError, ValueError):
        return ""
    if not 1 <= month <= 12 or not day:
        return ""
    year = when.get("year")
    return f"{day} {calendar.month_abbr[month]}" + (f" {year}" if year else "")


def birthday_of(person: Mapping[str, Any]) -> str:
    for birthday in items(person, "birthdays"):
        shown = date_text(birthday.get("date")) or clean(birthday.get("text"), 40)
        if shown:
            return shown
    return ""


def tag(resource: str, account: str = "") -> str:
    return f"[id: {resource}" + (f" · {label_of(account)}" if account else "") + "]"


def person_line(person: Mapping[str, Any], account: str = "") -> str:
    """One line a person can recognise a contact by, ending in its id."""
    parts = [name_of(person)]
    emails = [clean(i["value"]) for i in items(person, "emailAddresses") if i.get("value")]
    phones = [clean(i["value"], 40) for i in items(person, "phoneNumbers") if i.get("value")]
    if emails:
        parts.append(", ".join(emails[:3]))
    if phones:
        parts.append(", ".join(phones[:3]))
    organisation = organisation_of(person)
    if organisation:
        parts.append(organisation)
    resource = str(person.get("resourceName", ""))
    if resource.startswith("otherContacts/"):
        parts.append("emailed, not saved")
    return " · ".join(parts) + f"  {tag(resource, account)}"


def listed(values: Sequence[str]) -> str:
    return ", ".join(values) if values else "(none)"


# -- the tools -----------------------------------------------------------------


ACCOUNT = {
    "type": "string",
    "description": (
        "Which signed-in Google account, by its label. Needed for a change when more than "
        "one is signed in."
    ),
}
CONTACT_ID = {
    "type": "string",
    "description": "The contact's id, as contacts_search showed it (people/c...).",
}


class ContactsTool(Tool):
    """What all six share: owner-only, marked untrusted, failures as sentences."""

    trusted_only = True
    untrusted = True

    def __init__(self, contacts: Contacts) -> None:
        self.contacts = contacts

    async def run(self, **arguments: Any) -> ToolResult:
        try:
            assert_active()
            said = await self.act(**arguments)
            return said if isinstance(said, ToolResult) else ToolResult.ok(said)
        except (CredentialError, ToolError, GoogleError, WebError) as exc:
            return ToolResult.error(str(exc))

    async def act(self, *args: Any, **arguments: Any) -> Any:
        raise NotImplementedError


class Search(ContactsTool):
    name = "contacts_search"
    description = (
        "Find people in Google Contacts by name, nickname, email, phone number or company - "
        "the person's saved contacts and, unless other is false, the people they have emailed "
        "but never saved. Matches the start of words: 'sam' finds Samantha. Use it to turn a "
        "name into an email address or a phone number; each line ends with the contact's id "
        "for contacts_get and contacts_update."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "A name, email, phone number or company."},
            "other": {
                "type": "boolean",
                "description": "Also search people emailed but never saved. Default true.",
            },
            "max_results": {"type": "integer", "minimum": 1, "maximum": SEARCH_MAX},
            "account": ACCOUNT,
        },
        "required": ["query"],
    }

    async def act(
        self, query: str, other: bool = True, max_results: int = 0, account: str = ""
    ) -> str:
        words = clean(query, 200)
        if not words:
            raise ToolError("say who to look for - a name, an email or a phone number")
        limit = min(
            max(int(max_results or self.contacts.setting("max_results", 10)), 1), SEARCH_MAX
        )
        phone = bool(PHONE_QUERY.fullmatch(words)) and len(digits(words)) >= 6
        accounts = self.contacts.pick(account, write=False)
        several = len(accounts) > 1
        lines: list[str] = []
        notes: list[str] = []
        for who in accounts:
            found = await self.contacts.search(
                who, "people:searchContacts", SEARCH_FIELDS, words, limit
            )
            if not found and phone:
                # A number written differently from how it was saved: compare digits.
                people, _ = await self.contacts.everyone(who, SEARCH_FIELDS)
                found = [
                    p
                    for p in people
                    if any(same_number(words, i.get("value", "")) for i in items(p, "phoneNumbers"))
                ][:limit]
            if other:
                try:
                    found += await self.contacts.search(
                        who, "otherContacts:search", OTHER_FIELDS, words, limit
                    )
                except GoogleError as exc:
                    if exc.status != 403:
                        raise
                    notes.append(f"(people emailed but not saved were not searched: {exc})")
            lines += [person_line(p, who if several else "") for p in found]
        tail = "".join(f"\n{n}" for n in dict.fromkeys(notes))
        if not lines:
            return f"Nobody in Google Contacts matches {words!r}.{tail}"
        return f"{len(lines)} match(es) for {words!r}:\n" + "\n".join(lines) + tail


class Get(ContactsTool):
    name = "contacts_get"
    description = (
        "Everything about one contact: names, emails, phone numbers, addresses, birthday and "
        "other dates, organisation, websites, relations, labels and notes."
    )
    parameters = {
        "type": "object",
        "properties": {"contact": CONTACT_ID, "account": ACCOUNT},
        "required": ["contact"],
    }

    async def act(self, contact: str, account: str = "") -> str:
        who, person = await self.contacts.locate(contact, account)
        lines = [name_of(person)]
        nicknames = [clean(n["value"]) for n in items(person, "nicknames") if n.get("value")]
        if nicknames:
            lines[0] += f" (also {', '.join(nicknames)})"
        for label, values in (
            ("Emails", emails_of(person)),
            ("Phones", phones_of(person)),
            ("Addresses", addresses_of(person)),
        ):
            if values:
                lines.append(f"{label}: " + "; ".join(values))
        birthday = birthday_of(person)
        if birthday:
            lines.append(f"Birthday: {birthday}")
        events = [
            f"{_kind(e) or 'date'} {date_text(e.get('date'))}"
            for e in items(person, "events")
            if date_text(e.get("date"))
        ]
        if events:
            lines.append("Dates: " + "; ".join(events))
        organisation = organisation_of(person)
        if organisation:
            lines.append(f"Organisation: {organisation}")
        urls = [
            item_line(u, clean(u["value"], 300)) for u in items(person, "urls") if u.get("value")
        ]
        if urls:
            lines.append("Websites: " + "; ".join(urls))
        relations = [
            item_line(r, clean(r["person"])) for r in items(person, "relations") if r.get("person")
        ]
        if relations:
            lines.append("Relations: " + "; ".join(relations))
        member_of = [
            str((m.get("contactGroupMembership") or {}).get("contactGroupResourceName") or "")
            for m in items(person, "memberships")
        ]
        if any(member_of):
            try:
                names = {
                    str(g.get("resourceName")): clean(g.get("formattedName") or g.get("name"))
                    for g in await self.contacts.groups(who)
                }
                shown = [names[m] for m in member_of if m in names]
            except GoogleError:
                shown = []
            if shown:
                lines.append("Labels: " + ", ".join(shown))
        notes = notes_of(person)
        if notes:
            lines.append("Notes: " + clean(notes, NOTE_MAX))
        resource = str(person.get("resourceName") or person_id(contact))
        lines.append(tag(resource, who if account or len(self.contacts.accounts()) > 1 else ""))
        return "\n".join(lines)


def _next(month: int, day: int, start: date) -> date:
    """The next time a day of the year comes round, on or after `start`. The 29th of
    February comes round on the 28th in a year without one."""
    for year in (start.year, start.year + 1):
        last = calendar.monthrange(year, month)[1]
        moment = date(year, month, min(day, last))
        if moment >= start:
            return moment
    raise AssertionError("unreachable")  # pragma: no cover


def _in(days: int) -> str:
    return "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"


class Birthdays(ContactsTool):
    name = "contacts_birthdays"
    description = (
        "Whose birthday is coming up: every contact's birthday in the next `days` days, soonest "
        "first, with the age they turn when the year is known - and anniversaries and other "
        "dates saved on a contact unless events is false."
    )
    parameters = {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "minimum": 1, "maximum": DAYS_MAX},
            "events": {
                "type": "boolean",
                "description": "Include anniversaries and other saved dates. Default true.",
            },
            "account": ACCOUNT,
        },
        "required": [],
    }

    async def act(self, days: int = 0, events: bool = True, account: str = "") -> str:
        window = min(max(int(days or self.contacts.setting("birthday_days", 30)), 1), DAYS_MAX)
        start = today()
        accounts = self.contacts.pick(account, write=False)
        several = len(accounts) > 1
        found: list[tuple[date, str, str]] = []
        cut = False
        for who in accounts:
            people, more = await self.contacts.everyone(who, DATE_FIELDS)
            cut = cut or more
            for person in people:
                for what, when in self.dates(person, events):
                    try:
                        month, day = int(when.get("month") or 0), int(when.get("day") or 0)
                        coming = _next(month, day, start)
                    except (TypeError, ValueError):
                        continue
                    ahead = (coming - start).days
                    if ahead > window:
                        continue
                    year = when.get("year")
                    if isinstance(year, int) and 0 < year < coming.year:
                        age = coming.year - year
                        what += f", turns {age}" if what == "birthday" else f", {age} years"
                    if (month, day) == (2, 29) and coming.day == 28:
                        what += " (29 Feb, kept on the 28th this year)"
                    line = (
                        f"{coming.strftime('%a')} {coming.day} {coming.strftime('%b')} "
                        f"({_in(ahead)}) · {name_of(person)} · {what}  "
                        f"{tag(str(person.get('resourceName', '')), who if several else '')}"
                    )
                    found.append((coming, name_of(person).lower(), line))
        end = start + timedelta(days=window)
        tail = f"\n(only the first {PAGES_MAX * PAGE_SIZE} contacts were looked at)" if cut else ""
        if not found:
            return f"No birthdays or dates in the next {window} days (to {end:%d %b}).{tail}"
        found.sort()
        lines = list(dict.fromkeys(line for _, _, line in found))
        return f"In the next {window} days:\n" + "\n".join(lines) + tail

    @staticmethod
    def dates(person: Mapping[str, Any], events: bool) -> list[tuple[str, dict[str, Any]]]:
        """Each birthday once - a contact may carry the same one from two sources -
        and, with `events`, each anniversary and other saved date."""
        out: list[tuple[str, dict[str, Any]]] = []
        seen: set[tuple[Any, Any]] = set()
        for birthday in items(person, "birthdays"):
            when = birthday.get("date")
            if isinstance(when, dict) and (when.get("month"), when.get("day")) not in seen:
                seen.add((when.get("month"), when.get("day")))
                out.append(("birthday", when))
        if events:
            for event in items(person, "events"):
                when = event.get("date")
                if isinstance(when, dict):
                    out.append((_kind(event) or "date", when))
        return out


# -- changing things -----------------------------------------------------------


class Plan:
    """What a change will do, worked out once for the card and used again to run."""

    def __init__(self, account: str, card: str) -> None:
        self.account = account
        self.card = card
        self.resource = ""
        self.body: dict[str, Any] = {}
        self.fields: list[str] = []
        self.group = ""
        self.group_name = ""
        self.members: list[str] = []


def _key(arguments: Mapping[str, Any]) -> str:
    return json.dumps(arguments, sort_keys=True, default=str)


class ChangeTool(ContactsTool):
    """A change: gated, and carded from what is really there."""

    gated = True
    action = ""

    def __init__(self, contacts: Contacts) -> None:
        super().__init__(contacts)
        self._looked: dict[str, tuple[float, Plan]] = {}
        """What `subject` worked out, keyed by the call's arguments, so `run` does
        what the card said - against the version of the contact the card showed -
        and not a later look."""

    async def subject(self, arguments: Mapping[str, Any]) -> Subject | None:
        try:
            plan = await self.plan(arguments)
        except (CredentialError, ToolError):
            return None  # `run` fails the same way, before it changes anything
        except (GoogleError, WebError) as exc:
            # Still a card, and the strict one: a look that failed is no reason
            # to skip the question.
            summary = f"{self.action} in Google Contacts (could not look first: {exc})"
            return Subject(tool=self.name, action=self.action, summary=summary, confirm=True)
        self._looked[_key(arguments)] = (time.monotonic(), plan)
        return Subject(tool=self.name, action=self.action, summary=plan.card)

    async def act(self, **arguments: Any) -> Any:
        seen = self._looked.pop(_key(arguments), None)
        plan = seen[1] if seen is not None and time.monotonic() - seen[0] < 900 else None
        plan = plan or await self.plan(arguments)
        try:
            return await self.carry_out(plan, arguments)
        finally:
            self.contacts.cold(plan.account)

    async def plan(self, arguments: Mapping[str, Any]) -> Plan:
        raise NotImplementedError

    async def carry_out(self, plan: Plan, arguments: Mapping[str, Any]) -> str:
        raise NotImplementedError


ITEM = {
    "type": "object",
    "properties": {
        "value": {"type": "string"},
        "type": {"type": "string", "description": "home, work, mobile, other, or any word."},
    },
    "required": ["value"],
}
ADDRESS = {
    "type": "object",
    "properties": {
        "street": {"type": "string"},
        "city": {"type": "string"},
        "region": {"type": "string", "description": "State, county or province."},
        "postcode": {"type": "string"},
        "country": {"type": "string"},
        "formatted": {
            "type": "string",
            "description": "The whole address as one line, instead of the parts.",
        },
        "type": {"type": "string", "description": "home, work or other."},
    },
}
FIELDS = {
    "given_name": {"type": "string"},
    "family_name": {"type": "string"},
    "emails": {"type": "array", "items": ITEM},
    "phones": {"type": "array", "items": ITEM},
    "addresses": {"type": "array", "items": ADDRESS},
    "birthday": {"type": "string", "description": "YYYY-MM-DD, or --MM-DD without the year."},
    "company": {"type": "string"},
    "job_title": {"type": "string"},
    "notes": {"type": "string"},
}
LISTS = {"emails": "emailAddresses", "phones": "phoneNumbers", "addresses": "addresses"}
REPLACEABLE = ("emails", "phones", "addresses", "notes")


def _typed(item: Any) -> tuple[str, str]:
    if isinstance(item, Mapping):
        return str(item.get("value") or "").strip(), clean(item.get("type"), 40)
    return str(item or "").strip(), ""


def email_items(given: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in given or ():
        value, kind = _typed(entry)
        if not EMAIL.fullmatch(value):
            raise ToolError(f"{value!r} is not an email address")
        out.append({"value": value, **({"type": kind} if kind else {})})
    return out


def phone_items(given: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in given or ():
        value, kind = _typed(entry)
        if not PHONE.fullmatch(value) or len(digits(value)) < 3:
            raise ToolError(f"{value!r} is not a phone number")
        out.append({"value": clean(value, 40), **({"type": kind} if kind else {})})
    return out


ADDRESS_PARTS = {
    "street": "streetAddress",
    "city": "city",
    "region": "region",
    "postcode": "postalCode",
    "country": "country",
}


def address_items(given: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in given or ():
        if isinstance(entry, str):
            entry = {"formatted": entry}
        if not isinstance(entry, Mapping):
            raise ToolError("an address is its parts - street, city, region, postcode, country")
        made = {
            google: clean(entry[ours], 200)
            for ours, google in ADDRESS_PARTS.items()
            if clean(entry.get(ours), 200)
        }
        if not made and clean(entry.get("formatted"), 300):
            made["formattedValue"] = clean(entry.get("formatted"), 300)
        if not made:
            raise ToolError("an address needs at least one part")
        kind = clean(entry.get("type"), 40)
        out.append({**made, **({"type": kind} if kind else {})})
    return out


def birthday_item(given: Any) -> dict[str, Any] | None:
    text = str(given or "").strip()
    if not text:
        return None
    match = BIRTHDAY.fullmatch(text)
    if not match:
        raise ToolError(f"{text!r} is not a date - write YYYY-MM-DD, or --MM-DD without the year")
    year, month, day = match.group(1), int(match.group(2)), int(match.group(3))
    try:
        date(int(year) if year else 2000, month, day)  # 2000: the 29th of February exists
    except ValueError:
        raise ToolError(f"{text!r} is not a date that exists") from None
    when: dict[str, Any] = {"month": month, "day": day}
    if year:
        when["year"] = int(year)
    return {"date": when}


def notes_item(given: Any, workspace: Path) -> str:
    text = str(given or "").strip()
    if len(text) > NOTE_MAX:
        raise ToolError(f"notes of {len(text)} characters - at most {NOTE_MAX}")
    found = credentials_in(text, workspace=workspace) if text else []
    if found:
        raise ToolError(
            f"refused: the notes contain what looks like a credential ({', '.join(found)})"
        )
    return text


def _same(field: str, a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    if field == "phoneNumbers":
        return same_number(str(a.get("value", "")), str(b.get("value", "")))
    if field == "addresses":
        return address_of(a).lower() == address_of(b).lower()
    return str(a.get("value", "")).strip().lower() == str(b.get("value", "")).strip().lower()


def merge(
    field: str, existing: list[dict[str, Any]], added: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """What a list becomes: everything already there, kept as Google sent it, with
    what is new added - one already there given a new type if one was said."""
    out = [dict(e) for e in existing]
    for new in added:
        match = next((e for e in out if _same(field, e, new)), None)
        if match is None:
            out.append(new)
        elif new.get("type") and new["type"] != match.get("type"):
            match["type"] = new["type"]
            match.pop("formattedType", None)
    return out


SHOW: dict[str, Callable[[Mapping[str, Any]], list[str]]] = {
    "emailAddresses": emails_of,
    "phoneNumbers": phones_of,
    "addresses": addresses_of,
}
LABELS = {"emailAddresses": "emails", "phoneNumbers": "phones", "addresses": "addresses"}


class Create(ChangeTool):
    name = "contacts_create"
    action = "create"
    description = (
        "Save a new contact in Google Contacts: a name, emails, phone numbers, addresses, a "
        "birthday, a company and job title, notes - whatever is known. The person is shown "
        "every field, and any saved contact with the same email or number, before it is made."
    )
    parameters = {
        "type": "object",
        "properties": {**FIELDS, "account": ACCOUNT},
        "required": [],
    }

    async def plan(self, arguments: Mapping[str, Any]) -> Plan:
        given, family = clean(arguments.get("given_name")), clean(arguments.get("family_name"))
        emails = email_items(arguments.get("emails"))
        phones = phone_items(arguments.get("phones"))
        addresses = address_items(arguments.get("addresses"))
        birthday = birthday_item(arguments.get("birthday"))
        company, title = clean(arguments.get("company")), clean(arguments.get("job_title"))
        notes = notes_item(arguments.get("notes"), self.contacts.workspace)
        if not (given or family or emails or phones or company):
            raise ToolError("a new contact needs a name, an email, a phone number or a company")
        account = self.contacts.pick(str(arguments.get("account", "") or ""), write=True)[0]
        body: dict[str, Any] = {}
        if given or family:
            body["names"] = [{k: v for k, v in (("givenName", given), ("familyName", family)) if v}]
        for field, values in (
            ("emailAddresses", emails),
            ("phoneNumbers", phones),
            ("addresses", addresses),
        ):
            if values:
                body[field] = values
        if birthday:
            body["birthdays"] = [birthday]
        if company or title:
            body["organizations"] = [{k: v for k, v in (("name", company), ("title", title)) if v}]
        if notes:
            body["biographies"] = [{"value": notes, "contentType": "TEXT_PLAIN"}]
        where = f" · {label_of(account)}" if len(self.contacts.accounts()) > 1 else ""
        lines = [f"Create a contact in Google Contacts{where}"]
        lines.append(f"  name: {name_of(body) if body.get('names') else '(none)'}")
        for field, label in LABELS.items():
            if body.get(field):
                lines.append(f"  {label}: " + "; ".join(SHOW[field](body)))
        if birthday:
            lines.append(f"  birthday: {date_text(birthday['date'])}")
        if company or title:
            lines.append(f"  organisation: {organisation_of(body)}")
        if notes:
            lines.append(f"  notes: {clean(notes, 500)}")
        lines += await self.twins(account, emails, phones)
        plan = Plan(account, "\n".join(lines))
        plan.body = body
        return plan

    async def twins(
        self, account: str, emails: list[dict[str, Any]], phones: list[dict[str, Any]]
    ) -> list[str]:
        """Saved contacts that already carry one of the new addresses or numbers."""
        seen: dict[str, str] = {}
        for value in [e["value"] for e in emails][:2] + [p["value"] for p in phones][:2]:
            try:
                found = await self.contacts.search(
                    account, "people:searchContacts", SEARCH_FIELDS, value, 5
                )
            except GoogleError:
                return []  # the card is still drawn; the question is still asked
            for person in found:
                if any(
                    _same(f, i, {"value": value})
                    for f in ("emailAddresses", "phoneNumbers")
                    for i in items(person, f)
                ):
                    seen.setdefault(str(person.get("resourceName")), person_line(person))
        return [f"  already saved: {line}" for line in seen.values()]

    async def carry_out(self, plan: Plan, arguments: Mapping[str, Any]) -> str:
        made = await self.contacts.call(
            plan.account,
            "POST",
            "/people:createContact",
            params={"personFields": SEARCH_FIELDS},
            body=plan.body,
            retry=False,
        )
        self.contacts.record(
            "contacts_created",
            str(made.get("resourceName", "")),
            {"account": label_of(plan.account), "fields": sorted(plan.body)},
        )
        return f"Saved: {person_line(made)}"


class Update(ChangeTool):
    name = "contacts_update"
    action = "update"
    description = (
        "Add to or change a saved contact: a new email, phone number or address is added to "
        "the ones already there (one already there gets the new type), a name, company, job "
        "title or birthday is changed, and notes are added to the end. Name a list in replace "
        "to make it exactly what is given instead. The person is shown each field before and "
        "after."
    )
    parameters = {
        "type": "object",
        "properties": {
            "contact": CONTACT_ID,
            **FIELDS,
            "replace": {
                "type": "array",
                "items": {"type": "string", "enum": list(REPLACEABLE)},
                "description": (
                    "Fields to replace outright rather than add to - only when the person asked "
                    "for the old ones to go."
                ),
            },
            "account": ACCOUNT,
        },
        "required": ["contact"],
    }

    async def plan(self, arguments: Mapping[str, Any]) -> Plan:
        resource = person_id(arguments.get("contact"))
        replace = {str(r) for r in arguments.get("replace") or ()}
        unknown = replace - set(REPLACEABLE)
        if unknown:
            raise ToolError(f"replace takes {', '.join(REPLACEABLE)}")
        given_name = clean(arguments.get("given_name"))
        family_name = clean(arguments.get("family_name"))
        added = {
            "emailAddresses": email_items(arguments.get("emails")),
            "phoneNumbers": phone_items(arguments.get("phones")),
            "addresses": address_items(arguments.get("addresses")),
        }
        birthday = birthday_item(arguments.get("birthday"))
        company, title = clean(arguments.get("company")), clean(arguments.get("job_title"))
        notes = notes_item(arguments.get("notes"), self.contacts.workspace)
        account = self.contacts.pick(str(arguments.get("account", "") or ""), write=True)[0]
        person = await self.contacts.person(account, resource, EDIT_FIELDS, contact_only=True)
        sources = [
            s
            for s in (person.get("metadata") or {}).get("sources") or ()
            if isinstance(s, dict) and s.get("type") == "CONTACT"
        ]
        if not sources:
            raise ToolError(f"{resource} is not a saved contact, so there is nothing to change")
        body: dict[str, Any] = {
            "resourceName": resource,
            "etag": person.get("etag", ""),
            "metadata": {"sources": sources},
        }
        changes: list[str] = []
        fields: list[str] = []

        def change(field: str, label: str, before: str, after: str, value: Any) -> None:
            if before != after:
                body[field] = value
                fields.append(field)
                changes.append(f"  {label}: {before or '(none)'} → {after or '(none)'}")

        if given_name or family_name:
            old = (items(person, "names") or [{}])[0]
            new = {
                k: v
                for k, v in old.items()
                if k
                in ("givenName", "familyName", "middleName", "honorificPrefix", "honorificSuffix")
            }
            if given_name:
                new["givenName"] = given_name
            if family_name:
                new["familyName"] = family_name
            shown = " ".join(
                str(new.get(k) or "")
                for k in (
                    "honorificPrefix",
                    "givenName",
                    "middleName",
                    "familyName",
                    "honorificSuffix",
                )
            )
            change("names", "name", name_of(person) if old else "", clean(shown), [new])
        for field, values in added.items():
            label = LABELS[field]
            if not values and label not in replace:
                continue
            existing = items(person, field)
            after = values if label in replace else merge(field, existing, values)
            change(
                field,
                label,
                listed(SHOW[field]({field: existing})),
                listed(SHOW[field]({field: after})),
                after,
            )
        if birthday:
            change(
                "birthdays",
                "birthday",
                birthday_of(person),
                date_text(birthday["date"]),
                [birthday],
            )
        if company or title:
            old = (items(person, "organizations") or [{}])[0]
            new = {k: v for k, v in old.items() if k not in ("metadata", "formattedType")}
            if company:
                new["name"] = company
            if title:
                new["title"] = title
            rest = items(person, "organizations")[1:]
            change(
                "organizations",
                "organisation",
                organisation_of(person),
                organisation_of({"organizations": [new]}),
                [new, *rest],
            )
        if notes or "notes" in replace:
            before = notes_of(person)
            after = notes if "notes" in replace or not before else f"{before}\n{notes}"
            if len(after) > NOTE_MAX:
                raise ToolError(f"the notes would be {len(after)} characters - at most {NOTE_MAX}")
            change(
                "biographies",
                "notes",
                clean(before, 500),
                clean(after, 500),
                [{"value": after, "contentType": "TEXT_PLAIN"}] if after else [],
            )
        if not changes:
            raise ToolError(f"nothing to change - {name_of(person)} already has all of that")
        where = f" · {label_of(account)}" if len(self.contacts.accounts()) > 1 else ""
        plan = Plan(
            account, "\n".join([f'Update the contact "{name_of(person)}"{where}', *changes])
        )
        plan.resource, plan.body, plan.fields = resource, body, fields
        return plan

    async def carry_out(self, plan: Plan, arguments: Mapping[str, Any]) -> str:
        done = await self.contacts.call(
            plan.account,
            "PATCH",
            f"/{plan.resource}:updateContact",
            params={"updatePersonFields": ",".join(plan.fields), "personFields": SEARCH_FIELDS},
            body=plan.body,
            retry=False,
        )
        self.contacts.record(
            "contacts_updated",
            plan.resource,
            {"account": label_of(plan.account), "fields": plan.fields},
        )
        return f"Updated: {person_line(done or {'resourceName': plan.resource})}"


GROUP_ACTIONS = ("list", "add", "remove")


class Groups(ChangeTool):
    name = "contacts_groups"
    action = "label"
    description = (
        "Contact labels (groups): list them, or add contacts to a label or take them off one, "
        "by the contacts' ids and the label's name. Taking a contact off a label keeps the "
        "contact. Listing is free; adding and removing are shown to the person first."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(GROUP_ACTIONS)},
            "label": {
                "type": "string",
                "description": "For add and remove: the label's name, or its contactGroups/ id.",
            },
            "contacts": {
                "type": "array",
                "items": {"type": "string"},
                "description": f"For add and remove: contact ids - at most {LABEL_MAX}.",
            },
            "account": ACCOUNT,
        },
        "required": ["action"],
    }

    async def subject(self, arguments: Mapping[str, Any]) -> Subject | None:
        if str(arguments.get("action", "")) == "list":
            return None  # reading the labels changes nothing
        return await super().subject(arguments)

    async def act(self, **arguments: Any) -> Any:
        if str(arguments.get("action", "")) == "list":
            return await self.listing(str(arguments.get("account", "") or ""))
        return await super().act(**arguments)

    async def listing(self, account: str) -> str:
        accounts = self.contacts.pick(account, write=False)
        several = len(accounts) > 1
        lines: list[str] = []
        for who in accounts:
            for group in await self.contacts.groups(who):
                count = int(group.get("memberCount") or 0)
                lines.append(
                    f"{clean(group.get('formattedName') or group.get('name'))} · {count} "
                    f"contact(s)  {tag(str(group.get('resourceName')), who if several else '')}"
                )
        return "Labels:\n" + "\n".join(lines) if lines else "No labels."

    async def plan(self, arguments: Mapping[str, Any]) -> Plan:
        action = str(arguments.get("action", ""))
        if action not in GROUP_ACTIONS:
            raise ToolError(f"action is one of {', '.join(GROUP_ACTIONS)}")
        wanted = clean(arguments.get("label"))
        if not wanted:
            raise ToolError("say which label - contacts_groups with action: list shows them")
        ids = list(dict.fromkeys(person_id(c) for c in arguments.get("contacts") or () if c))
        if not ids:
            raise ToolError("say which contacts, by the ids contacts_search showed")
        if len(ids) > LABEL_MAX:
            raise ToolError(f"{len(ids)} contacts - at most {LABEL_MAX} at once")
        account = self.contacts.pick(str(arguments.get("account", "") or ""), write=True)[0]
        groups = await self.contacts.groups(account)
        group = next(
            (
                g
                for g in groups
                if wanted == g.get("resourceName")
                or wanted.lower()
                in (str(g.get("name", "")).lower(), str(g.get("formattedName", "")).lower())
            ),
            None,
        )
        if group is None:
            names = ", ".join(clean(g.get("formattedName") or g.get("name")) for g in groups)
            raise ToolError(
                f"no label {wanted!r} - there are: {names or 'none'}. A new label is made at "
                "contacts.google.com"
            )
        resource = str(group.get("resourceName", ""))
        if not GROUP_ID.fullmatch(resource):
            raise ToolError(f"Google named the label {resource!r}, which is not a label's id")
        name = clean(group.get("formattedName") or group.get("name"))
        moving: list[str] = []
        names: list[str] = []
        unchanged: list[str] = []
        for contact in ids:
            person = await self.contacts.person(
                account, contact, "names,emailAddresses,memberships"
            )
            member = any(
                (m.get("contactGroupMembership") or {}).get("contactGroupResourceName") == resource
                for m in items(person, "memberships")
            )
            if member == (action == "add"):
                unchanged.append(name_of(person))
            else:
                moving.append(contact)
                names.append(name_of(person))
        if not moving:
            state = "already" if action == "add" else "not"
            raise ToolError(f"nothing to change - {', '.join(unchanged)}: {state} on {name!r}")
        if action == "add":
            card = f'Add {len(moving)} contact(s) to the label "{name}": {", ".join(names)}'
        else:
            card = (
                f'Take {len(moving)} contact(s) off the label "{name}": {", ".join(names)} · '
                "the contacts themselves stay"
            )
        if unchanged:
            card += f"\n  unchanged: {', '.join(unchanged)}"
        plan = Plan(account, card)
        plan.group, plan.group_name, plan.members = resource, name, moving
        return plan

    async def carry_out(self, plan: Plan, arguments: Mapping[str, Any]) -> str:
        add = str(arguments["action"]) == "add"
        key = "resourceNamesToAdd" if add else "resourceNamesToRemove"
        said = await self.contacts.call(
            plan.account,
            "POST",
            f"/{plan.group}/members:modify",
            body={key: plan.members},
            retry=False,
        )
        missing = [str(r) for r in said.get("notFoundResourceNames") or ()]
        stuck = [str(r) for r in said.get("canNotRemoveLastContactGroupResourceNames") or ()]
        done = len(plan.members) - len(set(missing) | set(stuck))
        self.contacts.record(
            "contacts_labelled",
            plan.group,
            {
                "account": label_of(plan.account),
                "action": "add" if add else "remove",
                "contacts": plan.members,
            },
        )
        verb = "added to" if add else "taken off"
        lines = [f'{done} contact(s) {verb} the label "{plan.group_name}".']
        if missing:
            lines.append(f"Not found: {', '.join(missing)}")
        if stuck:
            lines.append(
                f"Kept on it, as it is their only label: {', '.join(stuck)} - add them to "
                "another label first"
            )
        return "\n".join(lines)


# -- signing in ----------------------------------------------------------------


def _label(response: Mapping[str, Any]) -> Mapping[str, str]:
    """The account's address, from the `id_token` the token endpoint sent. Decoded,
    not verified - it came back over TLS from the endpoint the flow itself called."""
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
        label="Google Contacts",
    )


def no_client(context: LoginContext) -> Tokens:
    """What signing in does before any client id exists: say so, and stop."""
    raise CredentialError(
        "Google Contacts has no OAuth client id yet. Create a Desktop-app client in a Google "
        "Cloud project with the People API enabled, then set "
        "plugins_settings.google-contacts.client_id to its id in config.json."
    )


class GoogleContactsPlugin(Plugin):
    name = PLUGIN
    description = "Google Contacts: look people up and see birthdays; save and change with a yes."

    def register(self, ctx: PluginContext) -> None:
        client_id = str(ctx.setting("client_id", "") or "").strip() or DEFAULT_CLIENT_ID
        ctx.register_login(LOGIN, client(client_id) if client_id else no_client)
        contacts = Contacts(ctx)
        for tool in (Search, Get, Birthdays, Create, Update, Groups):
            ctx.register_tool(tool(contacts), toolset="Google Contacts")
