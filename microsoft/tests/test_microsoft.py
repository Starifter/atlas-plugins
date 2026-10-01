"""The Microsoft plugin (`docs/spec/microsoft.md`), driven the way Atlas drives it with
Microsoft replaced: `request`, `grant` and `connections` are the plugin's module-level
names, and a fake answers Microsoft Graph's shapes. Sends are parsed back out of the
base64 MIME Graph would have been given.

The module is loaded as Atlas loads a plugin - never put in `sys.modules`.

Run from a checkout of Atlas (`uv run --project ../Atlas pytest microsoft/tests`).
"""

from __future__ import annotations

import base64
import email
import email.policy
import email.utils
import importlib.util
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
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
        "atlas_plugin_microsoft", HERE.parent / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ms = _load()

TZ = ZoneInfo("Australia/Sydney")
ME = "me@contoso.com"
OUTLOOK = "microsoft:outlook"


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def utc(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat() + ".0000000"


def mime(
    sender: str = "Sam Lee <sam@example.com>",
    subject: str = "Dinner Friday?",
    body: str = "Are you free Friday at 7?",
    *,
    to: str = ME,
    headers: dict[str, str] | None = None,
    files: tuple[tuple[str, bytes, str], ...] = (),
) -> bytes:
    message = EmailMessage(policy=email.policy.SMTP)
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = "Thu, 01 Oct 2026 18:02:00 +1000"
    message["Message-ID"] = f"<{abs(hash((sender, subject, body)))}@example.com>"
    for name, value in (headers or {}).items():
        del message[name]
        message[name] = value
    message.set_content(body)
    for name, data, kind in files:
        main, _, sub = kind.partition("/")
        message.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return message.as_bytes()


class Call(SimpleNamespace):
    method: str
    path: str
    query: dict[str, str]
    body: Any
    data: bytes | None
    headers: dict[str, str]


class Fake:
    """Microsoft Graph, as far as these tests need it."""

    def __init__(self) -> None:
        self.accounts: tuple[str, ...] = (OUTLOOK,)
        self.calls: list[Call] = []
        self.messages: dict[str, dict[str, Any]] = {}
        self.events: dict[str, dict[str, Any]] = {}
        self.sent: list[bytes] = []
        self.folders = [
            {"id": "inbox-id", "displayName": "Inbox", "unreadItemCount": 2},
            {"id": "archive-id", "displayName": "Archive"},
            {"id": "receipts-id", "displayName": "Receipts"},
        ]
        self.routes: list[tuple[str, str, Callable[[Call], tuple[int, Any]]]] = []
        self.count = 0

    def on(self, method: str, pattern: str, answer: Callable[[Call], tuple[int, Any]]) -> None:
        self.routes.insert(0, (method, pattern, answer))

    def add(self, raw: bytes, **meta: Any) -> str:
        self.count += 1
        graph_id = meta.pop("id", f"AAMk-{self.count:04d}-{'x' * 120}")
        parsed = email.message_from_bytes(raw, policy=email.policy.default)
        name, address = email.utils.parseaddr(str(parsed["From"]))
        self.messages[graph_id] = {
            "raw": raw,
            "folder": meta.pop("folder", "inbox"),
            "meta": {
                "id": graph_id,
                "conversationId": meta.pop("conversationId", f"conv-{self.count}"),
                "subject": str(parsed["Subject"]),
                "from": {"emailAddress": {"name": name, "address": address}},
                "receivedDateTime": meta.pop("received", f"2026-10-01T0{self.count % 10}:00:00Z"),
                "isRead": meta.pop("isRead", False),
                "flag": {"flagStatus": "notFlagged"},
                "importance": "normal",
                "hasAttachments": False,
                "categories": [],
                "isDraft": False,
                "changeKey": "k1",
                "inferenceClassification": meta.pop("focus", "focused"),
                **meta,
            },
        }
        return graph_id

    def event(
        self, start: datetime, minutes: int = 30, title: str = "Dentist", **extra: Any
    ) -> str:
        self.count += 1
        graph_id = extra.pop("id", f"AAMkEv-{self.count:04d}-{'y' * 100}")
        self.events[graph_id] = {
            "id": graph_id,
            "subject": title,
            "start": {"dateTime": utc(start), "timeZone": "UTC"},
            "end": {"dateTime": utc(start + timedelta(minutes=minutes)), "timeZone": "UTC"},
            "isAllDay": False,
            "isOrganizer": True,
            "attendees": [],
            "showAs": "busy",
            "responseStatus": {"response": "organizer"},
            "type": "singleInstance",
            "changeKey": "c1",
            "organizer": {"emailAddress": {"name": "Me", "address": ME}},
            **extra,
        }
        return graph_id

    def listing(self, call: Call, folder: str | None) -> tuple[int, Any]:
        found = [m for m in self.messages.values() if folder is None or m["folder"] == folder]
        wanted = call.query.get("$search", "").strip('"').lower()
        if wanted:

            def hit(m: dict[str, Any]) -> bool:
                text = m["raw"].decode("utf-8", "replace").lower()
                return all(w.split(":")[-1] in text for w in wanted.split())

            found = [m for m in found if hit(m)]
        condition = call.query.get("$filter", "")
        match = re.match(r"conversationId eq '(.*)'", condition)
        if match:
            found = [m for m in found if m["meta"]["conversationId"] == match.group(1)]
        match = re.match(r"receivedDateTime ge (\S+) and isRead eq false", condition)
        if match:
            found = [
                m
                for m in found
                if m["meta"]["receivedDateTime"] >= match.group(1) and not m["meta"]["isRead"]
            ]
        found.sort(
            key=lambda m: m["meta"]["receivedDateTime"],
            reverse="desc" in call.query.get("$orderby", "desc"),
        )
        top = int(call.query.get("$top", "50"))
        return 200, {"value": [m["meta"] for m in found[:top]]}

    def default(self, call: Call) -> tuple[int, Any]:
        path, method = call.path, call.method
        if path == "/me":
            return 200, {"mail": ME, "userPrincipalName": ME}
        if path == "/me/messages" and method == "GET":
            return self.listing(call, None)
        if path == "/me/messages" and method == "POST":
            assert call.headers.get("Content-Type", call.content_type) or True
            raw = base64.b64decode(call.data or b"")
            graph_id = self.add(raw, folder="drafts", isDraft=True, isRead=True)
            return 201, self.messages[graph_id]["meta"]
        if path == "/me/sendMail":
            self.sent.append(base64.b64decode(call.data or b""))
            return 202, None
        match = re.fullmatch(r"/me/mailFolders/([^/]+)/messages", path)
        if match:
            given = match.group(1)
            folder = {"inbox-id": "inbox", "archive-id": "archive", "receipts-id": "receipts"}.get(
                given, given
            )
            return self.listing(call, folder)
        if path == "/me/mailFolders":
            return 200, {"value": self.folders}
        if path == "/me/outlook/masterCategories":
            return 200, {"value": [{"displayName": "Family"}, {"displayName": "Red"}]}
        match = re.fullmatch(r"/me/messages/([^/]+)(/\$value|/move|/send)?", path)
        if match:
            graph_id, tail = match.group(1), match.group(2) or ""
            stored = self.messages.get(graph_id)
            if stored is None:
                return 404, {"error": {"code": "ErrorItemNotFound", "message": "Not found"}}
            if tail == "/$value":
                return 200, stored["raw"]
            if tail == "/move":
                stored["folder"] = {
                    "archive": "archive",
                    "inbox": "inbox",
                    "deleteditems": "deleteditems",
                    "receipts-id": "receipts",
                }[call.body["destinationId"]]
                return 201, stored["meta"]
            if tail == "/send":
                self.sent.append(stored["raw"])
                stored["folder"] = "sentitems"
                return 202, None
            if method == "PATCH":
                stored["meta"].update(call.body)
                return 200, stored["meta"]
            return 200, stored["meta"]
        if path == "/me/calendars":
            return 200, {
                "value": [
                    {"id": "cal-1", "name": "Calendar", "isDefaultCalendar": True, "canEdit": True},
                    {"id": "cal-2", "name": "Birthdays", "canEdit": False},
                ]
            }
        if re.fullmatch(r"/me/calendars/[^/]+/calendarView", path):
            items = sorted(self.events.values(), key=lambda e: e["start"]["dateTime"])
            return 200, {"value": items}
        if re.fullmatch(r"/me/calendars/[^/]+/events", path) and method == "POST":
            graph_id = self.event(at(1, 0), title=call.body["subject"])
            self.events[graph_id].update(call.body)
            return 201, self.events[graph_id]
        match = re.fullmatch(r"/me/events/([^/]+)(/[a-zA-Z]+)?", path)
        if match:
            stored = self.events.get(match.group(1))
            if stored is None:
                return 404, {"error": {"message": "Not found"}}
            if method == "PATCH":
                stored.update(call.body)
                stored["changeKey"] = stored["changeKey"] + "+"
                return 200, stored
            if method == "DELETE":
                del self.events[match.group(1)]
                return 204, None
            if method == "POST":
                stored["did"] = match.group(2)
                return 202, None
            return 200, stored
        return 404, {"error": {"message": f"no route for {method} {path}"}}

    async def request(self, method: str, url: str, **kwargs: Any) -> Response:
        parts = urlsplit(url)
        call = Call(
            method=method,
            path=parts.path.removeprefix("/v1.0"),
            query=dict(parse_qsl(parts.query)),
            body=kwargs.get("json"),
            data=kwargs.get("data"),
            content_type=kwargs.get("content_type", ""),
            headers=dict(kwargs.get("headers") or {}),
        )
        self.calls.append(call)
        answer: Callable[[Call], tuple[int, Any]] = self.default
        for verb, pattern, handler in self.routes:
            if verb == method and re.fullmatch(pattern, call.path):
                answer = handler
                break
        status, body = answer(call)
        raw = (
            body if isinstance(body, bytes) else b"" if body is None else json.dumps(body).encode()
        )
        return Response(
            url=url, status=status, headers=(("retry-after", "0"),), body=raw, truncated=False
        )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    made = Fake()
    monkeypatch.setattr(ms, "request", made.request)
    monkeypatch.setattr(ms, "connections", lambda plugin, workspace=None: made.accounts)
    monkeypatch.setattr(
        ms, "grant", lambda account, workspace=None: SimpleNamespace(bearer=lambda: "token-abc")
    )
    monkeypatch.setattr(ms, "local", lambda: TZ)
    return made


class Media:
    def __init__(self) -> None:
        self.put_calls: list[tuple[bytes, str]] = []

    def put(self, data: bytes, *, source: str) -> Any:
        self.put_calls.append((data, source))
        return SimpleNamespace(source=source)

    def get(self, block: Any) -> bytes | None:
        return None


class Context:
    def __init__(self, workspace: Path, **settings: Any) -> None:
        self.workspace = workspace
        self.settings = settings
        self.media = Media()
        self.web_policy: Any = SimpleNamespace(allow_private=False, hosts=None, max_redirects=5)
        self.audited: list[Any] = []

    def session_media(self) -> tuple[Any, ...]:
        return ()

    def audit(self, event: str, detail: str = "", **kwargs: Any) -> None:
        self.audited.append((event, kwargs.get("arguments")))


def tools(tmp_path: Path, **settings: Any) -> dict[str, Any]:
    outlook = ms.Outlook(Context(tmp_path, **settings))
    return {cls.name: cls(outlook) for cls in (*ms.MAIL_TOOLS, *ms.CALENDAR_TOOLS)}


async def carded(tool: Any, **arguments: Any) -> tuple[Any, Any]:
    subject = await tool.subject(arguments)
    return subject, await tool.run(**arguments)


def handle_of(text: str, index: int = 0) -> str:
    return re.findall(r"\[(?:draft )?id: ([me]\d+)", text)[index]


def sent(fake: Fake, index: int = -1) -> Any:
    return email.message_from_bytes(fake.sent[index], policy=email.policy.default)


# -- §4: signing in ----------------------------------------------------------------------------


async def test_nothing_is_reached_until_someone_signs_in(fake: Fake, tmp_path: Path) -> None:
    fake.accounts = ()
    for name, tool in tools(tmp_path).items():
        if name.endswith(
            ("_read", "_attachment", "_get_event", "_respond", "_update_event", "_delete_event")
        ):
            continue
        result = await tool.run(
            **(
                {"title": "x", "start": "2026-10-02T10:00"}
                if "create" in name
                else {"action": "read", "query": "x"}
                if "organise" in name
                else {"to": ["a@b.com"], "body": "x"}
                if name.endswith(("_send", "_draft"))
                else {}
            )
        )
        assert result.is_error, name
    assert fake.calls == []
    result = await tools(tmp_path)["outlook_mail_search"].run()
    assert "atlas auth login microsoft:outlook" in result.content


def test_the_login_asks_for_what_it_needs_from_the_tenant_set() -> None:
    client = ms.client("client-123", "consumers")
    assert (
        client.authorize_url == "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
    )
    assert client.token_url == "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
    assert {"offline_access", "Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite"} <= set(
        client.scopes
    )
    assert not any("delete" in s.lower() or s.endswith(".All") for s in client.scopes)
    assert client.authorize_params == {"prompt": "select_account"}
    assert ms.client("x", "../evil").authorize_url.startswith(
        "https://login.microsoftonline.com/common/"
    )


def test_without_a_client_id_signing_in_says_how_to_make_one() -> None:
    from atlas.sdk.runtime import CredentialError

    with pytest.raises(CredentialError) as raised:
        ms.no_client(SimpleNamespace())
    assert "entra.microsoft.com" in str(raised.value) and "http://localhost/callback" in str(
        raised.value
    )


def test_the_label_is_the_address_in_the_id_token() -> None:
    claims = (
        base64.urlsafe_b64encode(json.dumps({"preferred_username": ME}).encode())
        .decode()
        .rstrip("=")
    )
    assert ms._label({"id_token": f"h.{claims}.s"}) == {"email": ME, "label": ME}
    assert ms._label({"id_token": "garbage"}) == {}


def test_every_tool_is_owner_only_and_untrusted(tmp_path: Path) -> None:
    made = tools(tmp_path)
    assert len(made) == 15
    for tool in made.values():
        assert tool.trusted_only and tool.untrusted, tool.name
        assert tool.name.startswith("outlook_")
    gated = {n for n, t in made.items() if getattr(t, "gated", False)}
    assert gated == {
        "outlook_mail_draft",
        "outlook_mail_send",
        "outlook_mail_organise",
        "outlook_calendar_create_event",
        "outlook_calendar_update_event",
        "outlook_calendar_delete_event",
        "outlook_calendar_respond",
    }


async def test_a_write_with_two_accounts_says_which(fake: Fake, tmp_path: Path) -> None:
    fake.accounts = (OUTLOOK, "microsoft:work")
    result = await tools(tmp_path)["outlook_calendar_create_event"].run(
        title="x", start="2026-10-02T10:00"
    )
    assert result.is_error and "say which with account: outlook, work" in result.content


# -- §5, §6.1: reading mail --------------------------------------------------------------------


async def test_search_shows_handles_and_asks_graph_the_right_way(
    fake: Fake, tmp_path: Path
) -> None:
    sam = fake.add(mime(), importance="high")
    fake.add(mime("Shop <news@shop.example>", "Big sale"), isRead=True)
    made = tools(tmp_path)

    result = await made["outlook_mail_search"].run(query="from:sam")
    assert not result.is_error, result.content
    assert result.content.splitlines()[0] == "1 message(s) for 'from:sam', newest first:"
    assert (
        "Sam Lee <sam@example.com> · Dinner Friday? · unread, important  [id: m1]" in result.content
    )
    call = fake.calls[-1]
    assert call.path == "/me/mailFolders/inbox/messages" and call.query["$search"] == '"from:sam"'
    assert call.headers["Authorization"] == "Bearer token-abc"
    assert 'IdType="ImmutableId"' in call.headers["Prefer"]
    assert sam not in result.content  # Graph's long id is never shown

    everything = await made["outlook_mail_search"].run(folder="anywhere")
    assert "Big sale" in everything.content and fake.calls[-1].path == "/me/messages"
    assert fake.calls[-1].query["$orderby"] == "receivedDateTime desc"
    assert "[id: m1]" in everything.content  # the same message keeps its handle


async def test_an_unknown_handle_says_search_again(fake: Fake, tmp_path: Path) -> None:
    result = await tools(tmp_path)["outlook_mail_read"].run(message_id="m7")
    assert result.is_error and "search again" in result.content
    event = await tools(tmp_path)["outlook_calendar_get_event"].run(event_id="m1")
    assert event.is_error and "no event" in event.content


async def test_errors_are_said_in_words(fake: Fake, tmp_path: Path) -> None:
    fake.on(
        "GET",
        "/me/mailFolders/inbox/messages",
        lambda call: (403, {"error": {"message": "Access is denied."}}),
    )
    result = await tools(tmp_path)["outlook_mail_search"].run()
    assert result.is_error and "organisation may not allow it" in result.content
    fake.on("GET", "/me/mailFolders/inbox/messages", lambda call: (401, {}))
    again = await tools(tmp_path)["outlook_mail_search"].run()
    assert "atlas auth login microsoft:outlook" in again.content


async def test_reading_is_mime_and_an_unverified_sender_is_said_first(
    fake: Fake, tmp_path: Path
) -> None:
    fake.add(
        mime(
            "Bank <security@bank.example>",
            "Verify your account",
            "Click here.\n\nOn Wed, 30 Sep 2026, Me wrote:\n> old",
            headers={"Authentication-Results": "spf=fail smtp.mailfrom=bank.example"},
        )
    )
    made = tools(tmp_path)
    handle = handle_of((await made["outlook_mail_search"].run()).content)
    text = (await made["outlook_mail_read"].run(message_id=handle)).content
    assert text.index("(Outlook could not verify that this came from bank.example)") < text.index(
        "Click here."
    )
    assert "[... 2 quoted line(s) folded]" in text and "> old" not in text


async def test_a_conversation_is_read_whole(fake: Fake, tmp_path: Path) -> None:
    fake.add(mime(body="Are you free?"), conversationId="c-1", received="2026-10-01T01:00:00Z")
    fake.add(
        mime(ME, "Re: Dinner Friday?", "Yes!", to="sam@example.com"),
        conversationId="c-1",
        received="2026-10-01T02:00:00Z",
        folder="sentitems",
    )
    fake.add(mime("Other <o@example.com>", "Unrelated", "Nothing here."), conversationId="c-2")
    made = tools(tmp_path)
    handle = handle_of((await made["outlook_mail_search"].run(query="free")).content)
    text = (await made["outlook_mail_read"].run(message_id=handle, conversation=True)).content
    assert text.index("Are you free?") < text.index("Yes!") and "Unrelated" not in text


async def test_an_attachment_is_a_picture_or_a_file(fake: Fake, tmp_path: Path) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
    fake.add(
        mime(
            files=(("menu.pdf", b"%PDF-1.4 menu", "application/pdf"), ("map.png", png, "image/png"))
        )
    )
    made = tools(tmp_path)
    handle = handle_of((await made["outlook_mail_search"].run()).content)
    saved = await made["outlook_mail_attachment"].run(message_id=handle, attachment="1")
    assert "read_media" in saved.content
    [path] = list((tmp_path / "email-attachments").rglob("menu.pdf"))
    assert path.read_bytes() == b"%PDF-1.4 menu"
    picture = await made["outlook_mail_attachment"].run(message_id=handle, attachment="map.png")
    assert picture.images


# -- §6.2: writing mail ------------------------------------------------------------------------


async def test_a_send_is_a_confirm_card_and_goes_as_mime(fake: Fake, tmp_path: Path) -> None:
    made = tools(tmp_path)
    subject, result = await carded(
        made["outlook_mail_send"],
        to=["sam@example.com"],
        bcc=["boss@example.com"],
        subject="Friday",
        body="Friday works - see [the menu](https://cafe.example/menu).",
    )
    assert subject.confirm is True
    assert subject.summary.splitlines()[:6] == [
        f"Send from {ME}",
        "To: sam@example.com",
        "Bcc: boss@example.com",
        "Subject: Friday",
        "",
        "Friday works - see the menu <https://cafe.example/menu>.",
    ]
    assert not result.is_error, result.content
    call = next(c for c in fake.calls if c.path == "/me/sendMail")
    assert call.content_type == "text/plain"
    message = sent(fake)
    assert message["Bcc"] == "boss@example.com"  # Graph reads recipients from the headers
    assert message["From"] == ME
    html_part = message.get_body(preferencelist=("html",)).get_content()
    assert '<a href="https://cafe.example/menu">the menu</a>' in html_part

    nothing = await made["outlook_mail_send"].run(to=["sam@example.com"], subject="x", body="y")
    assert (
        nothing.is_error and "shown to the person first" in nothing.content and len(fake.sent) == 1
    )


async def test_a_reply_threads_and_goes_where_the_original_says(fake: Fake, tmp_path: Path) -> None:
    fake.add(
        mime(headers={"Reply-To": "Sam <sam.home@example.com>", "Cc": f"alex@example.com, {ME}"})
    )
    made = tools(tmp_path)
    handle = handle_of((await made["outlook_mail_search"].run()).content)
    subject, _ = await carded(
        made["outlook_mail_send"], reply_to=handle, reply_all=True, body="Friday works."
    )
    assert (
        "To: sam.home@example.com" in subject.summary
        and "Cc: alex@example.com\n" in subject.summary + "\n"
    )
    message = sent(fake)
    assert message["Subject"] == "Re: Dinner Friday?" and message["In-Reply-To"]
    assert "Are you free" not in message.get_content()


async def test_a_draft_is_free_and_sending_it_checks_it_did_not_change(
    fake: Fake, tmp_path: Path
) -> None:
    made = tools(tmp_path)
    arguments = {"to": ["sam@example.com"], "subject": "Plans", "body": "Draft text."}
    assert await made["outlook_mail_draft"].subject(arguments) is None
    saved = await made["outlook_mail_draft"].run(**arguments)
    draft = handle_of(saved.content)
    [stored] = [m for m in fake.messages.values() if m["folder"] == "drafts"]

    subject = await made["outlook_mail_send"].subject({"draft_id": draft})
    assert "Draft text." in subject.summary and "(sending a saved draft)" in subject.summary
    stored["meta"]["changeKey"] = "k2"  # edited in Outlook after the card
    changed = await made["outlook_mail_send"].run(draft_id=draft)
    assert changed.is_error and "changed since it was shown" in changed.content and fake.sent == []

    _, ok = await carded(made["outlook_mail_send"], draft_id=draft)
    assert not ok.is_error, ok.content
    assert stored["folder"] == "sentitems" and len(fake.sent) == 1


async def test_attachments_over_three_megabytes_are_refused_before_a_card(
    fake: Fake, tmp_path: Path
) -> None:
    (tmp_path / "big.pdf").write_bytes(b"%PDF-" + b"0" * (3 * 1024 * 1024))
    made = tools(tmp_path)
    arguments = {"to": ["sam@example.com"], "subject": "x", "body": "y", "attachments": ["big.pdf"]}
    assert await made["outlook_mail_send"].subject(arguments) is None
    result = await made["outlook_mail_send"].run(**arguments)
    assert result.is_error and "at most 3 MB" in result.content and fake.sent == []


@pytest.mark.parametrize(
    ("path", "why"),
    [
        (".env", "hidden file"),
        ("../outside.txt", "outside the workspace"),
        ("keys/id_rsa", "key material"),
    ],
)
async def test_what_may_not_leave_is_refused(
    fake: Fake, tmp_path: Path, path: str, why: str
) -> None:
    workspace = tmp_path / "ws"
    (workspace / "keys").mkdir(parents=True)
    (workspace / ".env").write_text("X=1")
    (workspace / "keys" / "id_rsa").write_text("-")
    (tmp_path / "outside.txt").write_text("x")
    result = await tools(workspace)["outlook_mail_send"].run(
        to=["sam@example.com"], subject="x", body="y", attachments=[path]
    )
    assert result.is_error and why in result.content and fake.sent == []


# -- §6.3: organising --------------------------------------------------------------------------


async def test_organising_moves_flags_and_categorises_and_is_capped(
    fake: Fake, tmp_path: Path
) -> None:
    news = [fake.add(mime("Shop <news@shop.example>", f"Sale {n}")) for n in range(2)]
    sam = fake.add(mime())
    made = tools(tmp_path)

    subject, result = await carded(made["outlook_mail_organise"], action="archive", query="shop")
    assert subject.summary == "Archive 2 message(s) from Shop (outlook)" and not subject.confirm
    assert not result.is_error, result.content
    assert all(fake.messages[n]["folder"] == "archive" for n in news)
    assert fake.messages[sam]["folder"] == "inbox"

    handle = handle_of((await made["outlook_mail_search"].run(query="dinner")).content)
    await carded(
        made["outlook_mail_organise"], action="label", label="Family", message_ids=[handle]
    )
    await carded(made["outlook_mail_organise"], action="flag", message_ids=[handle])
    await carded(
        made["outlook_mail_organise"], action="move", folder="Receipts", message_ids=[handle]
    )
    meta = fake.messages[sam]["meta"]
    assert meta["categories"] == ["Family"] and meta["flag"] == {"flagStatus": "flagged"}
    assert fake.messages[sam]["folder"] == "receipts"
    await carded(made["outlook_mail_organise"], action="trash", message_ids=[handle])
    assert fake.messages[sam]["folder"] == "deleteditems"
    assert not any(c.method == "DELETE" for c in fake.calls)  # never deleted for good

    for n in range(51):
        fake.add(mime("Bulk <bulk@example.com>", f"Bulk {n}"))
    too_many = await made["outlook_mail_organise"].run(action="read", query="bulk")
    assert too_many.is_error and "at most 50" in too_many.content


async def test_folders_and_categories_are_listed(fake: Fake, tmp_path: Path) -> None:
    text = (await tools(tmp_path)["outlook_mail_folders"].run()).content
    assert text == "outlook: folders: Inbox (2 unread), Archive, Receipts; categories: Family, Red"


# -- §7: the calendar --------------------------------------------------------------------------


async def test_events_are_listed_in_local_time_and_all_day_by_date(
    fake: Fake, tmp_path: Path
) -> None:
    fake.event(at(2, 9), 30, "Dentist", location={"displayName": "Clinic"})
    fake.events[fake.event(at(3, 0), title="Holiday")].update(
        isAllDay=True,
        start={"dateTime": "2026-10-03T00:00:00.0000000", "timeZone": "UTC"},
        end={"dateTime": "2026-10-05T00:00:00.0000000", "timeZone": "UTC"},
    )
    fake.event(
        at(2, 14),
        60,
        "Planning",
        isOrganizer=False,
        responseStatus={"response": "notResponded"},
        type="occurrence",
    )
    result = await tools(tmp_path)["outlook_calendar_list_events"].run(
        start="2026-10-02", end="2026-10-04"
    )
    assert not result.is_error, result.content
    lines = result.content.splitlines()
    assert lines[1] == "Fri 2 Oct 09:00-09:30 · Dentist · at Clinic  [id: e1]"
    assert lines[2] == "Fri 2 Oct 14:00-15:00 · Planning · not answered, repeats  [id: e2]"
    assert lines[3] == "Sat 3 Oct - Sun 4 Oct (all day) · Holiday  [id: e3]"
    view = next(c for c in fake.calls if c.path.endswith("/calendarView"))
    assert 'outlook.timezone="UTC"' in view.headers["Prefer"]
    assert view.query["startDateTime"] == "2026-10-01T14:00:00+00:00"  # local midnight, in UTC


async def test_a_new_event_names_who_will_be_invited(fake: Fake, tmp_path: Path) -> None:
    subject, result = await carded(
        tools(tmp_path)["outlook_calendar_create_event"],
        title="Lunch with Alex",
        start="2026-10-02T12:00",
        location="Cafe Rosa",
        attendees=["alex@example.com"],
    )
    assert subject.confirm is True
    assert subject.summary.splitlines() == [
        "Create in Calendar (outlook)",
        "Title: Lunch with Alex",
        "When: Fri 2 Oct 12:00-13:00",
        "Where: Cafe Rosa",
        "Invitations will be emailed to: alex@example.com",
    ]
    assert not result.is_error and "[id: e" in result.content
    post = next(c for c in fake.calls if c.method == "POST")
    assert post.path == "/me/calendars/cal-1/events"
    assert post.body["start"] == {"dateTime": "2026-10-02T02:00:00", "timeZone": "UTC"}
    assert post.body["attendees"] == [
        {"emailAddress": {"address": "alex@example.com"}, "type": "required"}
    ]

    _, _holiday = await carded(
        tools(tmp_path)["outlook_calendar_create_event"],
        title="Holiday",
        start="2026-10-05",
        end="2026-10-06",
    )
    body = [c for c in fake.calls if c.method == "POST"][-1].body
    assert body["isAllDay"] and body["end"]["dateTime"] == "2026-10-07T00:00:00"


async def test_an_update_shows_from_and_to_and_refuses_a_stale_event(
    fake: Fake, tmp_path: Path
) -> None:
    graph_id = fake.event(
        at(2, 9), 30, "Dentist", attendees=[{"emailAddress": {"address": "kim@example.com"}}]
    )
    made = tools(tmp_path)
    handle = handle_of(
        (
            await made["outlook_calendar_list_events"].run(start="2026-10-02", end="2026-10-02")
        ).content
    )
    subject = await made["outlook_calendar_update_event"].subject(
        {"event_id": handle, "start": "2026-10-02T16:00"}
    )
    assert "When: Fri 2 Oct 09:00-09:30 -> Fri 2 Oct 16:00-16:30" in subject.summary
    assert "The change will be emailed to: kim@example.com" in subject.summary
    fake.events[graph_id]["changeKey"] = "c2"  # moved in Outlook after the card
    stale = await made["outlook_calendar_update_event"].run(
        event_id=handle, start="2026-10-02T16:00"
    )
    assert stale.is_error and "changed since it was shown" in stale.content
    assert not any(c.method == "PATCH" for c in fake.calls)

    _, done = await carded(
        made["outlook_calendar_update_event"], event_id=handle, start="2026-10-02T16:00"
    )
    assert not done.is_error, done.content
    assert fake.events[graph_id]["start"] == {
        "dateTime": "2026-10-02T06:00:00",
        "timeZone": "UTC",
    }  # AEST, +10

    fake.events[graph_id]["isOrganizer"] = False
    theirs = await made["outlook_calendar_update_event"].run(event_id=handle, title="Mine now")
    assert theirs.is_error and "not the organiser" in theirs.content


async def test_cancelling_as_organiser_tells_the_attendees(fake: Fake, tmp_path: Path) -> None:
    graph_id = fake.event(
        at(2, 9), title="Standup", attendees=[{"emailAddress": {"address": "kim@example.com"}}]
    )
    made = tools(tmp_path)
    handle = handle_of(
        (
            await made["outlook_calendar_list_events"].run(start="2026-10-02", end="2026-10-02")
        ).content
    )
    subject, result = await carded(
        made["outlook_calendar_delete_event"], event_id=handle, message="Sick today"
    )
    assert subject.summary.splitlines()[0] == "Cancel Standup (outlook)"
    assert "The cancellation will be emailed to: kim@example.com" in subject.summary
    assert not result.is_error and fake.events[graph_id]["did"] == "/cancel"
    cancel = next(c for c in fake.calls if c.path.endswith("/cancel"))
    assert cancel.body == {"Comment": "Sick today"}


async def test_deleting_someone_elses_invite_says_they_are_not_told(
    fake: Fake, tmp_path: Path
) -> None:
    graph_id = fake.event(
        at(2, 9),
        title="Their meeting",
        isOrganizer=False,
        organizer={"emailAddress": {"name": "Priya", "address": "priya@example.com"}},
    )
    made = tools(tmp_path)
    handle = handle_of(
        (
            await made["outlook_calendar_list_events"].run(start="2026-10-02", end="2026-10-02")
        ).content
    )
    subject, _ = await carded(made["outlook_calendar_delete_event"], event_id=handle)
    assert "is not told - decline it instead" in subject.summary
    assert graph_id not in fake.events


async def test_answering_an_invitation_is_a_card(fake: Fake, tmp_path: Path) -> None:
    graph_id = fake.event(
        at(2, 15),
        title="Review",
        isOrganizer=False,
        organizer={"emailAddress": {"name": "Priya", "address": "priya@example.com"}},
        responseStatus={"response": "notResponded"},
    )
    made = tools(tmp_path)
    handle = handle_of(
        (
            await made["outlook_calendar_list_events"].run(start="2026-10-02", end="2026-10-02")
        ).content
    )
    subject, result = await carded(
        made["outlook_calendar_respond"],
        event_id=handle,
        response="tentative",
        comment="Might be late",
    )
    assert (
        subject.confirm
        and subject.summary.splitlines()[0] == "Tentatively accept: Review (outlook)"
    )
    assert (
        "The organiser, Priya <priya@example.com>, will be emailed your answer" in subject.summary
    )
    assert result.content.startswith("Tentatively accepted Review")
    post = next(c for c in fake.calls if c.path.endswith("/tentativelyAccept"))
    assert post.body == {"comment": "Might be late", "sendResponse": True}
    assert fake.events[graph_id]["did"] == "/tentativelyAccept"


async def test_free_busy_finds_the_gaps(fake: Fake, tmp_path: Path) -> None:
    fake.event(at(2, 9), 60, "A")
    fake.event(at(2, 9, 30), 60, "B")  # overlaps A: one block
    fake.event(at(2, 13), 30, "Lunch", showAs="free")  # free: not busy
    text = (
        await tools(tmp_path, day_start="08:00", day_end="12:00")["outlook_calendar_free_busy"].run(
            start="2026-10-02", end="2026-10-02", minutes=30
        )
    ).content
    assert text.splitlines() == [
        "Busy:",
        "  Fri 2 Oct 09:00-10:30",
        "Free (at least 30 minutes):",
        "  Fri 2 Oct 08:00-09:00",
        "  Fri 2 Oct 10:30-12:00",
    ]


# -- §8: new mail ------------------------------------------------------------------------------


class ServiceContext:
    def __init__(self, workspace: Path, sleeps: int = 1, **settings: Any) -> None:
        self.workspace = workspace
        self.settings = {"notify": "focused", "poll_minutes": 2, **settings}
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


async def test_new_focused_mail_is_one_line_and_the_backlog_is_not_announced(
    fake: Fake, tmp_path: Path
) -> None:
    fake.add(mime("Old <old@example.com>", "From last week"), received="2026-10-01T01:00:00Z")
    ctx = ServiceContext(tmp_path, sleeps=3)
    ctx.between = [
        lambda: (
            fake.add(mime(), received="2026-10-01T03:00:00Z"),
            fake.add(
                mime("Shop <news@shop.example>", "Sale"),
                received="2026-10-01T03:01:00Z",
                focus="other",
            ),
            fake.add(mime(f"Me <{ME}>", "Note to self"), received="2026-10-01T03:02:00Z"),
        ),
        lambda: [
            fake.add(
                mime(f"Person {n} <p{n}@example.com>", f"Hello {n}"),
                received=f"2026-10-01T04:0{n}:00Z",
            )
            for n in range(3)
        ],
        lambda: None,
    ]
    await ms.NewMail().run(ctx)  # type: ignore[arg-type]
    assert ctx.sent == [
        "📧 Sam Lee - Dinner Friday?",
        "📧 3 new emails: Person 0, Person 1, Person 2",
    ]


async def test_a_refused_sign_in_is_said_once(fake: Fake, tmp_path: Path) -> None:
    fake.on("GET", "/me/mailFolders/inbox/messages", lambda call: (401, {}))
    ctx = ServiceContext(tmp_path, sleeps=3)
    await ms.NewMail().run(ctx)  # type: ignore[arg-type]
    assert len(ctx.sent) == 1 and "sign in again" in ctx.sent[0]
