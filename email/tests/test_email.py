"""The email plugin against an in-memory IMAP server and a recording SMTP sender
(`docs/spec/email.md` §14). The aioimaplib wrapper's parsing is pinned on its own
against the lines the library hands back.

Run from a checkout of Atlas (`uv run pytest path/to/email/tests`).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from collections.abc import Awaitable, Callable
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from atlas.plugins import read_manifest
from atlas.tools.authority import run_started
from atlas.tools.profiles import ToolProfile

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("atlas_plugin_email", HERE.parent / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()

MARKER = "ATLAS_UNTRUSTED"


# -- a server ---------------------------------------------------------------------------


def mail(
    sender: str = "Billing <billing@example.net>",
    subject: str = "Invoice 2291 due Friday",
    body: str = "Hi, please pay invoice 2291.",
    to: str = "me@example.com",
    message_id: str = "<a1@example.net>",
    references: str = "",
    attach: tuple[str, bytes, str] | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = "Tue, 21 Oct 2026 10:13:58 +0100"
    message["Message-ID"] = message_id
    if references:
        message["References"] = references
        message["In-Reply-To"] = references.split()[-1]
    message.set_content(body)
    if attach:
        name, data, media_type = attach
        main, _, sub = media_type.partition("/")
        message.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return message.as_bytes()


class Box:
    def __init__(self, validity: int = 1714, flags: tuple[str, ...] = ()) -> None:
        self.validity = validity
        self.flags = flags
        self.messages: dict[int, tuple[set[str], bytes]] = {}
        self.next = 1

    def add(self, data: bytes, *flags: str) -> int:
        uid = self.next
        self.messages[uid] = (set(flags), data)
        self.next += 1
        return uid


class Server:
    """One account's mailboxes, and every command a client sent."""

    def __init__(self, *, idle: bool = True, move: bool = True) -> None:
        self.boxes: dict[str, Box] = {
            "INBOX": Box(),
            "Sent": Box(flags=("\\Sent",)),
            "Drafts": Box(flags=("\\Drafts",)),
            "Trash": Box(flags=("\\Trash",)),
            "Archive": Box(flags=("\\Archive",)),
        }
        self.capabilities = {"IDLE"} if idle else set()
        if move:
            self.capabilities.add("MOVE")
        self.log: list[tuple[Any, ...]] = []
        self.pushed = asyncio.Event()
        self.refuse_login = False
        self.opened = 0


class FakeImap:
    def __init__(self, server: Server) -> None:
        self.server = server
        self.box: Box | None = None

    async def open(self) -> FakeImap:
        if self.server.refuse_login:
            raise plugin.LoginRefused("refused")
        self.server.opened += 1
        return self

    def has(self, capability: str) -> bool:
        return capability in self.server.capabilities

    async def close(self) -> None:
        pass

    async def folders(self) -> list[Any]:
        return [plugin.Folder(name, box.flags) for name, box in self.server.boxes.items()]

    async def status(self, folder: str) -> tuple[int, int]:
        box = self.server.boxes[folder]
        unseen = sum(1 for flags, _ in box.messages.values() if "\\Seen" not in flags)
        return len(box.messages), unseen

    async def select(self, folder: str, *, write: bool = False) -> Any:
        if folder not in self.server.boxes:
            raise plugin.MailError(f"no folder {folder!r}")
        self.box = self.server.boxes[folder]
        self.server.log.append(("select", folder, write))
        return plugin.Selected(self.box.validity, self.box.next)

    async def search(self, criteria: list[str]) -> list[int]:
        assert self.box is not None
        self.server.log.append(("search", *criteria))
        found = []
        for uid, (flags, data) in self.box.messages.items():
            if self._matches(uid, flags, data, list(criteria)):
                found.append(uid)
        return found

    def _matches(self, uid: int, flags: set[str], data: bytes, criteria: list[str]) -> bool:
        message = plugin.parse_message(data)
        i = 0
        while i < len(criteria):
            word = criteria[i]
            if word == "ALL":
                i += 1
            elif word == "UNSEEN":
                if "\\Seen" in flags:
                    return False
                i += 1
            elif word == "UID":
                low = int(criteria[i + 1].split(":")[0])
                if uid < low:
                    return False
                i += 2
            elif word in ("FROM", "SUBJECT", "TEXT", "TO"):
                needle = json.loads(criteria[i + 1]).lower()
                hay = (
                    data.decode("utf-8", "replace")
                    if word == "TEXT"
                    else plugin.header(message, word.title())
                ).lower()
                if needle not in hay:
                    return False
                i += 2
            elif word == "HEADER":
                name, value = criteria[i + 1], json.loads(criteria[i + 2])
                if value not in plugin.header(message, name):
                    return False
                i += 3
            else:
                i += 1
        return True

    async def fetch(self, uids: list[int], parts: str) -> list[Any]:
        assert self.box is not None
        self.server.log.append(("fetch", tuple(uids), parts))
        out = []
        for uid in uids:
            if uid in self.box.messages:
                flags, data = self.box.messages[uid]
                out.append(
                    plugin.Fetched(
                        uid, tuple(sorted(flags)), len(data), "21-Oct-2026 09:14:00 +0000", data
                    )
                )
        return out

    async def append(self, folder: str, data: bytes, flags: str) -> None:
        self.server.log.append(("append", folder, flags))
        self.server.boxes[folder].add(data, *flags.strip("()").split())

    async def move(self, uids: list[int], folder: str) -> None:
        assert self.box is not None
        if "MOVE" not in self.server.capabilities:
            raise plugin.MailError("no MOVE")
        self.server.log.append(("move", tuple(uids), folder))
        for uid in uids:
            flags, data = self.box.messages.pop(uid)
            self.server.boxes[folder].add(data, *flags)

    async def store(self, uids: list[int], operation: str, flag: str) -> None:
        assert self.box is not None
        self.server.log.append(("store", tuple(uids), operation, flag))
        for uid in uids:
            flags = self.box.messages[uid][0]
            (flags.add if operation == "+FLAGS" else flags.discard)(flag)

    async def idle(self, timeout: float) -> None:
        self.server.log.append(("idle",))
        await self.server.pushed.wait()
        self.server.pushed.clear()


def connector(server: Server) -> Callable[[Any], Awaitable[FakeImap]]:
    async def connect(account: Any) -> FakeImap:
        return await FakeImap(server).open()

    return connect


class Outbox:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def __call__(self, account: Any, message: EmailMessage) -> None:
        self.sent.append(message)


ACCOUNT = {
    "address": "me@example.com",
    "imap_host": "imap.example.com",
    "smtp_host": "smtp.example.com",
    "watch": ["INBOX"],
}


def settings(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"accounts": {"work": dict(ACCOUNT)}, "on_new": "Summarise it."}
    base.update(overrides)
    return base


@pytest.fixture
def server() -> Server:
    s = Server()
    s.boxes["INBOX"].add(mail())
    return s


@pytest.fixture
def outbox() -> Outbox:
    return Outbox()


@pytest.fixture(autouse=True)
def password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_EMAIL_PASSWORD_WORK", "app-password-123")


def tool(server: Server, outbox: Outbox, tmp_path: Path, **kw: Any) -> Any:
    return plugin.EmailTool(
        workspace=tmp_path,
        settings=kw.pop("settings", None) or settings(),
        connector=connector(server),
        sender=outbox,
        **kw,
    )


async def as_(starter: str | None, call: Callable[[], Awaitable[Any]]) -> Any:
    """Run `call` on a task whose run was started by `starter`, as the lane does."""

    async def inside() -> Any:
        if starter is not None:
            run_started(starter)
        return await call()

    return await asyncio.create_task(inside())


# -- the manifest ----------------------------------------------------------------------


def test_the_manifest_declares_what_register_installs() -> None:
    manifest = read_manifest(HERE.parent / "PLUGIN.md", source="dir")
    assert manifest.name == "email"
    assert manifest.wakes is True
    assert manifest.contracts()["tools"] == ("email",)
    assert manifest.contracts()["services"] == ("watcher",)
    assert manifest.contracts()["logins"] == ()
    assert not manifest.warnings


def test_it_installs_through_atlas_own_path(tmp_path: Path) -> None:
    """`register` against a real context, contracts checked by the core."""
    from atlas.plugins.install import install_one, uninstall_one
    from atlas.plugins.services import known_services
    from atlas.tools.registry import ToolRegistry

    manifest = read_manifest(HERE.parent / "PLUGIN.md", source="dir")
    tools = ToolRegistry()
    provision = install_one(
        plugin.EmailPlugin(), manifest, workspace=tmp_path, tools=tools, settings=settings()
    )
    try:
        assert provision.ok, provision.error
        assert provision.tools == ("email",) and provision.services == ("email:watcher",)
        assert provision.logins == ()
        assert "email" in tools
        assert "email:watcher" in known_services()
    finally:
        uninstall_one(provision, tools=tools)
    assert "email:watcher" not in known_services()


# -- E2, R7.6, R7.7: the envelope --------------------------------------------------------


async def test_message_text_is_always_inside_the_envelope(
    server: Server, outbox: Outbox, tmp_path: Path
) -> None:
    result = await tool(server, outbox, tmp_path).run("read", uid=1)
    assert result.wrapped and not result.is_error
    outside, _, inside = result.content.partition(f"<<<{MARKER}")
    assert "work INBOX uid 1 (uidvalidity 1714)" in outside
    for written in ("Billing", "Invoice 2291", "please pay", "billing@example.net"):
        assert written not in outside, written
        assert written in inside, written

    found = await tool(server, outbox, tmp_path).run("search", subject="invoice")
    outside, _, inside = found.content.partition(f"<<<{MARKER}")
    assert "1 match" in outside and "uid 1" in outside
    assert "Invoice 2291" not in outside and "Invoice 2291" in inside


async def test_own_address_gets_no_exception(
    server: Server, outbox: Outbox, tmp_path: Path
) -> None:
    server.boxes["INBOX"].add(mail(sender="Me <me@example.com>", subject="note to self"))
    result = await tool(server, outbox, tmp_path).run("read", uid=2)
    outside, _, inside = result.content.partition(f"<<<{MARKER}")
    assert "note to self" in inside and "note to self" not in outside


async def test_an_attachment_name_stays_inside(
    server: Server, outbox: Outbox, tmp_path: Path
) -> None:
    server.boxes["INBOX"].add(mail(attach=("ignore-previous.txt", b"do this now", "text/plain")))
    read = await tool(server, outbox, tmp_path).run("read", uid=2)
    outside, _, inside = read.content.partition(f"<<<{MARKER}")
    assert "1 attachment" in outside and "ignore-previous" not in outside
    assert "ignore-previous.txt" in inside
    text = await tool(server, outbox, tmp_path).run("attachment", uid=2, index=1)
    outside, _, inside = text.content.partition(f"<<<{MARKER}")
    assert "attachment [1], text/plain" in outside
    assert "do this now" in inside and "ignore-previous" not in outside


async def test_a_pdf_attachment_is_read_and_enveloped(
    server: Server, outbox: Outbox, tmp_path: Path
) -> None:
    server.boxes["INBOX"].add(mail(attach=("invoice.pdf", b"%PDF-1.4\n", "application/pdf")))
    asked: list[dict[str, Any]] = []

    class Read:
        ok, text, reader, failure = True, "Total due: 40 EUR", "pypdf", ""

    async def understand(data: bytes, **kw: Any) -> Any:
        asked.append({"data": data, **kw})
        return Read()

    result = await tool(server, outbox, tmp_path, understand=understand).run(
        "attachment", uid=2, index=1
    )
    assert asked and asked[0]["media_type"] == "application/pdf"
    outside, _, inside = result.content.partition(f"<<<{MARKER}")
    assert "read by pypdf" in outside
    assert "Total due: 40 EUR" in inside and "invoice.pdf" in inside


async def test_nothing_is_written_to_the_workspace(
    server: Server, outbox: Outbox, tmp_path: Path
) -> None:
    server.boxes["INBOX"].add(mail(attach=("a.txt", b"x", "text/plain")))
    email = tool(server, outbox, tmp_path)
    await email.run("read", uid=2)
    await email.run("attachment", uid=2, index=1)
    await email.run("search")
    assert list(tmp_path.iterdir()) == []


# -- E3, R8.4, R8.4a: who may send -------------------------------------------------------


async def test_send_is_refused_in_a_woken_run_in_yolo(
    server: Server, outbox: Outbox, tmp_path: Path
) -> None:
    email = tool(server, outbox, tmp_path)
    arguments = {"action": "send", "to": ["a@x.com"], "subject": "hi", "body": "b"}
    # Refused before any card: nobody is asked about a send that will not run.
    assert await as_("system", lambda: asyncio.sleep(0, email.subject(arguments))) is None
    result = await as_("system", lambda: email.run(**arguments))
    assert result.is_error and "not started by a person" in result.content
    assert outbox.sent == []


async def test_send_is_refused_in_a_cron_run_and_a_sub_agent(
    server: Server, outbox: Outbox, tmp_path: Path
) -> None:
    email = tool(server, outbox, tmp_path)
    for starter in ("system", "sub", "hook", "untrusted", None):
        for action, extra in (
            ("send", {"to": ["a@x.com"]}),
            ("reply", {"uid": 1, "to": ["billing@example.net"]}),
            ("forward", {"uid": 1, "to": ["a@x.com"]}),
        ):
            result = await as_(starter, lambda a=action, e=extra: email.run(a, **e))
            assert result.is_error and "not started by a person" in result.content
    assert outbox.sent == []


async def test_draft_is_allowed_in_a_woken_run(
    server: Server, outbox: Outbox, tmp_path: Path
) -> None:
    email = tool(server, outbox, tmp_path)
    result = await as_(
        "system",
        lambda: email.run("draft", to=["billing@example.net"], body="Paid.", reply_to_uid=1),
    )
    assert not result.is_error and "Drafts" in result.content
    [(flags, data)] = server.boxes["Drafts"].messages.values()
    draft = plugin.parse_message(data)
    assert "\\Draft" in flags
    assert plugin.header(draft, "In-Reply-To") == "<a1@example.net>"
    assert plugin.header(draft, "Subject") == "Re: Invoice 2291 due Friday"
    assert outbox.sent == []


async def test_organizing_is_allowed_in_a_woken_run(
    server: Server, outbox: Outbox, tmp_path: Path
) -> None:
    second = server.boxes["INBOX"].add(mail(subject="two"))
    email = tool(server, outbox, tmp_path)
    marked = await as_("system", lambda: email.run("mark", uid=1, seen=True))
    assert not marked.is_error and "\\Seen" in server.boxes["INBOX"].messages[1][0]
    # Still decided by the mode: a card, reviewable.
    subject = await as_("system", lambda: asyncio.sleep(0, email.subject({"action": "move"})))
    assert subject is not None and subject.reviewable
    archived = await as_("system", lambda: email.run("archive", uid=second))
    assert not archived.is_error and len(server.boxes["Archive"].messages) == 1


async def test_reply_and_forward_are_refused_in_a_woken_run(
    server: Server, outbox: Outbox, tmp_path: Path
) -> None:
    email = tool(server, outbox, tmp_path)
    for action in ("reply", "forward"):
        result = await as_("system", lambda a=action: email.run(a, uid=1, to=["x@y.com"]))
        assert "not started by a person" in result.content
    assert outbox.sent == []


async def test_send_is_confirmed_in_a_persons_run(
    server: Server, outbox: Outbox, tmp_path: Path
) -> None:
    email = tool(server, outbox, tmp_path)
    arguments = {
        "action": "reply",
        "uid": 1,
        "to": ["billing@example.net"],
        "body": "Paid today.",
        "all": True,
    }
    subject = await as_("person", lambda: asyncio.sleep(0, email.subject(arguments)))
    # Every send is a person's yes, in every mode (`confirm`), never the reviewer's.
    assert subject is not None and subject.confirm and not subject.reviewable
    result = await as_("person", lambda: email.run(**arguments))
    assert not result.is_error, result.content
    [sent] = outbox.sent
    assert plugin.header(sent, "In-Reply-To") == "<a1@example.net>"
    assert plugin.header(sent, "Subject") == "Re: Invoice 2291 due Friday"
    assert "me@example.com" not in plugin.header(sent, "Cc"), "never cc ourselves on reply-all"
    # A copy is filed in Sent for a server that keeps none itself.
    assert len(server.boxes["Sent"].messages) == 1


async def test_send_card_names_every_recipient(
    server: Server, outbox: Outbox, tmp_path: Path
) -> None:
    email = tool(server, outbox, tmp_path)
    arguments = {
        "action": "send",
        "to": ["a@x.com", "b@y.com"],
        "cc": ["c@z.com"],
        "bcc": ["d@w.com"],
        "subject": "Re: invoice",
        "attach": ["report.pdf"],
    }
    subject = await as_("person", lambda: asyncio.sleep(0, email.subject(arguments)))
    for named in (
        "work",
        "a@x.com",
        "b@y.com",
        "c@z.com",
        "bcc d@w.com",
        "Re: invoice",
        "1 attachment",
    ):
        assert named in subject.summary, named


async def test_gmail_keeps_its_own_sent_copy(
    server: Server, outbox: Outbox, tmp_path: Path
) -> None:
    gmail = dict(ACCOUNT, smtp_host="smtp.gmail.com")
    email = tool(server, outbox, tmp_path, settings=settings(accounts={"work": gmail}))
    result = await as_("person", lambda: email.run("send", to=["a@x.com"], body="b"))
    assert not result.is_error and len(outbox.sent) == 1
    assert server.boxes["Sent"].messages == {}


async def test_a_failed_sent_copy_never_reports_the_send_as_failed(
    server: Server, outbox: Outbox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: Any, folder: str, data: bytes, flags: str) -> None:
        raise OSError("connection reset")

    monkeypatch.setattr(FakeImap, "append", broken)
    email = tool(server, outbox, tmp_path)
    result = await as_("person", lambda: email.run("send", to=["a@x.com"], body="b"))
    assert not result.is_error, "an error here is a model sending it twice"
    assert len(outbox.sent) == 1 and "the mail was sent" in result.content


# -- E4: nothing is destroyed ------------------------------------------------------------


async def test_trash_moves_and_never_expunges(
    server: Server, outbox: Outbox, tmp_path: Path
) -> None:
    result = await tool(server, outbox, tmp_path).run("trash", uid=1)
    assert not result.is_error
    assert 1 not in server.boxes["INBOX"].messages and len(server.boxes["Trash"].messages) == 1
    assert not any(entry[0] == "store" and "Deleted" in str(entry) for entry in server.log)
    assert "EXPUNGE" not in str(server.log).upper()


async def test_without_move_a_server_is_told_no_rather_than_expunged(
    outbox: Outbox, tmp_path: Path
) -> None:
    server = Server(move=False)
    server.boxes["INBOX"].add(mail())
    result = await tool(server, outbox, tmp_path).run("trash", uid=1)
    assert result.is_error and "MOVE" in result.content
    assert 1 in server.boxes["INBOX"].messages


# -- E5: owner lanes, and the owner's schedule ----------------------------------------------


def test_tool_is_absent_from_a_group_lane(tmp_path: Path) -> None:
    """`trusted_only` is the whole mechanism: the core removes such a tool from
    every lane no owner holds, and pins that in `tool-profiles.md` R3.8."""
    email = plugin.EmailTool(workspace=tmp_path, settings=settings())
    assert email.trusted_only is True and email.gated is True


def test_tool_is_offered_in_a_cron_run_and_no_other_lane_no_owner_holds(tmp_path: Path) -> None:
    """E5, R3.8a: `scheduled` lets a cron job have the tool; a group, a hook
    session and a stranger's DM still do not. Sending stays E3's."""
    email = plugin.EmailTool(workspace=tmp_path, settings=settings())
    assert email.scheduled is True
    assert ToolProfile(kind="cron", untrusted="a scheduled run").hidden("email", email) == ""
    for kind in ("group", "hook", "direct"):
        assert ToolProfile(kind=kind, untrusted="no owner").hidden("email", email), kind


# -- R6.1, R7.1: correctness against the server -------------------------------------------


async def test_uidvalidity_change_refuses_the_call(
    server: Server, outbox: Outbox, tmp_path: Path
) -> None:
    result = await tool(server, outbox, tmp_path).run("read", uid=1, uidvalidity=99)
    assert result.is_error and "renumbered" in result.content


def test_search_quotes_every_value() -> None:
    criteria = plugin.search_criteria(
        {"from": 'a" OR ALL "', "subject": "back\\slash", "since": "2026-10-01", "unseen": True}
    )
    assert criteria == [
        "FROM",
        '"a\\" OR ALL \\""',
        "SUBJECT",
        '"back\\\\slash"',
        "SINCE",
        "1-Oct-2026",
        "UNSEEN",
    ]
    with pytest.raises(plugin.MailError):
        plugin.search_criteria({"text": "x\r\nA1 DELETE INBOX"})
    assert plugin.search_criteria({}) == ["ALL"]


def test_a_fetch_is_parsed_whatever_order_the_server_answers_in() -> None:
    raw = mail()
    lines = [
        b"1 FETCH (UID 4412 FLAGS (\\Seen) RFC822.SIZE 1820 "
        b'INTERNALDATE "21-Oct-2026 09:14:00 +0000" BODY[] {120}',
        bytearray(raw),
        b")",
        b"2 FETCH (BODY[] {120}",
        bytearray(raw),
        b' UID 4413 FLAGS () INTERNALDATE "21-Oct-2026 10:00:00 +0000")',
        b"Success",
    ]
    first, second = plugin.parse_fetch(lines)
    assert (first.uid, first.flags, first.size) == (4412, ("\\Seen",), 1820)
    assert first.data == raw
    assert (second.uid, second.flags, second.data) == (4413, (), raw)


def test_folders_and_select_are_parsed() -> None:
    folders = plugin.parse_list(
        [b'(\\HasNoChildren \\Sent) "/" "Sent Items"', b'(\\HasNoChildren) "/" INBOX', b"Done"]
    )
    assert folders[0] == plugin.Folder("Sent Items", ("\\HasNoChildren", "\\Sent"))
    assert folders[1].name == "INBOX"
    selected = plugin.parse_select(
        [b"OK [UIDVALIDITY 1714] UIDs valid", b"OK [UIDNEXT 4414] Predicted next UID"]
    )
    assert selected == plugin.Selected(1714, 4414)
    assert plugin.parse_search([b"4411 4412 4413", b"Search completed"]) == [4411, 4412, 4413]


class Client:
    """Stands in for `aioimaplib.IMAP4_SSL`: the calls the wrapper makes, and
    the `Response(result, lines)` shapes the library answers with."""

    def __init__(self) -> None:
        from collections import namedtuple

        self.Response = namedtuple("Response", "result lines")
        self.calls: list[tuple[Any, ...]] = []
        # The library's own state, kept as it keeps it: SELECT moves it to
        # SELECTED, EXAMINE (a plain command there) does not, and a command
        # outside its states is refused before it is sent.
        self.protocol = type(
            "P", (), {"capabilities": {"IMAP4REV1", "MOVE", "IDLE"}, "state": "AUTH"}
        )()

    def has_capability(self, capability: str) -> bool:
        return capability in self.protocol.capabilities

    async def examine(self, mailbox: str) -> Any:
        self.calls.append(("examine", mailbox))
        return self.Response("OK", [b"OK [UIDVALIDITY 9] x", b"OK [UIDNEXT 12] y"])

    async def select(self, mailbox: str) -> Any:
        self.calls.append(("select", mailbox))
        self.protocol.state = "SELECTED"
        return self.Response("OK", [b"OK [UIDVALIDITY 9] x"])

    def _selected(self, command: str) -> None:
        import aioimaplib

        if self.protocol.state != "SELECTED":
            raise aioimaplib.Abort(f"command {command} illegal in state {self.protocol.state}")

    async def uid_search(self, *criteria: str) -> Any:
        self._selected("SEARCH")
        self.calls.append(("uid_search", *criteria))
        return self.Response("OK", [b"3 5 8", b"SEARCH completed"])

    async def uid(self, command: str, *args: str) -> Any:
        self._selected("UID")
        self.calls.append(("uid", command, *args))
        return self.Response("OK", [b"Done"])

    async def append(self, data: bytes, mailbox: str, flags: str) -> Any:
        self.calls.append(("append", mailbox, flags))
        return self.Response("OK", [b"APPEND completed"])


async def test_the_wrapper_speaks_aioimaplib(tmp_path: Path) -> None:
    account = plugin.load_accounts(settings())[0]["work"]
    imap = plugin.Imap(account)
    imap.client = Client()
    assert await imap.select("Sent Items") == plugin.Selected(9, 12)
    assert await imap.search(["UNSEEN"]) == [3, 5, 8]
    await imap.move([3, 5], "Archive")
    await imap.store([8], "+FLAGS", "\\Seen")
    await imap.append("Drafts", b"x", "(\\Draft)")
    assert imap.client.calls == [
        ("examine", '"Sent Items"'),
        ("uid_search", "UNSEEN"),
        ("uid", "move", "3,5", '"Archive"'),
        ("uid", "store", "8", "+FLAGS", "(\\Seen)"),
        ("append", '"Drafts"', "(\\Draft)"),
    ]


# -- E7, E8, R5: configuration -------------------------------------------------------------


async def test_password_is_never_in_a_result_or_record(
    server: Server, outbox: Outbox, tmp_path: Path
) -> None:
    email = tool(server, outbox, tmp_path)
    for action, extra in (("accounts", {}), ("read", {"uid": 1}), ("search", {}), ("folders", {})):
        result = await email.run(action, **extra)
        assert "app-password-123" not in result.content


async def test_without_an_account_the_tool_says_what_it_needs(
    server: Server, outbox: Outbox, tmp_path: Path
) -> None:
    email = tool(server, outbox, tmp_path, settings={"accounts": {}})
    result = await email.run("search")
    assert result.is_error and "plugins_settings.email.accounts" in result.content
    assert "secret" in result.content and server.opened == 0


def test_a_bad_account_is_refused_with_its_key() -> None:
    accounts, problems = plugin.load_accounts(
        {
            "accounts": {
                "Work Mail": dict(ACCOUNT),
                "plain": dict(ACCOUNT, smtp_port=25),
                "partial": {"address": "x@y.z"},
                "ok": dict(ACCOUNT),
            }
        }
    )
    assert list(accounts) == ["ok"]
    joined = " ".join(problems)
    assert "Work Mail" in joined and "no plaintext mode" in joined and "missing" in joined


async def test_several_accounts_need_a_name_or_a_default(
    server: Server, outbox: Outbox, tmp_path: Path
) -> None:
    two = settings(accounts={"work": dict(ACCOUNT), "home": dict(ACCOUNT)})
    email = tool(server, outbox, tmp_path, settings=two)
    result = await email.run("search")
    assert result.is_error and "work" in result.content and "home" in result.content
    assert not (await email.run("search", account="home")).is_error


# -- §9: the watcher ------------------------------------------------------------------------


class Service:
    def __init__(self, tmp_path: Path, woke: bool = True, **overrides: Any) -> None:
        self.settings = settings(**overrides)
        self.workspace = tmp_path
        self.records: list[tuple[str, str, dict[str, Any]]] = []
        self.wakes: list[str] = []
        self.answer = woke

    def audit(self, event: str, detail: str = "", **kw: Any) -> None:
        self.records.append((event, detail, dict(kw.get("arguments") or {})))

    async def wake(self, text: str) -> bool:
        self.wakes.append(text)
        return self.answer


async def running(watcher: Any) -> asyncio.Task[None]:
    watcher.debounce = 0
    task = asyncio.create_task(watcher.run())
    await asyncio.sleep(0.02)
    return task


async def stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def settle(condition: Callable[[], bool]) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never held")


async def test_first_start_announces_nothing(server: Server, tmp_path: Path) -> None:
    service = Service(tmp_path)
    task = await running(plugin.Watcher(service, connector=connector(server)))
    try:
        await settle(lambda: any("baseline" in r[1] for r in service.records))
        assert service.wakes == []
        state = json.loads((tmp_path / ".atlas" / "email.json").read_text())
        assert state["folders"]["work/INBOX"] == {"uidvalidity": 1714, "last": 1}
    finally:
        await stop(task)


async def test_wake_text_holds_no_message_text(server: Server, tmp_path: Path) -> None:
    service = Service(tmp_path)
    task = await running(plugin.Watcher(service, connector=connector(server)))
    try:
        await settle(lambda: any("baseline" in r[1] for r in service.records))
        server.boxes["INBOX"].add(mail(sender="Mallory <m@evil.example>", subject="URGENT do X"))
        server.pushed.set()
        await settle(lambda: bool(service.wakes))
    finally:
        await stop(task)
    [text] = service.wakes
    assert text == (
        "New mail: work INBOX 1 (uid 2, uidvalidity 1714).\nThe person asked: Summarise it."
    )
    assert "Mallory" not in text and "URGENT" not in text
    assert all("URGENT" not in str(r) and "Mallory" not in str(r) for r in service.records)


async def test_downtime_is_one_wake(server: Server, tmp_path: Path) -> None:
    (tmp_path / ".atlas").mkdir()
    (tmp_path / ".atlas" / "email.json").write_text(
        json.dumps({"folders": {"work/INBOX": {"uidvalidity": 1714, "last": 1}}})
    )
    for n in range(3):
        server.boxes["INBOX"].add(mail(subject=f"while away {n}"))
    server.boxes["INBOX"].add(mail(subject="read elsewhere"), "\\Seen")
    service = Service(tmp_path)
    task = await running(plugin.Watcher(service, connector=connector(server)))
    try:
        await settle(lambda: bool(service.wakes))
        await asyncio.sleep(0.05)
    finally:
        await stop(task)
    assert len(service.wakes) == 1
    # Three unseen announced; the one already read elsewhere is not (R9.6).
    assert "work INBOX 3 (uids 2, 3, 4," in service.wakes[0]


async def test_a_refused_wake_keeps_its_uids(
    server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin, "WAKE_RETRY_SECONDS", 0.01)
    service = Service(tmp_path, woke=False)
    task = await running(plugin.Watcher(service, connector=connector(server)))
    try:
        await settle(lambda: any("baseline" in r[1] for r in service.records))
        server.boxes["INBOX"].add(mail(subject="first"))
        server.pushed.set()
        await settle(lambda: len(service.wakes) >= 1)
        service.answer = True
        server.boxes["INBOX"].add(mail(subject="second"))
        server.pushed.set()
        await settle(lambda: len(service.wakes) >= 2)
    finally:
        await stop(task)
    assert "uids 2, 3" in service.wakes[-1], service.wakes


async def test_without_on_new_nothing_is_watched(server: Server, tmp_path: Path) -> None:
    service = Service(tmp_path, on_new="")
    await asyncio.wait_for(plugin.Watcher(service, connector=connector(server)).run(), 1)
    assert server.opened == 0 and service.wakes == []


async def test_a_refused_login_stops_that_watcher(server: Server, tmp_path: Path) -> None:
    server.refuse_login = True
    service = Service(tmp_path)
    watcher = plugin.Watcher(service, connector=connector(server))
    folder = asyncio.create_task(watcher.folder(watcher.accounts["work"], "INBOX"))
    await asyncio.wait_for(folder, 1)
    assert any("stopped" in r[1] for r in service.records)


def test_many_uids_are_a_count_and_a_range() -> None:
    text = plugin.wake_text({("work", "INBOX"): (7, list(range(1, 120)))}, "Go.")
    assert text.startswith("New mail: work INBOX 119 (uids 1-119, uidvalidity 7).")
    assert len(text) < 2000


# -- §6.1: the Gmail preset ------------------------------------------------------------------


GMAIL_ACCOUNT = {"provider": "gmail", "address": "me@gmail.com", "watch": ["INBOX"]}


class LoginClient(Client):
    """`aioimaplib.IMAP4_SSL` as far as a login goes, recording which one ran."""

    def __init__(self, *, accept: bool = True) -> None:
        super().__init__()
        self.accept = accept

    async def wait_hello_from_server(self) -> None:
        pass

    async def login(self, user: str, password: str) -> Any:
        self.calls.append(("login", user, password))
        return self.Response("OK" if self.accept else "NO", [b"LOGIN failed"])


def imap_library(monkeypatch: pytest.MonkeyPatch, *, accept: bool = True) -> None:
    import aioimaplib

    monkeypatch.setattr(aioimaplib, "IMAP4_SSL", lambda **_: LoginClient(accept=accept))


def test_a_provider_fills_the_hosts_and_a_typed_one_wins() -> None:
    accounts, problems = plugin.load_accounts(
        {
            "accounts": {
                "home": dict(GMAIL_ACCOUNT),
                "mine": dict(GMAIL_ACCOUNT, imap_host="imap.proxy.example"),
                "odd": {"provider": "yahoo", "address": "me@yahoo.com"},
                "gone": {"provider": "outlook", "address": "me@contoso.com"},
            }
        }
    )
    home = accounts["home"]
    assert (home.imap_host, home.smtp_host, home.smtp_port) == (
        "imap.gmail.com",
        "smtp.gmail.com",
        465,
    )
    assert accounts["mine"].imap_host == "imap.proxy.example"
    assert "odd" not in accounts and "gone" not in accounts
    assert sum("provider" in p for p in problems) == 2


async def test_a_provider_account_logs_in_with_its_app_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no OAuth sign-in: a Gmail account is a password
    account whose servers are filled in."""
    import aiosmtplib

    monkeypatch.setenv("ATLAS_EMAIL_PASSWORD_WORK", "app-password")
    imap_library(monkeypatch)
    account = plugin.load_accounts(settings(accounts={"work": dict(GMAIL_ACCOUNT)}))[0]["work"]
    imap = await plugin.Imap(account).open()
    assert imap.client.calls == [("login", "me@gmail.com", "app-password")]

    sent: dict[str, Any] = {}

    async def send(message: Any, **kw: Any) -> None:
        sent.update(kw)

    monkeypatch.setattr(aiosmtplib, "send", send)
    await plugin.smtp_send(account, plugin.compose(account, to=["a@x.com"], body="hi"))
    assert sent["username"] == "me@gmail.com" and sent["password"] == "app-password"
    assert "oauth_token_generator" not in sent and sent["hostname"] == "smtp.gmail.com"


async def test_a_refused_provider_login_names_the_app_password(
    monkeypatch: pytest.MonkeyPatch, outbox: Outbox, tmp_path: Path
) -> None:
    monkeypatch.setenv("ATLAS_EMAIL_PASSWORD_WORK", "wrong")
    imap_library(monkeypatch, accept=False)

    async def opened(account: Any) -> Any:
        return await plugin.Imap(account).open()

    email = plugin.EmailTool(
        workspace=tmp_path,
        settings=settings(accounts={"work": dict(GMAIL_ACCOUNT)}),
        connector=opened,
        sender=outbox,
    )
    result = await email.run("search")
    assert result.is_error and "Gmail app password" in result.content
    assert "ATLAS_EMAIL_PASSWORD_WORK" in result.content and "wrong" not in result.content


def test_the_form_describes_every_account_key() -> None:
    """The settings page draws accounts from the manifest (Atlas
    `plugin-sdk.md` section 7.5.1): every key `load_accounts` reads is a field,
    and every account keeps its password where the plugin reads it."""
    import re

    manifest = read_manifest(HERE.parent / "PLUGIN.md", source="dir")
    accounts = manifest.config_schema["accounts"]
    source = (HERE.parent / "plugin.py").read_text(encoding="utf-8")
    start = source.index("def load_accounts")
    body = source[start : source.index("\ndef ", start + 1)]
    read = set(re.findall(r'entry(?:\.get\(|\[)"([a-z_]+)"', body))
    assert read == {f.key for f in accounts.entries}
    assert not accounts.login_by and not manifest.logins
    assert set(accounts.entries[0].choices) == {"", *plugin.PROVIDERS}
    assert accounts.secret_for("work") == plugin.PASSWORD_VARIABLE.format(account="WORK")


async def test_a_password_stored_after_start_is_read_without_a_restart(
    server: Server, outbox: Outbox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5.2: the `secret` tool and the settings page write `.env` while the
    gateway runs and never touch the process's environment, so the password
    is read from the file at each connection. Reading only `os.environ`, a
    watcher said "no password" until the gateway restarted."""
    monkeypatch.delenv("ATLAS_EMAIL_PASSWORD_HOME", raising=False)
    email = tool(server, outbox, tmp_path, settings=settings(accounts={"home": dict(ACCOUNT)}))
    account = email.accounts["home"]
    assert account.password() == ""
    assert "no password (ATLAS_EMAIL_PASSWORD_HOME)" in (await email.run("accounts")).content

    (tmp_path / ".atlas").mkdir(exist_ok=True)
    (tmp_path / ".atlas" / ".env").write_text("ATLAS_EMAIL_PASSWORD_HOME=stored-later\n")

    assert account.password() == "stored-later"
    listed = (await email.run("accounts")).content
    assert "no password" not in listed and "stored-later" not in listed
    watcher = plugin.Watcher(Service(tmp_path, accounts={"home": dict(ACCOUNT)}))
    assert watcher.accounts["home"].password() == "stored-later"
    # The process still wins over the file, as for every credential.
    monkeypatch.setenv("ATLAS_EMAIL_PASSWORD_HOME", "from-the-shell")
    assert account.password() == "from-the-shell"


async def test_a_folder_opened_to_read_is_searchable() -> None:
    """The library enters its selected state on SELECT only; the plugin opens a
    folder to read with EXAMINE, so it says the folder is open itself. Without
    that every search failed with "command SEARCH illegal in state AUTH",
    before anything reached the server, and so did the new-mail watcher."""
    account = plugin.load_accounts(settings())[0]["work"]
    imap = plugin.Imap(account)
    imap.client = Client()
    assert imap.client.protocol.state == "AUTH"
    await imap.select("INBOX")
    assert imap.client.calls == [("examine", '"INBOX"')]  # read-only, still
    assert imap.client.protocol.state == "SELECTED"
    assert await imap.search(["UNSEEN"]) == [3, 5, 8]


class PageContext:
    """The parts of a plugin context the settings page's method reads."""

    def __init__(self, current: dict[str, Any]) -> None:
        self.settings: dict[str, Any] = {}  # what the session installed with: stale
        self.current = current

    def current_settings(self) -> dict[str, Any]:
        return self.current


async def test_folders_answers_for_a_saved_account_from_the_file(
    server: Server, tmp_path: Path
) -> None:
    """`email.folders` (the watch picker): the account comes from the config as
    it is now, so one saved a moment ago answers - and a server the page names
    is never used, so a stored password cannot be pointed somewhere else."""
    # A folder that holds folders and no mail is nothing to watch.
    server.boxes["[Gmail]"] = Box(flags=("\\Noselect", "\\HasChildren"))
    seen: list[str] = []

    async def connect(account: Any) -> FakeImap:
        seen.append(account.imap_host)
        return await FakeImap(server).open()

    handler = plugin.folders_method(PageContext(settings()), connector=connect)
    answer = await handler({"entry": "work", "imap_host": "evil.example"}, tmp_path)
    assert answer["options"] == ["INBOX", "Sent", "Drafts", "Trash", "Archive"]
    assert seen == ["imap.example.com"]
    with pytest.raises(plugin.MailError, match="no saved account 'nobody'"):
        await handler({"entry": "nobody"}, tmp_path)


def test_the_manifest_declares_the_folders_method() -> None:
    manifest = read_manifest(HERE.parent / "PLUGIN.md", source="dir")
    assert manifest.contracts()["methods"] == ("email.folders",)
    watch = next(f for f in manifest.config_schema["accounts"].entries if f.key == "watch")
    assert watch.options_from == "folders"
