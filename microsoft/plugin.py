"""Microsoft: Outlook mail and calendar through Microsoft Graph (`docs/spec/microsoft.md`).

One login, fifteen tools and a service. Every tool is `trusted_only` - offered
only in a session an owner holds - and `untrusted`, because mail and invitations
are words strangers chose. Reading is free; a send, and every change to the
calendar, is a card a person answers in every mode (`Subject.confirm`); tidying
mail is gated and capped. Nothing is deleted for good.

Mail is read and sent as MIME, so what `email` does with a message - the card,
the attachments, the links, the quoting rules - is done here the same way. The
tools are named `outlook_*` so they sit beside `email` and `google-calendar`.

The `new-mail` service tells the owner, in one line and with no model, when mail
lands in Outlook's Focused inbox.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import email
import email.policy
import email.utils
import hashlib
import html
import json
import mimetypes
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta, tzinfo
from email.message import EmailMessage, Message
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote, unquote, urlencode, urlsplit

from atlas.sdk.auth import credentials_in
from atlas.sdk.oauth import LoginContext, OAuthClient, Tokens, connections, grant
from atlas.sdk.plugin_entry import Plugin, PluginContext
from atlas.sdk.runtime import CredentialError, NetworkPolicyError, ToolError, assert_active
from atlas.sdk.service import NotifyError, Service, ServiceContext
from atlas.sdk.tool_plugin import ImageResult, Subject, Tool, ToolResult
from atlas.sdk.web import WebError, extract, get, request

PLUGIN = "microsoft"
LOGIN = "outlook"
LOGIN_NAME = f"{PLUGIN}:{LOGIN}"
"""What a person types: `atlas auth login microsoft:outlook`."""

DEFAULT_CLIENT_ID = ""
"""Atlas's own Entra app registration (`microsoft.md` R4.1). Empty until it exists;
`client_id` in settings is used instead, and with neither, signing in says how
to make one."""

LOGIN_HOST = "https://login.microsoftonline.com"
API = "https://graph.microsoft.com/v1.0"
SCOPES = (
    "offline_access",
    "openid",
    "email",
    "profile",
    "User.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "Calendars.ReadWrite",
)
"""R4.2: none needs an administrator in a default tenant, and none permanently deletes."""

USER_AGENT = "atlas-microsoft"
RESPONSE_BYTES = 40_000_000
"""A message read as MIME carries its attachments, up to Outlook's own limit."""
RETRY_CAP = 10.0
IMMUTABLE = 'IdType="ImmutableId"'
"""R5.1: a message keeps its id when it is moved."""

SEND_MAX_BYTES = 3 * 1024 * 1024
"""R6.5: Graph takes 4 MB in one request, and base64 makes the MIME a third bigger."""
SCAN_MAX_BYTES = 5 * 1024 * 1024
ORGANISE_MAX = 50
SUBJECT_MAX = 100
TITLE_MAX = 120
DESCRIPTION_MAX = 2000
KEY_FILES = re.compile(
    r".*\.(pem|key|p12|pfx|kdbx|keystore|jks)$|^id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$", re.I
)
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"PK\x03\x04", "application/zip"),
)
NOT_SIGNED_IN = f"not signed in to Microsoft: atlas auth login {LOGIN_NAME}"
WELL_KNOWN = {
    "inbox": "inbox",
    "archive": "archive",
    "drafts": "drafts",
    "sent": "sentitems",
    "sent items": "sentitems",
    "trash": "deleteditems",
    "deleted": "deleteditems",
    "deleted items": "deleteditems",
    "junk": "junkemail",
    "spam": "junkemail",
    "junk email": "junkemail",
}
"""Graph's well-known folder names, by the words a person uses for them (R5.2)."""


# -- talking to Graph --------------------------------------------------------------


class GraphError(ToolError):
    """A Graph status that was not 2xx, said in words (R10)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def label_of(connection_id: str) -> str:
    """`microsoft:work` -> `work`."""
    return connection_id.partition(":")[2] or connection_id


def _error_text(response: Any) -> str:
    try:
        data = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "")[:300]
    return ""


def _retry_after(response: Any) -> float:
    try:
        return min(float(response.header("retry-after") or 1), RETRY_CAP)
    except ValueError:
        return 1.0


def _explain(status: int, text: str) -> str:
    if status == 401:
        return (
            f"Microsoft refused the sign-in (401): sign in again with atlas auth login {LOGIN_NAME}"
        )
    if status == 403:
        return (
            "Microsoft refused this (403) - for a work or school account, the organisation may "
            "not allow it; ask IT to allow Atlas's mail and calendar access"
            + (f": {text}" if text else "")
        )
    if status == 404:
        return "Microsoft has no such item (404) - it was deleted or moved; search again"
    if status == 429:
        return "Microsoft is limiting this account for now (429); try again in a minute"
    return f"HTTP {status} from Microsoft{f': {text}' if text else ''}"


class Graph:
    """One install's way to Graph: which connection, which verb, what came back."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self._addresses: dict[str, str] = {}

    def accounts(self) -> tuple[str, ...]:
        return connections(PLUGIN, workspace=self.workspace)

    def pick(self, account: str, *, write: bool) -> list[str]:
        """R4.5: the only account, the named one, or - for a read - all of them."""
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
        data: bytes | None = None,
        content_type: str = "",
        prefer: Sequence[str] = (),
        raw: bool = False,
        retry: bool = True,
    ) -> Any:
        """One Graph call: the decoded JSON (`{}` for an empty body), or the bytes
        with `raw`. A status that is not 2xx is a `GraphError` in words."""
        query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        url = f"{API}{path}" + (f"?{urlencode(query, quote_via=quote)}" if query else "")
        headers = {"Prefer": ", ".join((IMMUTABLE, *prefer))}
        for attempt in range(2):
            token = await self.bearer(account)
            response = await request(
                method,
                url,
                json=body,
                data=data,
                content_type=content_type,
                headers={"Authorization": f"Bearer {token}", **headers},
                max_bytes=RESPONSE_BYTES,
                timeout=60.0,
                user_agent=USER_AGENT,
            )
            if 200 <= response.status < 300:
                if raw:
                    return response.body
                if not response.body:
                    return {}
                try:
                    return json.loads(response.body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise GraphError(
                        response.status, f"Microsoft sent something unreadable: {exc}"
                    ) from None
            transient = response.status == 429 or response.status >= 500
            if transient and retry and attempt == 0:
                await asyncio.sleep(_retry_after(response))
                continue
            raise GraphError(response.status, _explain(response.status, _error_text(response)))
        raise AssertionError("unreachable")  # pragma: no cover

    async def address(self, account: str) -> str:
        """The account's own address, for `From` and for leaving the person off a reply."""
        if account not in self._addresses:
            me = await self.call(
                account, "GET", "/me", params={"$select": "mail,userPrincipalName"}
            )
            found = (
                str(me.get("mail") or me.get("userPrincipalName") or "")
                if isinstance(me, dict)
                else ""
            )
            self._addresses[account] = found
        return self._addresses[account]


def path_id(graph_id: str) -> str:
    """R5.2: an id Graph gave, as one path segment."""
    return quote(graph_id, safe="")


class Handles:
    """R5.3: `m4` for a message, `e2` for an event, standing for Graph's long ids."""

    def __init__(self) -> None:
        self._by_handle: dict[str, tuple[str, str]] = {}
        self._by_id: dict[tuple[str, str, str], str] = {}
        self._count: dict[str, int] = {}

    def give(self, kind: str, account: str, graph_id: str) -> str:
        key = (kind, account, graph_id)
        if key not in self._by_id:
            self._count[kind] = self._count.get(kind, 0) + 1
            handle = f"{kind}{self._count[kind]}"
            self._by_id[key] = handle
            self._by_handle[handle] = (account, graph_id)
        return self._by_id[key]

    def take(self, kind: str, handle: str) -> tuple[str, str]:
        found = self._by_handle.get(handle.strip().lower())
        if found is None or not handle.strip().lower().startswith(kind):
            what = "message" if kind == "m" else "event"
            raise ToolError(f"no {what} {handle!r} - search again for a fresh id")
        return found


class Account(NamedTuple):
    """A connection, its label, and the account's address."""

    id: str
    label: str
    address: str


# -- reading a message (as `email.md` §6.2-6.3) ---------------------------------------------


def parse(raw: bytes) -> Message:
    return email.message_from_bytes(raw, policy=email.policy.default)


def header(message: Message, name: str) -> str:
    return " ".join(str(message.get(name, "") or "").split())


def when(message: Message) -> str:
    try:
        moment = email.utils.parsedate_to_datetime(header(message, "Date")).astimezone()
    except (TypeError, ValueError):
        return header(message, "Date")
    return short(moment)


def stamp(message: Message, fallback: float) -> float:
    try:
        return email.utils.parsedate_to_datetime(header(message, "Date")).timestamp()
    except (TypeError, ValueError):
        return fallback


def short(moment: datetime) -> str:
    return (
        f"{moment.strftime('%a')} {moment.day} {moment.strftime('%b')} {moment.strftime('%H:%M')}"
    )


def sender_name(message: Message) -> str:
    name, address = email.utils.parseaddr(header(message, "From"))
    return name or address or "(unknown sender)"


def unverified(message: Message) -> str:
    """R6.2: Exchange's verdict, when it says SPF, DKIM or DMARC failed for the sender."""
    results = " ".join(str(v) for v in message.get_all("Authentication-Results", []) or [])
    if not results:
        return ""
    domain = email.utils.parseaddr(header(message, "From"))[1].rpartition("@")[2].lower()
    failed = re.search(r"\b(spf|dkim|dmarc)=(fail|softfail|permerror)\b", results, re.I)
    if failed and domain:
        return f"(Outlook could not verify that this came from {domain})"
    return ""


def attachments_of(message: Message) -> list[Message]:
    return [part for part in message.walk() if part.get_filename() and not part.is_multipart()]


def body_text(message: Message, limit: int) -> str:
    part = (
        message.get_body(preferencelist=("plain", "html")) if hasattr(message, "get_body") else None
    )
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):
        payload = part.get_payload(decode=True) or b""
        content = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)
    if part.get_content_type() == "text/html":
        content = extract(str(content), mode="text", readability=False).text
    text = fold_quotes(str(content).replace("\r\n", "\n").strip())
    if len(text) > limit:
        return text[:limit] + f"\n[... cut at {limit} characters of {len(text)}]"
    return text


QUOTE_HEAD = re.compile(r"^On .{4,200} wrote:\s*$")


def fold_quotes(text: str) -> str:
    lines = text.split("\n")
    kept: list[str] = []
    folded = 0
    for line in lines:
        if line.startswith(">") or (folded and not line.strip()):
            folded += 1
            continue
        if QUOTE_HEAD.match(line.strip()):
            folded += 1
            continue
        if folded:
            kept.append(f"[... {folded} quoted line(s) folded]")
            folded = 0
        kept.append(line)
    if folded:
        kept.append(f"[... {folded} quoted line(s) folded]")
    return "\n".join(kept).strip()


def human_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def clean_name(name: str) -> str:
    base = name.replace("\\", "/").rpartition("/")[2]
    kept = "".join(c for c in base if c.isalnum() or c in " .-_()").strip(" .")
    return kept[:120] or "attachment"


def sniff(data: bytes, name: str) -> str:
    for signature, kind in SIGNATURES:
        if data.startswith(signature):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def _choose(files: Sequence[Message], which: str) -> Message:
    which = which.strip()
    if which.isdigit() and 1 <= int(which) <= len(files):
        return files[int(which) - 1]
    for part in files:
        if str(part.get_filename()) == which:
            return part
    names = ", ".join(f"{i}. {p.get_filename()}" for i, p in enumerate(files, start=1)) or "none"
    raise ToolError(f"no attachment {which!r} - this message has: {names}")


# -- links (`email.md` R6.20-R6.23) ----------------------------------------------------------

LINK = re.compile(r"\[([^\]\n]{1,200})\]\(([^()\s]+)\)")
LINK_SCHEMES = ("https", "http", "mailto")
LOOKS_LIKE_ADDRESS = re.compile(
    r"^(?:https?://)?(?:www\.)?((?:[a-z0-9-]+\.)+[a-z]{2,})(?:[/:?#]\S*)?$", re.I
)


def links_in(body: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for match in LINK.finditer(body):
        words, target = match.group(1).strip(), match.group(2).strip()
        parts = urlsplit(target)
        scheme = parts.scheme.lower()
        if scheme not in LINK_SCHEMES:
            raise ToolError(
                f"refused: the link {words!r} goes to a {scheme or 'relative'} address - only "
                "https, http and mailto links are sent"
            )
        if scheme in ("http", "https") and not parts.hostname:
            raise ToolError(f"refused: the link {words!r} has no host")
        named = LOOKS_LIKE_ADDRESS.match(words)
        if named and scheme in ("http", "https"):
            shown = named.group(1).lower()
            actual = (parts.hostname or "").lower().removeprefix("www.")
            if shown.removeprefix("www.") != actual and not actual.endswith("." + shown):
                raise ToolError(
                    f"refused: the link reads {words!r} but goes to {actual} - link words that "
                    "name a different site are how phishing works"
                )
        found.append((words, target))
    return found


def as_plain(body: str) -> str:
    return LINK.sub(lambda m: f"{m.group(1).strip()} ({m.group(2).strip()})", body)


def as_card(body: str) -> str:
    return LINK.sub(lambda m: f"{m.group(1).strip()} <{m.group(2).strip()}>", body)


def as_html(body: str, tail: str = "") -> str:
    pieces: list[str] = []
    last = 0
    for match in LINK.finditer(body):
        pieces.append(html.escape(body[last : match.start()]))
        words, target = html.escape(match.group(1).strip()), html.escape(match.group(2).strip())
        pieces.append(f'<a href="{target}">{words}</a>')
        last = match.end()
    pieces.append(html.escape(body[last:]))
    pieces.append(html.escape(tail))
    text = "".join(pieces).replace("\r\n", "\n")
    paragraphs = [p.replace("\n", "<br>\n") for p in text.split("\n\n") if p.strip()]
    return "<html><body>\n" + "\n".join(f"<p>{p}</p>" for p in paragraphs) + "\n</body></html>\n"


# -- what the tools share ---------------------------------------------------------------------


ACCOUNT = {
    "type": "string",
    "description": "Which account, by its label. Needed for a change when several are signed in.",
}
MESSAGE_ID = {"type": "string", "description": "A message id, as a search showed it (m4)."}
EVENT_ID = {"type": "string", "description": "An event id, as a listing showed it (e2)."}


class Outlook:
    """What every tool shares: Graph, the handles, the settings, the plugin's context."""

    def __init__(self, ctx: PluginContext, graph: Graph | None = None) -> None:
        self.ctx = ctx
        self.workspace = Path(ctx.workspace)
        self.settings = dict(ctx.settings)
        self.graph = graph or Graph(self.workspace)
        self.handles = Handles()
        self.sent: dict[str, list[float]] = {}

    def setting(self, key: str, default: Any) -> Any:
        value = self.settings.get(key)
        return default if value is None else value

    def number(self, key: str, default: int) -> int:
        try:
            return int(self.setting(key, default))
        except (TypeError, ValueError):
            return default

    async def account(self, connection: str) -> Account:
        return Account(connection, label_of(connection), await self.graph.address(connection))

    async def writer(self, label: str) -> Account:
        return await self.account(self.graph.pick(label, write=True)[0])

    async def mime(self, account: str, graph_id: str) -> bytes:
        data: bytes = await self.graph.call(
            account, "GET", f"/me/messages/{path_id(graph_id)}/$value", raw=True
        )
        return data

    # -- the files a send may carry (`email.md` R6.14-R6.15) --------------------

    async def gather(self, entries: Iterable[Any]) -> list[Attached]:
        names = [str(e).strip() for e in entries if str(e).strip()]
        most = self.number("attachments_max", 10)
        if len(names) > most:
            raise ToolError(f"{len(names)} files - at most {most} on one message")
        files: list[Attached] = []
        for name in names:
            if name.startswith("chat:"):
                files.append(self.from_chat(name[len("chat:") :].strip()))
            elif urlsplit(name).scheme.lower() in ("http", "https"):
                files.append(await self.from_url(name))
            else:
                files.append(self.from_workspace(name))
        return files

    async def from_url(self, address: str) -> Attached:
        policy = self.ctx.web_policy
        try:
            response = await get(
                address,
                allow_private=policy.allow_private,
                hosts=policy.hosts,
                max_redirects=policy.max_redirects,
                max_bytes=SEND_MAX_BYTES + 1,
                timeout=60.0,
                user_agent=USER_AGENT,
            )
        except NetworkPolicyError as exc:
            raise ToolError(f"refused: {address} - {exc}") from None
        except WebError as exc:
            raise ToolError(f"could not download {address}: {exc}") from None
        if not 200 <= response.status < 300:
            raise ToolError(f"could not download {address}: HTTP {response.status}")
        if response.truncated or len(response.body) > SEND_MAX_BYTES:
            raise ToolError(f"{address} is over 3 MB, the most a message here can carry")
        data = response.body
        _scan(data, address, self.workspace)
        return Attached(_download_name(response, address), data, f"downloaded from {address}")

    def from_workspace(self, entry: str) -> Attached:
        root = self.workspace.resolve()
        target = Path(entry)
        target = (target if target.is_absolute() else root / target).resolve()
        if not target.is_relative_to(root):
            raise ToolError(f"refused: {entry} is outside the workspace")
        relative = target.relative_to(root)
        if relative.parts and relative.parts[0] == ".atlas":
            raise ToolError(f"refused: {entry} is Atlas's own state")
        if any(part.startswith(".") for part in relative.parts):
            raise ToolError(f"refused: {entry} is a hidden file or inside a hidden folder")
        if KEY_FILES.match(target.name):
            raise ToolError(f"refused: {entry} is named like key material")
        if not target.is_file():
            raise ToolError(f"no file {entry} in the workspace")
        data = target.read_bytes()
        _scan(data, entry, self.workspace)
        return Attached(target.name, data, f"from the workspace: {relative.as_posix()}")

    def from_chat(self, name: str) -> Attached:
        media = self.ctx.media
        for block in reversed(self.ctx.session_media()):
            given = str(getattr(block, "name", "") or "")
            source = str(getattr(block, "source", "") or "")
            if name and (given == name or source.split(" (")[0] == name):
                data = media.get(block) if media is not None else None
                if not data:
                    raise ToolError(f"{name} was sent here but is no longer stored")
                _scan(data, name, self.workspace)
                return Attached(given or name, data, "you sent it in this chat")
        raise ToolError(f"no file called {name!r} was sent in this conversation")

    # -- sends ----------------------------------------------------------------------

    def count_send(self, account: Account) -> None:
        now = time.time()
        recent = [t for t in self.sent.get(account.id, []) if now - t < 3600]
        if len(recent) >= self.number("sends_per_hour", 20):
            raise ToolError(
                f"{len(recent)} emails sent from {account.label} this hour - the limit; "
                "nothing was sent"
            )
        recent.append(now)
        self.sent[account.id] = recent

    def record_send(self, out: Outgoing) -> None:
        with contextlib.suppress(Exception):
            self.ctx.audit(
                "outlook_mail_sent",
                f"{len(out.recipients())} recipient(s), {len(out.files)} file(s)",
                arguments={
                    "account": out.account.label,
                    "recipients": len(out.recipients()),
                    "files": [f.record() for f in out.files],
                },
            )


def _download_name(response: Any, address: str) -> str:
    disposition = response.header("content-disposition")
    match = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)", disposition or "", re.I)
    if match:
        return unquote(match.group(1) or match.group(2))
    tail = unquote(urlsplit(response.url or address).path.rstrip("/").rpartition("/")[2])
    return tail or "download"


def _scan(data: bytes, shown: str, workspace: Path) -> None:
    if len(data) > SCAN_MAX_BYTES:
        return
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return
    found = credentials_in(text, workspace=workspace)
    if found:
        raise ToolError(
            f"refused: {shown} contains what looks like a credential ({', '.join(found)})"
        )


class OutlookTool(Tool):
    """Owner-only, marked untrusted, failures as sentences."""

    trusted_only = True
    untrusted = True

    def __init__(self, outlook: Outlook) -> None:
        self.outlook = outlook

    @property
    def graph(self) -> Graph:
        return self.outlook.graph

    async def run(self, **arguments: Any) -> ToolResult:
        try:
            assert_active()
            said = await self.act(**arguments)
            return said if isinstance(said, ToolResult) else ToolResult.ok(said)
        except (CredentialError, ToolError) as exc:
            return ToolResult.error(str(exc))

    async def act(self, *args: Any, **arguments: Any) -> Any:
        raise NotImplementedError


class Carded(OutlookTool):
    """A tool whose call is worked out once, for the card, and done as it was shown."""

    def __init__(self, outlook: Outlook) -> None:
        super().__init__(outlook)
        self._looked: dict[str, tuple[float, Any]] = {}

    def _remember(self, arguments: Mapping[str, Any], prepared: Any) -> None:
        self._looked[json.dumps(arguments, sort_keys=True, default=str)] = (
            time.monotonic(),
            prepared,
        )

    def _recall(self, arguments: Mapping[str, Any]) -> Any:
        seen = self._looked.pop(json.dumps(arguments, sort_keys=True, default=str), None)
        return seen[1] if seen is not None and time.monotonic() - seen[0] < 900 else None


# -- mail: reading ----------------------------------------------------------------------------

MESSAGE_FIELDS = (
    "id,conversationId,subject,from,receivedDateTime,isRead,flag,importance,"
    "hasAttachments,categories,isDraft,changeKey"
)


def graph_moment(value: Any) -> datetime | None:
    """Graph's `2026-10-01T08:00:00.0000000` (UTC unless it says) as an aware moment."""
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    match = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(.*)$", text)
    if not match:
        return None
    fraction = (match.group(2) or "")[:7]  # Python reads six digits; Graph writes seven
    try:
        moment = datetime.fromisoformat(match.group(1) + fraction + (match.group(3) or ""))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def who(item: Mapping[str, Any]) -> str:
    sender = (item.get("from") or {}).get("emailAddress") or {}
    name, address = str(sender.get("name") or ""), str(sender.get("address") or "")
    if name and address and name != address:
        return f"{name} <{address}>"
    return name or address or "(unknown sender)"


def marks_of(item: Mapping[str, Any]) -> str:
    marks = []
    if item.get("isRead") is False:
        marks.append("unread")
    if (item.get("flag") or {}).get("flagStatus") == "flagged":
        marks.append("flagged")
    if item.get("importance") == "high":
        marks.append("important")
    if item.get("hasAttachments"):
        marks.append("has files")
    marks += [str(c) for c in item.get("categories") or []]
    return ", ".join(marks)


async def folder_path(graph: Graph, account: str, folder: str) -> str:
    """`/me/messages` for anywhere, a well-known folder by its name, or one of the
    account's own folders by its display name - looked up, never trusted (R5.2)."""
    wanted = folder.strip().lower()
    if wanted in ("", "inbox"):
        return "/me/mailFolders/inbox/messages"
    if wanted in ("anywhere", "all"):
        return "/me/messages"
    if wanted in WELL_KNOWN:
        return f"/me/mailFolders/{WELL_KNOWN[wanted]}/messages"
    return f"/me/mailFolders/{path_id(await folder_id(graph, account, folder))}/messages"


async def folder_id(graph: Graph, account: str, name: str) -> str:
    if name.strip().lower() in WELL_KNOWN:
        return WELL_KNOWN[name.strip().lower()]
    folders = await list_folders(graph, account)
    for folder in folders:
        if str(folder.get("displayName") or "").lower() == name.strip().lower():
            return str(folder["id"])
    names = ", ".join(str(f.get("displayName")) for f in folders)
    raise ToolError(f"no folder {name!r} - this account has: {names}")


async def list_folders(graph: Graph, account: str) -> list[Mapping[str, Any]]:
    data = await graph.call(
        account,
        "GET",
        "/me/mailFolders",
        params={"$top": 200, "$select": "id,displayName,unreadItemCount"},
    )
    return [f for f in data.get("value", []) if isinstance(f, dict) and f.get("id")]


async def search(
    graph: Graph, account: str, query: str, folder: str, limit: int
) -> tuple[list[Mapping[str, Any]], bool]:
    """Messages newest first, and whether there are more."""
    params: dict[str, Any] = {"$top": limit + 1, "$select": MESSAGE_FIELDS}
    if query:
        params["$search"] = '"' + query.replace("\\", "").replace('"', "'") + '"'
    else:
        params["$orderby"] = "receivedDateTime desc"
    data = await graph.call(
        account, "GET", await folder_path(graph, account, folder), params=params
    )
    items: list[Mapping[str, Any]] = [
        m for m in data.get("value", []) if isinstance(m, dict) and m.get("id")
    ]
    items.sort(key=lambda m: str(m.get("receivedDateTime") or ""), reverse=True)
    more = len(items) > limit or bool(data.get("@odata.nextLink"))
    return items[:limit], more


class MailSearch(OutlookTool):
    name = "outlook_mail_search"
    description = (
        "Search Outlook mail, newest first. query is Outlook's own search - from:sam "
        "subject:dinner, or words; folder is inbox (default), a folder's name, or anywhere; "
        "unread narrows to unread. Shows sender, date, subject and marks, with each message's id."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Outlook search words. Default: none."},
            "folder": {
                "type": "string",
                "description": "inbox, sent, archive, a folder name, or anywhere.",
            },
            "unread": {"type": "boolean"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
            "account": ACCOUNT,
        },
        "required": [],
    }

    async def act(
        self,
        query: str = "",
        folder: str = "inbox",
        unread: bool = False,
        max_results: int = 0,
        account: str = "",
    ) -> str:
        limit = max(1, min(int(max_results or self.outlook.number("max_results", 20)), 100))
        accounts = self.graph.pick(account, write=False)
        lines: list[tuple[str, str]] = []
        more = False
        for connection in accounts:
            found, extra = await search(self.graph, connection, query.strip(), folder, limit)
            more = more or extra
            for item in found:
                if unread and item.get("isRead") is not False:
                    continue
                moment = graph_moment(item.get("receivedDateTime"))
                marks = marks_of(item)
                where = f" · {label_of(connection)}" if len(accounts) > 1 else ""
                handle = self.outlook.handles.give("m", connection, str(item["id"]))
                lines.append(
                    (
                        str(item.get("receivedDateTime") or ""),
                        f"{short(moment.astimezone()) if moment else '?'} · {who(item)} · "
                        f"{item.get('subject') or '(no subject)'}"
                        + (f" · {marks}" if marks else "")
                        + f"  [id: {handle}{where}]",
                    )
                )
        lines.sort(key=lambda pair: pair[0], reverse=True)
        shown = f"{query!r}" if query else f"the {folder or 'inbox'}"
        if not lines:
            return f"No messages in {shown}." if not query else f"No messages match {shown}."
        text = f"{len(lines[:limit])} message(s) for {shown}, newest first:\n" + "\n".join(
            t for _, t in lines[:limit]
        )
        if more or len(lines) > limit:
            text += "\n(more match - narrow the search to see them)"
        return text


def describe(handle: str, message: Message, limit: int) -> str:
    lines = [f"From: {header(message, 'From')}", f"To: {header(message, 'To')}"]
    if header(message, "Cc"):
        lines.append(f"Cc: {header(message, 'Cc')}")
    lines += [f"Date: {when(message)}", f"Subject: {header(message, 'Subject') or '(no subject)'}"]
    warning = unverified(message)
    if warning:
        lines.append(warning)
    lines += ["", body_text(message, limit) or "(no text)"]
    files = attachments_of(message)
    if files:
        lines += ["", "Attachments:"]
        for number, part in enumerate(files, start=1):
            payload = part.get_payload(decode=True) or b""
            size = len(payload) if isinstance(payload, bytes) else 0
            lines.append(
                f"  {number}. {part.get_filename()} ({part.get_content_type()}, {human_size(size)})"
            )
    lines.append(f"[id: {handle}]")
    return "\n".join(lines)


class MailRead(OutlookTool):
    name = "outlook_mail_read"
    description = (
        "Read one Outlook message as text - headers, body and attachment names - or, with "
        "conversation, every message in its conversation. Attachments themselves are fetched "
        "with outlook_mail_attachment."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message_id": MESSAGE_ID,
            "conversation": {"type": "boolean", "description": "Read the whole conversation."},
        },
        "required": ["message_id"],
    }

    async def act(self, message_id: str, conversation: bool = False) -> str:
        account, graph_id = self.outlook.handles.take("m", message_id)
        ids = [graph_id]
        if conversation:
            meta = await self.graph.call(
                account,
                "GET",
                f"/me/messages/{path_id(graph_id)}",
                params={"$select": "conversationId"},
            )
            thread = str(meta.get("conversationId") or "")
            if thread:
                data = await self.graph.call(
                    account,
                    "GET",
                    "/me/messages",
                    params={
                        "$filter": f"conversationId eq '{thread.replace(chr(39), chr(39) * 2)}'",
                        "$select": "id,receivedDateTime",
                        "$top": 50,
                    },
                )
                items = sorted(
                    (m for m in data.get("value", []) if isinstance(m, dict) and m.get("id")),
                    key=lambda m: str(m.get("receivedDateTime") or ""),
                )
                ids = [str(m["id"]) for m in items] or ids
        limit = self.outlook.number("max_chars", 20000) // max(1, len(ids))
        blocks = []
        for each in ids:
            message = parse(await self.outlook.mime(account, each))
            blocks.append(describe(self.outlook.handles.give("m", account, each), message, limit))
        return "\n\n---\n\n".join(blocks)


class MailAttachment(OutlookTool):
    name = "outlook_mail_attachment"
    description = (
        "Fetch one attachment of an Outlook message, by its number or name as "
        "outlook_mail_read listed it. A picture is shown to you; anything else is saved in "
        "the workspace for read_media."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message_id": MESSAGE_ID,
            "attachment": {"type": "string", "description": "Its number (1, 2, ...) or file name."},
        },
        "required": ["message_id", "attachment"],
    }

    async def act(self, message_id: str, attachment: str) -> Any:
        account, graph_id = self.outlook.handles.take("m", message_id)
        files = attachments_of(parse(await self.outlook.mime(account, graph_id)))
        chosen = _choose(files, attachment)
        data = chosen.get_payload(decode=True) or b""
        if not isinstance(data, bytes):
            raise ToolError("that attachment has no content")
        cap = self.outlook.number("attachment_max_mb", 25) * 1024 * 1024
        if len(data) > cap:
            raise ToolError(
                f"{chosen.get_filename()} is {human_size(len(data))}, "
                f"over the {human_size(cap)} limit"
            )
        name = clean_name(str(chosen.get_filename()))
        kind = sniff(data, name)
        media = self.outlook.ctx.media
        if kind in IMAGE_TYPES and media is not None:
            block = media.put(data, source=f"{name} (attached to an email)")
            return ImageResult(f"{name}, {kind}, {human_size(len(data))}", images=(block,))
        digest = hashlib.sha256(graph_id.encode()).hexdigest()[:12]
        folder = self.outlook.workspace / "email-attachments" / f"outlook-{digest}"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        target.write_bytes(data)
        relative = target.relative_to(self.outlook.workspace).as_posix()
        return (
            f"Saved {name} ({kind}, {human_size(len(data))}) to {relative} - read it with "
            f"read_media(path={relative!r})."
        )


class MailFolders(OutlookTool):
    name = "outlook_mail_folders"
    description = (
        "The Outlook account's mail folders, with unread counts, and its categories - for "
        "outlook_mail_search's folder and outlook_mail_organise."
    )
    parameters = {"type": "object", "properties": {"account": ACCOUNT}, "required": []}

    async def act(self, account: str = "") -> str:
        lines = []
        for connection in self.graph.pick(account, write=False):
            folders = await list_folders(self.graph, connection)
            named = [
                f"{f.get('displayName')}"
                + (f" ({f.get('unreadItemCount')} unread)" if f.get("unreadItemCount") else "")
                for f in folders
            ]
            data = await self.graph.call(connection, "GET", "/me/outlook/masterCategories")
            categories = [
                str(c.get("displayName")) for c in data.get("value", []) if isinstance(c, dict)
            ]
            lines.append(
                f"{label_of(connection)}: folders: {', '.join(named) or 'none'}; "
                f"categories: {', '.join(categories) or 'none'}"
            )
        return "\n".join(lines)


# -- mail: writing ----------------------------------------------------------------------------


class Outgoing:
    """A message worked out once - for the card - and sent as it was shown (`email.md` R6.17)."""

    def __init__(self, account: Account) -> None:
        self.account = account
        self.to: list[str] = []
        self.cc: list[str] = []
        self.bcc: list[str] = []
        self.subject = ""
        self.body = ""
        self.in_reply_to = ""
        self.references = ""
        self.context = ""
        self.files: list[Attached] = []
        self.forwarded = ""
        self.draft_id = ""
        self.draft_key = ""

    def recipients(self) -> list[str]:
        return [a for a in self.to + self.cc + self.bcc if a]

    def build(self) -> bytes:
        """The MIME Graph sends: `Bcc` included, because Graph reads the recipients
        from the headers and Exchange removes `Bcc` before delivering (R6.3)."""
        message = EmailMessage(policy=email.policy.SMTP)
        message["From"] = self.account.address
        if self.to:
            message["To"] = ", ".join(self.to)
        if self.cc:
            message["Cc"] = ", ".join(self.cc)
        if self.bcc:
            message["Bcc"] = ", ".join(self.bcc)
        message["Subject"] = self.subject
        message["Date"] = email.utils.formatdate(localtime=True)
        message["Message-ID"] = email.utils.make_msgid(
            domain=self.account.address.rpartition("@")[2] or "localhost"
        )
        if self.in_reply_to:
            message["In-Reply-To"] = self.in_reply_to
            message["References"] = self.references
        tail = f"\n\n{self.forwarded}" if self.forwarded else ""
        if links_in(self.body):
            message.set_content(as_plain(self.body) + tail)
            message.add_alternative(as_html(self.body, tail), subtype="html")
        else:
            message.set_content(self.body + tail)
        for file in self.files:
            main, _, sub = file.kind.partition("/")
            message.add_attachment(
                file.data, maintype=main, subtype=sub or "octet-stream", filename=file.name
            )
        return message.as_bytes()

    def card(self, verb: str) -> str:
        lines = [f"{verb} from {self.account.address}"]
        for name, people in (("To", self.to), ("Cc", self.cc), ("Bcc", self.bcc)):
            if people:
                lines.append(f"{name}: {', '.join(people)}")
        lines += [
            f"Subject: {self.subject or '(no subject)'}",
            "",
            as_card(self.body).rstrip() or "(no text)",
        ]
        if self.forwarded:
            lines += ["", self.forwarded]
        if self.files:
            total = sum(len(f.data) for f in self.files)
            lines += ["", f"Attached ({len(self.files)} file(s), {human_size(total)}):"]
            lines += [f"  {f.describe()}" for f in self.files]
        if self.context:
            lines += ["", f"({self.context})"]
        return "\n".join(lines)


class Attached:
    """One file on its way out: its bytes, read once, and where it came from."""

    def __init__(self, name: str, data: bytes, source: str) -> None:
        self.name = clean_name(name)
        self.data = data
        self.source = source
        self.kind = sniff(data, self.name)
        self.sha256 = hashlib.sha256(data).hexdigest()

    def describe(self) -> str:
        kind = self.kind.rpartition("/")[2].upper()
        size = human_size(len(self.data))
        return f"{self.name} - {kind}, {size}, sha256 {self.sha256[:8]} - {self.source}"

    def record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size": len(self.data),
            "sha256": self.sha256,
            "source": self.source,
        }


def _addresses(value: Any) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    found: list[str] = []
    for _, address in email.utils.getaddresses([str(i) for i in items]):
        address = address.strip()
        if address and "@" in address:
            found.append(address)
        elif address:
            raise ToolError(f"{address!r} is not an email address")
    return found


SEND_FIELDS: dict[str, Any] = {
    "to": {"type": "array", "items": {"type": "string"}, "description": "Addresses."},
    "cc": {"type": "array", "items": {"type": "string"}},
    "bcc": {"type": "array", "items": {"type": "string"}},
    "subject": {"type": "string"},
    "body": {
        "type": "string",
        "description": (
            "The text, exactly as it should be sent. [words](https://address) makes the words "
            "a link; the person sees the real address beside them."
        ),
    },
    "reply_to": {"type": "string", "description": "The id of a message to reply to (m4)."},
    "reply_all": {"type": "boolean", "description": "Reply to everyone on the original."},
    "forward": {"type": "string", "description": "The id of a message to forward."},
    "keep_attachments": {
        "type": "boolean",
        "description": "On a forward, keep the original's attachments. Default true.",
    },
    "attachments": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Files to attach, 3 MB in all: a path inside the workspace, chat:<name> for a file "
            "the person sent in this conversation, or an https:// address to download."
        ),
    },
    "account": ACCOUNT,
}


class MailWriter(Carded):
    """What a draft and a send share: turning a call into an `Outgoing`."""

    async def compose(self, arguments: Mapping[str, Any]) -> Outgoing:
        reply_to = str(arguments.get("reply_to", "") or "").strip()
        forward = str(arguments.get("forward", "") or "").strip()
        if reply_to and forward:
            raise ToolError("reply_to or forward, not both")
        original: Message | None = None
        if reply_to or forward:
            connection, graph_id = self.outlook.handles.take("m", reply_to or forward)
            account = await self.outlook.account(connection)
            original = parse(await self.outlook.mime(connection, graph_id))
        else:
            account = await self.outlook.writer(str(arguments.get("account", "") or ""))
        out = Outgoing(account)
        out.to = _addresses(arguments.get("to"))
        out.cc = _addresses(arguments.get("cc"))
        out.bcc = _addresses(arguments.get("bcc"))
        out.subject = str(arguments.get("subject", "") or "")
        out.body = str(arguments.get("body", "") or "")
        links_in(out.body)
        if original is not None and reply_to:
            self._reply(out, original, bool(arguments.get("reply_all")))
        elif original is not None:
            self._forward(out, original, arguments.get("keep_attachments", True) is not False)
        if not out.recipients():
            raise ToolError("no recipients - say who it goes to")
        out.files += await self.outlook.gather(arguments.get("attachments") or ())
        total = sum(len(f.data) for f in out.files)
        if total > SEND_MAX_BYTES:
            raise ToolError(
                f"the attachments come to {human_size(total)}; a message sent through Microsoft "
                "here carries at most 3 MB - share a link to the file instead"
            )
        return out

    def _reply(self, out: Outgoing, original: Message, everyone: bool) -> None:
        mine = out.account.address.lower()
        back = _addresses(header(original, "Reply-To") or header(original, "From"))
        extra = (
            _addresses(header(original, "To")) + _addresses(header(original, "Cc"))
            if everyone
            else []
        )
        out.to = [a for a in dict.fromkeys(out.to + back) if a.lower() != mine]
        out.cc = [
            a
            for a in dict.fromkeys(out.cc + extra)
            if a.lower() != mine and a.lower() not in {t.lower() for t in out.to}
        ]
        subject = header(original, "Subject")
        out.subject = out.subject or (
            subject if subject.lower().startswith("re:") else f"Re: {subject}"
        )
        message_id = header(original, "Message-ID")
        out.in_reply_to = message_id
        out.references = " ".join(
            part for part in (header(original, "References"), message_id) if part
        )
        out.context = f"replying to {sender_name(original)}, {when(original)}"

    def _forward(self, out: Outgoing, original: Message, keep: bool) -> None:
        subject = header(original, "Subject")
        out.subject = out.subject or (
            subject if subject.lower().startswith("fw") else f"Fwd: {subject}"
        )
        out.forwarded = "\n".join(
            [
                "---------- Forwarded message ---------",
                f"From: {header(original, 'From')}",
                f"Date: {when(original)}",
                f"Subject: {subject}",
                f"To: {header(original, 'To')}",
                "",
                body_text(original, self.outlook.number("max_chars", 20000)),
            ]
        )
        out.context = f"forwarding {sender_name(original)}'s message of {when(original)}"
        if keep:
            for part in attachments_of(original):
                data = part.get_payload(decode=True) or b""
                if isinstance(data, bytes):
                    out.files.append(Attached(str(part.get_filename()), data, "from the original"))


def as_mime_body(data: bytes) -> bytes:
    """Graph's MIME upload: the message base64-encoded, sent as `text/plain`."""
    return base64.b64encode(data)


class MailDraft(MailWriter):
    name = "outlook_mail_draft"
    description = (
        "Save a draft in Outlook's Drafts - new, a reply, or a forward - without sending it. "
        "Use it whenever you are not sure; the person can send it from Outlook, or you can "
        "send it with outlook_mail_send(draft_id=...). A draft with attachments asks first."
    )
    parameters = {"type": "object", "properties": SEND_FIELDS, "required": []}
    gated = True
    """Gated only when it carries files (`email.md` R6.2): `subject` answers None otherwise."""

    async def subject(self, arguments: Mapping[str, Any]) -> Subject | None:
        if not arguments.get("attachments"):
            return None
        try:
            out = await self.compose(arguments)
        except (CredentialError, ToolError):
            return None
        self._remember(arguments, out)
        return Subject(tool=self.name, action="draft", summary=out.card("Draft (with files)"))

    async def act(self, **arguments: Any) -> str:
        out: Outgoing = self._recall(arguments) or await self.compose(arguments)
        made = await self.graph.call(
            out.account.id,
            "POST",
            "/me/messages",
            data=as_mime_body(out.build()),
            content_type="text/plain",
        )
        handle = self.outlook.handles.give("m", out.account.id, str(made.get("id") or ""))
        return (
            f"Draft saved for {', '.join(out.recipients())}: {out.subject or '(no subject)'}"
            + (f", with {len(out.files)} file(s)" if out.files else "")
            + f" [draft id: {handle}]"
        )


class MailSend(MailWriter):
    name = "outlook_mail_send"
    description = (
        "Send an Outlook email - new, a reply (reply_to), a forward (forward), or a saved "
        "draft (draft_id). The person is shown every recipient, the subject, the whole text "
        "and every file, and must say yes. Write the body exactly as it should go."
    )
    parameters = {
        "type": "object",
        "properties": {
            **SEND_FIELDS,
            "draft_id": {"type": "string", "description": "Send this draft (m7)."},
        },
        "required": [],
    }
    gated = True

    async def subject(self, arguments: Mapping[str, Any]) -> Subject | None:
        try:
            out = await self.prepare(arguments)
        except (CredentialError, ToolError):
            return None
        self._remember(arguments, out)
        return Subject(tool=self.name, action="send", summary=out.card("Send"), confirm=True)

    async def prepare(self, arguments: Mapping[str, Any]) -> Outgoing:
        draft = str(arguments.get("draft_id", "") or "").strip()
        if not draft:
            return await self.compose(arguments)
        connection, graph_id = self.outlook.handles.take("m", draft)
        meta = await self.graph.call(
            connection,
            "GET",
            f"/me/messages/{path_id(graph_id)}",
            params={"$select": "isDraft,changeKey"},
        )
        if not meta.get("isDraft"):
            raise ToolError(f"{draft!r} is not a draft")
        message = parse(await self.outlook.mime(connection, graph_id))
        out = Outgoing(await self.outlook.account(connection))
        out.to = _addresses(message.get_all("To", []))
        out.cc = _addresses(message.get_all("Cc", []))
        out.bcc = _addresses(message.get_all("Bcc", []))
        out.subject = header(message, "Subject")
        out.body = body_text(message, 10**9)
        for part in attachments_of(message):
            data = part.get_payload(decode=True) or b""
            if isinstance(data, bytes):
                out.files.append(Attached(str(part.get_filename()), data, "in the draft"))
        out.draft_id, out.draft_key = graph_id, str(meta.get("changeKey") or "")
        out.context = "sending a saved draft"
        return out

    async def act(self, **arguments: Any) -> str:
        out: Outgoing | None = self._recall(arguments)
        if out is None:
            await self.prepare(arguments)  # says why no card could be drawn, when one could not
            raise ToolError("a send has to be shown to the person first; nothing was sent")
        if not out.recipients():
            raise ToolError("no recipients - nothing was sent")
        self.outlook.count_send(out.account)
        try:
            if out.draft_id:
                now = await self.graph.call(
                    out.account.id,
                    "GET",
                    f"/me/messages/{path_id(out.draft_id)}",
                    params={"$select": "changeKey"},
                )
                if str(now.get("changeKey") or "") != out.draft_key:
                    raise ToolError(
                        "the draft changed since it was shown - nothing was sent; look again"
                    )
                await self.graph.call(
                    out.account.id,
                    "POST",
                    f"/me/messages/{path_id(out.draft_id)}/send",
                    retry=False,
                )
            else:
                await self.graph.call(
                    out.account.id,
                    "POST",
                    "/me/sendMail",
                    data=as_mime_body(out.build()),
                    content_type="text/plain",
                    retry=False,
                )
        except GraphError as exc:
            if exc.status >= 500 or exc.status == 429:
                raise ToolError(
                    f"the send may not have gone: {exc} - look in Sent Items before asking again"
                ) from None
            raise ToolError(f"{exc} - nothing was sent") from None
        self.outlook.record_send(out)
        files = f" with {len(out.files)} file(s)" if out.files else ""
        return f"Sent to {', '.join(out.recipients())}: {out.subject or '(no subject)'}{files}."


# -- mail: organising -------------------------------------------------------------------------

ACTIONS = {
    "archive": "Archive",
    "inbox": "Move to Inbox",
    "read": "Mark read",
    "unread": "Mark unread",
    "flag": "Flag",
    "unflag": "Unflag",
    "label": "Add the category",
    "unlabel": "Remove the category",
    "move": "Move to",
    "trash": "Move to Deleted Items",
}


class MailOrganise(Carded):
    name = "outlook_mail_organise"
    description = (
        "Tidy Outlook mail: archive, inbox, read, unread, flag, unflag, label or unlabel (an "
        "Outlook category), move (to a folder), or trash (Deleted Items) - by message ids, or "
        "by a search across every folder. At most 50 at once. Nothing is deleted for good."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(ACTIONS)},
            "message_ids": {"type": "array", "items": {"type": "string"}},
            "query": {
                "type": "string",
                "description": "An Outlook search naming the messages instead.",
            },
            "label": {"type": "string", "description": "The category, for label and unlabel."},
            "folder": {"type": "string", "description": "The folder, for move."},
            "account": ACCOUNT,
        },
        "required": ["action"],
    }
    gated = True

    async def subject(self, arguments: Mapping[str, Any]) -> Subject | None:
        try:
            aimed = await self.aim(arguments)
        except (CredentialError, ToolError):
            return None
        self._remember(arguments, aimed)
        connection, items, senders = aimed
        action = str(arguments.get("action"))
        extra = (
            f" {arguments.get('label')!r}"
            if action in ("label", "unlabel")
            else f" {arguments.get('folder')!r}"
            if action == "move"
            else ""
        )
        return Subject(
            tool=self.name,
            action=action,
            summary=(
                f"{ACTIONS[action]}{extra} {len(items)} message(s) from {senders} "
                f"({label_of(connection)})"
            ),
        )

    async def aim(self, arguments: Mapping[str, Any]) -> tuple[str, list[Mapping[str, Any]], str]:
        action = str(arguments.get("action", ""))
        if action not in ACTIONS:
            raise ToolError(f"action is one of {', '.join(sorted(ACTIONS))}")
        if action in ("label", "unlabel") and not str(arguments.get("label", "") or "").strip():
            raise ToolError("say which category")
        if action == "move" and not str(arguments.get("folder", "") or "").strip():
            raise ToolError("say which folder")
        ids = [str(i) for i in arguments.get("message_ids") or ()]
        query = str(arguments.get("query", "") or "").strip()
        if bool(ids) == bool(query):
            raise ToolError("give message_ids or a query, one of them")
        items: list[Mapping[str, Any]] = []
        if query:
            connection = self.graph.pick(str(arguments.get("account", "") or ""), write=True)[0]
            found, more = await search(self.graph, connection, query, "anywhere", ORGANISE_MAX)
            if more:
                raise ToolError(
                    f"more than {ORGANISE_MAX} messages match - at most {ORGANISE_MAX} at once; "
                    "narrow it"
                )
            items = found
        else:
            if len(ids) > ORGANISE_MAX:
                raise ToolError(f"{len(ids)} messages - at most {ORGANISE_MAX} at once")
            taken = [self.outlook.handles.take("m", i) for i in ids]
            accounts = {a for a, _ in taken}
            if len(accounts) > 1:
                raise ToolError("those messages are in different accounts - one account at a time")
            connection = taken[0][0]
            for _, graph_id in taken:
                items.append(
                    await self.graph.call(
                        connection,
                        "GET",
                        f"/me/messages/{path_id(graph_id)}",
                        params={"$select": MESSAGE_FIELDS},
                    )
                )
        if not items:
            raise ToolError("no messages match")
        names = [who(i).split(" <")[0] for i in items]
        common = sorted(set(names), key=names.count, reverse=True)
        senders = ", ".join(common[:3]) + (
            f" and {len(common) - 3} more" if len(common) > 3 else ""
        )
        return connection, items, senders

    async def act(self, **arguments: Any) -> str:
        aimed = self._recall(arguments) or await self.aim(arguments)
        connection, items, _ = aimed
        action = str(arguments["action"])
        label = str(arguments.get("label", "") or "").strip()
        target = ""
        if action in ("archive", "inbox", "trash"):
            target = {"archive": "archive", "inbox": "inbox", "trash": "deleteditems"}[action]
        elif action == "move":
            target = await folder_id(self.graph, connection, str(arguments.get("folder")))
        for item in items:
            where = f"/me/messages/{path_id(str(item['id']))}"
            if target:
                await self.graph.call(
                    connection, "POST", f"{where}/move", body={"destinationId": target}
                )
                continue
            if action in ("read", "unread"):
                change: dict[str, Any] = {"isRead": action == "read"}
            elif action in ("flag", "unflag"):
                change = {"flag": {"flagStatus": "flagged" if action == "flag" else "notFlagged"}}
            else:
                now = [str(c) for c in item.get("categories") or []]
                kept = [c for c in now if c.lower() != label.lower()]
                change = {"categories": [*kept, label] if action == "label" else kept}
            await self.graph.call(connection, "PATCH", where, body=change)
        extra = (
            f" {label!r}" if label else f" {arguments.get('folder')!r}" if action == "move" else ""
        )
        return f"{ACTIONS[action]}{extra}: {len(items)} message(s)."


# -- calendar: time (R7.2) --------------------------------------------------------------------

DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
CLOCK_TIME = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
UTC_PREFER = 'outlook.timezone="UTC"'
TEXT_PREFER = 'outlook.body-content-type="text"'
EVENT_FIELDS = (
    "id,subject,start,end,isAllDay,location,organizer,attendees,showAs,responseStatus,type,"
    "seriesMasterId,isCancelled,isOrganizer,changeKey,onlineMeeting"
)

Moment = datetime | date
"""A time, or - for an all-day event - a date."""


def local() -> tzinfo:
    return datetime.now().astimezone().tzinfo or UTC


def parse_when(value: str) -> Moment:
    """A local time - `2026-10-02T15:00` - or a date, for an all-day event."""
    text = str(value or "").strip()
    if DATE_ONLY.fullmatch(text):
        return date.fromisoformat(text)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(
            f"{value!r} is not a time - write 2026-10-02T15:00, or 2026-10-02"
        ) from None
    return moment if moment.tzinfo else moment.replace(tzinfo=local())


def timed(value: Moment | None) -> bool:
    return isinstance(value, datetime)


def to_graph(value: Moment) -> dict[str, str]:
    """R7.2: Graph is sent UTC; an all-day event is its date, never shifted."""
    if isinstance(value, datetime):
        moment = value.astimezone(UTC).replace(tzinfo=None)
        return {"dateTime": moment.isoformat(timespec="seconds"), "timeZone": "UTC"}
    return {"dateTime": f"{value.isoformat()}T00:00:00", "timeZone": "UTC"}


def times_of(event: Mapping[str, Any]) -> tuple[Moment | None, Moment | None]:
    """An event's start and end: local times, or for an all-day event its dates (end exclusive)."""
    start, end = event.get("start") or {}, event.get("end") or {}
    if event.get("isAllDay"):
        try:
            return (
                date.fromisoformat(str(start.get("dateTime"))[:10]),
                date.fromisoformat(str(end.get("dateTime"))[:10]),
            )
        except ValueError:
            return None, None
    first, last = graph_moment(start.get("dateTime")), graph_moment(end.get("dateTime"))
    return (
        first.astimezone(local()) if first else None,
        last.astimezone(local()) if last else None,
    )


def day_name(day: date) -> str:
    return f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"


def span_of(start: Moment | None, end: Moment | None) -> str:
    if start is None:
        return "(no time)"
    if not isinstance(start, datetime):
        last = end - timedelta(days=1) if end is not None and not timed(end) else start
        if last <= start:
            return f"{day_name(start)} (all day)"
        return f"{day_name(start)} - {day_name(last)} (all day)"
    clock = f"{start:%H:%M}"
    if not isinstance(end, datetime):
        return f"{day_name(start.date())} {clock}"
    if end.date() == start.date():
        return f"{day_name(start.date())} {clock}-{end:%H:%M}"
    return f"{day_name(start.date())} {clock} - {day_name(end.date())} {end:%H:%M}"


def span(event: Mapping[str, Any]) -> str:
    return span_of(*times_of(event))


def title_of(event: Mapping[str, Any]) -> str:
    return " ".join(str(event.get("subject") or "(no title)").split())[:TITLE_MAX]


def place_of(event: Mapping[str, Any]) -> str:
    return " ".join(str((event.get("location") or {}).get("displayName") or "").split())[:TITLE_MAX]


def people_of(event: Mapping[str, Any]) -> list[str]:
    found = []
    for attendee in event.get("attendees") or []:
        address = str(((attendee or {}).get("emailAddress") or {}).get("address") or "")
        if address:
            found.append(address)
    return found


def organiser_of(event: Mapping[str, Any]) -> str:
    person = (event.get("organizer") or {}).get("emailAddress") or {}
    name, address = str(person.get("name") or ""), str(person.get("address") or "")
    return f"{name} <{address}>" if name and address and name != address else name or address


ANSWERS = {
    "accepted": "accepted",
    "tentativelyAccepted": "tentative",
    "declined": "declined",
    "notResponded": "not answered",
}


def event_marks(event: Mapping[str, Any]) -> str:
    marks = []
    if event.get("isCancelled"):
        marks.append("cancelled")
    if event.get("isOrganizer") and event.get("attendees"):
        marks.append(f"you organise, {len(event.get('attendees') or [])} invited")
    answer = ANSWERS.get(str((event.get("responseStatus") or {}).get("response") or ""), "")
    if answer and not event.get("isOrganizer"):
        marks.append(answer)
    if event.get("type") in ("occurrence", "exception"):
        marks.append("repeats")
    if event.get("showAs") in ("free", "tentative"):
        marks.append(f"shown as {event.get('showAs')}")
    return ", ".join(marks)


async def calendars(graph: Graph, account: str) -> list[Mapping[str, Any]]:
    data = await graph.call(
        account,
        "GET",
        "/me/calendars",
        params={"$top": 100, "$select": "id,name,isDefaultCalendar,canEdit"},
    )
    return [c for c in data.get("value", []) if isinstance(c, dict) and c.get("id")]


async def calendar_named(graph: Graph, account: str, name: str) -> Mapping[str, Any]:
    """R5.2: a calendar by its name - or the default - from Graph's own list."""
    found = await calendars(graph, account)
    wanted = name.strip().lower()
    for entry in found:
        if (not wanted and entry.get("isDefaultCalendar")) or (
            wanted and str(entry.get("name") or "").lower() == wanted
        ):
            return entry
    if not wanted and found:
        return found[0]
    names = ", ".join(str(c.get("name")) for c in found)
    raise ToolError(f"no calendar {name!r} - this account has: {names}")


async def get_event(
    graph: Graph, account: str, graph_id: str, *, text: bool = False
) -> dict[str, Any]:
    fields = EVENT_FIELDS + (",body,recurrence" if text else "")
    data = await graph.call(
        account,
        "GET",
        f"/me/events/{path_id(graph_id)}",
        params={"$select": fields},
        prefer=(UTC_PREFER, TEXT_PREFER) if text else (UTC_PREFER,),
    )
    if not isinstance(data, dict) or not data.get("id"):
        raise ToolError("Microsoft has no such event - search again")
    return data


async def view(
    graph: Graph, account: str, calendar_id: str, start: datetime, end: datetime, limit: int
) -> tuple[list[Mapping[str, Any]], bool]:
    """Occurrences between two moments, recurring events expanded, in start order."""
    data = await graph.call(
        account,
        "GET",
        f"/me/calendars/{path_id(calendar_id)}/calendarView",
        params={
            "startDateTime": start.astimezone(UTC).isoformat(timespec="seconds"),
            "endDateTime": end.astimezone(UTC).isoformat(timespec="seconds"),
            "$orderby": "start/dateTime",
            "$top": limit + 1,
            "$select": EVENT_FIELDS,
        },
        prefer=(UTC_PREFER,),
    )
    items: list[Mapping[str, Any]] = [
        e for e in data.get("value", []) if isinstance(e, dict) and e.get("id")
    ]
    return items[:limit], len(items) > limit or bool(data.get("@odata.nextLink"))


def window(start: str, end: str, *, days: int) -> tuple[datetime, datetime]:
    """A listing's span: given times; a date's local midnight - for an end, the whole of
    that day; with neither, now and `days` on."""
    first = datetime.now(local())
    if start:
        given = parse_when(start)
        first = (
            given
            if isinstance(given, datetime)
            else datetime.combine(given, datetime.min.time(), tzinfo=local())
        )
    last = first + timedelta(days=days)
    if end:
        given = parse_when(end)
        last = (
            given
            if isinstance(given, datetime)
            else datetime.combine(given + timedelta(days=1), datetime.min.time(), tzinfo=local())
        )
    if last <= first:
        raise ToolError("the end is not after the start")
    return first, last


def stretch(first: datetime, last: datetime) -> str:
    return f"{day_name(first.date())} {first:%H:%M} to {day_name(last.date())} {last:%H:%M}"


# -- calendar: reading ------------------------------------------------------------------------


class ListCalendars(OutlookTool):
    name = "outlook_calendar_list_calendars"
    description = "The Outlook account's calendars, by name, the default one marked."
    parameters = {"type": "object", "properties": {"account": ACCOUNT}, "required": []}

    async def act(self, account: str = "") -> str:
        lines = []
        for connection in self.graph.pick(account, write=False):
            for entry in await calendars(self.graph, connection):
                marks = []
                if entry.get("isDefaultCalendar"):
                    marks.append("default")
                if entry.get("canEdit") is False:
                    marks.append("read-only")
                lines.append(
                    f"{label_of(connection)}: {entry.get('name')}"
                    + (f" ({', '.join(marks)})" if marks else "")
                )
        return "\n".join(lines) or "No calendars."


class ListEvents(OutlookTool):
    name = "outlook_calendar_list_events"
    description = (
        "What is on an Outlook calendar between two times (default: now to seven days on), "
        "recurring events expanded, in start order, with each event's id. Times are local: "
        "2026-10-02T09:00, or a date. calendar is a name, or * for all; query narrows by title."
    )
    parameters = {
        "type": "object",
        "properties": {
            "start": {"type": "string"},
            "end": {"type": "string"},
            "calendar": {
                "type": "string",
                "description": "A calendar's name, or *. Default: the default one.",
            },
            "query": {"type": "string", "description": "Words the title must contain."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
            "account": ACCOUNT,
        },
        "required": [],
    }

    async def act(
        self,
        start: str = "",
        end: str = "",
        calendar: str = "",
        query: str = "",
        max_results: int = 0,
        account: str = "",
    ) -> str:
        first, last = window(start, end, days=7)
        limit = max(1, min(int(max_results or self.outlook.number("max_results", 20)), 100))
        accounts = self.graph.pick(account, write=False)
        found: list[tuple[str, str]] = []
        more = False
        for connection in accounts:
            if calendar.strip() == "*":
                chosen = await calendars(self.graph, connection)
            else:
                chosen = [await calendar_named(self.graph, connection, calendar)]
            for entry in chosen:
                events, extra = await view(
                    self.graph, connection, str(entry["id"]), first, last, limit
                )
                more = more or extra
                for event in events:
                    if query and query.lower() not in title_of(event).lower():
                        continue
                    where = (f" · {entry.get('name')}" if len(chosen) > 1 else "") + (
                        f" · {label_of(connection)}" if len(accounts) > 1 else ""
                    )
                    begins = str((event.get("start") or {}).get("dateTime") or "")
                    found.append((begins, self._line(connection, event, where)))
        found.sort(key=lambda pair: pair[0])
        if not found:
            return f"Nothing on, {stretch(first, last)}."
        text = f"{len(found[:limit])} event(s), {stretch(first, last)}:\n" + "\n".join(
            t for _, t in found[:limit]
        )
        if more or len(found) > limit:
            text += "\n(more - narrow the time to see them)"
        return text

    def _line(self, connection: str, event: Mapping[str, Any], where: str) -> str:
        handle = self.outlook.handles.give("e", connection, str(event["id"]))
        place, marks = place_of(event), event_marks(event)
        return (
            f"{span(event)} · {title_of(event)}"
            + (f" · at {place}" if place else "")
            + (f" · {marks}" if marks else "")
            + f"  [id: {handle}{where}]"
        )


class GetEvent(OutlookTool):
    name = "outlook_calendar_get_event"
    description = (
        "One Outlook event in full: times, place, organiser, attendees and their answers, the "
        "online-meeting link, how it repeats, and its description."
    )
    parameters = {
        "type": "object",
        "properties": {"event_id": EVENT_ID},
        "required": ["event_id"],
    }

    async def act(self, event_id: str) -> str:
        account, graph_id = self.outlook.handles.take("e", event_id)
        event = await get_event(self.graph, account, graph_id, text=True)
        lines = [f"Title: {title_of(event)}", f"When: {span(event)}"]
        if place_of(event):
            lines.append(f"Where: {place_of(event)}")
        if organiser_of(event):
            mine = " (you)" if event.get("isOrganizer") else ""
            lines.append(f"Organiser: {organiser_of(event)}{mine}")
        attendees = event.get("attendees") or []
        if attendees:
            lines.append("Attendees:")
            for attendee in attendees:
                person = (attendee or {}).get("emailAddress") or {}
                status = ((attendee or {}).get("status") or {}).get("response") or ""
                answer = ANSWERS.get(str(status), "")
                name = person.get("name") or person.get("address")
                lines.append(
                    f"  {name} <{person.get('address')}>" + (f" - {answer}" if answer else "")
                )
        joining = str((event.get("onlineMeeting") or {}).get("joinUrl") or "")
        if joining:
            lines.append(f"Online meeting: {joining}")
        pattern = ((event.get("recurrence") or {}).get("pattern") or {}).get("type")
        if pattern or event.get("seriesMasterId"):
            lines.append(f"Repeats: {pattern or 'yes'}")
        marks = event_marks(event)
        if marks:
            lines.append(f"Marks: {marks}")
        text = str((event.get("body") or {}).get("content") or "").strip()
        if text:
            cut = " [...]" if len(text) > DESCRIPTION_MAX else ""
            lines += ["", text[:DESCRIPTION_MAX] + cut]
        lines.append(f"[id: {self.outlook.handles.give('e', account, graph_id)}]")
        return "\n".join(lines)


def _merge(blocks: Sequence[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(blocks):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _clock(value: Any, default: str) -> timedelta:
    match = CLOCK_TIME.fullmatch(str(value or default).strip()) or CLOCK_TIME.fullmatch(default)
    assert match is not None
    return timedelta(hours=int(match.group(1)), minutes=int(match.group(2)))


def _midnight(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=local())


class FreeBusy(OutlookTool):
    name = "outlook_calendar_free_busy"
    description = (
        "When the person is busy and free between two dates on their default Outlook "
        "calendar, inside their day (day_start to day_end in settings): busy blocks, and free "
        "gaps of at least minutes (default 30)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "A date or time. Default: now."},
            "end": {"type": "string", "description": "A date or time. Default: seven days on."},
            "minutes": {"type": "integer", "minimum": 5},
            "account": ACCOUNT,
        },
        "required": [],
    }

    async def act(
        self, start: str = "", end: str = "", minutes: int = 30, account: str = ""
    ) -> str:
        first, last = window(start, end, days=7)
        busy: list[tuple[datetime, datetime]] = []
        for connection in self.graph.pick(account, write=False):
            entry = await calendar_named(self.graph, connection, "")
            events, _ = await view(self.graph, connection, str(entry["id"]), first, last, 100)
            for event in events:
                if event.get("isCancelled") or event.get("showAs") in ("free", "workingElsewhere"):
                    continue
                if (event.get("responseStatus") or {}).get("response") == "declined":
                    continue
                begin, finish = times_of(event)
                if isinstance(begin, datetime) and isinstance(finish, datetime):
                    busy.append((begin, finish))
                elif begin is not None and finish is not None:
                    busy.append((_midnight(begin), _midnight(finish)))
        blocks = _merge(busy)
        opens = _clock(self.outlook.setting("day_start", "08:00"), "08:00")
        closes = _clock(self.outlook.setting("day_end", "18:00"), "18:00")
        least = timedelta(minutes=max(5, int(minutes or 30)))
        gaps: list[tuple[datetime, datetime]] = []
        day = first.date()
        while day <= last.date():
            cursor = max(_midnight(day) + opens, first)
            stop = min(_midnight(day) + closes, last)
            for begin, finish in blocks:
                if finish <= cursor or begin >= stop:
                    continue
                if begin - cursor >= least:
                    gaps.append((cursor, begin))
                cursor = max(cursor, finish)
            if stop - cursor >= least:
                gaps.append((cursor, stop))
            day += timedelta(days=1)
        shown = [f"  {span_of(b, f)}" for b, f in blocks if f > first and b < last]
        lines = ["Busy:"] + (shown or ["  nothing"])
        lines.append(f"Free (at least {int(least.total_seconds() // 60)} minutes):")
        lines += [f"  {span_of(b, f)}" for b, f in gaps] or ["  no gap that long"]
        return "\n".join(lines)


# -- calendar: changing (R7.3-R7.5) -----------------------------------------------------------


class Change:
    """A calendar change worked out for its card, and the one call that makes it."""

    def __init__(self, card: str, run: Callable[[], Awaitable[str]]) -> None:
        self.card = card
        self.run = run


class CalendarChange(Carded):
    """Every change is a confirm card, drawn from what is really there."""

    gated = True
    verb = "change"

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        raise NotImplementedError

    async def subject(self, arguments: Mapping[str, Any]) -> Subject | None:
        try:
            change = await self.prepare(arguments)
        except (CredentialError, ToolError):
            return None  # `run` fails the same way, before anything changes
        self._remember(arguments, change)
        return Subject(tool=self.name, action=self.verb, summary=change.card, confirm=True)

    async def act(self, **arguments: Any) -> str:
        change: Change | None = self._recall(arguments)
        if change is None:
            await self.prepare(arguments)  # says why no card could be drawn, when one could not
            raise ToolError(
                "a calendar change has to be shown to the person first; nothing was changed"
            )
        return await change.run()

    async def unchanged(self, account: str, graph_id: str, key: str) -> None:
        """R7.4: the event is still the one the card showed."""
        now = await get_event(self.graph, account, graph_id)
        if str(now.get("changeKey") or "") != key:
            raise ToolError(
                "the event changed since it was shown - nothing was changed; look again"
            )


def _emailed(people: Sequence[str], what: str) -> list[str]:
    return [f"{what}: {', '.join(people)}"] if people else []


class CreateEvent(CalendarChange):
    name = "outlook_calendar_create_event"
    verb = "create"
    description = (
        "Create an Outlook event. start is local - 2026-10-02T15:00 - or a date for an all-day "
        "event; give end (for all day, the last day) or duration_minutes (default 60). "
        "Attendees are emailed invitations by Outlook. The person sees everything, and who "
        "will be invited, and must say yes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "duration_minutes": {"type": "integer", "minimum": 1},
            "location": {"type": "string"},
            "description": {"type": "string"},
            "attendees": {"type": "array", "items": {"type": "string"}},
            "online_meeting": {"type": "boolean", "description": "Add a Teams meeting link."},
            "calendar": {
                "type": "string",
                "description": "A calendar's name. Default: the default one.",
            },
            "account": ACCOUNT,
        },
        "required": ["title", "start"],
    }

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        connection = self.graph.pick(str(arguments.get("account", "") or ""), write=True)[0]
        entry = await calendar_named(
            self.graph, connection, str(arguments.get("calendar", "") or "")
        )
        title = " ".join(str(arguments.get("title") or "").split())[:TITLE_MAX]
        if not title:
            raise ToolError("say what the event is called")
        start = parse_when(str(arguments.get("start") or ""))
        end: Moment
        if isinstance(start, datetime):
            if arguments.get("end"):
                end = parse_when(str(arguments.get("end")))
            else:
                end = start + timedelta(minutes=int(arguments.get("duration_minutes") or 60))
        else:
            last = parse_when(str(arguments.get("end"))) if arguments.get("end") else start
            end = last + timedelta(days=1)  # the end Graph wants is the day after the last
        if timed(start) != timed(end):
            raise ToolError("start and end are both times, or both dates for an all-day event")
        if end <= start:  # type: ignore[operator]
            raise ToolError("the end is not after the start")
        people = _addresses(arguments.get("attendees"))
        place = str(arguments.get("location") or "").strip()
        notes = str(arguments.get("description") or "").strip()
        online = bool(arguments.get("online_meeting"))
        lines = [
            f"Create in {entry.get('name')} ({label_of(connection)})",
            f"Title: {title}",
            f"When: {span_of(start, end)}",
        ]
        lines += [f"Where: {place}"] if place else []
        lines += ["Online meeting: a Teams link will be added"] if online else []
        lines += _emailed(people, "Invitations will be emailed to")
        lines += ["", notes] if notes else []
        body: dict[str, Any] = {
            "subject": title,
            "start": to_graph(start),
            "end": to_graph(end),
            "isAllDay": not timed(start),
        }
        if place:
            body["location"] = {"displayName": place}
        if notes:
            body["body"] = {"contentType": "text", "content": notes}
        if people:
            body["attendees"] = [
                {"emailAddress": {"address": p}, "type": "required"} for p in people
            ]
        if online:
            body["isOnlineMeeting"] = True
        target = f"/me/calendars/{path_id(str(entry['id']))}/events"

        async def run() -> str:
            made = await self.graph.call(connection, "POST", target, body=body, retry=False)
            handle = self.outlook.handles.give("e", connection, str(made.get("id") or ""))
            invited = f"; invitations sent to {', '.join(people)}" if people else ""
            return f"Created {title}, {span_of(start, end)}{invited} [id: {handle}]."

        return Change("\n".join(lines), run)


async def _aim(outlook: Outlook, event_id: str, scope: Any) -> tuple[str, str, dict[str, Any]]:
    """The event a change is for: this occurrence, or with `scope: series` its series."""
    account, graph_id = outlook.handles.take("e", event_id)
    event = await get_event(outlook.graph, account, graph_id)
    if str(scope or "this") == "series":
        master = str(event.get("seriesMasterId") or "")
        if not master:
            raise ToolError("that event does not repeat - leave scope out")
        graph_id = master
        event = await get_event(outlook.graph, account, graph_id)
    return account, graph_id, event


SCOPE = {
    "type": "string",
    "enum": ["this", "series"],
    "description": "This occurrence (default) or the whole series.",
}


class UpdateEvent(CalendarChange):
    name = "outlook_calendar_update_event"
    verb = "update"
    description = (
        "Change an Outlook event you organise - title, times, place, description, or invite "
        "more people - this occurrence or the whole series. Moving start alone keeps the "
        "length. The person sees each change from and to, and who will be emailed it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "event_id": EVENT_ID,
            "title": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "location": {"type": "string"},
            "description": {"type": "string"},
            "add_attendees": {"type": "array", "items": {"type": "string"}},
            "scope": SCOPE,
        },
        "required": ["event_id"],
    }

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        account, graph_id, event = await _aim(
            self.outlook, str(arguments.get("event_id")), arguments.get("scope")
        )
        people = people_of(event)
        if not event.get("isOrganizer") and people:
            raise ToolError(
                "you are not the organiser of this one - answer it with outlook_calendar_respond"
            )
        change: dict[str, Any] = {}
        lines = [f"Change {title_of(event)} ({label_of(account)})"]
        title = " ".join(str(arguments.get("title") or "").split())[:TITLE_MAX]
        if title and title != title_of(event):
            change["subject"] = title
            lines.append(f"Title: {title_of(event)} -> {title}")
        old_start, old_end = times_of(event)
        if arguments.get("start") or arguments.get("end"):
            start = parse_when(str(arguments.get("start"))) if arguments.get("start") else old_start
            end: Moment | None
            if arguments.get("end"):
                end = parse_when(str(arguments.get("end")))
                if end is not None and not timed(end):
                    end = end + timedelta(days=1)
            elif (
                start is not None
                and old_start is not None
                and old_end is not None
                and timed(start) == timed(old_start)
            ):
                end = start + (old_end - old_start)  # type: ignore[operator]
            else:
                raise ToolError("say the new end too")
            if start is None or end is None or timed(start) != timed(end) or end <= start:  # type: ignore[operator]
                raise ToolError("the end is not after the start")
            change["start"], change["end"] = to_graph(start), to_graph(end)
            change["isAllDay"] = not timed(start)
            lines.append(f"When: {span_of(old_start, old_end)} -> {span_of(start, end)}")
        place = str(arguments.get("location") or "").strip()
        if place and place != place_of(event):
            change["location"] = {"displayName": place}
            lines.append(f"Where: {place_of(event) or '(none)'} -> {place}")
        notes = str(arguments.get("description") or "").strip()
        if notes:
            change["body"] = {"contentType": "text", "content": notes}
            lines.append(f"Description -> {notes[:DESCRIPTION_MAX]}")
        known = {p.lower() for p in people}
        added = [p for p in _addresses(arguments.get("add_attendees")) if p.lower() not in known]
        if added:
            change["attendees"] = list(event.get("attendees") or []) + [
                {"emailAddress": {"address": p}, "type": "required"} for p in added
            ]
            lines.append(f"Invite: {', '.join(added)}")
        if not change:
            raise ToolError("nothing to change - say what is different")
        if str(arguments.get("scope") or "this") == "series":
            lines.append("(the whole series)")
        told = people + added
        lines += _emailed(told, "The change will be emailed to")
        key = str(event.get("changeKey") or "")

        async def run() -> str:
            await self.unchanged(account, graph_id, key)
            await self.graph.call(
                account, "PATCH", f"/me/events/{path_id(graph_id)}", body=change, retry=False
            )
            return (
                f"Changed {title_of(event)}"
                + (f"; {', '.join(told)} emailed" if told else "")
                + "."
            )

        return Change("\n".join(lines), run)


class DeleteEvent(CalendarChange):
    name = "outlook_calendar_delete_event"
    verb = "delete"
    description = (
        "Remove an Outlook event, this occurrence or the whole series. As its organiser with "
        "attendees, it is cancelled and they are emailed, with message if given. The person "
        "sees the event and who is told, and must say yes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "event_id": EVENT_ID,
            "scope": SCOPE,
            "message": {"type": "string", "description": "A note sent with a cancellation."},
        },
        "required": ["event_id"],
    }

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        account, graph_id, event = await _aim(
            self.outlook, str(arguments.get("event_id")), arguments.get("scope")
        )
        people = people_of(event)
        cancel = bool(event.get("isOrganizer")) and bool(people)
        note = str(arguments.get("message") or "").strip()
        series = " (the whole series)" if str(arguments.get("scope") or "this") == "series" else ""
        verb = "Cancel" if cancel else "Delete"
        lines = [f"{verb} {title_of(event)}{series} ({label_of(account)})", f"When: {span(event)}"]
        if cancel:
            lines += _emailed(people, "The cancellation will be emailed to")
            lines += ["", note] if note else []
        elif not event.get("isOrganizer") and organiser_of(event):
            lines.append(
                f"The organiser, {organiser_of(event)}, is not told - decline it instead to "
                "tell them"
            )
        key = str(event.get("changeKey") or "")
        where = f"/me/events/{path_id(graph_id)}"

        async def run() -> str:
            await self.unchanged(account, graph_id, key)
            if cancel:
                await self.graph.call(
                    account, "POST", f"{where}/cancel", body={"Comment": note}, retry=False
                )
                return f"Cancelled {title_of(event)}; {', '.join(people)} emailed."
            await self.graph.call(account, "DELETE", where, retry=False)
            return f"Deleted {title_of(event)} from the calendar."

        return Change("\n".join(lines), run)


RESPONSES = {
    "accept": ("accept", "Accept", "Accepted"),
    "decline": ("decline", "Decline", "Declined"),
    "tentative": ("tentativelyAccept", "Tentatively accept", "Tentatively accepted"),
}


class Respond(CalendarChange):
    name = "outlook_calendar_respond"
    verb = "respond"
    description = (
        "Answer an Outlook invitation - accept, decline or tentative - with an optional "
        "comment. The organiser is emailed the answer unless notify is false. The person must "
        "say yes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "event_id": EVENT_ID,
            "response": {"type": "string", "enum": sorted(RESPONSES)},
            "comment": {"type": "string"},
            "notify": {
                "type": "boolean",
                "description": "Email the organiser the answer. Default true.",
            },
        },
        "required": ["event_id", "response"],
    }

    async def prepare(self, arguments: Mapping[str, Any]) -> Change:
        response = str(arguments.get("response") or "")
        if response not in RESPONSES:
            raise ToolError(f"response is one of {', '.join(sorted(RESPONSES))}")
        account, graph_id = self.outlook.handles.take("e", str(arguments.get("event_id")))
        event = await get_event(self.graph, account, graph_id)
        if event.get("isOrganizer"):
            raise ToolError("you organise this one - there is no invitation to answer")
        notify = arguments.get("notify", True) is not False
        comment = str(arguments.get("comment") or "").strip()
        path, verb, done = RESPONSES[response]
        organiser = organiser_of(event)
        lines = [f"{verb}: {title_of(event)} ({label_of(account)})", f"When: {span(event)}"]
        lines += [f"From: {organiser}"] if organiser else []
        if notify:
            lines.append(
                f"The organiser{f', {organiser},' if organiser else ''} will be emailed your answer"
            )
        else:
            lines.append("The organiser is not told")
        lines += ["", comment] if comment else []
        target = f"/me/events/{path_id(graph_id)}/{path}"

        async def run() -> str:
            await self.graph.call(
                account,
                "POST",
                target,
                body={"comment": comment, "sendResponse": notify},
                retry=False,
            )
            return (
                f"{done} {title_of(event)}"
                + (f"; {organiser} emailed" if notify and organiser else "")
                + "."
            )

        return Change("\n".join(lines), run)


# -- new mail (§8) ----------------------------------------------------------------------------


class Seen:
    """R8.2: per connection, the last `receivedDateTime` seen and the ids at that instant."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.rows: dict[str, dict[str, Any]] = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            self.rows = {}

    def save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.rows, indent=1, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)


INBOX = "/me/mailFolders/inbox/messages"
NEW_FIELDS = "id,from,subject,receivedDateTime,inferenceClassification,isRead"


async def check_new(
    graph: Graph, account: str, last: Mapping[str, Any] | None, mode: str
) -> tuple[dict[str, Any], list[str]]:
    """New unread inbox mail since `last`: the new position, and the lines worth saying."""
    if not last:  # R8.2: a first run records where the inbox is and says nothing
        data = await graph.call(
            account,
            "GET",
            INBOX,
            params={"$top": 1, "$orderby": "receivedDateTime desc", "$select": NEW_FIELDS},
        )
        top = [m for m in data.get("value", []) if isinstance(m, dict)]
        now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        after = str(top[0].get("receivedDateTime")) if top else now
        return {"after": after, "ids": [str(m.get("id")) for m in top]}, []
    after, seen_ids = str(last.get("after") or ""), set(last.get("ids") or [])
    data = await graph.call(
        account,
        "GET",
        INBOX,
        params={
            "$filter": f"receivedDateTime ge {after} and isRead eq false",
            "$orderby": "receivedDateTime asc",
            "$top": 50,
            "$select": NEW_FIELDS,
        },
    )
    items = [
        m for m in data.get("value", []) if isinstance(m, dict) and str(m.get("id")) not in seen_ids
    ]
    if not items:
        return dict(last), []
    newest = max(str(m.get("receivedDateTime") or "") for m in items)
    at_newest = [str(m.get("id")) for m in items if str(m.get("receivedDateTime") or "") == newest]
    position = {"after": newest, "ids": at_newest + (sorted(seen_ids) if newest == after else [])}
    mine = (await graph.address(account)).lower()
    found = []
    for item in items:
        sender = (item.get("from") or {}).get("emailAddress") or {}
        if str(sender.get("address") or "").lower() == mine:
            continue
        if mode == "focused" and item.get("inferenceClassification") != "focused":
            continue
        subject = " ".join(str(item.get("subject") or "").split()) or "(no subject)"
        if len(subject) > SUBJECT_MAX:
            subject = subject[: SUBJECT_MAX - 1] + "…"
        name = sender.get("name") or sender.get("address") or "(unknown sender)"
        found.append(f"{name}\x00{subject}")
    return position, found


def notice(found: Sequence[str], label: str, several: bool) -> str:
    where = f" ({label})" if several else ""
    if len(found) <= 2:
        return "\n".join(f"📧 {s.split(chr(0))[0]} - {s.split(chr(0))[1]}{where}" for s in found)
    names = list(dict.fromkeys(s.split("\x00")[0] for s in found))
    shown = ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
    return f"📧 {len(found)} new emails{where}: {shown}"


class NewMail(Service):
    """§8: check each inbox, say what Outlook put in Focused, act on nothing."""

    async def run(self, ctx: ServiceContext) -> None:
        graph = Graph(Path(ctx.workspace))
        seen = Seen(ctx.state_dir / "seen.json")
        warned: dict[str, str] = {}
        while not ctx.stopping:
            mode = str(ctx.setting("notify", "focused") or "focused")
            try:
                poll = max(1, int(ctx.setting("poll_minutes", 2) or 2)) * 60
            except (TypeError, ValueError):
                poll = 120
            if mode not in ("focused", "all"):
                if await ctx.sleep_for(600):
                    return
                continue
            known = graph.accounts()
            for account in known:
                try:
                    here, found = await check_new(graph, account, seen.rows.get(account), mode)
                    warned.pop(account, None)
                except (CredentialError, GraphError) as exc:
                    refused = isinstance(exc, CredentialError) or exc.status in (401, 403)
                    if refused and warned.get(account) != str(exc):
                        warned[account] = str(exc)  # R8.3: said once
                        await _tell(ctx, f"Microsoft ({label_of(account)}): {exc}")
                    continue
                except (ToolError, OSError, WebError):
                    continue
                seen.rows[account] = here
                seen.save()
                if found:
                    await _tell(ctx, notice(found, label_of(account), len(known) > 1))
            if await ctx.sleep_for(poll):
                return


async def _tell(ctx: ServiceContext, text: str) -> str:
    try:
        return await ctx.notify(text)
    except NotifyError as exc:
        return f"failed: {exc}"


# -- signing in (§4) --------------------------------------------------------------------------


def _label(response: Mapping[str, Any]) -> Mapping[str, str]:
    """R4.3: the address, from the `id_token` - decoded, not verified."""
    token = str(response.get("id_token") or "")
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}
    if not isinstance(claims, dict):
        return {}
    address = str(claims.get("preferred_username") or claims.get("email") or "")
    return {"email": address, "label": address} if address else {}


def tenant_of(value: Any) -> str:
    text = str(value or "").strip() or "common"
    return text if re.fullmatch(r"[A-Za-z0-9.-]{1,64}", text) else "common"


def client(client_id: str, tenant: str = "common") -> OAuthClient:
    base = f"{LOGIN_HOST}/{tenant_of(tenant)}/oauth2/v2.0"
    return OAuthClient(
        authorize_url=f"{base}/authorize",
        token_url=f"{base}/token",
        client_id=client_id,
        scopes=SCOPES,
        authorize_params={"prompt": "select_account"},
        parse=_label,
        label="Microsoft",
    )


def no_client(context: LoginContext) -> Tokens:
    """What signing in does before any client id exists: say how to make one, and stop."""
    raise CredentialError(
        "Microsoft sign-in has no app registration yet. At entra.microsoft.com: App "
        "registrations, New registration; choose 'Accounts in any organizational directory and "
        "personal Microsoft accounts'; under Redirect URI pick 'Public client/native (mobile & "
        "desktop)' and enter http://localhost/callback; register, then copy the Application "
        "(client) ID into plugins_settings.microsoft.client_id in config.json."
    )


MAIL_TOOLS = (
    MailSearch,
    MailRead,
    MailAttachment,
    MailFolders,
    MailDraft,
    MailSend,
    MailOrganise,
)
CALENDAR_TOOLS = (
    ListCalendars,
    ListEvents,
    GetEvent,
    FreeBusy,
    CreateEvent,
    UpdateEvent,
    DeleteEvent,
    Respond,
)


class MicrosoftPlugin(Plugin):
    name = PLUGIN
    description = "Microsoft: Outlook mail and calendar - read, send and change with a yes."

    def register(self, ctx: PluginContext) -> None:
        client_id = str(ctx.setting("client_id", "") or "").strip() or DEFAULT_CLIENT_ID
        tenant = str(ctx.setting("tenant", "common") or "common")
        ctx.register_login(LOGIN, client(client_id, tenant) if client_id else no_client)
        outlook = Outlook(ctx)
        for mail_tool in MAIL_TOOLS:
            ctx.register_tool(mail_tool(outlook), toolset="Outlook mail")
        for calendar_tool in CALENDAR_TOOLS:
            ctx.register_tool(calendar_tool(outlook), toolset="Outlook calendar")
        ctx.register_service("new-mail", NewMail)
