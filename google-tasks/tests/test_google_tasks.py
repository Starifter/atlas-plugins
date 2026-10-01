"""The Google Tasks plugin, driven the way Atlas drives it with Google replaced:
`request`, `grant` and `connections` are the plugin's module-level names, and a
fake answers the Tasks API's shapes - its pages, its hidden completed tasks and
its `move` included. Nothing reaches Google.

Run from a checkout of Atlas (`uv run --project ../Atlas pytest google-tasks/tests`).
"""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest

from atlas.web.client import Response

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "atlas_plugin_google_tasks", HERE.parent / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gt = _load()

ME = "google-tasks:google"
TODAY = date.today()


def due(days: int) -> str:
    return f"{(TODAY + timedelta(days=days)).isoformat()}T00:00:00.000Z"


def shown(days: int) -> str:
    return gt.day(TODAY + timedelta(days=days))


def task(tid: str, title: str, position: str = "0", **extra: Any) -> dict[str, Any]:
    return {
        "id": tid,
        "title": title,
        "status": "needsAction",
        "position": position,
        "updated": "2026-09-29T04:05:00.000Z",
        **extra,
    }


def done(tid: str, title: str, position: str = "0", **extra: Any) -> dict[str, Any]:
    return task(
        tid, title, position, status="completed", completed="2026-09-29T04:05:00.000Z", **extra
    )


class Call(SimpleNamespace):
    method: str
    host: str
    path: str
    query: dict[str, str]
    body: Any
    headers: dict[str, str]


Answer = Callable[[Call], tuple[int, Any]]


class Fake:
    """The Tasks API, as far as these tests need it."""

    def __init__(self) -> None:
        self.accounts: tuple[str, ...] = (ME,)
        self.calls: list[Call] = []
        self.lists: dict[str, dict[str, Any]] = {
            "L0": {"id": "L0", "title": "My Tasks", "updated": "2026-09-29T04:05:00.000Z"},
            "L1": {"id": "L1", "title": "Errands", "updated": "2026-09-29T04:05:00.000Z"},
        }
        self.default = "L0"
        self.tasks: dict[str, list[dict[str, Any]]] = {"L0": [], "L1": []}
        self.page = 100
        self.made = 0
        self.routes: list[tuple[str, str, Answer]] = []

    def on(self, method: str, pattern: str, answer: Answer) -> None:
        self.routes.insert(0, (method, pattern, answer))

    def paged(self, items: list[dict[str, Any]], key: str, q: dict[str, str]) -> dict[str, Any]:
        size = min(int(q.get("maxResults", 100)), self.page)
        start = int(q.get("pageToken") or 0)
        page: dict[str, Any] = {key: items[start : start + size]}
        if start + size < len(items):
            page["nextPageToken"] = str(start + size)
        return page

    def find(self, lid: str, tid: str) -> dict[str, Any] | None:
        return next((t for t in self.tasks.get(lid, []) if t["id"] == tid), None)

    def default_route(self, call: Call) -> tuple[int, Any]:
        path, q, body = call.path, call.query, call.body
        missing = 404, {"error": {"code": 404, "message": "Not Found", "errors": [{}]}}
        if path == "/tasks/v1/users/@me/lists":
            if call.method == "GET":
                return 200, self.paged(list(self.lists.values()), "items", q)
            self.made += 1
            lid = f"NL{self.made}"
            self.lists[lid] = {"id": lid, "title": body["title"]}
            self.tasks[lid] = []
            return 200, self.lists[lid]
        match = re.fullmatch(r"/tasks/v1/users/@me/lists/([^/]+)", path)
        if match:
            lid = self.default if match.group(1) == "@default" else match.group(1)
            if lid not in self.lists:
                return missing
            if call.method == "PATCH":
                self.lists[lid].update(body)
            if call.method == "DELETE":
                del self.lists[lid]
                del self.tasks[lid]
                return 204, None
            return 200, self.lists[lid]
        match = re.fullmatch(r"/tasks/v1/lists/([^/]+)/clear", path)
        if match:
            for t in self.tasks[match.group(1)]:
                if t["status"] == "completed":
                    t["hidden"] = True
            return 204, None
        match = re.fullmatch(r"/tasks/v1/lists/([^/]+)/tasks(?:/([^/]+))?(/move)?", path)
        if match:
            lid, tid, move = match.group(1), match.group(2), match.group(3)
            held = self.tasks.get(lid)
            if held is None:
                return missing
            if tid is None and call.method == "GET":
                items = list(held)
                if q.get("showCompleted") == "false":
                    items = [t for t in items if t["status"] != "completed"]
                if q.get("showHidden") != "true":
                    items = [t for t in items if not t.get("hidden")]
                if q.get("dueMin"):
                    items = [t for t in items if t.get("due") and t["due"] >= q["dueMin"]]
                if q.get("dueMax"):
                    items = [t for t in items if t.get("due") and t["due"] <= q["dueMax"]]
                return 200, self.paged(items, "items", q)
            if tid is None and call.method == "POST":
                self.made += 1
                made = {**task(f"n{self.made}", "", f"-{self.made:04d}"), **body}
                if q.get("parent"):
                    made["parent"] = q["parent"]
                held.insert(0, made)
                return 200, made
            found = self.find(lid, tid or "")
            if found is None:
                return missing
            if move:
                held.remove(found)
                target = q.get("destinationTasklist") or lid
                found.pop("parent", None)
                if q.get("parent"):
                    found["parent"] = q["parent"]
                found["moved_after"] = q.get("previous", "")
                self.tasks[target].append(found)
                return 200, found
            if call.method == "GET":
                return 200, found
            if call.method == "PATCH":
                for k, v in body.items():
                    if v is None:
                        found.pop(k, None)
                    else:
                        found[k] = v
                return 200, found
            if call.method == "DELETE":
                held.remove(found)
                return 204, None
        return 404, {"error": {"message": f"no route for {call.method} {path}"}}

    async def request(self, method: str, url: str, **kwargs: Any) -> Response:
        parts = urlsplit(url)
        call = Call(
            method=method,
            host=parts.hostname or "",
            path=unquote(parts.path),
            query=dict(parse_qsl(parts.query)),
            body=kwargs.get("json"),
            headers=dict(kwargs.get("headers") or {}),
        )
        self.calls.append(call)
        answer: Answer = self.default_route
        for verb, pattern, handler in self.routes:
            if verb == method and re.fullmatch(pattern, call.path):
                answer = handler
                break
        status, body = answer(call)
        raw = b"" if body is None else json.dumps(body).encode()
        return Response(
            url=url, status=status, headers=(("retry-after", "0"),), body=raw, truncated=False
        )

    def changes(self) -> list[Call]:
        return [c for c in self.calls if c.method != "GET"]


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> Fake:
    fake = Fake()
    monkeypatch.setattr(gt, "request", fake.request)
    monkeypatch.setattr(gt, "connections", lambda plugin, workspace=None: fake.accounts)
    monkeypatch.setattr(
        gt, "grant", lambda name, workspace=None: SimpleNamespace(bearer=lambda: f"tok-{name}")
    )
    return fake


class Context:
    """A `PluginContext`, as far as the tools use it."""

    def __init__(self, workspace: Path, **settings: Any) -> None:
        self.workspace = workspace
        self.settings = settings
        self.audited: list[tuple[str, str, dict[str, Any]]] = []

    def audit(self, event: str, detail: str = "", **kwargs: Any) -> None:
        self.audited.append((event, detail, dict(kwargs.get("arguments") or {})))


TOOLS = ("Lists", "Show", "Add", "Update", "Remove", "ListManage")


def tools(tmp_path: Path, **settings: Any) -> tuple[Context, dict[str, Any]]:
    ctx = Context(tmp_path, **settings)
    shared = gt.Tasks(ctx)
    made = {getattr(gt, n).name: getattr(gt, n)(shared) for n in TOOLS}
    return ctx, made


async def carded(tool: Any, **arguments: Any) -> tuple[Any, Any]:
    """What the executor does: the card, then - the person having said yes - the run."""
    subject = await tool.subject(arguments)
    return subject, await tool.run(**arguments)


# -- signing in, and what every tool is -------------------------------------------


async def test_nothing_reaches_google_until_someone_signs_in(google: Fake, tmp_path: Path) -> None:
    google.accounts = ()
    _, made = tools(tmp_path)
    for name in ("tasks_lists", "tasks_show"):
        result = await made[name].run()
        assert result.is_error and "atlas auth login google-tasks:google" in result.content
    assert await made["tasks_add"].subject({"tasks": [{"title": "milk"}]}) is None
    assert google.calls == []


def test_the_plugin_installs_whole_with_no_one_signed_in(tmp_path: Path) -> None:
    from atlas.auth.oauth import known_logins, unregister_login
    from atlas.plugins.install import install_one
    from atlas.tools.registry import ToolRegistry

    provision = install_one(gt.GoogleTasksPlugin(), workspace=tmp_path, tools=ToolRegistry())
    try:
        assert provision.ok, provision.error
        assert provision.logins == ("google-tasks:google",)
        assert len(provision.tools) == 6 and provision.services == ()
        assert known_logins()["google-tasks:google"].run is gt.no_client
    finally:
        unregister_login("google-tasks:google")

    install_one(
        gt.GoogleTasksPlugin(),
        workspace=tmp_path,
        tools=ToolRegistry(),
        settings={"client_id": "c"},
    )
    try:
        client = known_logins()["google-tasks:google"].client
        assert client is not None and client.client_id == "c"
    finally:
        unregister_login("google-tasks:google")


def test_the_client_asks_for_tasks_offline_and_reads_the_email() -> None:
    client = gt.client("cid")
    assert set(client.scopes) == {"https://www.googleapis.com/auth/tasks", "openid", "email"}
    assert client.authorize_params == {"access_type": "offline", "prompt": "consent"}
    claims = base64.urlsafe_b64encode(json.dumps({"email": "a@b.com"}).encode()).rstrip(b"=")
    assert gt._label({"id_token": f"h.{claims.decode()}.s"}) == {
        "email": "a@b.com",
        "label": "a@b.com",
    }
    assert gt._label({"id_token": "nonsense"}) == {}


def test_every_tool_is_owner_only_and_untrusted_and_every_change_is_gated(tmp_path: Path) -> None:
    _, made = tools(tmp_path)
    assert sorted(made) == sorted(
        f"tasks_{n}" for n in ("lists", "show", "add", "update", "remove", "list_manage")
    )
    for tool in made.values():
        assert tool.trusted_only and tool.untrusted, tool.name
    assert {n for n, t in made.items() if t.gated} == {
        "tasks_add",
        "tasks_update",
        "tasks_remove",
        "tasks_list_manage",
    }


# -- looking ---------------------------------------------------------------------


async def test_lists_come_page_by_page_and_the_main_one_is_marked(
    google: Fake, tmp_path: Path
) -> None:
    google.page = 1
    _, made = tools(tmp_path)
    result = await made["tasks_lists"].run()
    lines = result.content.splitlines()
    assert lines[0] == "2 list(s):"
    assert lines[1].startswith("My Tasks · the main list · changed ") and "[id: L0]" in lines[1]
    assert lines[2].startswith("Errands · changed ") and lines[2].endswith("[id: L1]")
    pages = [c for c in google.calls if c.path == "/tasks/v1/users/@me/lists"]
    assert [c.query.get("pageToken", "") for c in pages] == ["", "1"]
    assert all(c.host == "tasks.googleapis.com" for c in google.calls)
    assert google.calls[0].headers["Authorization"] == f"Bearer tok-{ME}"


async def test_show_lists_open_tasks_with_subtasks_due_dates_and_notes(
    google: Fake, tmp_path: Path
) -> None:
    google.tasks["L0"] = [
        task("t2", "Dentist", "2", due=due(-1), notes="Call\nfirst"),
        task("t1", "Renew passport", "1", due=due(8)),
        task("s1", "Find photos", "0", parent="t1"),
        done("t3", "Old thing", "3", hidden=True),
    ]
    google.tasks["L1"] = [task("e1", "Buy milk")]
    _, made = tools(tmp_path)
    result = await made["tasks_show"].run()
    assert result.content == "\n".join(
        [
            "My Tasks - 3 task(s)  [list id: L0]",
            f"  [ ] Renew passport · due {shown(8)}  [id: t1]",
            "    [ ] Find photos  [id: s1]",
            f"  [ ] Dentist · due {shown(-1)} - overdue  [id: t2]",
            "      notes: Call first",
            "",
            "Errands - 1 task(s)  [list id: L1]",
            "  [ ] Buy milk  [id: e1]",
        ]
    )
    asked = next(c for c in google.calls if c.path == "/tasks/v1/lists/L0/tasks")
    assert asked.query["showCompleted"] == "false" and asked.query["showHidden"] == "false"


async def test_completed_tasks_are_found_even_when_google_hid_them(
    google: Fake, tmp_path: Path
) -> None:
    google.tasks["L1"] = [task("e1", "Buy milk"), done("e2", "Post letter", hidden=True)]
    _, made = tools(tmp_path)
    result = await made["tasks_show"].run(list="errands", status="completed")
    assert result.content.splitlines()[1].startswith("  [x] Post letter · done ")
    assert "Buy milk" not in result.content
    asked = [c for c in google.calls if c.path == "/tasks/v1/lists/L1/tasks"][-1]
    assert asked.query["showCompleted"] == "true" and asked.query["showHidden"] == "true"


async def test_due_filters_are_whole_days_and_words_narrow_it(google: Fake, tmp_path: Path) -> None:
    google.tasks["L0"] = [
        task("a", "Pay rent", due=due(0)),
        task("b", "Pay gas", due=due(3)),
        task("c", "Water plants"),
    ]
    _, made = tools(tmp_path)
    end = (TODAY + timedelta(days=1)).isoformat()
    result = await made["tasks_show"].run(due_before=end, query="pay")
    assert "Pay rent" in result.content and "Pay gas" not in result.content
    asked = next(c for c in google.calls if c.path == "/tasks/v1/lists/L0/tasks")
    assert asked.query["dueMax"] == f"{end}T23:59:59.999Z"

    after = await made["tasks_show"].run(due_after=TODAY.isoformat(), query="plants")
    assert after.content == "No open tasks match."
    bad = await made["tasks_show"].run(due_before="Friday")
    assert bad.is_error and "YYYY-MM-DD" in bad.content


async def test_a_due_date_is_the_day_google_stored_in_any_zone() -> None:
    assert gt.due_date({"due": "2026-10-09T00:00:00.000Z"}) == date(2026, 10, 9)
    assert gt.due_date({"due": "nonsense"}) is None


async def test_a_long_listing_is_paged_and_cut(google: Fake, tmp_path: Path) -> None:
    google.page = 2
    google.tasks["L0"] = [task(f"t{i}", f"Task {i}", f"{i:02d}") for i in range(5)]
    _, made = tools(tmp_path, max_results=3)
    result = await made["tasks_show"].run(list="My Tasks")
    assert result.content.splitlines()[0] == "My Tasks - 3 task(s)  [list id: L0]"
    assert "showing the first 3" in result.content
    unknown = await made["tasks_show"].run(list="Holiday")
    assert unknown.is_error and '"My Tasks", "Errands"' in unknown.content


# -- adding ----------------------------------------------------------------------


async def test_adding_is_an_ordinary_card_that_says_a_due_date_is_only_a_date(
    google: Fake, tmp_path: Path
) -> None:
    google.tasks["L1"] = [task("e1", "Groceries")]
    ctx, made = tools(tmp_path)
    friday = (TODAY + timedelta(days=8)).isoformat()
    subject, result = await carded(
        made["tasks_add"],
        list="Errands",
        tasks=[
            {"title": "Renew passport", "due": friday, "notes": "Form at the post office"},
            {"title": "Milk", "parent": "e1"},
        ],
    )
    assert subject.confirm is False and subject.action == "add"
    assert subject.summary.splitlines() == [
        'Add 2 task(s) to "Errands" (Google Tasks)',
        f"  Renew passport · due {shown(8)}",
        "    notes: Form at the post office",
        '  Milk · a subtask of "Groceries"',
        "Due dates are a date only - Google Tasks keeps no time of day.",
    ]
    assert not result.is_error, result.content
    posts = [c for c in google.changes() if c.method == "POST"]
    assert [c.body["title"] for c in posts] == ["Milk", "Renew passport"]  # top goes last
    assert posts[0].query["parent"] == "e1" and "parent" not in posts[1].query
    assert posts[1].body == {
        "title": "Renew passport",
        "notes": "Form at the post office",
        "due": f"{friday}T00:00:00.000Z",
    }
    assert result.content.splitlines()[0] == 'Added to "Errands":'
    assert "Renew passport" in result.content.splitlines()[1]
    event, _, record = ctx.audited[-1]
    assert event == "tasks_added" and record["list"] == "L1" and len(record["tasks"]) == 2
    assert "Renew passport" not in json.dumps(record)


async def test_a_task_goes_to_the_main_list_by_default(google: Fake, tmp_path: Path) -> None:
    _, made = tools(tmp_path)
    subject, _ = await carded(made["tasks_add"], tasks=[{"title": "buy milk"}])
    assert subject.summary == 'Add 1 task(s) to "My Tasks" (Google Tasks)\n  buy milk'
    assert google.tasks["L0"][0]["title"] == "buy milk"


@pytest.mark.parametrize(
    ("arguments", "why"),
    [
        ({"tasks": [{"title": "x", "due": "2026-10-09T15:00"}]}, "keeps no time of day"),
        ({"tasks": [{"title": "x", "due": "2026-02-30"}]}, "not a real date"),
        ({"tasks": [{"title": "  "}]}, "give it a title"),
        ({"tasks": [{"title": f"t{i}"} for i in range(21)]}, "at most 20"),
        ({"tasks": [{"title": "x", "parent": "s1"}]}, "nests one level deep"),
        ({"tasks": [{"title": "x", "parent": "nope"}]}, "to put a subtask under"),
        ({"tasks": [{"title": "x"}], "list": "Holiday"}, "no task list 'Holiday'"),
    ],
)
async def test_what_cannot_be_added_is_refused_before_any_card(
    google: Fake, tmp_path: Path, arguments: dict[str, Any], why: str
) -> None:
    google.tasks["L0"] = [task("t1", "Parent"), task("s1", "Child", parent="t1")]
    _, made = tools(tmp_path)
    assert await made["tasks_add"].subject(arguments) is None
    result = await made["tasks_add"].run(**arguments)
    assert result.is_error and why in result.content and google.changes() == []


# -- changing --------------------------------------------------------------------


async def test_updates_are_carded_from_what_is_there_and_patched(
    google: Fake, tmp_path: Path
) -> None:
    google.tasks["L0"] = [
        task("t1", "Dentist", due=due(2), notes="ring them"),
        done("t2", "Gym", hidden=True),
    ]
    ctx, made = tools(tmp_path)
    monday = (TODAY + timedelta(days=5)).isoformat()
    subject, result = await carded(
        made["tasks_update"],
        changes=[
            {"task_id": "t1", "status": "done", "due": monday, "notes": ""},
            {"task_id": "t2", "status": "open", "title": "Gym - legs"},
        ],
    )
    assert subject.confirm is False
    assert subject.summary.splitlines() == [
        "Change 2 task(s) in Google Tasks",
        f'  "Dentist" (My Tasks): mark done; clear the notes; due {shown(2)} → {shown(5)}',
        '  "Gym" (My Tasks): mark not done; rename → "Gym - legs"',
        "Due dates are a date only - Google Tasks keeps no time of day.",
    ]
    assert not result.is_error, result.content
    patches = [c.body for c in google.changes()]
    assert patches == [
        {"status": "completed", "notes": None, "due": f"{monday}T00:00:00.000Z"},
        {"status": "needsAction", "completed": None, "title": "Gym - legs"},
    ]
    assert result.content.startswith("Changed 2 task(s):\n  [x] Dentist")
    assert ctx.audited[-1][0] == "tasks_updated"


async def test_a_task_moves_to_another_list_or_place_keeping_what_it_should(
    google: Fake, tmp_path: Path
) -> None:
    google.tasks["L0"] = [
        task("t1", "Shoes", "1"),
        task("s1", "Laces", "0", parent="t1"),
        task("t2", "Plan", "2"),
        task("s2", "Step one", "0", parent="t2"),
        task("s3", "Step two", "1", parent="t2"),
    ]
    _, made = tools(tmp_path)
    subject, result = await carded(
        made["tasks_update"],
        changes=[
            {"task_id": "t1", "to_list": "Errands"},
            {"task_id": "s2", "after": "s3"},
        ],
    )
    assert subject.summary.splitlines()[1:] == [
        '  "Shoes" (My Tasks): move to "Errands"; its 1 subtask(s) go with it',
        '  "Step one" (My Tasks): put it after "Step two"',
    ]
    assert not result.is_error, result.content
    moves = [c for c in google.changes() if c.path.endswith("/move")]
    assert moves[0].query == {"destinationTasklist": "L1"}
    assert moves[1].query == {"parent": "t2", "previous": "s3"}  # still under its parent
    assert [c.method for c in google.changes()] == ["POST", "POST"]  # nothing to patch


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"task_id": "s1", "parent": "t2"}, "nothing to change"),
        ({"task_id": "t2", "parent": "s1"}, "nests one level deep"),
        ({"task_id": "t2", "parent": "t1"}, "has subtasks of its own"),
        ({"task_id": "s1", "after": "t1"}, "not at the same level"),
        ({"task_id": "nope", "status": "done"}, "no task 'nope'"),
        ({"task_id": "a1", "to_list": "Errands"}, "cannot leave its list"),
    ],
)
async def test_impossible_changes_are_refused_before_any_card(
    google: Fake, tmp_path: Path, change: dict[str, Any], why: str
) -> None:
    google.tasks["L0"] = [
        task("t1", "Shoes"),
        task("t2", "Plan"),
        task("s1", "Laces", parent="t2"),
        task("a1", "Review doc", assignmentInfo={"surfaceType": "DOCUMENT"}),
    ]
    _, made = tools(tmp_path)
    if why == "nothing to change":
        google.tasks["L0"][2]["parent"] = "t2"
    assert await made["tasks_update"].subject({"changes": [change]}) is None
    result = await made["tasks_update"].run(changes=[change])
    assert result.is_error and why in result.content and google.changes() == []


# -- removing --------------------------------------------------------------------


async def test_deleting_asks_in_every_mode_and_names_every_task_subtasks_first(
    google: Fake, tmp_path: Path
) -> None:
    google.tasks["L0"] = [
        task("t1", "Party", "0"),
        task("s1", "Balloons", "0", parent="t1"),
        task("t2", "Milk", "1"),
    ]
    ctx, made = tools(tmp_path)
    subject, result = await carded(made["tasks_remove"], task_ids=["t1", "s1", "t2"])
    assert subject.confirm is True
    assert subject.summary.splitlines() == [
        "Delete 3 task(s) from Google Tasks - for good:",
        "  [ ] Party  [id: t1] (My Tasks)",
        "    and its subtask [ ] Balloons  [id: s1]",
        "  [ ] Milk  [id: t2] (My Tasks)",
        "Google Tasks cannot undo this.",
    ]
    assert [c.path.rpartition("/")[2] for c in google.changes()] == ["s1", "t1", "t2"]
    assert result.content == 'Deleted 3 task(s): "Balloons", "Party", "Milk"'
    assert google.tasks["L0"] == []
    assert ctx.audited[-1][2]["deleted"] == ["s1", "t1", "t2"]


async def test_the_run_deletes_what_the_card_named_not_a_later_look(
    google: Fake, tmp_path: Path
) -> None:
    google.tasks["L0"] = [task("t1", "Party")]
    _, made = tools(tmp_path)
    subject = await made["tasks_remove"].subject({"task_ids": ["t1"]})
    google.tasks["L0"].append(task("s9", "Added since", parent="t1"))
    await made["tasks_remove"].run(task_ids=["t1"])
    assert subject is not None and "Added since" not in subject.summary
    assert [c.path.rpartition("/")[2] for c in google.changes()] == ["t1"]


async def test_clearing_completed_tasks_hides_them_on_an_ordinary_card(
    google: Fake, tmp_path: Path
) -> None:
    google.tasks["L1"] = [task("e1", "Milk"), done("e2", "Stamps"), done("e3", "Bread")]
    _, made = tools(tmp_path)
    subject, result = await carded(made["tasks_remove"], clear_completed="Errands")
    assert subject.confirm is False
    assert subject.summary == (
        'Clear 2 completed task(s) from "Errands": "Stamps", "Bread" - hidden from the list, '
        "not deleted"
    )
    assert [c.path for c in google.changes()] == ["/tasks/v1/lists/L1/clear"]
    assert result.content == 'Cleared the completed tasks from "Errands".'
    again = await made["tasks_remove"].run(clear_completed="Errands")
    assert again.is_error and "no completed tasks showing" in again.content


# -- lists -----------------------------------------------------------------------


async def test_making_and_renaming_a_list_are_ordinary_cards(google: Fake, tmp_path: Path) -> None:
    _, made = tools(tmp_path)
    created, result = await carded(made["tasks_list_manage"], action="create", title="Holiday")
    assert created.confirm is False
    assert created.summary == 'Create a task list "Holiday" in Google Tasks'
    assert result.content == 'Created the list "Holiday"  [id: NL1]'
    renamed, _ = await carded(
        made["tasks_list_manage"], action="rename", list="Errands", title="Shopping"
    )
    assert renamed.summary == 'Rename the task list "Errands" → "Shopping"'
    assert google.lists["L1"]["title"] == "Shopping"


async def test_deleting_a_list_says_how_many_tasks_go_and_spares_the_main_one(
    google: Fake, tmp_path: Path
) -> None:
    google.page = 2
    google.tasks["L1"] = [
        task("e1", "Milk"),
        task("e2", "Eggs"),
        done("e3", "Stamps", hidden=True),
        task("e4", "Review", assignmentInfo={"surfaceType": "SPACE"}),
    ]
    _, made = tools(tmp_path)
    subject, result = await carded(made["tasks_list_manage"], action="delete", list="Errands")
    assert subject.confirm is True
    assert subject.summary.splitlines() == [
        'Delete the task list "Errands" and all 4 task(s) in it - 3 still open, 1 completed - '
        "for good.",
        '  Open: "Milk", "Eggs", "Review"',
        "Google Tasks cannot undo this. Tasks assigned from a Doc or a Chat space are deleted "
        "there too.",
    ]
    assert result.content == 'Deleted the list "Errands" and the 4 task(s) in it.'
    assert "L1" not in google.lists

    main = await made["tasks_list_manage"].run(action="delete", list="My Tasks")
    assert main.is_error and "the main list" in main.content
    assert [c.method for c in google.changes()] == ["DELETE"]


# -- accounts, retries, the host -------------------------------------------------


async def test_two_accounts_label_every_list_and_a_change_must_name_one(
    google: Fake, tmp_path: Path
) -> None:
    google.accounts = (ME, "google-tasks:work")
    google.tasks["L0"] = [task("t1", "Milk")]
    _, made = tools(tmp_path)
    found = await made["tasks_show"].run()
    assert "[list id: L0 · google]" in found.content and "[list id: L0 · work]" in found.content
    refused = await made["tasks_add"].run(tasks=[{"title": "x"}])
    assert refused.is_error and "say which with account: google, work" in refused.content
    named = await made["tasks_add"].run(tasks=[{"title": "x"}], account="work")
    assert not named.is_error
    assert google.changes()[-1].headers["Authorization"] == "Bearer tok-google-tasks:work"


async def test_a_rate_limit_is_waited_out_once_and_a_change_is_never_retried(
    google: Fake, tmp_path: Path
) -> None:
    limited = {"error": {"message": "Rate", "errors": [{"reason": "userRateLimitExceeded"}]}}
    answers = iter([(403, limited), (200, {"items": [google.lists["L0"]]})])
    google.on("GET", "/tasks/v1/users/@me/lists", lambda call: next(answers))
    _, made = tools(tmp_path)
    found = await made["tasks_lists"].run()
    assert not found.is_error and "My Tasks" in found.content

    google.on("POST", "/tasks/v1/lists/L0/tasks", lambda call: (503, {"error": {"message": "x"}}))
    failed = await made["tasks_add"].run(tasks=[{"title": "x"}])
    assert failed.is_error and "HTTP 503" in failed.content
    assert len(google.changes()) == 1


async def test_a_missing_scope_says_what_to_do(google: Fake, tmp_path: Path) -> None:
    google.on(
        "GET",
        "/tasks/v1/users/@me/lists",
        lambda call: (
            403,
            {
                "error": {
                    "message": "Insufficient",
                    "errors": [{"reason": "insufficientPermissions"}],
                }
            },
        ),
    )
    _, made = tools(tmp_path)
    result = await made["tasks_lists"].run()
    assert result.is_error and "tick every box" in result.content


async def test_the_token_goes_to_google_and_nowhere_else(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gt, "API", "https://collector.example/tasks/v1")
    _, made = tools(tmp_path)
    result = await made["tasks_lists"].run()
    assert result.is_error and "nothing was sent" in result.content
    assert google.calls == []
