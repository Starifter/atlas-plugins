"""Gmail over IMAP and SMTP with an app password (`docs/spec/gmail.md`).

Seven tools and a service. Every tool is `trusted_only` - offered only in a
session an owner holds - and `untrusted`, because an email is words a stranger
chose, delivered to the model on request. Searching, reading and drafting are
free; a send is a card a person answers in every mode, showing every recipient,
the subject, the whole body and every attached file; organising is gated and
capped. Nothing is ever deleted for good.

The `new-mail` service tells the owner, in one line and with no model, when mail
Gmail marks important arrives.

`imaplib` and `smtplib` are synchronous, so every exchange runs off the event
loop, one account's at a time on its one kept connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import email.policy
import email.utils
import hashlib
import html
import imaplib
import json
import mimetypes
import re
import ssl
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from email.message import EmailMessage, Message
from pathlib import Path
from smtplib import SMTP_SSL, SMTPAuthenticationError, SMTPException
from typing import Any
from urllib.parse import unquote, urlsplit

from atlas.sdk.auth import SecretRef, credentials_in, read_dotenv, resolve
from atlas.sdk.plugin_entry import Plugin, PluginContext
from atlas.sdk.runtime import CredentialError, NetworkPolicyError, ToolError, assert_active
from atlas.sdk.service import NotifyError, Service, ServiceContext
from atlas.sdk.tool_plugin import ImageResult, Subject, Tool, ToolResult
from atlas.sdk.web import WebError, extract, get

IMAP4_SSL = imaplib.IMAP4_SSL
"""Module-level so a test puts a fake Gmail here, as it puts one in `SMTP_SSL`."""

PLUGIN = "gmail"
IMAP_HOST, IMAP_PORT = "imap.gmail.com", 993
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
"""R4.2: fixed. The plugin talks to Gmail and nothing else."""

ALL_MAIL = '"[Gmail]/All Mail"'
DRAFTS = '"[Gmail]/Drafts"'
TRASH = '"[Gmail]/Trash"'
INBOX = "INBOX"
TIMEOUT = 30.0

MAX_TOTAL_BYTES = 25 * 1024 * 1024
"""Gmail's own limit on what one message may carry."""
SCAN_MAX_BYTES = 5 * 1024 * 1024
"""R6.15: a text file up to this size is scanned for credentials."""
ORGANISE_MAX = 50
"""R6.3: more than this at once asks for a narrower query."""
SUBJECT_MAX = 100
KEY_FILES = re.compile(
    r".*\.(pem|key|p12|pfx|kdbx|keystore|jks)$|^id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$", re.I
)
"""R6.15 (2): names shaped like key material."""
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"PK\x03\x04", "application/zip"),
)

NOT_SET_UP = (
    'Gmail is not set up: store an app password (atlas "store my gmail app password") and '
    "your address as GMAIL_ADDRESS - an app password is made at myaccount.google.com, "
    "Security, App passwords, and needs 2-Step Verification on"
)


# -- accounts --------------------------------------------------------------------


class Account:
    """One mailbox: its label, address and app password, and its kept connection."""

    def __init__(self, label: str, address: str, password: str) -> None:
        self.label = label
        self.address = address
        self.password = password
        self._conn: Any = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # never the password
        return f"<Account {self.label} {self.address}>"

    # -- IMAP --------------------------------------------------------------------

    def _connect(self) -> Any:
        conn = IMAP4_SSL(
            IMAP_HOST, IMAP_PORT, ssl_context=ssl.create_default_context(), timeout=TIMEOUT
        )
        try:
            conn.login(self.address, self.password)
        except imaplib.IMAP4.error as exc:
            with contextlib.suppress(Exception):
                conn.logout()
            if b"AUTHENTICATIONFAILED" in str(exc).encode() or "Invalid credentials" in str(exc):
                raise CredentialError(
                    f"Gmail ({self.label}) refused the app password - it was revoked, or the "
                    "account's password changed. Make a new one at myaccount.google.com, "
                    "Security, App passwords"
                ) from None
            raise ToolError(f"Gmail ({self.label}) would not log in: {_said(exc)}") from None
        return conn

    def run(self, work: Callable[[Any], Any]) -> Any:
        """`work(conn)` on this account's connection, reconnecting once if Gmail
        closed it (R4.3). One exchange at a time per account."""
        with self._lock:
            for attempt in range(2):
                if self._conn is None:
                    self._conn = self._connect()
                try:
                    return work(self._conn)
                except (imaplib.IMAP4.abort, OSError, EOFError):
                    self.close_unlocked()
                    if attempt:
                        raise ToolError(f"lost the connection to Gmail ({self.label})") from None
            raise AssertionError("unreachable")  # pragma: no cover

    async def call(self, work: Callable[[Any], Any]) -> Any:
        return await asyncio.to_thread(self.run, work)

    def close_unlocked(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.logout()
        self._conn = None

    # -- SMTP --------------------------------------------------------------------

    def send(self, data: bytes, sender: str, recipients: Sequence[str]) -> None:
        """One SMTP exchange for one message. Never retried (§10): a failure after
        the server accepted the message would otherwise send it twice."""
        try:
            with SMTP_SSL(
                SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=TIMEOUT
            ) as smtp:
                smtp.login(self.address, self.password)
                refused = smtp.sendmail(sender, list(recipients), data)
        except SMTPAuthenticationError:
            raise CredentialError(
                f"Gmail ({self.label}) refused the app password for sending"
            ) from None
        except (SMTPException, OSError) as exc:
            raise ToolError(
                f"the send may not have gone: {type(exc).__name__}: {_said(exc)} - look in Sent "
                "before asking again"
            ) from None
        if refused:
            raise ToolError(f"Gmail refused some recipients: {', '.join(sorted(refused))}")


def _said(exc: BaseException) -> str:
    text = str(exc)
    return text[:200]


class Accounts:
    """R4.1: the accounts in `.env`, read fresh so one stored mid-session is used."""

    def __init__(self, workspace: Path, settings: Mapping[str, Any]) -> None:
        self.workspace = workspace
        self.settings = settings
        self._kept: dict[str, Account] = {}

    def all(self) -> list[Account]:
        address_env = str(self.settings.get("address_env") or "GMAIL_ADDRESS")
        password_env = str(self.settings.get("password_env") or "GMAIL_APP_PASSWORD")
        labels = [
            str(label).strip().lower() for label in self.settings.get("accounts") or ["personal"]
        ]
        dotenv = read_dotenv(self.workspace)
        found: list[Account] = []
        for index, label in enumerate(labels):
            suffix = "" if index == 0 else f"_{label.upper()}"
            address = _read(f"{address_env}{suffix}", dotenv)
            password = _read(f"{password_env}{suffix}", dotenv).replace(" ", "")
            if not address or not password:
                continue
            kept = self._kept.get(label)
            if kept is None or (kept.address, kept.password) != (address, password):
                if kept is not None:
                    with kept._lock:
                        kept.close_unlocked()
                kept = self._kept[label] = Account(label, address, password)
            found.append(kept)
        return found

    def pick(self, label: str, *, write: bool) -> list[Account]:
        """R5.1: the only account, the named one, or - to read - all of them."""
        known = self.all()
        if not known:
            raise CredentialError(NOT_SET_UP)
        if label:
            chosen = [a for a in known if a.label == label.strip().lower()]
            if not chosen:
                raise ToolError(
                    f"no account {label!r} - set up: {', '.join(a.label for a in known)}"
                )
            return chosen
        if len(known) > 1 and write:
            raise ToolError(
                "more than one account is set up; say which with account: "
                + ", ".join(a.label for a in known)
            )
        return known


def _read(variable: str, dotenv: Mapping[str, str]) -> str:
    try:
        return resolve(SecretRef("env", variable), dotenv=dotenv).strip()
    except CredentialError:
        return ""


# -- IMAP, one exchange at a time -------------------------------------------------------


FETCH_ITEM = re.compile(rb"(UID|X-GM-MSGID|X-GM-THRID|RFC822\.SIZE) (\d+)")


class Fetched:
    """One message as a FETCH answered it."""

    def __init__(self) -> None:
        self.uid = 0
        self.msgid = 0
        self.thrid = 0
        self.size = 0
        self.flags: set[str] = set()
        self.labels: set[str] = set()
        self.body = b""

    @property
    def id(self) -> str:
        return f"{self.msgid:x}"

    @property
    def thread(self) -> str:
        return f"{self.thrid:x}"


def _parenthesised(meta: bytes, name: bytes) -> list[str]:
    """The tokens of `NAME (a "b c" \\d)` in a FETCH line."""
    start = meta.find(name + b" (")
    if start < 0:
        return []
    i = start + len(name) + 2
    tokens: list[str] = []
    current = bytearray()
    quoted = False
    while i < len(meta):
        char = meta[i : i + 1]
        if quoted:
            if char == b"\\" and i + 1 < len(meta):
                current += meta[i + 1 : i + 2]
                i += 2
                continue
            if char == b'"':
                quoted = False
                tokens.append(current.decode("utf-8", "replace"))
                current = bytearray()
            else:
                current += char
        elif char == b'"':
            quoted = True
        elif char == b")":
            if current:
                tokens.append(current.decode("utf-8", "replace"))
            return tokens
        elif char == b" ":
            if current:
                tokens.append(current.decode("utf-8", "replace"))
                current = bytearray()
        else:
            current += char
        i += 1
    return tokens


def parse_fetch(data: Sequence[Any]) -> list[Fetched]:
    """imaplib's FETCH answer - `(meta, literal)` tuples and bare `)` lines - as messages."""
    found: list[Fetched] = []
    for item in data:
        if isinstance(item, tuple):
            meta, literal = item[0], item[1]
        elif isinstance(item, bytes) and item.strip() not in (b")", b""):
            meta, literal = item, b""
        else:
            continue
        message = Fetched()
        for name, value in FETCH_ITEM.findall(meta):
            number = int(value)
            if name == b"UID":
                message.uid = number
            elif name == b"X-GM-MSGID":
                message.msgid = number
            elif name == b"X-GM-THRID":
                message.thrid = number
            else:
                message.size = number
        message.flags = set(_parenthesised(meta, b"FLAGS"))
        message.labels = set(_parenthesised(meta, b"X-GM-LABELS"))
        message.body = literal if isinstance(literal, bytes) else b""
        found.append(message)
    return found


def _check(result: tuple[Any, Any], what: str) -> Any:
    kind, data = result
    if kind != "OK":
        text = b" ".join(d for d in data if isinstance(d, bytes)).decode("utf-8", "replace")
        if "THROTTLED" in text.upper():
            raise ToolError(f"Gmail is limiting this account for now ({what}); try again later")
        raise ToolError(f"Gmail refused {what}: {text[:200]}")
    return data


def quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def search(
    conn: Any, query: str, *, folder: str = ALL_MAIL, extra: Sequence[str] = ()
) -> list[int]:
    """UIDs matching a Gmail query (`X-GM-RAW`, R4.4), oldest first."""
    _check(conn.select(folder, readonly=True), f"opening {folder}")
    if query.isascii():
        data = _check(conn.uid("SEARCH", "X-GM-RAW", quote(query), *extra), "the search")
    else:
        conn.literal = query.encode("utf-8")
        data = _check(conn.uid("SEARCH", "CHARSET", "UTF-8", *extra, "X-GM-RAW"), "the search")
    return sorted(int(u) for u in (data[0] or b"").split())


def fetch(conn: Any, uids: Iterable[int], parts: str) -> list[Fetched]:
    uids = list(uids)
    if not uids:
        return []
    data = _check(
        conn.uid(
            "FETCH",
            ",".join(str(u) for u in uids),
            f"(UID X-GM-MSGID X-GM-THRID X-GM-LABELS FLAGS RFC822.SIZE {parts})",
        ),
        "reading messages",
    )
    return parse_fetch(data)


def by_id(conn: Any, message_id: str, *, key: str = "X-GM-MSGID") -> list[int]:
    """The UIDs in All Mail of a message (or, by `X-GM-THRID`, a thread) by its hex id."""
    try:
        number = int(message_id.strip(), 16)
    except ValueError:
        raise ToolError(f"{message_id!r} is not a Gmail id - use one a search showed") from None
    _check(conn.select(ALL_MAIL, readonly=True), "opening All Mail")
    data = _check(conn.uid("SEARCH", key, str(number)), "finding the message")
    return sorted(int(u) for u in (data[0] or b"").split())


# -- reading a message -------------------------------------------------------------------


def parse(raw: bytes) -> Message:
    return email.message_from_bytes(raw, policy=email.policy.default)


def header(message: Message, name: str) -> str:
    return " ".join(str(message.get(name, "") or "").split())


def when(message: Message) -> str:
    try:
        moment = email.utils.parsedate_to_datetime(header(message, "Date")).astimezone()
    except (TypeError, ValueError):
        return header(message, "Date")
    return (
        f"{moment.strftime('%a')} {moment.day} {moment.strftime('%b')} {moment.strftime('%H:%M')}"
    )


def sender_name(message: Message) -> str:
    name, address = email.utils.parseaddr(header(message, "From"))
    return name or address or "(unknown sender)"


def unverified(message: Message) -> str:
    """R6.5: Gmail's own verdict, when it says SPF or DKIM failed for the sender."""
    results = " ".join(str(v) for v in message.get_all("Authentication-Results", []) or [])
    if not results:
        return ""
    domain = email.utils.parseaddr(header(message, "From"))[1].rpartition("@")[2].lower()
    failed = re.search(r"\b(spf|dkim|dmarc)=(fail|softfail|permerror)\b", results, re.I)
    if failed and domain:
        return f"(Gmail could not verify that this came from {domain})"
    return ""


def attachments_of(message: Message) -> list[Message]:
    return [part for part in message.walk() if part.get_filename() and not part.is_multipart()]


def body_text(message: Message, limit: int) -> str:
    """R6.4: the plain part, or the HTML as text; quoted history folded; capped."""
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


def labels_line(fetched: Fetched) -> str:
    marks = []
    if "\\Seen" not in fetched.flags:
        marks.append("unread")
    if "\\Flagged" in fetched.flags or "\\Starred" in fetched.labels:
        marks.append("starred")
    if "\\Important" in fetched.labels:
        marks.append("important")
    marks += sorted(label for label in fetched.labels if not label.startswith("\\"))
    return ", ".join(marks)


# -- links (R6.20-R6.23) ------------------------------------------------------------------------

LINK = re.compile(r"\[([^\]\n]{1,200})\]\(([^()\s]+)\)")
"""`[the menu](https://example.com/menu)` in a body: words that link somewhere."""
LINK_SCHEMES = ("https", "http", "mailto")
LOOKS_LIKE_ADDRESS = re.compile(
    r"^(?:https?://)?(?:www\.)?((?:[a-z0-9-]+\.)+[a-z]{2,})(?:[/:?#]\S*)?$", re.I
)


def links_in(body: str) -> list[tuple[str, str]]:
    """R6.21: every `[words](address)`, checked - a scheme that is a link, and words
    that do not name a different site from the one the link goes to."""
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
    """The text part: `words (address)`, so a reader without HTML sees both."""
    return LINK.sub(lambda m: f"{m.group(1).strip()} ({m.group(2).strip()})", body)


def as_card(body: str) -> str:
    """The card: `words <address>` - the real address always beside the words (R6.22)."""
    return LINK.sub(lambda m: f"{m.group(1).strip()} <{m.group(2).strip()}>", body)


def as_html(body: str, tail: str = "") -> str:
    """The HTML part: everything escaped, paragraphs and line breaks kept, and the body's
    links the only markup - so nothing the model wrote becomes HTML but an `<a href>`.
    `tail` - a forwarded original - is escaped and never linked."""
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


# -- the tools -------------------------------------------------------------------------------


ACCOUNT = {
    "type": "string",
    "description": (
        "Which account, by its label. Needed for a send or a change when several are set up."
    ),
}
MESSAGE_ID = {"type": "string", "description": "A message id, as a search showed it."}


class MailTool(Tool):
    """Owner-only, marked untrusted, failures as sentences."""

    trusted_only = True
    untrusted = True

    def __init__(self, mail: Mail) -> None:
        self.mail = mail

    async def run(self, **arguments: Any) -> ToolResult:
        try:
            assert_active()
            said = await self.act(**arguments)
            return said if isinstance(said, ToolResult) else ToolResult.ok(said)
        except (CredentialError, ToolError) as exc:
            return ToolResult.error(str(exc))

    async def act(self, *args: Any, **arguments: Any) -> Any:
        raise NotImplementedError


class Mail:
    """What the tools share: the accounts, the settings, the plugin's context."""

    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.workspace = Path(ctx.workspace)
        self.settings = dict(ctx.settings)
        self.accounts = Accounts(self.workspace, self.settings)
        self.sent: dict[str, list[float]] = {}

    def setting(self, key: str, default: Any) -> Any:
        value = self.settings.get(key)
        return default if value is None else value

    async def locate(
        self, message_id: str, label: str, *, key: str = "X-GM-MSGID"
    ) -> tuple[Account, list[int]]:
        """Which account holds a message - the named one, or the first that has it."""
        for account in self.accounts.pick(label, write=False):
            uids = await account.call(lambda conn: by_id(conn, message_id, key=key))
            if uids:
                return account, uids
        raise ToolError(f"no message {message_id!r} - it may have been deleted")

    async def read(self, account: Account, uids: Sequence[int]) -> list[Fetched]:
        def work(conn: Any) -> list[Fetched]:
            _check(conn.select(ALL_MAIL, readonly=True), "opening All Mail")
            return fetch(conn, uids, "BODY.PEEK[]")

        found: list[Fetched] = await account.call(work)
        return found

    # -- the files a send may carry (R6.14-R6.15) ------------------------------

    async def gather(self, entries: Iterable[Any]) -> list[Attached]:
        """Each entry a workspace path, `chat:<name>` or a web address, refused before
        any card."""
        names = [str(e).strip() for e in entries if str(e).strip()]
        most = int(self.setting("attachments_max", 10))
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
        """R6.14a: a file downloaded now, through the core's client under the operator's
        address rules, so the card can hash exactly what will be sent."""
        policy = self.ctx.web_policy
        cap = MAX_TOTAL_BYTES
        try:
            response = await get(
                address,
                allow_private=policy.allow_private,
                hosts=policy.hosts,
                max_redirects=policy.max_redirects,
                max_bytes=cap + 1,
                timeout=60.0,
                user_agent="atlas-gmail",
            )
        except NetworkPolicyError as exc:
            raise ToolError(f"refused: {address} - {exc}") from None
        except WebError as exc:
            raise ToolError(f"could not download {address}: {exc}") from None
        if not 200 <= response.status < 300:
            raise ToolError(f"could not download {address}: HTTP {response.status}")
        if response.truncated or len(response.body) > cap:
            raise ToolError(f"{address} is over 25 MB, which is all Gmail takes")
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
        """R8.3: a file the person sent in this conversation, the latest of that name."""
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

    # -- sends -------------------------------------------------------------------

    def count_send(self, account: Account) -> None:
        """R6.12: at most `sends_per_hour` a session, per account."""
        now = time.time()
        recent = [t for t in self.sent.get(account.label, []) if now - t < 3600]
        if len(recent) >= int(self.setting("sends_per_hour", 20)):
            raise ToolError(
                f"{len(recent)} emails sent from {account.label} this hour - the limit; "
                "nothing was sent"
            )
        recent.append(now)
        self.sent[account.label] = recent

    def record_send(self, out: Outgoing) -> None:
        """R6.19: what left, and from where, on the trail - never the contents."""
        with contextlib.suppress(Exception):
            self.ctx.audit(
                "email_sent",
                f"{len(out.recipients())} recipient(s), {len(out.files)} file(s)",
                arguments={
                    "account": out.account.label,
                    "recipients": len(out.recipients()),
                    "files": [f.record() for f in out.files],
                },
            )


def _download_name(response: Any, address: str) -> str:
    """The name the server gave the file, else the last part of the address."""
    disposition = response.header("content-disposition")
    match = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)", disposition or "", re.I)
    if match:
        return unquote(match.group(1) or match.group(2))
    tail = unquote(urlsplit(response.url or address).path.rstrip("/").rpartition("/")[2])
    return tail or "download"


def _scan(data: bytes, shown: str, workspace: Path) -> None:
    """R6.15 (3): a text file with a credential in it does not leave."""
    if len(data) > SCAN_MAX_BYTES:
        return
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return  # binary: the card is what stands between it and the send
    found = credentials_in(text, workspace=workspace)
    if found:
        raise ToolError(
            f"refused: {shown} contains what looks like a credential ({', '.join(found)})"
        )


class Search(MailTool):
    name = "email_search"
    description = (
        "Search the person's Gmail with Gmail's own search syntax - from:sam is:unread "
        "newer_than:7d has:attachment label:school - newest first. Default: the inbox. "
        "Shows sender, date, subject, labels and size, with each message's id."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "A Gmail search. Default: in:inbox."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
            "account": ACCOUNT,
        },
        "required": [],
    }

    async def act(self, query: str = "", max_results: int = 0, account: str = "") -> str:
        query = query.strip() or "in:inbox"
        limit = max(1, min(int(max_results or self.mail.setting("max_results", 20)), 100))
        accounts = self.mail.accounts.pick(account, write=False)
        lines: list[tuple[float, str]] = []
        more = 0
        for who in accounts:

            def work(conn: Any) -> tuple[list[Fetched], int]:
                uids = search(conn, query)
                newest = uids[-limit:]
                return fetch(conn, newest, "BODY.PEEK[HEADER]"), len(uids) - len(newest)

            found, extra = await who.call(work)
            more += extra
            for item in found:
                message = parse(item.body)
                try:
                    stamp = email.utils.parsedate_to_datetime(header(message, "Date")).timestamp()
                except (TypeError, ValueError):
                    stamp = float(item.uid)
                marks = labels_line(item)
                tags = f"id: {item.id} · thread: {item.thread}" + (
                    f" · {who.label}" if len(accounts) > 1 else ""
                )
                lines.append(
                    (
                        stamp,
                        f"{when(message)} · {header(message, 'From')} · "
                        f"{header(message, 'Subject') or '(no subject)'}"
                        + (f" · {marks}" if marks else "")
                        + f" · {human_size(item.size)}  [{tags}]",
                    )
                )
        lines.sort(key=lambda pair: -pair[0])
        if not lines:
            return f"No messages match {query!r}."
        text = f"{len(lines)} message(s) for {query!r}, newest first:\n" + "\n".join(
            t for _, t in lines
        )
        if more:
            text += f"\n({more} more match - narrow the query to see them)"
        return text


class Read(MailTool):
    name = "email_read"
    description = (
        "Read one message, or a whole thread, as text: headers, body and attachment names. "
        "Attachments themselves are fetched with email_attachment."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message_id": MESSAGE_ID,
            "thread_id": {
                "type": "string",
                "description": "A thread id, to read every message in it.",
            },
            "account": ACCOUNT,
        },
        "required": [],
    }

    async def act(self, message_id: str = "", thread_id: str = "", account: str = "") -> str:
        if bool(message_id) == bool(thread_id):
            raise ToolError("give message_id or thread_id, one of them")
        if thread_id:
            who, uids = await self.mail.locate(thread_id, account, key="X-GM-THRID")
        else:
            who, uids = await self.mail.locate(message_id, account)
        limit = int(self.mail.setting("max_chars", 20000))
        blocks = [
            describe(item, parse(item.body), limit // max(1, len(uids)))
            for item in await self.mail.read(who, uids)
        ]
        return "\n\n---\n\n".join(blocks)


def describe(item: Fetched, message: Message, limit: int) -> str:
    lines = [
        f"From: {header(message, 'From')}",
        f"To: {header(message, 'To')}",
    ]
    if header(message, "Cc"):
        lines.append(f"Cc: {header(message, 'Cc')}")
    lines += [f"Date: {when(message)}", f"Subject: {header(message, 'Subject') or '(no subject)'}"]
    marks = labels_line(item)
    if marks:
        lines.append(f"Labels: {marks}")
    warning = unverified(message)
    if warning:
        lines.append(warning)
    lines += ["", body_text(message, limit) or "(no text)"]
    files = attachments_of(message)
    if files:
        lines.append("")
        lines.append("Attachments:")
        for number, part in enumerate(files, start=1):
            payload = part.get_payload(decode=True) or b""
            size = len(payload) if isinstance(payload, bytes) else 0
            lines.append(
                f"  {number}. {part.get_filename()} ({part.get_content_type()}, {human_size(size)})"
            )
    lines.append(f"[id: {item.id} · thread: {item.thread}]")
    return "\n".join(lines)


class Attachment(MailTool):
    name = "email_attachment"
    description = (
        "Fetch one attachment of a message, by its number or name as email_read listed it. "
        "A picture is shown to you; anything else is saved in the workspace for read_media."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message_id": MESSAGE_ID,
            "attachment": {"type": "string", "description": "Its number (1, 2, ...) or file name."},
            "account": ACCOUNT,
        },
        "required": ["message_id", "attachment"],
    }

    async def act(self, message_id: str, attachment: str, account: str = "") -> Any:
        who, uids = await self.mail.locate(message_id, account)
        read = await self.mail.read(who, uids[:1])
        if not read:
            raise ToolError(f"no message {message_id!r} - it may have been deleted")
        item = read[0]
        files = attachments_of(parse(item.body))
        chosen = _choose(files, attachment)
        data = chosen.get_payload(decode=True) or b""
        if not isinstance(data, bytes):
            raise ToolError("that attachment has no content")
        cap = int(self.mail.setting("attachment_max_mb", 25)) * 1024 * 1024
        if len(data) > cap:
            raise ToolError(
                f"{chosen.get_filename()} is {human_size(len(data))}, "
                f"over the {human_size(cap)} limit"
            )
        name = clean_name(str(chosen.get_filename()))
        kind = sniff(data, name)
        media = self.mail.ctx.media
        if kind in IMAGE_TYPES and media is not None:
            block = media.put(data, source=f"{name} (attached to an email)")
            return ImageResult(f"{name}, {kind}, {human_size(len(data))}", images=(block,))
        folder = self.mail.workspace / "email-attachments" / item.id
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        target.write_bytes(data)
        relative = target.relative_to(self.mail.workspace).as_posix()
        return (
            f"Saved {name} ({kind}, {human_size(len(data))}) to {relative} - read it with "
            f"read_media(path={relative!r})."
        )


def _choose(files: Sequence[Message], which: str) -> Message:
    which = which.strip()
    if which.isdigit() and 1 <= int(which) <= len(files):
        return files[int(which) - 1]
    for part in files:
        if str(part.get_filename()) == which:
            return part
    names = ", ".join(f"{i}. {p.get_filename()}" for i, p in enumerate(files, start=1)) or "none"
    raise ToolError(f"no attachment {which!r} - this message has: {names}")


def clean_name(name: str) -> str:
    """R6.18: letters, digits, spaces and `.-_()`; never a path the sender chose."""
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


class Labels(MailTool):
    name = "email_labels"
    description = "The account's Gmail labels, for searches (label:name) and email_organise."
    parameters = {"type": "object", "properties": {"account": ACCOUNT}, "required": []}

    async def act(self, account: str = "") -> str:
        lines: list[str] = []
        for who in self.mail.accounts.pick(account, write=False):
            data = await who.call(lambda conn: _check(conn.list(), "listing labels"))
            names = []
            for row in data:
                text = row.decode("utf-8", "replace") if isinstance(row, bytes) else str(row)
                match = re.search(r'"((?:[^"\\]|\\.)*)"\s*$|(\S+)\s*$', text)
                if not match:
                    continue
                name = (match.group(1) or match.group(2) or "").replace('\\"', '"')
                if name.startswith("[Gmail]") or name == "INBOX":
                    continue
                names.append(name)
            lines.append(f"{who.label}: " + (", ".join(sorted(names)) or "(no labels of your own)"))
        return "\n".join(lines)


# -- writing ---------------------------------------------------------------------------------


class Outgoing:
    """A message worked out once - for the card - and sent as it was shown (R6.17)."""

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
        """A line naming what this replies to or forwards, for the card."""
        self.files: list[Attached] = []
        self.forwarded = ""
        self.draft_uid = 0
        self.draft_hash = ""
        self.raw: bytes = b""

    def recipients(self) -> list[str]:
        return [a for a in self.to + self.cc + self.bcc if a]

    def build(self, *, with_bcc: bool) -> bytes:
        if self.raw:
            return self.raw
        message = EmailMessage(policy=email.policy.SMTP)
        message["From"] = self.account.address
        if self.to:
            message["To"] = ", ".join(self.to)
        if self.cc:
            message["Cc"] = ", ".join(self.cc)
        if with_bcc and self.bcc:
            message["Bcc"] = ", ".join(self.bcc)
        message["Subject"] = self.subject
        message["Date"] = email.utils.formatdate(localtime=True)
        message["Message-ID"] = email.utils.make_msgid(domain="gmail.com")
        if self.in_reply_to:
            message["In-Reply-To"] = self.in_reply_to
            message["References"] = self.references
        tail = f"\n\n{self.forwarded}" if self.forwarded else ""
        if links_in(self.body):
            # R6.20: words that link, as HTML beside a plain part that says the address.
            # Only the body's own links: a forwarded original is somebody else's text,
            # checked by nobody, and goes as text in both parts.
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
    "reply_to": {"type": "string", "description": "The id of a message to reply to."},
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
            "Files to attach: a path inside the workspace (reports/q3.pdf), chat:<name> for a "
            "file the person sent in this conversation, or an https:// address to download."
        ),
    },
    "account": ACCOUNT,
}


class Writer(MailTool):
    """What a draft and a send share: turning a call into an `Outgoing`."""

    verb = ""

    def __init__(self, mail: Mail) -> None:
        super().__init__(mail)
        self._looked: dict[str, tuple[float, Outgoing]] = {}

    def _remember(self, arguments: Mapping[str, Any], outgoing: Outgoing) -> None:
        self._looked[json.dumps(arguments, sort_keys=True, default=str)] = (
            time.monotonic(),
            outgoing,
        )

    def _recall(self, arguments: Mapping[str, Any]) -> Outgoing | None:
        seen = self._looked.pop(json.dumps(arguments, sort_keys=True, default=str), None)
        return seen[1] if seen is not None and time.monotonic() - seen[0] < 900 else None

    async def compose(self, arguments: Mapping[str, Any]) -> Outgoing:
        account = self.mail.accounts.pick(str(arguments.get("account", "") or ""), write=True)[0]
        out = Outgoing(account)
        out.to = _addresses(arguments.get("to"))
        out.cc = _addresses(arguments.get("cc"))
        out.bcc = _addresses(arguments.get("bcc"))
        out.subject = str(arguments.get("subject", "") or "")
        out.body = str(arguments.get("body", "") or "")
        links_in(out.body)  # R6.21: refused before any card
        reply_to = str(arguments.get("reply_to", "") or "")
        forward = str(arguments.get("forward", "") or "")
        if reply_to and forward:
            raise ToolError("reply_to or forward, not both")
        if reply_to or forward:
            uids = await account.call(lambda conn: by_id(conn, reply_to or forward))
            if not uids:
                raise ToolError(
                    "the message being replied to could not be read - it may have been deleted"
                )
            [item] = await self.mail.read(account, uids[:1])
            original = parse(item.body)
            if reply_to:
                self._reply(out, original, bool(arguments.get("reply_all")))
            else:
                self._forward(out, original, arguments.get("keep_attachments", True) is not False)
        if not out.recipients():
            raise ToolError("no recipients - say who it goes to")
        out.files += await self.mail.gather(arguments.get("attachments") or ())
        total = sum(len(f.data) for f in out.files)
        if total > MAX_TOTAL_BYTES:
            raise ToolError(
                f"the attachments come to {human_size(total)}; Gmail takes at most 25 MB"
            )
        return out

    def _reply(self, out: Outgoing, original: Message, everyone: bool) -> None:
        """R6.10-R6.11: to the original's Reply-To or From; with reply_all, its To and Cc
        less the person's own address; threaded by In-Reply-To and References."""
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
            subject if subject.lower().startswith("fwd:") else f"Fwd: {subject}"
        )
        out.forwarded = "\n".join(
            [
                "---------- Forwarded message ---------",
                f"From: {header(original, 'From')}",
                f"Date: {when(original)}",
                f"Subject: {subject}",
                f"To: {header(original, 'To')}",
                "",
                body_text(original, int(self.mail.setting("max_chars", 20000))),
            ]
        )
        out.context = f"forwarding {sender_name(original)}'s message of {when(original)}"
        if keep:
            for part in attachments_of(original):
                data = part.get_payload(decode=True) or b""
                if isinstance(data, bytes):
                    out.files.append(Attached(str(part.get_filename()), data, "from the original"))


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


class Draft(Writer):
    name = "email_draft"
    verb = "Draft"
    description = (
        "Save a draft to Gmail's Drafts - new, a reply, or a forward - without sending it. "
        "Use it whenever you are not sure; the person can send it from Gmail, or you can send "
        "it with email_send(draft_id=...). A draft with attachments asks the person first."
    )
    parameters = {"type": "object", "properties": SEND_FIELDS, "required": []}
    gated = True
    """Gated only when it carries files (R6.2): `subject` answers None otherwise."""

    async def subject(self, arguments: Mapping[str, Any]) -> Subject | None:
        if not arguments.get("attachments"):
            return None  # a draft reaches nobody: free
        try:
            out = await self.compose(arguments)
        except (CredentialError, ToolError):
            return None  # `run` fails the same way, before it writes anything
        self._remember(arguments, out)
        return Subject(tool=self.name, action="draft", summary=out.card("Draft (with files)"))

    async def act(self, **arguments: Any) -> str:
        out = self._recall(arguments) or await self.compose(arguments)
        data = out.build(with_bcc=True)

        def work(conn: Any) -> str:
            answer = _check(
                conn.append(DRAFTS, "(\\Draft)", imaplib.Time2Internaldate(time.time()), data),
                "saving the draft",
            )
            match = re.search(
                rb"APPENDUID \d+ (\d+)", b" ".join(a for a in answer if isinstance(a, bytes))
            )
            if not match:
                return ""
            _check(conn.select(DRAFTS, readonly=True), "opening Drafts")
            fetched = fetch(conn, [int(match.group(1))], "")
            return fetched[0].id if fetched else ""

        draft_id = await out.account.call(work)
        people = ", ".join(out.recipients())
        return (
            f"Draft saved for {people}: {out.subject or '(no subject)'}"
            + (f", with {len(out.files)} file(s)" if out.files else "")
            + (f" [draft id: {draft_id}]" if draft_id else "")
        )


class Send(Writer):
    name = "email_send"
    verb = "Send"
    description = (
        "Send an email - new, a reply (reply_to), a forward (forward), or a saved draft "
        "(draft_id). The person is shown every recipient, the subject, the whole text and "
        "every file, and must say yes. Write the body exactly as it should go."
    )
    parameters = {
        "type": "object",
        "properties": {
            **SEND_FIELDS,
            "draft_id": {"type": "string", "description": "Send this saved draft."},
        },
        "required": [],
    }
    gated = True

    async def subject(self, arguments: Mapping[str, Any]) -> Subject | None:
        try:
            out = await self.prepare(arguments)
        except (CredentialError, ToolError):
            return None  # `run` fails the same way, before anything is sent
        self._remember(arguments, out)
        return Subject(tool=self.name, action="send", summary=out.card("Send"), confirm=True)

    async def prepare(self, arguments: Mapping[str, Any]) -> Outgoing:
        draft_id = str(arguments.get("draft_id", "") or "")
        if not draft_id:
            return await self.compose(arguments)
        account = self.mail.accounts.pick(str(arguments.get("account", "") or ""), write=True)[0]
        return await self._draft(account, draft_id)

    async def _draft(self, account: Account, draft_id: str) -> Outgoing:
        uids = await account.call(lambda conn: by_id(conn, draft_id))
        if not uids:
            raise ToolError(f"no draft {draft_id!r} - it may have been sent or deleted")
        [item] = await self.mail.read(account, uids[:1])
        if "\\Draft" not in item.flags and "\\Draft" not in item.labels:
            raise ToolError(f"{draft_id!r} is not a draft")
        message = parse(item.body)
        out = Outgoing(account)
        out.to = _addresses(message.get_all("To", []))
        out.cc = _addresses(message.get_all("Cc", []))
        out.bcc = _addresses(message.get_all("Bcc", []))
        out.subject = header(message, "Subject")
        out.body = body_text(message, 10**9)
        for part in attachments_of(message):
            data = part.get_payload(decode=True) or b""
            if isinstance(data, bytes):
                out.files.append(Attached(str(part.get_filename()), data, "in the draft"))
        del message["Bcc"]
        out.raw = message.as_bytes()
        out.draft_uid = item.uid
        out.draft_hash = hashlib.sha256(item.body).hexdigest()
        out.context = "sending a saved draft"
        return out

    async def act(self, **arguments: Any) -> str:
        out = self._recall(arguments)
        if out is None:
            await self.prepare(arguments)  # says why no card could be drawn, when one could not
            raise ToolError("a send has to be shown to the person first; nothing was sent")
        if not out.recipients():
            raise ToolError("no recipients - nothing was sent")
        self.mail.count_send(out.account)
        if out.draft_uid:
            await self._still_the_draft(out)
        data = out.build(with_bcc=False)
        await asyncio.to_thread(out.account.send, data, out.account.address, out.recipients())
        if out.draft_uid:
            with contextlib.suppress(ToolError):
                await out.account.call(lambda conn: _trash(conn, [out.draft_uid]))
        self.mail.record_send(out)
        files = f" with {len(out.files)} file(s)" if out.files else ""
        return f"Sent to {', '.join(out.recipients())}: {out.subject or '(no subject)'}{files}."

    async def _still_the_draft(self, out: Outgoing) -> None:
        """R6.13: the draft as it is now is the draft that was shown."""

        def work(conn: Any) -> str:
            _check(conn.select(ALL_MAIL, readonly=True), "opening All Mail")
            found = fetch(conn, [out.draft_uid], "BODY.PEEK[]")
            return hashlib.sha256(found[0].body).hexdigest() if found else ""

        if await out.account.call(work) != out.draft_hash:
            raise ToolError("the draft changed since it was shown - nothing was sent; look again")


def _trash(conn: Any, uids: Sequence[int]) -> None:
    _check(conn.select(ALL_MAIL), "opening All Mail")
    _check(conn.uid("MOVE", ",".join(str(u) for u in uids), TRASH), "moving to Trash")


# -- organising --------------------------------------------------------------------------------


ACTIONS = {
    "archive": "Archive",
    "inbox": "Move to Inbox",
    "read": "Mark read",
    "unread": "Mark unread",
    "star": "Star",
    "unstar": "Unstar",
    "label": "Label",
    "unlabel": "Remove a label from",
    "trash": "Move to Trash",
}


class Organise(MailTool):
    name = "email_organise"
    description = (
        "Tidy mail: archive, inbox, read, unread, star, unstar, label, unlabel, or trash - by "
        "message ids, or by a Gmail search. At most 50 at once. Nothing is deleted for good; "
        "Trash empties itself after 30 days."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(ACTIONS)},
            "message_ids": {"type": "array", "items": {"type": "string"}},
            "query": {
                "type": "string",
                "description": "A Gmail search naming the messages instead.",
            },
            "label": {"type": "string", "description": "For label and unlabel."},
            "account": ACCOUNT,
        },
        "required": ["action"],
    }
    gated = True

    def __init__(self, mail: Mail) -> None:
        super().__init__(mail)
        self._looked: dict[str, tuple[Account, list[int], str]] = {}

    async def subject(self, arguments: Mapping[str, Any]) -> Subject | None:
        try:
            account, uids, who = await self.aim(arguments)
        except (CredentialError, ToolError):
            return None
        self._looked[json.dumps(arguments, sort_keys=True, default=str)] = (account, uids, who)
        action = str(arguments.get("action"))
        label = f" {arguments.get('label')!r}" if action in ("label", "unlabel") else ""
        return Subject(
            tool=self.name,
            action=action,
            summary=f"{ACTIONS[action]}{label} {len(uids)} message(s) from {who} ({account.label})",
        )

    async def aim(self, arguments: Mapping[str, Any]) -> tuple[Account, list[int], str]:
        action = str(arguments.get("action", ""))
        if action not in ACTIONS:
            raise ToolError(f"action is one of {', '.join(sorted(ACTIONS))}")
        if action in ("label", "unlabel") and not str(arguments.get("label", "") or "").strip():
            raise ToolError("say which label")
        ids = [str(i) for i in arguments.get("message_ids") or ()]
        query = str(arguments.get("query", "") or "").strip()
        if bool(ids) == bool(query):
            raise ToolError("give message_ids or a query, one of them")
        account = self.mail.accounts.pick(str(arguments.get("account", "") or ""), write=True)[0]

        def work(conn: Any) -> tuple[list[int], str]:
            if query:
                uids = search(conn, query)
            else:
                uids = sorted({u for i in ids for u in by_id(conn, i)})
            if len(uids) > ORGANISE_MAX:
                raise ToolError(f"{len(uids)} messages - at most {ORGANISE_MAX} at once; narrow it")
            if not uids:
                raise ToolError("no messages match")
            _check(conn.select(ALL_MAIL, readonly=True), "opening All Mail")
            senders = [
                sender_name(parse(f.body))
                for f in fetch(conn, uids[:200], "BODY.PEEK[HEADER.FIELDS (FROM)]")
            ]
            common = sorted(set(senders), key=senders.count, reverse=True)
            who = ", ".join(common[:3]) + (
                f" and {len(common) - 3} more" if len(common) > 3 else ""
            )
            return uids, who

        uids, who = await account.call(work)
        return account, uids, who

    async def act(self, **arguments: Any) -> str:
        seen = self._looked.pop(json.dumps(arguments, sort_keys=True, default=str), None)
        account, uids, _ = seen if seen is not None else await self.aim(arguments)
        action = str(arguments["action"])
        label = str(arguments.get("label", "") or "")

        def work(conn: Any) -> None:
            if action == "trash":
                _trash(conn, uids)
                return
            _check(conn.select(ALL_MAIL), "opening All Mail")
            which = ",".join(str(u) for u in uids)
            store = {
                "archive": ("-X-GM-LABELS", "\\Inbox"),
                "inbox": ("+X-GM-LABELS", "\\Inbox"),
                "read": ("+FLAGS", "\\Seen"),
                "unread": ("-FLAGS", "\\Seen"),
                "star": ("+FLAGS", "\\Flagged"),
                "unstar": ("-FLAGS", "\\Flagged"),
                "label": ("+X-GM-LABELS", quote(label)),
                "unlabel": ("-X-GM-LABELS", quote(label)),
            }[action]
            _check(conn.uid("STORE", which, store[0], f"({store[1]})"), ACTIONS[action].lower())

        await account.call(work)
        return f"{ACTIONS[action]}{f' {label!r}' if label else ''}: {len(uids)} message(s)."


# -- important mail ---------------------------------------------------------------------------


class Seen:
    """R7.4: the last UID seen per account, and the UIDVALIDITY it belongs to."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.rows: dict[str, dict[str, int]] = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            self.rows = {}

    def save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.rows, indent=1, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)


def check_new(
    conn: Any, last: Mapping[str, int] | None, mode: str, mine: str
) -> tuple[dict[str, int], list[str]]:
    """New mail in the inbox since `last`: the new position, and the senders worth a line."""
    _check(conn.select(INBOX, readonly=True), "opening the inbox")
    validity = int((conn.response("UIDVALIDITY")[1] or [b"0"])[0] or 0)
    following = int((conn.response("UIDNEXT")[1] or [b"0"])[0] or 0)
    here = {"uidvalidity": validity, "uid": max(following - 1, 0)}
    if not last or int(last.get("uidvalidity", -1)) != validity:
        return here, []  # R7.4: a first run - or a reset mailbox - says nothing
    after = int(last.get("uid", 0))
    if following and following - 1 <= after:
        return {"uidvalidity": validity, "uid": after}, []
    wanted = {
        "important": "is:important category:primary is:unread",
        "primary": "category:primary is:unread",
    }[mode]
    data = _check(
        conn.uid("SEARCH", "X-GM-RAW", quote(wanted), "UID", f"{after + 1}:*"),
        "checking for new mail",
    )
    uids = [int(u) for u in (data[0] or b"").split() if int(u) > after]
    senders: list[str] = []
    newest = after
    for item in fetch(conn, uids, "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)]"):
        newest = max(newest, item.uid)
        message = parse(item.body)
        if email.utils.parseaddr(header(message, "From"))[1].lower() == mine.lower():
            continue
        subject = " ".join(header(message, "Subject").split()) or "(no subject)"
        if len(subject) > SUBJECT_MAX:
            subject = subject[: SUBJECT_MAX - 1] + "…"
        senders.append(f"{sender_name(message)}\x00{subject}")
    return {"uidvalidity": validity, "uid": max(newest, here["uid"], after)}, senders


def notice(found: Sequence[str], label: str, several: bool) -> str:
    """R7.3: one line; more than two found at once, the senders only."""
    where = f" ({label})" if several else ""
    if len(found) <= 2:
        return "\n".join(f"📧 {s.split(chr(0))[0]} - {s.split(chr(0))[1]}{where}" for s in found)
    names = list(dict.fromkeys(s.split("\x00")[0] for s in found))
    shown = ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
    return f"📧 {len(found)} important emails{where}: {shown}"


class NewMail(Service):
    """§7: check each inbox, say what Gmail thinks is important, act on nothing."""

    async def run(self, ctx: ServiceContext) -> None:
        accounts = Accounts(Path(ctx.workspace), ctx.settings)
        seen = Seen(ctx.state_dir / "seen.json")
        warned: dict[str, str] = {}
        while not ctx.stopping:
            mode = str(ctx.setting("notify", "important") or "important")
            poll = max(1, int(ctx.setting("poll_minutes", 2) or 2)) * 60
            if mode not in ("important", "primary"):
                if await ctx.sleep_for(600):
                    return
                continue
            known = accounts.all()
            for account in known:

                def look(conn: Any, a: Account = account, m: str = mode) -> Any:
                    return check_new(conn, seen.rows.get(a.label), m, a.address)

                try:
                    here, found = await account.call(look)
                    warned.pop(account.label, None)
                except CredentialError as exc:
                    if warned.get(account.label) != str(exc):
                        warned[account.label] = str(exc)
                        await _tell(ctx, str(exc))
                    continue
                except (ToolError, imaplib.IMAP4.error, OSError):
                    continue
                seen.rows[account.label] = here
                seen.save()
                if found:
                    await _tell(ctx, notice(found, account.label, len(known) > 1))
            if await ctx.sleep_for(poll):
                return


async def _tell(ctx: ServiceContext, text: str) -> str:
    try:
        return await ctx.notify(text)
    except NotifyError as exc:
        return f"failed: {exc}"


# -- the plugin ---------------------------------------------------------------------------------


class GmailPlugin(Plugin):
    name = PLUGIN
    description = "Gmail: search, read, draft, send with a yes, tidy, and important-mail notices."

    def register(self, ctx: PluginContext) -> None:
        mail = Mail(ctx)
        for tool in (Search, Read, Attachment, Labels, Draft, Send, Organise):
            ctx.register_tool(tool(mail), toolset="Gmail")
        ctx.register_service("new-mail", NewMail)
