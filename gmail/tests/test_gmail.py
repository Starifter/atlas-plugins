"""The Gmail plugin (`docs/spec/gmail.md`), driven against a fake Gmail.

`FakeImap` speaks the part of IMAP the plugin uses, the way `imaplib` hands it
back - Gmail's `X-GM-RAW`, `X-GM-MSGID`, `X-GM-THRID` and `X-GM-LABELS`, UIDPLUS's
`APPENDUID`, `MOVE` - and `FakeSmtp` records what it was sent. Neither reaches
Google. The plugin's `IMAP4_SSL` and `SMTP_SSL` are replaced with them.

Run from a checkout of Atlas (`uv run --project ../Atlas pytest gmail/tests`).
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import imaplib
import importlib.util
import re
import sys
from email.message import EmailMessage
from pathlib import Path
from smtplib import SMTPAuthenticationError
from types import SimpleNamespace
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("atlas_plugin_gmail", HERE.parent / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gm = _load()

ME = "me@gmail.com"
PASSWORD = "abcdefghijklmnop"


# -- the fake Gmail ---------------------------------------------------------------------


class Stored:
    def __init__(self, uid: int, raw: bytes, **extra: Any) -> None:
        self.uid = uid
        self.msgid = 0x18C0000000000000 + uid
        self.thrid = extra.pop("thrid", self.msgid)
        self.raw = raw
        self.labels: set[str] = set(extra.pop("labels", {"\\Inbox"}))
        self.flags: set[str] = set(extra.pop("flags", set()))
        self.category = extra.pop("category", "primary")
        self.trashed = False

    @property
    def parsed(self) -> Any:
        return email.message_from_bytes(self.raw, policy=email.policy.default)


class Gmail:
    """One account's server state."""

    def __init__(self) -> None:
        self.messages: list[Stored] = []
        self.password = PASSWORD
        self.sent: list[tuple[str, list[str], bytes]] = []
        self.logins = 0

    def add(self, raw: bytes, **extra: Any) -> Stored:
        stored = Stored(len(self.messages) + 1, raw, **extra)
        self.messages.append(stored)
        return stored

    def in_folder(self, folder: str) -> list[Stored]:
        if folder == "INBOX":
            return [m for m in self.messages if "\\Inbox" in m.labels and not m.trashed]
        if folder == '"[Gmail]/Drafts"':
            return [m for m in self.messages if "\\Draft" in m.flags and not m.trashed]
        if folder == '"[Gmail]/Trash"':
            return [m for m in self.messages if m.trashed]
        return [m for m in self.messages if not m.trashed]


def matches(message: Stored, query: str) -> bool:
    """A few of Gmail's search operators - enough for what the tests ask."""
    parsed = message.parsed
    body = parsed.get_body(preferencelist=("plain", "html"))
    text = f"{parsed.get('Subject', '')} {body.get_content() if body else ''}".lower()
    for token in query.split():
        key, _, value = token.partition(":")
        value = value.lower()
        if key == "in" and value == "inbox":
            ok = "\\Inbox" in message.labels
        elif key == "is" and value == "unread":
            ok = "\\Seen" not in message.flags
        elif key == "is" and value == "important":
            ok = "\\Important" in message.labels
        elif key == "category":
            ok = message.category == value
        elif key == "from":
            ok = value in str(parsed.get("From", "")).lower()
        elif key == "label":
            ok = any(label.lower() == value for label in message.labels)
        elif key == "has" and value == "attachment":
            ok = any(part.get_filename() for part in parsed.walk())
        else:
            ok = token.lower() in text
        if not ok:
            return False
    return True


class FakeImap:
    server: Gmail

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        assert (host, port) == ("imap.gmail.com", 993)
        self.folder = ""
        self.literal: bytes | None = None

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        if password != self.server.password:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        self.server.logins += 1
        return "OK", [b"logged in"]

    def logout(self) -> tuple[str, list[bytes]]:
        return "BYE", [b""]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.folder = folder
        return "OK", [str(len(self.server.in_folder(folder))).encode()]

    def response(self, name: str) -> tuple[str, list[bytes]]:
        if name == "UIDVALIDITY":
            return name, [b"7"]
        if name == "UIDNEXT":
            return name, [str(len(self.server.messages) + 1).encode()]
        return name, [None]  # type: ignore[list-item]

    def append(self, folder: str, flags: str, date: Any, data: bytes) -> tuple[str, list[bytes]]:
        assert folder == '"[Gmail]/Drafts"' and "\\Draft" in flags
        stored = self.server.add(data, labels=set(), flags={"\\Draft", "\\Seen"})
        return "OK", [b"[APPENDUID 7 %d] (Success)" % stored.uid]

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        here = self.server.in_folder(self.folder)
        if command == "SEARCH":
            found = list(here)
            items = list(args)
            while items:
                key = items.pop(0)
                if key == "X-GM-RAW":
                    query = items.pop(0).strip('"').replace('\\"', '"')
                    found = [m for m in found if matches(m, query)]
                elif key == "X-GM-MSGID":
                    wanted = int(items.pop(0))
                    found = [m for m in found if m.msgid == wanted]
                elif key == "X-GM-THRID":
                    wanted = int(items.pop(0))
                    found = [m for m in found if m.thrid == wanted]
                elif key == "UID":
                    low = int(items.pop(0).split(":")[0])
                    newer = [m for m in found if m.uid >= low]
                    # Gmail's quirk: `n:*` with nothing at or above n answers the last one.
                    found = newer or ([max(here, key=lambda m: m.uid)] if here else [])
            return "OK", [" ".join(str(m.uid) for m in found).encode()]
        if command == "FETCH":
            uids = {int(u) for u in args[0].split(",")}
            parts = args[1]
            out: list[Any] = []
            for message in sorted(
                (m for m in self.server.messages if m.uid in uids), key=lambda m: m.uid
            ):
                labels = " ".join(
                    f'"{label}"' if " " in label else label for label in sorted(message.labels)
                )
                flags = " ".join(sorted(message.flags))
                meta = (
                    f"{message.uid} (UID {message.uid} X-GM-MSGID {message.msgid} "
                    f"X-GM-THRID {message.thrid} X-GM-LABELS ({labels}) FLAGS ({flags}) "
                    f"RFC822.SIZE {len(message.raw)}"
                )
                if "BODY.PEEK[]" in parts:
                    body = message.raw
                elif "HEADER.FIELDS" in parts:
                    names = re.search(r"HEADER\.FIELDS \(([^)]*)\)", parts).group(1).split()  # type: ignore[union-attr]
                    head = message.raw.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
                    body = (
                        b"\r\n".join(
                            line for line in head if line.split(b":")[0].upper().decode() in names
                        )
                        + b"\r\n\r\n"
                    )
                elif "HEADER" in parts:
                    body = message.raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                else:
                    out.append((meta + ")").encode())
                    continue
                out.append(((meta + f" BODY[] {{{len(body)}}}").encode(), body))
                out.append(b")")
            return "OK", out
        if command == "STORE":
            uids = {int(u) for u in args[0].split(",")}
            operation, value = args[1], args[2].strip("()").strip('"')
            for message in self.server.messages:
                if message.uid not in uids:
                    continue
                target = message.labels if "X-GM-LABELS" in operation else message.flags
                (target.add if operation.startswith("+") else target.discard)(value)
            return "OK", [b""]
        if command == "MOVE":
            uids = {int(u) for u in args[0].split(",")}
            assert args[1] == '"[Gmail]/Trash"'
            for message in self.server.messages:
                if message.uid in uids:
                    message.trashed = True
            return "OK", [b""]
        raise AssertionError(f"unexpected UID {command}")

    def list(self) -> tuple[str, list[bytes]]:
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren) "/" "School"',
            b'(\\HasNoChildren) "/" "Receipts"',
            b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
            b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"',
        ]


class FakeSmtp:
    server: Gmail

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        assert (host, port) == ("smtp.gmail.com", 465)

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def login(self, user: str, password: str) -> None:
        if password != self.server.password:
            raise SMTPAuthenticationError(535, b"Username and Password not accepted")

    def sendmail(self, sender: str, recipients: list[str], data: bytes) -> dict[str, Any]:
        self.server.sent.append((sender, list(recipients), data))
        return {}


def mail(
    sender: str = "Sam Lee <sam@example.com>",
    subject: str = "Dinner Friday?",
    body: str = "Are you free Friday at 7?",
    *,
    to: str = ME,
    cc: str = "",
    html: str = "",
    files: tuple[tuple[str, bytes, str], ...] = (),
    headers: dict[str, str] | None = None,
) -> bytes:
    message = EmailMessage(policy=email.policy.SMTP)
    message["From"] = sender
    message["To"] = to
    if cc:
        message["Cc"] = cc
    message["Subject"] = subject
    message["Date"] = "Thu, 01 Oct 2026 18:02:00 +1000"
    message["Message-ID"] = f"<{abs(hash((sender, subject)))}@example.com>"
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content(body)
    if html:
        message.add_alternative(html, subtype="html")
    for name, data, kind in files:
        main, _, sub = kind.partition("/")
        message.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return message.as_bytes()


@pytest.fixture
def gmail(monkeypatch: pytest.MonkeyPatch) -> Gmail:
    server = Gmail()
    FakeImap.server = server
    FakeSmtp.server = server
    monkeypatch.setattr(gm, "IMAP4_SSL", FakeImap)
    monkeypatch.setattr(gm, "SMTP_SSL", FakeSmtp)
    monkeypatch.setenv("GMAIL_ADDRESS", ME)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")  # as Google shows it
    return server


class Media:
    """The media store, as far as the tools use it."""

    def __init__(self) -> None:
        self.put_calls: list[tuple[bytes, str]] = []
        self.held: dict[str, bytes] = {}

    def put(self, data: bytes, *, source: str) -> Any:
        self.put_calls.append((data, source))
        return SimpleNamespace(source=source, sha256=hashlib.sha256(data).hexdigest())

    def get(self, block: Any) -> bytes | None:
        return self.held.get(block.sha256)


class Context:
    """A `PluginContext`, as far as the tools use it."""

    def __init__(self, workspace: Path, **settings: Any) -> None:
        self.workspace = workspace
        self.settings = settings
        self.media = Media()
        self.web_policy: Any = SimpleNamespace(allow_private=False, hosts=None, max_redirects=5)
        self.sent_here: list[Any] = []
        self.audited: list[tuple[str, str, dict[str, Any]]] = []

    def session_media(self) -> tuple[Any, ...]:
        return tuple(self.sent_here)

    def audit(self, event: str, detail: str = "", **kwargs: Any) -> None:
        self.audited.append((event, detail, dict(kwargs.get("arguments") or {})))


def tools(tmp_path: Path, **settings: Any) -> tuple[Context, dict[str, Any]]:
    ctx = Context(tmp_path, **settings)
    shared = gm.Mail(ctx)
    made = {
        cls.name: cls(shared)
        for cls in (gm.Search, gm.Read, gm.Attachment, gm.Labels, gm.Draft, gm.Send, gm.Organise)
    }
    return ctx, made


async def carded(tool: Any, **arguments: Any) -> tuple[Any, Any]:
    """What the executor does: the card, then - the person having said yes - the run."""
    subject = await tool.subject(arguments)
    return subject, await tool.run(**arguments)


def sent_message(server: Gmail, index: int = -1) -> Any:
    return email.message_from_bytes(server.sent[index][2], policy=email.policy.default)


# -- G1, R5.2, R5.3 ------------------------------------------------------------------------


async def test_nothing_is_reached_until_an_account_is_set_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    _, made = tools(tmp_path)
    result = await made["email_search"].run()
    assert result.is_error and "Gmail is not set up" in result.content


async def test_a_refused_password_says_what_to_do_and_never_shows_it(
    gmail: Gmail, tmp_path: Path
) -> None:
    gmail.password = "something-else"
    _, made = tools(tmp_path)
    result = await made["email_search"].run()
    assert result.is_error and "refused the app password" in result.content
    assert "abcd" not in result.content and PASSWORD not in result.content


# -- reading ---------------------------------------------------------------------------------


async def test_search_uses_gmails_own_syntax_newest_first(gmail: Gmail, tmp_path: Path) -> None:
    gmail.add(
        mail("Newsletter <news@shop.example>", "Big sale"), labels={"\\Inbox"}, flags={"\\Seen"}
    )
    sam = gmail.add(mail(), labels={"\\Inbox", "\\Important"})
    gmail.add(mail("Sam Lee <sam@example.com>", "Photos"), labels={"\\Inbox", "School"})
    _, made = tools(tmp_path)

    result = await made["email_search"].run(query="from:sam")
    assert not result.is_error, result.content
    lines = result.content.splitlines()
    assert lines[0] == "2 message(s) for 'from:sam', newest first:"
    assert "Dinner Friday? · unread, important" in result.content
    assert f"[id: {sam.msgid:x} · thread: {sam.thrid:x}]" in result.content
    assert "Big sale" not in result.content
    everything = await made["email_search"].run()
    assert "Big sale" in everything.content  # the default is the inbox


async def test_reading_folds_quotes_turns_html_to_text_and_lists_files(
    gmail: Gmail, tmp_path: Path
) -> None:
    body = "Friday works.\n\nOn Wed, 30 Sep 2026, Me wrote:\n> Are you free?\n> Let me know"
    stored = gmail.add(
        mail(
            body=body,
            html="<html><body><p>Friday <b>works</b>.</p></body></html>",
            files=(("menu.pdf", b"%PDF-1.4 menu", "application/pdf"),),
        )
    )
    _, made = tools(tmp_path)
    result = await made["email_read"].run(message_id=f"{stored.msgid:x}")
    text = result.content
    assert "From: Sam Lee <sam@example.com>" in text and "Subject: Dinner Friday?" in text
    assert "Friday works." in text and "[... 3 quoted line(s) folded]" in text
    assert "Are you free?" not in text
    assert "1. menu.pdf (application/pdf, 13 B)" in text

    html_only = gmail.add(mail(body="x", subject="Receipt"))
    html_only.raw = html_only.raw.replace(
        b"Content-Type: text/plain", b"Content-Type: text/html"
    ).replace(b"x\r\n", b"<p>Total: <b>$42</b></p>\r\n")
    receipt = await made["email_read"].run(message_id=f"{html_only.msgid:x}")
    assert "Total: $42" in receipt.content and "<b>" not in receipt.content


async def test_an_unverified_sender_is_said_before_the_body(gmail: Gmail, tmp_path: Path) -> None:
    stored = gmail.add(
        mail(
            "Bank <security@bank.example>",
            "Verify your account",
            "Click here and reply with your password.",
            headers={
                "Authentication-Results": "mx.google.com; spf=fail smtp.mailfrom=bank.example"
            },
        )
    )
    _, made = tools(tmp_path)
    text = (await made["email_read"].run(message_id=f"{stored.msgid:x}")).content
    warning = text.index("(Gmail could not verify that this came from bank.example)")
    assert warning < text.index("Click here")


async def test_a_thread_is_read_whole(gmail: Gmail, tmp_path: Path) -> None:
    first = gmail.add(mail(body="Are you free?"))
    gmail.add(mail(ME, "Re: Dinner Friday?", "Yes!", to="sam@example.com"), thrid=first.thrid)
    _, made = tools(tmp_path)
    text = (await made["email_read"].run(thread_id=f"{first.thrid:x}")).content
    assert "Are you free?" in text and "Yes!" in text and text.count("---") >= 1


async def test_an_attachment_is_a_picture_or_a_file_in_the_workspace(
    gmail: Gmail, tmp_path: Path
) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
    stored = gmail.add(
        mail(
            files=(
                ("../../evil name.pdf", b"%PDF-1.4 x", "application/pdf"),
                ("map.png", png, "image/png"),
            )
        )
    )
    ctx, made = tools(tmp_path)
    saved = await made["email_attachment"].run(message_id=f"{stored.msgid:x}", attachment="1")
    target = tmp_path / "email-attachments" / f"{stored.msgid:x}" / "evil name.pdf"
    assert target.read_bytes() == b"%PDF-1.4 x"
    assert "read_media" in saved.content

    picture = await made["email_attachment"].run(
        message_id=f"{stored.msgid:x}", attachment="map.png"
    )
    assert picture.images and ctx.media.put_calls == [(png, "map.png (attached to an email)")]


async def test_labels_are_listed_without_gmails_own_folders(gmail: Gmail, tmp_path: Path) -> None:
    _, made = tools(tmp_path)
    assert (await made["email_labels"].run()).content == "personal: Receipts, School"


def test_every_tool_is_owner_only_and_untrusted(tmp_path: Path) -> None:
    _, made = tools(tmp_path)
    for tool in made.values():
        assert tool.trusted_only and tool.untrusted, tool.name
    assert {n for n, t in made.items() if t.gated} == {
        "email_draft",
        "email_send",
        "email_organise",
    }


# -- G2, R6.9-R6.13: sending -------------------------------------------------------------------


async def test_a_send_is_a_confirm_card_with_everything_on_it(gmail: Gmail, tmp_path: Path) -> None:
    ctx, made = tools(tmp_path)
    subject, result = await carded(
        made["email_send"],
        to=["sam@example.com"],
        bcc=["boss@example.com"],
        subject="Friday",
        body="Friday works - see you at 7.",
    )
    assert subject.confirm is True
    assert subject.summary.splitlines()[:6] == [
        f"Send from {ME}",
        "To: sam@example.com",
        "Bcc: boss@example.com",
        "Subject: Friday",
        "",
        "Friday works - see you at 7.",
    ]
    assert not result.is_error, result.content
    [(sender, recipients, _)] = gmail.sent
    assert sender == ME and recipients == ["sam@example.com", "boss@example.com"]
    message = sent_message(gmail)
    assert message["Bcc"] is None and message["From"] == ME
    assert message.get_content().strip() == "Friday works - see you at 7."
    assert ctx.audited[-1][0] == "email_sent" and ctx.audited[-1][2]["recipients"] == 2


async def test_a_reply_goes_where_the_original_says_and_threads(
    gmail: Gmail, tmp_path: Path
) -> None:
    original = gmail.add(
        mail(
            cc="alex@example.com, me@gmail.com",
            headers={"Reply-To": "Sam <sam.home@example.com>", "References": "<older@example.com>"},
        )
    )
    _, made = tools(tmp_path)
    subject, _ = await carded(
        made["email_send"], reply_to=f"{original.msgid:x}", reply_all=True, body="Friday works."
    )
    assert "To: sam.home@example.com" in subject.summary
    assert (
        "Cc: alex@example.com" in subject.summary
        and "Cc: alex@example.com, me" not in subject.summary
    )
    assert "(replying to Sam Lee, " in subject.summary
    message = sent_message(gmail)
    assert message["Subject"] == "Re: Dinner Friday?"
    assert message["In-Reply-To"] == original.parsed["Message-ID"]
    assert message["References"].startswith("<older@example.com>")
    assert "Are you free" not in message.get_content()  # the original is not quoted


async def test_a_send_without_a_card_sends_nothing(gmail: Gmail, tmp_path: Path) -> None:
    _, made = tools(tmp_path)
    result = await made["email_send"].run(to=["sam@example.com"], subject="x", body="y")
    assert result.is_error and "shown to the person first" in result.content
    assert gmail.sent == []
    nobody = await made["email_send"].run(subject="x", body="y")
    assert nobody.is_error and "no recipients" in nobody.content


async def test_sends_are_capped_per_hour(gmail: Gmail, tmp_path: Path) -> None:
    _, made = tools(tmp_path, sends_per_hour=1)
    arguments = {"to": ["sam@example.com"], "subject": "x", "body": "y"}
    await carded(made["email_send"], **arguments)
    _, second = await carded(made["email_send"], **arguments)
    assert second.is_error and "the limit" in second.content and len(gmail.sent) == 1


async def test_a_draft_is_free_and_sending_it_checks_it_did_not_change(
    gmail: Gmail, tmp_path: Path
) -> None:
    _, made = tools(tmp_path)
    arguments = {"to": ["sam@example.com"], "subject": "Plans", "body": "Draft text."}
    assert await made["email_draft"].subject(arguments) is None  # a draft reaches nobody
    saved = await made["email_draft"].run(**arguments)
    draft_id = re.search(r"draft id: (\w+)", saved.content).group(1)  # type: ignore[union-attr]
    [draft] = [m for m in gmail.messages if "\\Draft" in m.flags]
    assert f"{draft.msgid:x}" == draft_id

    subject = await made["email_send"].subject({"draft_id": draft_id})
    assert "Draft text." in subject.summary and "(sending a saved draft)" in subject.summary
    draft.raw = draft.raw.replace(b"Draft text.", b"Edited in Gmail.")
    changed = await made["email_send"].run(draft_id=draft_id)
    assert changed.is_error and "changed since it was shown" in changed.content and gmail.sent == []

    _, sent = await carded(made["email_send"], draft_id=draft_id)
    assert not sent.is_error and "Edited in Gmail." in sent_message(gmail).get_content()
    assert draft.trashed


# -- G9, R6.14-R6.19: attachments ---------------------------------------------------------------


async def test_a_workspace_file_is_attached_as_the_card_showed_it(
    gmail: Gmail, tmp_path: Path
) -> None:
    (tmp_path / "docs").mkdir()
    menu = tmp_path / "docs" / "menu.pdf"
    menu.write_bytes(b"%PDF-1.4 the real menu")
    ctx, made = tools(tmp_path)
    arguments = {
        "to": ["sam@example.com"],
        "subject": "Menu",
        "body": "Attached.",
        "attachments": ["docs/menu.pdf"],
    }

    subject = await made["email_send"].subject(arguments)
    digest = hashlib.sha256(b"%PDF-1.4 the real menu").hexdigest()
    assert (
        f"menu.pdf - PDF, 22 B, sha256 {digest[:8]} - from the workspace: docs/menu.pdf"
        in subject.summary
    )
    menu.write_bytes(b"%PDF-1.4 swapped after the card")
    await made["email_send"].run(**arguments)

    [part] = [p for p in sent_message(gmail).walk() if p.get_filename()]
    assert part.get_filename() == "menu.pdf" and part.get_content_type() == "application/pdf"
    assert part.get_payload(decode=True) == b"%PDF-1.4 the real menu"  # R6.17
    assert ctx.audited[-1][2]["files"] == [
        {
            "name": "menu.pdf",
            "size": 22,
            "sha256": digest,
            "source": "from the workspace: docs/menu.pdf",
        }
    ]


@pytest.mark.parametrize(
    ("path", "why"),
    [
        ("../outside.txt", "outside the workspace"),
        (".env", "hidden file"),
        ("notes/.ssh/config", "hidden file"),
        (".atlas/connections.json", "Atlas's own state"),
        ("keys/id_ed25519", "key material"),
        ("keys/server.pem", "key material"),
        ("notes/setup.txt", "credential"),
        ("missing.pdf", "no file"),
    ],
)
async def test_what_may_not_leave_is_refused_before_any_card(
    gmail: Gmail, tmp_path: Path, path: str, why: str
) -> None:
    workspace = tmp_path / "ws"
    for name, content in {
        ".env": b"X=1",
        "notes/.ssh/config": b"Host x",
        ".atlas/connections.json": b"{}",
        "keys/id_ed25519": b"-",
        "keys/server.pem": b"-",
        "notes/setup.txt": b"my key is sk-ant-api03-" + b"a" * 90,
    }.items():
        (workspace / name).parent.mkdir(parents=True, exist_ok=True)
        (workspace / name).write_bytes(content)
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    _, made = tools(workspace)
    arguments = {"to": ["sam@example.com"], "subject": "x", "body": "y", "attachments": [path]}
    assert await made["email_send"].subject(arguments) is None
    result = await made["email_send"].run(**arguments)
    assert result.is_error and why in result.content and gmail.sent == []
    if path == "notes/setup.txt":
        assert "sk-ant" not in result.content  # the label, never the value


async def test_a_file_sent_in_this_chat_can_be_attached(gmail: Gmail, tmp_path: Path) -> None:
    ctx, made = tools(tmp_path)
    data = b"%PDF-1.4 the lease"
    block = SimpleNamespace(name="lease.pdf", source="lease.pdf (attached)", sha256="abc")
    ctx.media.held["abc"] = data
    ctx.sent_here.append(block)
    subject, _ = await carded(
        made["email_send"],
        to=["agent@example.com"],
        subject="Lease",
        body="Signed.",
        attachments=["chat:lease.pdf"],
    )
    assert (
        "lease.pdf - PDF, 18 B" in subject.summary and "you sent it in this chat" in subject.summary
    )
    [part] = [p for p in sent_message(gmail).walk() if p.get_filename()]
    assert part.get_payload(decode=True) == data

    missing = await made["email_send"].run(
        to=["agent@example.com"], subject="x", body="y", attachments=["chat:nothing.pdf"]
    )
    assert missing.is_error and "was sent in this conversation" in missing.content


async def test_a_forward_carries_the_original_and_its_files(gmail: Gmail, tmp_path: Path) -> None:
    original = gmail.add(mail(files=(("menu.pdf", b"%PDF-1.4 menu", "application/pdf"),)))
    _, made = tools(tmp_path)
    subject, _ = await carded(
        made["email_send"], forward=f"{original.msgid:x}", to=["alex@example.com"], body="FYI"
    )
    assert "---------- Forwarded message ---------" in subject.summary
    assert "Are you free Friday at 7?" in subject.summary  # the forwarded text is on the card
    assert "menu.pdf - PDF, 13 B" in subject.summary and "from the original" in subject.summary
    message = sent_message(gmail)
    assert message["Subject"] == "Fwd: Dinner Friday?"
    assert [p.get_filename() for p in message.walk() if p.get_filename()] == ["menu.pdf"]


async def test_a_draft_with_files_asks_first(gmail: Gmail, tmp_path: Path) -> None:
    (tmp_path / "plan.txt").write_text("the plan", encoding="utf-8")
    _, made = tools(tmp_path)
    subject = await made["email_draft"].subject(
        {
            "to": ["sam@example.com"],
            "subject": "Plan",
            "body": "See file.",
            "attachments": ["plan.txt"],
        }
    )
    assert subject is not None and subject.confirm is False
    assert "plan.txt - PLAIN, 8 B" in subject.summary


# -- R6.3: organising --------------------------------------------------------------------------


async def test_organising_is_carded_capped_and_never_permanent(
    gmail: Gmail, tmp_path: Path
) -> None:
    news = [gmail.add(mail("Shop <news@shop.example>", f"Sale {n}")) for n in range(3)]
    keep = gmail.add(mail())
    _, made = tools(tmp_path)
    subject, result = await carded(made["email_organise"], action="archive", query="from:shop")
    assert subject.summary == "Archive 3 message(s) from Shop (personal)" and not subject.confirm
    assert not result.is_error and all("\\Inbox" not in m.labels for m in news)
    assert "\\Inbox" in keep.labels

    _, trashed = await carded(
        made["email_organise"], action="trash", message_ids=[f"{news[0].msgid:x}"]
    )
    assert not trashed.is_error and news[0].trashed and news[0] in gmail.messages

    for n in range(51):
        gmail.add(mail("Bulk <bulk@example.com>", f"Bulk {n}"))
    too_many = await made["email_organise"].run(action="read", query="from:bulk")
    assert too_many.is_error and "at most 50" in too_many.content


# -- §7: important mail ------------------------------------------------------------------------


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


async def test_important_mail_is_one_line_and_the_backlog_is_not_announced(
    gmail: Gmail, tmp_path: Path
) -> None:
    gmail.add(mail("Old <old@example.com>", "From last week"), labels={"\\Inbox", "\\Important"})
    ctx = ServiceContext(tmp_path, sleeps=2)
    ctx.between = [
        lambda: (
            gmail.add(mail(), labels={"\\Inbox", "\\Important"}),
            gmail.add(mail("Shop <news@shop.example>", "Sale"), labels={"\\Inbox"}),
            gmail.add(mail(f"Me <{ME}>", "Note to self"), labels={"\\Inbox", "\\Important"}),
        ),
        lambda: None,
    ]
    await gm.NewMail().run(ctx)  # type: ignore[arg-type]
    assert ctx.sent == ["📧 Sam Lee - Dinner Friday?"]


async def test_several_at_once_are_one_line_of_senders(gmail: Gmail, tmp_path: Path) -> None:
    ctx = ServiceContext(tmp_path, sleeps=1)
    ctx.between = [
        lambda: [
            gmail.add(
                mail(f"Person {n} <p{n}@example.com>", f"Hello {n}"),
                labels={"\\Inbox", "\\Important"},
            )
            for n in range(3)
        ]
    ]
    await gm.NewMail().run(ctx)  # type: ignore[arg-type]
    assert ctx.sent == ["📧 3 important emails: Person 0, Person 1, Person 2"]


async def test_a_revoked_password_is_said_once(gmail: Gmail, tmp_path: Path) -> None:
    gmail.password = "revoked"
    ctx = ServiceContext(tmp_path, sleeps=3)
    await gm.NewMail().run(ctx)  # type: ignore[arg-type]
    assert len(ctx.sent) == 1 and "refused the app password" in ctx.sent[0]


# -- SDK 1.34 ------------------------------------------------------------------------------------


# -- R6.20-R6.23: links ----------------------------------------------------------------------


def parts_of(message: Any) -> dict[str, str]:
    return {
        part.get_content_type(): part.get_content()
        for part in message.walk()
        if part.get_content_type() in ("text/plain", "text/html")
    }


async def test_link_words_go_as_html_and_the_card_shows_the_address(
    gmail: Gmail, tmp_path: Path
) -> None:
    _, made = tools(tmp_path)
    body = "The menu is [here](https://cafe.example/menu?day=fri).\n\nSee you <b>then</b>."
    subject, result = await carded(
        made["email_send"], to=["sam@example.com"], subject="Friday", body=body
    )
    assert "The menu is here <https://cafe.example/menu?day=fri>." in subject.summary
    assert not result.is_error, result.content
    parts = parts_of(sent_message(gmail))
    assert "The menu is here (https://cafe.example/menu?day=fri)." in parts["text/plain"]
    html_part = parts["text/html"]
    assert '<a href="https://cafe.example/menu?day=fri">here</a>' in html_part
    assert "&lt;b&gt;then&lt;/b&gt;" in html_part and "<b>" not in html_part


async def test_a_body_without_links_is_plain_text(gmail: Gmail, tmp_path: Path) -> None:
    _, made = tools(tmp_path)
    await carded(
        made["email_send"], to=["sam@example.com"], subject="x", body="See https://a.example"
    )
    assert list(parts_of(sent_message(gmail))) == ["text/plain"]


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ("[click](javascript:alert%281%29)", "only https, http and mailto"),
        ("[photo](data:text/html,hi)", "only https, http and mailto"),
        ("[see](file:///C:/secret.txt)", "only https, http and mailto"),
        ("[paypal.com](https://paypal.evil.example/login)", "goes to paypal.evil.example"),
        ("[https://bank.example](https://bank.example.evil.example)", "goes to"),
    ],
)
async def test_a_dangerous_or_misleading_link_is_refused(
    gmail: Gmail, tmp_path: Path, body: str, why: str
) -> None:
    _, made = tools(tmp_path)
    arguments = {"to": ["sam@example.com"], "subject": "x", "body": body}
    assert await made["email_send"].subject(arguments) is None
    result = await made["email_send"].run(**arguments)
    assert result.is_error and why in result.content and gmail.sent == []


@pytest.mark.parametrize(
    "body",
    [
        "[example.com](https://www.example.com/x)",
        "[example.com](https://shop.example.com)",
        "[email me](mailto:me@gmail.com)",
    ],
)
async def test_link_words_naming_the_same_site_are_fine(
    gmail: Gmail, tmp_path: Path, body: str
) -> None:
    _, made = tools(tmp_path)
    _, result = await carded(made["email_send"], to=["sam@example.com"], subject="x", body=body)
    assert not result.is_error, result.content


async def test_a_forwarded_originals_brackets_are_never_links(gmail: Gmail, tmp_path: Path) -> None:
    original = gmail.add(mail(body="Win a prize: [claim](javascript:steal())"))
    _, made = tools(tmp_path)
    await carded(
        made["email_send"],
        forward=f"{original.msgid:x}",
        to=["alex@example.com"],
        body="Is [this](https://scam.example) a scam?",
    )
    html_part = parts_of(sent_message(gmail))["text/html"]
    assert '<a href="https://scam.example">this</a>' in html_part
    assert "javascript" in html_part and 'href="javascript' not in html_part


# -- R6.14a: a file downloaded to attach ---------------------------------------------------------


class Download:
    def __init__(
        self, body: bytes, *, status: int = 200, name: str = "", truncated: bool = False
    ) -> None:
        self.body, self.status, self.name, self.truncated = body, status, name, truncated
        self.asked: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, url: str, **kwargs: Any) -> Any:
        from atlas.web.client import Response

        self.asked.append((url, kwargs))
        headers = (
            (("content-disposition", f'attachment; filename="{self.name}"'),) if self.name else ()
        )
        return Response(
            url=url, status=self.status, headers=headers, body=self.body, truncated=self.truncated
        )


async def test_a_file_at_a_web_address_is_downloaded_hashed_and_attached(
    gmail: Gmail, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    download = Download(b"%PDF-1.4 the timetable", name="timetable.pdf")
    monkeypatch.setattr(gm, "get", download)
    _, made = tools(tmp_path)
    address = "https://school.example/files/12345"
    subject, result = await carded(
        made["email_send"],
        to=["sam@example.com"],
        subject="Timetable",
        body="Attached.",
        attachments=[address],
    )
    assert (
        "timetable.pdf - PDF, 22 B" in subject.summary
        and f"downloaded from {address}" in subject.summary
    )
    assert not result.is_error, result.content
    assert (
        len(download.asked) == 1 and download.asked[0][1]["allow_private"] is False
    )  # the operator's rules
    [part] = [p for p in sent_message(gmail).walk() if p.get_filename()]
    assert (
        part.get_filename() == "timetable.pdf"
        and part.get_payload(decode=True) == b"%PDF-1.4 the timetable"
    )


@pytest.mark.parametrize(
    ("download", "why"),
    [
        (Download(b"nope", status=404), "HTTP 404"),
        (Download(b"x" * 10, truncated=True), "over 25 MB"),
        (Download(b"token: sk-ant-api03-" + b"c" * 90, name="notes.txt"), "credential"),
    ],
)
async def test_a_download_that_should_not_go_is_refused(
    gmail: Gmail, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, download: Download, why: str
) -> None:
    monkeypatch.setattr(gm, "get", download)
    _, made = tools(tmp_path)
    result = await made["email_send"].run(
        to=["sam@example.com"], subject="x", body="y", attachments=["https://files.example/a"]
    )
    assert result.is_error and why in result.content and gmail.sent == []


async def test_a_private_address_is_refused_by_the_web_rules(
    gmail: Gmail, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atlas.errors import NetworkPolicyError

    async def refuse(url: str, **kwargs: Any) -> Any:
        raise NetworkPolicyError("192.168.1.1 is a private address")

    monkeypatch.setattr(gm, "get", refuse)
    _, made = tools(tmp_path)
    result = await made["email_send"].run(
        to=["sam@example.com"], subject="x", body="y", attachments=["http://192.168.1.1/router.cfg"]
    )
    assert result.is_error and "private address" in result.content and gmail.sent == []
