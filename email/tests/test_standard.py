"""The email plugin in standard mode (`docs/spec/email.md` §4.3), against a fake IMAP server.

`FakeImap` speaks the standard IMAP the plugin uses the way `imaplib` hands it
back - folders with `SPECIAL-USE` flags (or, by choice, without), `UIDVALIDITY`,
`SEARCH` with `NOT`, `OR`, `HEADER` and a `CHARSET` literal, `APPENDUID`, and
`MOVE` or only `UIDPLUS`'s `UID EXPUNGE`. The SMTP fakes record what they were
sent and whether TLS was started. None of it reaches a real server.

Run from a checkout of Atlas (`uv run --project ../Atlas pytest email/tests`).
"""

from __future__ import annotations

import datetime
import email
import email.policy
import email.utils
import imaplib
import importlib.util
import re
import shlex
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from smtplib import SMTPAuthenticationError
from types import SimpleNamespace
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    """The module as Atlas imports a plugin: never put in `sys.modules`, so anything
    that needs to find itself there - a dataclass - fails here as it would there."""
    name = "atlas_plugin_email_standard"
    spec = importlib.util.spec_from_file_location(name, HERE.parent / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


em = _load()

ME = "me@icloud.com"
PASSWORD = "abcdefghijklmnop"
SPECIAL = {"Sent Messages": "\\Sent", "Drafts": "\\Drafts", "Deleted Messages": "\\Trash"}
SPECIAL |= {"Junk": "\\Junk", "Archive": "\\Archive"}


# -- the fake server ------------------------------------------------------------------------


class Stored:
    def __init__(self, uid: int, raw: bytes, flags: set[str]) -> None:
        self.uid, self.raw, self.flags = uid, raw, set(flags)

    @property
    def parsed(self) -> Any:
        return email.message_from_bytes(self.raw, policy=email.policy.default)


class Folder:
    def __init__(self, name: str, validity: int) -> None:
        self.name, self.validity = name, validity
        self.messages: list[Stored] = []
        self.next_uid = 1

    def add(self, raw: bytes, flags: set[str]) -> Stored:
        stored = Stored(self.next_uid, raw, flags)
        self.next_uid += 1
        self.messages.append(stored)
        return stored


class Server:
    """One account's mailbox, on a server that is not Gmail's."""

    def __init__(self) -> None:
        self.password = PASSWORD
        self.folders = {
            name: Folder(name, 1700000000 + n)
            for n, name in enumerate(["INBOX", *SPECIAL, "Family"])
        }
        self.capabilities: tuple[str, ...] = ("IMAP4REV1", "MOVE", "UIDPLUS", "SPECIAL-USE")
        self.special_use = True
        self.hosts: list[tuple[str, int]] = []
        self.searches: list[tuple[str, ...]] = []
        self.expunged: list[int] = []
        self.sent: list[tuple[str, list[str], bytes]] = []
        self.smtp: list[tuple[str, int, bool]] = []

    def add(self, folder: str, raw: bytes, *flags: str) -> Stored:
        return self.folders[folder].add(raw, set(flags))

    def id_of(self, folder: str, stored: Stored) -> str:
        return f"{folder}#{self.folders[folder].validity}.{stored.uid}"

    def where(self, raw_contains: bytes) -> list[str]:
        return [f.name for f in self.folders.values() for m in f.messages if raw_contains in m.raw]


def unquote(text: str) -> str:
    if text.startswith('"') and text.endswith('"'):
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return text


MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def day_of(text: str) -> datetime.date:
    d, m, y = text.split("-")
    return datetime.date(int(y), MONTHS.index(m) + 1, int(d))


def predicate(items: list[str], literal: bytes | None) -> Callable[[Stored], bool]:
    """One search key off the front of `items`, as a test on a message."""
    key = items.pop(0).upper()

    def value() -> str:
        if items:
            return unquote(items.pop(0))
        assert literal is not None, f"{key} has no value"
        return literal.decode("utf-8")

    if key == "NOT":
        inner = predicate(items, literal)
        return lambda m: not inner(m)
    if key == "OR":
        left, right = predicate(items, literal), predicate(items, literal)
        return lambda m: left(m) or right(m)
    if key == "ALL":
        return lambda m: True
    if key in ("UNSEEN", "SEEN", "FLAGGED"):
        flag = {"UNSEEN": "\\Seen", "SEEN": "\\Seen", "FLAGGED": "\\Flagged"}[key]
        wanted = key != "UNSEEN"
        return lambda m: (flag in m.flags) == wanted
    if key in ("FROM", "TO", "CC", "SUBJECT"):
        text = value().lower()
        return lambda m: text in str(m.parsed.get(key.title(), "")).lower()
    if key == "TEXT":
        text = value().lower()
        return lambda m: text in m.raw.decode("utf-8", "replace").lower()
    if key == "HEADER":
        name, text = value(), value().lower()
        return lambda m: text in str(m.parsed.get(name, "")).lower()
    if key in ("SINCE", "BEFORE"):
        day = day_of(value())

        def dated(m: Stored) -> bool:
            sent = email.utils.parsedate_to_datetime(m.parsed["Date"]).date()
            return sent >= day if key == "SINCE" else sent < day

        return dated
    if key in ("LARGER", "SMALLER"):
        size = int(value())
        return lambda m: len(m.raw) > size if key == "LARGER" else len(m.raw) < size
    if key == "UID":
        span = value()
        low, _, high = span.partition(":")
        if not high:
            return lambda m: m.uid == int(low)
        return lambda m: m.uid >= int(low)
    raise AssertionError(f"the fake does not know {key}")


class FakeImap:
    server: Server

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.server.hosts.append((host, port))
        self.folder: Folder | None = None
        self.literal: bytes | None = None
        self.pending: dict[str, list[bytes]] = {}

    @property
    def capabilities(self) -> tuple[str, ...]:
        return self.server.capabilities

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        if password != self.server.password:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Authentication failed.")
        return "OK", [b"logged in"]

    def logout(self) -> tuple[str, list[bytes]]:
        return "BYE", [b""]

    def select(self, name: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        folder = self.server.folders.get(unquote(name))
        if folder is None:
            return "NO", [b"[NONEXISTENT] no such folder"]
        self.folder = folder
        self.pending = {
            "UIDVALIDITY": [str(folder.validity).encode()],
            "UIDNEXT": [str(folder.next_uid).encode()],
        }
        return "OK", [str(len(folder.messages)).encode()]

    def response(self, name: str) -> tuple[str, list[Any]]:
        return name, self.pending.pop(name, [None])

    def append(self, name: str, flags: str, date: Any, data: bytes) -> tuple[str, list[bytes]]:
        folder = self.server.folders.get(unquote(name))
        if folder is None:
            return "NO", [b"[TRYCREATE] no such folder"]
        stored = folder.add(data, set(flags.strip("()").split()))
        return "OK", [b"[APPENDUID %d %d] Append completed." % (folder.validity, stored.uid)]

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        assert self.folder is not None
        here = self.folder.messages
        if command == "SEARCH":
            self.server.searches.append(args)
            items = list(args)
            literal, self.literal = self.literal, None
            if items[:2] == ["CHARSET", "UTF-8"]:
                items = items[2:]
            tests = []
            while items:
                tests.append(predicate(items, literal))
            found = [m for m in here if all(t(m) for t in tests)]
            return "OK", [" ".join(str(m.uid) for m in found).encode()]
        uids = {int(u) for u in args[0].split(",")}
        chosen = [m for m in here if m.uid in uids]
        if command == "FETCH":
            parts = args[1]
            out: list[Any] = []
            for message in chosen:
                meta = (
                    f"{message.uid} (UID {message.uid} FLAGS ({' '.join(sorted(message.flags))}) "
                    f"RFC822.SIZE {len(message.raw)}"
                )
                if "HEADER.FIELDS" in parts:
                    names = re.search(r"HEADER\.FIELDS \(([^)]*)\)", parts).group(1).split()  # type: ignore[union-attr]
                    head = message.raw.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
                    body = (
                        b"\r\n".join(
                            line for line in head if line.split(b":")[0].upper().decode() in names
                        )
                        + b"\r\n\r\n"
                    )
                elif "BODY.PEEK[HEADER]" in parts:
                    body = message.raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                elif "BODY.PEEK[]" in parts:
                    body = message.raw
                else:
                    out.append((meta + ")").encode())
                    continue
                out.append(((meta + f" BODY[] {{{len(body)}}}").encode(), body))
                out.append(b")")
            return "OK", out
        if command == "STORE":
            operation, flag = args[1], args[2].strip("()")
            for message in chosen:
                (message.flags.add if operation.startswith("+") else message.flags.discard)(flag)
            return "OK", [b""]
        if command in ("MOVE", "COPY"):
            if command == "MOVE":
                assert "MOVE" in self.server.capabilities
            target = self.server.folders[unquote(args[1])]
            for message in chosen:
                target.add(message.raw, message.flags - {"\\Deleted"})
                if command == "MOVE":
                    here.remove(message)
            return "OK", [b""]
        if command == "EXPUNGE":
            assert "UIDPLUS" in self.server.capabilities
            for message in chosen:
                assert "\\Deleted" in message.flags
                here.remove(message)
                self.server.expunged.append(message.uid)
            return "OK", [b""]
        raise AssertionError(f"unexpected UID {command}")

    def list(self) -> tuple[str, list[bytes]]:
        rows = []
        for name in self.server.folders:
            flags = ["\\HasNoChildren"]
            if self.server.special_use and name in SPECIAL:
                flags.append(SPECIAL[name])
            shown = f'"{name}"' if " " in name else name
            rows.append(f'({" ".join(flags)}) "/" {shown}'.encode())
        return "OK", rows


class FakeSmtp:
    """`SMTP` and `SMTP_SSL` both: which one, and whether TLS was started, is recorded."""

    server: Server
    tls = False

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.host, self.port = host, port
        self.started = self.tls

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def ehlo(self) -> None:
        return None

    def has_extn(self, name: str) -> bool:
        return name.lower() == "starttls"

    def starttls(self, **kwargs: Any) -> None:
        self.started = True

    def login(self, user: str, password: str) -> None:
        assert self.started, "a password over a connection without TLS"
        if password != self.server.password:
            raise SMTPAuthenticationError(535, b"Authentication failed")

    def sendmail(self, sender: str, recipients: list[str], data: bytes) -> dict[str, Any]:
        self.server.smtp.append((self.host, self.port, self.started))
        self.server.sent.append((sender, list(recipients), data))
        return {}


class FakeSmtpSsl(FakeSmtp):
    tls = True


def mail(
    sender: str = "Sam Lee <sam@example.com>",
    subject: str = "Dinner Friday?",
    body: str = "Are you free Friday at 7?",
    *,
    to: str = ME,
    date: str = "Thu, 01 Oct 2026 18:02:00 +1000",
    headers: dict[str, str] | None = None,
) -> bytes:
    message = EmailMessage(policy=email.policy.SMTP)
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = date
    message["Message-ID"] = f"<{abs(hash((sender, subject, body)))}@example.com>"
    for name, value in (headers or {}).items():
        del message[name]
        message[name] = value
    message.set_content(body)
    return message.as_bytes()


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Server:
    made = Server()
    FakeImap.server = made
    FakeSmtp.server = made
    monkeypatch.setattr(em, "IMAP4_SSL", FakeImap)
    monkeypatch.setattr(em, "SMTP_SSL", FakeSmtpSsl)
    monkeypatch.setattr(em, "SMTP", FakeSmtp)
    for old in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):
        monkeypatch.delenv(old, raising=False)
    monkeypatch.setenv("EMAIL_ADDRESS", ME)
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "abcd-efgh-ijkl-mnop".replace("-", ""))
    return made


class Context:
    """A `PluginContext`, as far as the tools use it."""

    def __init__(self, workspace: Path, **settings: Any) -> None:
        self.workspace = workspace
        self.settings = settings
        self.media = None
        self.web_policy: Any = SimpleNamespace(allow_private=False, hosts=None, max_redirects=5)
        self.audited: list[Any] = []

    def session_media(self) -> tuple[Any, ...]:
        return ()

    def audit(self, event: str, detail: str = "", **kwargs: Any) -> None:
        self.audited.append((event, detail))


def tools(tmp_path: Path, **settings: Any) -> dict[str, Any]:
    shared = em.Mail(Context(tmp_path, **settings))
    return {
        cls.name: cls(shared)
        for cls in (em.Search, em.Read, em.Attachment, em.Labels, em.Draft, em.Send, em.Organise)
    }


async def carded(tool: Any, **arguments: Any) -> tuple[Any, Any]:
    subject = await tool.subject(arguments)
    return subject, await tool.run(**arguments)


# -- R4.1, R4.2: which servers ----------------------------------------------------------------


async def test_the_old_gmail_variables_are_still_read(
    server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EMAIL_ADDRESS")
    monkeypatch.delenv("EMAIL_APP_PASSWORD")
    monkeypatch.setenv("GMAIL_ADDRESS", ME)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", PASSWORD)
    server.add("INBOX", mail())

    result = await tools(tmp_path)["email_search"].run()

    assert not result.is_error, result.content
    assert "Dinner Friday?" in result.content
    assert server.hosts == [("imap.mail.me.com", 993)]

    renamed = await tools(tmp_path, address_env="WORK_MAIL")["email_search"].run()
    assert renamed.is_error  # a variable the person named is the only one read


@pytest.mark.parametrize(
    ("address", "servers", "host"),
    [
        ("me@icloud.com", {}, ("imap.mail.me.com", 993)),
        ("me@fastmail.com", {}, ("imap.fastmail.com", 993)),
        ("me@yahoo.co.uk", {}, ("imap.mail.yahoo.com", 993)),
        ("me@family.example", {"personal": "fastmail"}, ("imap.fastmail.com", 993)),
        (
            "me@family.example",
            {"personal": {"imap": "mail.family.example:1993", "smtp": "mail.family.example"}},
            ("mail.family.example", 1993),
        ),
    ],
)
async def test_the_server_comes_from_the_address_or_from_settings(
    server: Server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    address: str,
    servers: dict[str, Any],
    host: tuple[str, int],
) -> None:
    monkeypatch.setenv("EMAIL_ADDRESS", address)
    result = await tools(tmp_path, servers=servers)["email_search"].run()
    assert not result.is_error, result.content
    assert server.hosts == [host]


@pytest.mark.parametrize(
    ("address", "said"),
    [
        ("me@family.example", "servers.personal"),
        ("me@outlook.com", "Microsoft"),
        ("me@hotmail.co.uk", "Microsoft"),
    ],
)
async def test_an_unknown_domain_or_a_microsoft_address_is_refused(
    server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, address: str, said: str
) -> None:
    monkeypatch.setenv("EMAIL_ADDRESS", address)
    result = await tools(tmp_path)["email_search"].run()
    assert result.is_error and said in result.content
    assert server.hosts == []  # nothing was reached


async def test_a_refused_password_names_the_provider_and_where_to_make_one(
    server: Server, tmp_path: Path
) -> None:
    server.password = "revoked"
    result = await tools(tmp_path)["email_search"].run()
    assert result.is_error and "iCloud Mail (personal) refused the app password" in result.content
    assert "account.apple.com" in result.content and PASSWORD not in result.content


# -- R4.9-R4.11: searching and ids -----------------------------------------------------------


async def test_a_query_becomes_standard_search_and_ids_name_the_folder(
    server: Server, tmp_path: Path
) -> None:
    today = datetime.date.today()
    recent = email.utils.format_datetime(
        datetime.datetime(today.year, today.month, today.day, 9, tzinfo=datetime.UTC)
    )
    sam = server.add("INBOX", mail(date=recent))
    server.add("INBOX", mail("Shop <news@shop.example>", "Big sale"), "\\Seen")
    old = server.add("Archive", mail("Sam Lee <sam@example.com>", "Photos from June"), "\\Seen")
    made = tools(tmp_path)

    result = await made["email_search"].run(query="from:sam")
    assert not result.is_error, result.content
    assert result.content.splitlines()[0] == "1 message(s) for 'from:sam', newest first:"
    assert "Dinner Friday? · unread" in result.content
    assert f"[id: {server.id_of('INBOX', sam)}]" in result.content
    assert server.searches[-1] == ("FROM", '"sam"')

    anywhere = await made["email_search"].run(query="from:sam in:anywhere")
    assert "Photos from June" in anywhere.content and "in Archive" in anywhere.content
    assert f"id: {server.id_of('Archive', old)}" in anywhere.content

    await made["email_search"].run(query='-from:shop is:unread newer_than:7d subject:"dinner"')
    week = today - datetime.timedelta(days=7)
    assert server.searches[-1] == (
        "NOT",
        "FROM",
        '"shop"',
        "UNSEEN",
        "SINCE",
        f"{week.day:02d}-{MONTHS[week.month - 1]}-{week.year}",
        "SUBJECT",
        '"dinner"',
    )

    server.add("INBOX", mail(subject="Café on Friday"))
    accented = await made["email_search"].run(query="from:sam subject:café")
    assert "Café on Friday" in accented.content
    assert server.searches[-1] == ("CHARSET", "UTF-8", "FROM", '"sam"', "SUBJECT")

    read = await made["email_read"].run(message_id=server.id_of("INBOX", sam))
    assert "Are you free Friday at 7?" in read.content


@pytest.mark.parametrize("query", ["is:important", "category:primary", "from:a OR from:b"])
async def test_what_imap_cannot_search_for_is_refused_by_name(
    server: Server, tmp_path: Path, query: str
) -> None:
    result = await tools(tmp_path)["email_search"].run(query=query)
    assert result.is_error and "Gmail's alone" in result.content
    assert "from:, to:" in result.content
    assert server.searches == []


async def test_an_out_of_date_id_is_never_another_message(server: Server, tmp_path: Path) -> None:
    sam = server.add("INBOX", mail())
    message_id = server.id_of("INBOX", sam)
    server.folders["INBOX"].validity += 1  # the server renumbered the folder

    result = await tools(tmp_path)["email_read"].run(message_id=message_id)

    assert result.is_error and "out of date - search again" in result.content


# -- R4.12 ------------------------------------------------------------------------------------


async def test_a_thread_is_found_by_its_references(server: Server, tmp_path: Path) -> None:
    first = server.add(
        "INBOX", mail(body="Are you free?", headers={"Message-ID": "<root@example.com>"})
    )
    server.add(
        "Sent Messages",
        mail(
            ME,
            "Re: Dinner Friday?",
            "Yes, Friday works!",
            to="sam@example.com",
            headers={"References": "<root@example.com>"},
        ),
        "\\Seen",
    )
    server.add("INBOX", mail("Shop <news@shop.example>", "Unrelated"))

    result = await tools(tmp_path)["email_read"].run(thread_id=server.id_of("INBOX", first))

    assert "Are you free?" in result.content and "Yes, Friday works!" in result.content
    assert "Unrelated" not in result.content


async def test_organising_moves_between_folders(server: Server, tmp_path: Path) -> None:
    news = [server.add("INBOX", mail("Shop <news@shop.example>", f"Sale {n}")) for n in range(2)]
    sam = server.add("INBOX", mail())
    made = tools(tmp_path)

    subject, archived = await carded(made["email_organise"], action="archive", query="from:shop")
    assert subject.summary == "Archive 2 message(s) from Shop (personal)"
    assert not archived.is_error, archived.content
    assert server.where(b"Sale 0") == ["Archive"] and server.where(b"Sale 1") == ["Archive"]
    assert news[0] not in server.folders["INBOX"].messages

    sam_id = server.id_of("INBOX", sam)
    _, labelled = await carded(
        made["email_organise"], action="label", label="Family", message_ids=[sam_id]
    )
    assert not labelled.is_error and server.where(b"Dinner Friday?") == ["Family"]

    unlabel = await made["email_organise"].run(action="unlabel", label="Family", query="in:Family")
    assert unlabel.is_error and "folders, not labels" in unlabel.content

    _, starred = await carded(made["email_organise"], action="star", query="in:Family")
    assert not starred.is_error
    assert "\\Flagged" in server.folders["Family"].messages[0].flags

    _, trashed = await carded(made["email_organise"], action="trash", query="in:Family")
    assert not trashed.is_error and server.where(b"Dinner Friday?") == ["Deleted Messages"]
    assert server.expunged == []  # MOVE, never an expunge


async def test_without_move_a_copy_and_an_expunge_of_exactly_those(
    server: Server, tmp_path: Path
) -> None:
    server.capabilities = ("IMAP4REV1", "UIDPLUS")
    keep = server.add("INBOX", mail("Keep <keep@example.com>", "Keep me"))
    gone = server.add("INBOX", mail("Shop <news@shop.example>", "Sale"))
    made = tools(tmp_path)

    _, result = await carded(made["email_organise"], action="trash", query="from:shop")

    assert not result.is_error, result.content
    assert server.expunged == [gone.uid]
    assert server.folders["INBOX"].messages == [keep]
    assert server.where(b"Sale") == ["Deleted Messages"]

    server.capabilities = ("IMAP4REV1",)
    refused = await made["email_organise"].run(action="trash", query="from:keep")
    assert refused.is_error and "nothing was changed" in refused.content
    assert server.folders["INBOX"].messages == [keep]


async def test_a_draft_and_a_send_are_filed_in_drafts_and_sent(
    server: Server, tmp_path: Path
) -> None:
    made = tools(tmp_path)
    saved = await made["email_draft"].run(to=["sam@example.com"], subject="Plans", body="Draft.")
    draft_id = re.search(r"draft id: ([^\]]+)\]", saved.content).group(1)  # type: ignore[union-attr]
    [draft] = server.folders["Drafts"].messages
    assert draft_id == server.id_of("Drafts", draft) and "\\Draft" in draft.flags

    subject, sent = await carded(made["email_send"], draft_id=draft_id)
    assert "Draft." in subject.summary and not sent.is_error, sent.content
    assert server.folders["Drafts"].messages == []  # the draft went to Trash
    assert server.where(b"Subject: Plans") == ["Sent Messages", "Deleted Messages"]

    _, plain = await carded(
        made["email_send"], to=["sam@example.com"], bcc=["me2@example.com"], subject="Hi", body="x"
    )
    assert not plain.is_error and "not filed" not in plain.content
    [filed] = [m for m in server.folders["Sent Messages"].messages if b"Subject: Hi" in m.raw]
    assert "\\Seen" in filed.flags and filed.parsed["Bcc"] == "me2@example.com"
    assert server.sent[-1][2].find(b"Bcc:") < 0  # what went out carries no Bcc header

    server.folders.pop("Sent Messages")
    _, unfiled = await carded(made["email_send"], to=["sam@example.com"], subject="x", body="y")
    assert not unfiled.is_error and "It went, but was not filed in Sent" in unfiled.content


async def test_folders_are_listed_without_the_special_ones(server: Server, tmp_path: Path) -> None:
    assert (await tools(tmp_path)["email_labels"].run()).content == "personal: Family"
    server.special_use = False  # found by the names providers give them
    assert (await tools(tmp_path)["email_labels"].run()).content == "personal: Family"


async def test_icloud_sends_over_starttls(server: Server, tmp_path: Path) -> None:
    _, result = await carded(
        tools(tmp_path)["email_send"], to=["sam@example.com"], subject="x", body="y"
    )
    assert not result.is_error, result.content
    assert server.smtp == [("smtp.mail.me.com", 587, True)]


# -- R7.2 in standard mode ----------------------------------------------------------------------


class ServiceContext:
    def __init__(self, workspace: Path, sleeps: int = 1, **settings: Any) -> None:
        self.workspace = workspace
        self.settings = {"notify": "important", "poll_minutes": 2, **settings}
        self.left = sleeps
        self.sent: list[str] = []
        self.state_dir = workspace / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.between: list[Any] = []

    @property
    def stopping(self) -> bool:
        return self.left < 0

    def setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    async def sleep_for(self, seconds: float) -> bool:
        self.left -= 1
        if self.between:
            self.between.pop(0)()
        return self.stopping

    async def notify(self, text: str) -> str:
        self.sent.append(text)
        return "telegram:dm:1"


async def test_new_mail_outside_gmail_is_new_unread_inbox_mail(
    server: Server, tmp_path: Path
) -> None:
    server.add("INBOX", mail("Old <old@example.com>", "From last week"))
    ctx = ServiceContext(tmp_path, sleeps=3)
    ctx.between = [
        lambda: (
            server.add("INBOX", mail()),
            server.add("INBOX", mail("Read <read@example.com>", "Already read"), "\\Seen"),
            server.add("INBOX", mail(f"Me <{ME}>", "Note to self")),
            server.add("Archive", mail("Elsewhere <x@example.com>", "Not the inbox")),
        ),
        lambda: [
            server.add("INBOX", mail(f"Person {n} <p{n}@example.com>", f"Hello {n}"))
            for n in range(3)
        ],
        lambda: None,
    ]
    await em.NewMail().run(ctx)  # type: ignore[arg-type]
    assert ctx.sent == [
        "📧 Sam Lee - Dinner Friday?",
        "📧 3 new emails: Person 0, Person 1, Person 2",
    ]


def test_query_tokens_are_read_as_a_shell_would() -> None:
    """The tokenizer the plugin uses keeps a quoted phrase whole, behind an operator or not."""
    assert shlex.split('subject:"big sale" "a phrase" -from:x') == [
        "subject:big sale",
        "a phrase",
        "-from:x",
    ]
