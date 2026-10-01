"""Google Tasks: a login and six tools.

Every tool is `trusted_only` - offered only in a session an owner holds - and
`untrusted`, because a task's title and notes can come from somewhere else: a
task assigned from a Google Doc or a Chat space is written by whoever assigned
it. Looking is free. Every change is a card: an ordinary one for what Google
Tasks can put back - adding, completing, renaming, moving, hiding the completed
ones - and a confirm card, asked in every mode, for what it cannot: deleting a
task, or a list and every task in it. That card names each task that goes.

A due date is a date. Google Tasks keeps no time of day, so none is offered.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from atlas.sdk.oauth import LoginContext, OAuthClient, Tokens, connections, grant
from atlas.sdk.plugin_entry import Plugin, PluginContext
from atlas.sdk.runtime import CredentialError, ToolError, assert_active
from atlas.sdk.tool_plugin import Subject, Tool, ToolResult
from atlas.sdk.web import Response, WebError, request

PLUGIN = "google-tasks"
LOGIN = "google"
LOGIN_NAME = f"{PLUGIN}:{LOGIN}"
"""What a person types: `atlas auth login google-tasks:google`."""

DEFAULT_CLIENT_ID = ""
"""Atlas's own Google OAuth client (a Desktop-app client) with the Tasks API
enabled. Empty until it exists; `client_id` in settings is used instead, and
with neither, signing in says what is missing."""

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://tasks.googleapis.com/tasks/v1"
GOOGLE_HOST = "tasks.googleapis.com"
"""The only host the token is ever sent to: the one Google's discovery document
names for the Tasks API."""
SCOPES = ("https://www.googleapis.com/auth/tasks", "openid", "email")
"""Read and write the person's tasks - a sensitive scope, not a restricted one -
plus the address that labels a connection."""

USER_AGENT = "atlas-google-tasks"
RESPONSE_BYTES = 5_000_000
RETRY_CAP = 10.0
"""A read waits out a 429, a rate-limit 403 or a 5xx once, for at most this long."""

PAGE = 100
"""The most tasks Google sends in one page."""
SCAN_MAX = 5000
"""The most tasks one list is read to, to find a task or count what a deletion takes."""
ADD_MAX = 20
"""The most tasks one tasks_add makes."""
UPDATE_MAX = 25
"""The most tasks one tasks_update changes."""
REMOVE_MAX = 50
"""The most tasks one tasks_remove deletes."""
TITLE_MAX = 1024
NOTES_MAX = 8192
"""Google's own limits on a task's title and notes."""
LIST_TITLE_MAX = 1024
NAME_MAX = 120
NOTES_SHOWN = 300
"""How much of a task's notes a listing shows."""
CARD_NOTES = 500
"""How much of a task's new notes its card shows."""
DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")

NONE_WORDS = ("none", "no", "clear", "remove")
"""What `due` or `parent` says to take the value away."""

NOT_SIGNED_IN = f"not signed in to Google Tasks: atlas auth login {LOGIN_NAME}"
DATE_NOTE = "a date only - Google Tasks keeps no time of day"


# -- talking to Google ---------------------------------------------------------


class GoogleError(Exception):
    """A status Google answered with, as a sentence the model can repeat."""

    def __init__(self, status: int, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


def _error_of(response: Response) -> tuple[str, str]:
    """Google's message and its first reason (`rateLimitExceeded`, ...)."""
    try:
        data = json.loads(response.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "", ""
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return str(error or ""), ""
    reasons = [e.get("reason", "") for e in error.get("errors") or () if isinstance(e, dict)]
    message = str(error.get("message") or error.get("status") or "")
    return message, str(reasons[0] if reasons else "")


def _retry_after(response: Response) -> float:
    try:
        return min(max(float(response.header("retry-after") or 1.0), 0.0), RETRY_CAP)
    except ValueError:
        return 1.0


def _transient(status: int, reason: str) -> bool:
    return (
        status == 429
        or status >= 500
        or (status == 403 and reason in ("rateLimitExceeded", "userRateLimitExceeded"))
    )


def _explain(status: int, text: str, reason: str) -> str:
    detail = f": {text}" if text else ""
    if status == 401:
        return f"Google refused the sign-in (401): sign in again with atlas auth login {LOGIN_NAME}"
    if status == 403 and (
        reason == "insufficientPermissions" or "insufficient authentication" in text.lower()
    ):
        return (
            f"a permission was not granted (403{detail}) - sign in again with "
            f"atlas auth login {LOGIN_NAME} and tick every box"
        )
    if status == 403 and (reason == "accessNotConfigured" or "has not been used" in text):
        return (
            f"the Tasks API is not enabled for this Google client (403{detail}) - enable it "
            "in the client's Google Cloud project"
        )
    if status == 404:
        return f"not found (404){detail} - check the id; it may have been deleted"
    return f"HTTP {status} from Google Tasks{detail}"


def label_of(connection_id: str) -> str:
    """`google-tasks:work` -> `work`."""
    return connection_id.partition(":")[2] or connection_id


def _key(arguments: Mapping[str, Any]) -> str:
    return json.dumps(arguments, sort_keys=True, default=str)


class Listing:
    """One task list and the tasks read from it."""

    def __init__(self, info: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]]) -> None:
        self.info = dict(info)
        self.tasks = [dict(t) for t in tasks]
        self.id = str(info.get("id", ""))
        self.title = clean(info.get("title")) or "(no name)"

    def children(self, task_id: str) -> list[dict[str, Any]]:
        """A task's subtasks. Google nests one level deep."""
        return [t for t in self.tasks if t.get("parent") == task_id]

    def find(self, task_id: str) -> dict[str, Any] | None:
        return next((t for t in self.tasks if t.get("id") == task_id), None)


class Tasks:
    """What the tools share: the accounts, the requests, the lists."""

    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.workspace = Path(ctx.workspace)
        self.settings = dict(ctx.settings)

    def setting(self, key: str, default: Any) -> Any:
        value = self.settings.get(key)
        return default if value is None else value

    # -- accounts -------------------------------------------------------------

    def accounts(self) -> tuple[str, ...]:
        return connections(PLUGIN, workspace=self.workspace)

    def pick(self, account: str, *, write: bool) -> list[str]:
        """The only account, the named one, or - for a read - all of them. A
        change with more than one account and none named is refused."""
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
        """One request to Google. `path` is under the API. Raises `GoogleError`
        for a status that is not 2xx."""
        url = f"{API}{path}"
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != GOOGLE_HOST:
            # The token goes to Google and nowhere else.
            raise GoogleError(0, "that address is not Google Tasks - nothing was sent")
        query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        if query:
            url += "?" + urlencode(query, doseq=True)
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
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoogleError(response.status, f"Google sent something unreadable: {exc}") from None

    # -- lists ----------------------------------------------------------------

    async def lists(self, account: str) -> list[dict[str, Any]]:
        """Every task list the account has, page after page."""
        found: list[dict[str, Any]] = []
        token = ""
        while True:
            data = await self.call(
                account,
                "GET",
                "/users/@me/lists",
                params={"maxResults": PAGE, "pageToken": token},
            )
            items = data.get("items", []) if isinstance(data, dict) else []
            found.extend(item for item in items if isinstance(item, dict))
            token = str(data.get("nextPageToken") or "") if isinstance(data, dict) else ""
            if not token:
                return found

    async def default_list(self, account: str) -> dict[str, Any]:
        data = await self.call(account, "GET", "/users/@me/lists/@default")
        return data if isinstance(data, dict) else {}

    async def resolve(self, account: str, ref: str) -> dict[str, Any]:
        """A list by its id or its name - exactly, else the one name containing
        it. With nothing named, the account's default list."""
        wanted = str(ref or "").strip()
        if not wanted:
            return await self.default_list(account)
        known = await self.lists(account)
        for item in known:
            if item.get("id") == wanted:
                return item
        folded = wanted.casefold()
        exact = [i for i in known if clean(i.get("title")).casefold() == folded]
        if len(exact) == 1:
            return exact[0]
        partial = exact or [i for i in known if folded in clean(i.get("title")).casefold()]
        if len(partial) == 1:
            return partial[0]
        names = ", ".join(f'"{clean(i.get("title"), 60)}"' for i in known) or "none"
        if partial:
            raise ToolError(f"{wanted!r} could be more than one list - say which: {names}")
        raise ToolError(f"no task list {wanted!r} - the lists are: {names}")

    async def tasks(
        self,
        account: str,
        list_id: str,
        *,
        completed: bool = True,
        hidden: bool = True,
        due_min: str = "",
        due_max: str = "",
        limit: int = SCAN_MAX,
    ) -> tuple[list[dict[str, Any]], bool]:
        """A list's tasks, page after page. The flag says whether `limit` cut them."""
        found: list[dict[str, Any]] = []
        token = ""
        while True:
            data = await self.call(
                account,
                "GET",
                f"/lists/{quote(list_id, safe='@')}/tasks",
                params={
                    "maxResults": PAGE,
                    "pageToken": token,
                    "showCompleted": "true" if completed else "false",
                    # A task completed in Google's own apps is hidden as well;
                    # without this it would never be shown as done.
                    "showHidden": "true" if hidden else "false",
                    "dueMin": due_min,
                    "dueMax": due_max,
                },
            )
            items = data.get("items", []) if isinstance(data, dict) else []
            found.extend(item for item in items if isinstance(item, dict))
            token = str(data.get("nextPageToken") or "") if isinstance(data, dict) else ""
            if len(found) > limit:
                return found[:limit], True
            if not token:
                return found, False

    async def listing(self, account: str, info: Mapping[str, Any]) -> Listing:
        """Everything in one list, completed and hidden included."""
        found, _ = await self.tasks(account, str(info.get("id", "")))
        return Listing(info, found)

    async def everything(self, account: str, ref: str = "") -> list[Listing]:
        """The named list, or - with none named - every list, each read whole."""
        if str(ref or "").strip():
            return [await self.listing(account, await self.resolve(account, ref))]
        return [await self.listing(account, info) for info in await self.lists(account)]

    def record(self, event: str, detail: str, arguments: Mapping[str, Any]) -> None:
        """What changed, by id, on the trail - never a task's words."""
        with contextlib.suppress(Exception):
            self.ctx.audit(event, detail, arguments=dict(arguments))


def locate(listings: Sequence[Listing], task_id: str) -> tuple[Listing, dict[str, Any]]:
    """Which list a task is in, from what was read."""
    wanted = str(task_id or "").strip()
    if not wanted:
        raise ToolError("say which task, by the id tasks_show showed")
    for listing in listings:
        found = listing.find(wanted)
        if found is not None:
            return listing, found
    raise ToolError(f"no task {wanted!r} - tasks_show lists them with their ids")


# -- showing things ------------------------------------------------------------


def clean(text: Any, limit: int = NAME_MAX) -> str:
    """One line of someone else's words, capped."""
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def due_date(task: Mapping[str, Any]) -> date | None:
    """A task's due date. Read from the text itself, never moved into this
    machine's zone: Google stores a date as midnight UTC, and converting it would
    put it on the day before anywhere west of Greenwich."""
    stamp = str(task.get("due") or "")[:10]
    if not DATE_ONLY.fullmatch(stamp):
        return None
    try:
        return date.fromisoformat(stamp)
    except ValueError:
        return None


def day(value: date) -> str:
    """`Fri 3 Oct`, with the year when it is not this one."""
    year = f" {value.year}" if value.year != date.today().year else ""
    return f"{value.strftime('%a')} {value.day} {value.strftime('%b')}{year}"


def when(stamp: Any) -> str:
    """A moment, in this machine's zone, as a day."""
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return str(stamp)
    return day(moment.date())


def is_done(task: Mapping[str, Any]) -> bool:
    return task.get("status") == "completed"


def task_line(task: Mapping[str, Any], indent: str = "  ") -> str:
    """One task a person can recognise, ending in its id, and its notes below."""
    parts = [f"[{'x' if is_done(task) else ' '}] {clean(task.get('title')) or '(no title)'}"]
    due = due_date(task)
    if due is not None:
        late = not is_done(task) and due < date.today()
        parts.append(f"due {day(due)}" + (" - overdue" if late else ""))
    if is_done(task) and task.get("completed"):
        parts.append(f"done {when(task['completed'])}")
    if task.get("assignmentInfo"):
        surface = str((task.get("assignmentInfo") or {}).get("surfaceType") or "").lower()
        parts.append("assigned from " + ("a Doc" if surface == "document" else "a Chat space"))
    line = f"{indent}{' · '.join(parts)}  [id: {task.get('id', '')}]"
    notes = clean(task.get("notes"), NOTES_SHOWN)
    return line + (f"\n{indent}    notes: {notes}" if notes else "")


def ordered(tasks: Sequence[Mapping[str, Any]]) -> list[tuple[Mapping[str, Any], int]]:
    """Tasks as the list shows them - by position, each subtask under its
    parent - with their depth. A subtask whose parent is not in `tasks` stands
    on its own."""
    present = {t.get("id") for t in tasks}

    def position(task: Mapping[str, Any]) -> str:
        return str(task.get("position") or "")

    tops = sorted((t for t in tasks if t.get("parent") not in present), key=position)
    rows: list[tuple[Mapping[str, Any], int]] = []
    for top in tops:
        rows.append((top, 0))
        subs = [t for t in tasks if t.get("parent") == top.get("id")]
        rows.extend((sub, 1) for sub in sorted(subs, key=position))
    return rows


def titles(tasks: Sequence[Mapping[str, Any]], most: int = 5) -> str:
    shown = [f'"{clean(t.get("title"), 60)}"' for t in tasks[:most]]
    more = f" and {len(tasks) - most} more" if len(tasks) > most else ""
    return ", ".join(shown) + more


def parse_due(value: Any) -> str:
    """`YYYY-MM-DD` as Google wants it: midnight UTC, which is how it stores a date."""
    text = str(value or "").strip()
    if not DATE_ONLY.fullmatch(text):
        raise ToolError(
            f"due {text!r} is not a date - write YYYY-MM-DD ({DATE_NOTE}, so give none)"
        )
    try:
        date.fromisoformat(text)
    except ValueError:
        raise ToolError(f"due {text!r} is not a real date") from None
    return f"{text}T00:00:00.000Z"


def title_of(value: Any, what: str = "title") -> str:
    title = " ".join(str(value or "").split())
    if not title:
        raise ToolError(f"give it a {what}")
    if len(title) > TITLE_MAX:
        raise ToolError(f"that {what} is {len(title)} characters - Google allows {TITLE_MAX}")
    return title


def notes_of(value: Any) -> str:
    notes = str(value or "").strip()
    if len(notes) > NOTES_MAX:
        raise ToolError(f"those notes are {len(notes)} characters - Google allows {NOTES_MAX}")
    return notes


def is_none(value: Any) -> bool:
    return str(value or "").strip().lower() in NONE_WORDS


def card_notes(notes: str) -> str:
    shown = notes if len(notes) <= CARD_NOTES else notes[:CARD_NOTES] + "…"
    return " ".join(shown.split())


# -- the tools -----------------------------------------------------------------


ACCOUNT = {
    "type": "string",
    "description": (
        "Which signed-in Google account, by its label. Needed for a change when more than "
        "one is signed in."
    ),
}
LIST_REF = {
    "type": "string",
    "description": "A task list, by its name or the id tasks_lists showed. Default: the main list.",
}
TASK_ID = {"type": "string", "description": "The task's id, as tasks_show showed it."}
DUE = {
    "type": "string",
    "description": f"YYYY-MM-DD - {DATE_NOTE}.",
}


class TasksTool(Tool):
    """What all six share: owner-only, marked untrusted, failures as sentences."""

    trusted_only = True
    untrusted = True

    def __init__(self, tasks: Tasks) -> None:
        self.tasks = tasks

    async def run(self, **arguments: Any) -> ToolResult:
        try:
            assert_active()
            said = await self.act(**arguments)
            return said if isinstance(said, ToolResult) else ToolResult.ok(said)
        except (CredentialError, ToolError, GoogleError, WebError) as exc:
            return ToolResult.error(str(exc))

    async def act(self, *args: Any, **arguments: Any) -> Any:
        raise NotImplementedError


class Lists(TasksTool):
    name = "tasks_lists"
    description = (
        "The person's Google Tasks lists - My Tasks, Errands and so on - each with its id for "
        "the other tasks tools. The main list, where a new task goes by default, is marked."
    )
    parameters = {"type": "object", "properties": {"account": ACCOUNT}, "required": []}

    async def act(self, account: str = "") -> str:
        accounts = self.tasks.pick(account, write=False)
        several = len(accounts) > 1
        lines: list[str] = []
        for who in accounts:
            main = str((await self.tasks.default_list(who)).get("id") or "")
            for item in await self.tasks.lists(who):
                parts = [clean(item.get("title")) or "(no name)"]
                if item.get("id") == main:
                    parts.append("the main list")
                if item.get("updated"):
                    parts.append(f"changed {when(item['updated'])}")
                tags = f"id: {item.get('id', '')}" + (f" · {label_of(who)}" if several else "")
                lines.append(" · ".join(parts) + f"  [{tags}]")
        if not lines:
            return "No task lists."
        return f"{len(lines)} list(s):\n" + "\n".join(lines)


STATUSES = ("open", "completed", "all")


class Show(TasksTool):
    name = "tasks_show"
    description = (
        "What is on the person's to-do lists: one list, or every list. Open tasks by default; "
        "`status` shows completed ones or both. Each task shows its due date, whether it is "
        "overdue, its notes and its subtasks (indented), and ends with its id for "
        "tasks_update and tasks_remove. Due dates are dates only - Google Tasks keeps no time. "
        "A due filter leaves out tasks with no due date."
    )
    parameters = {
        "type": "object",
        "properties": {
            "list": {
                "type": "string",
                "description": "A list's name or id. Default: every list.",
            },
            "status": {"type": "string", "enum": list(STATUSES)},
            "due_before": {"type": "string", "description": "YYYY-MM-DD: due on or before."},
            "due_after": {"type": "string", "description": "YYYY-MM-DD: due on or after."},
            "query": {
                "type": "string",
                "description": "Words to find in a task's title or notes.",
            },
            "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
            "account": ACCOUNT,
        },
        "required": [],
    }

    async def act(
        self,
        list: str = "",
        status: str = "open",
        due_before: str = "",
        due_after: str = "",
        query: str = "",
        max_results: int = 0,
        account: str = "",
    ) -> str:
        status = status or "open"
        if status not in STATUSES:
            raise ToolError(f"status is one of {', '.join(STATUSES)}")
        limit = min(max(int(max_results or self.tasks.setting("max_results", 100)), 1), 1000)
        due_min = parse_due(due_after) if due_after.strip() else ""
        due_max = ""
        if due_before.strip():
            # On or before: a date is stored as midnight, so the bound is the day's end.
            due_max = parse_due(due_before)[:10] + "T23:59:59.999Z"
        words = [w.casefold() for w in query.split()]
        accounts = self.tasks.pick(account, write=False)
        several = len(accounts) > 1
        blocks: list[str] = []
        shown = 0
        cut = False
        missing: list[str] = []
        for who in accounts:
            if list.strip():
                try:
                    infos = [await self.tasks.resolve(who, list)]
                except ToolError as exc:
                    if several:
                        missing.append(str(exc))
                        continue
                    raise
            else:
                infos = await self.tasks.lists(who)
            for info in infos:
                found, more = await self.tasks.tasks(
                    who,
                    str(info.get("id", "")),
                    completed=status != "open",
                    hidden=status != "open",
                    due_min=due_min,
                    due_max=due_max,
                    limit=limit,
                )
                cut = cut or more
                if status == "completed":
                    found = [t for t in found if is_done(t)]
                elif status == "open":
                    found = [t for t in found if not is_done(t)]
                if words:
                    found = [
                        t
                        for t in found
                        if all(
                            w in f"{t.get('title', '')} {t.get('notes', '')}".casefold()
                            for w in words
                        )
                    ]
                if not found:
                    continue
                room = limit - shown
                if room <= 0:
                    cut = True
                    break
                rows = ordered(found)
                if len(rows) > room:
                    rows, cut = rows[:room], True
                shown += len(rows)
                tags = f"list id: {info.get('id', '')}" + (f" · {label_of(who)}" if several else "")
                head = f"{clean(info.get('title')) or '(no name)'} - {len(rows)} task(s)  [{tags}]"
                body = [task_line(t, "  " * (depth + 1)) for t, depth in rows]
                blocks.append("\n".join([head, *body]))
        if not blocks:
            if missing and len(missing) == len(accounts):
                raise ToolError(missing[0])
            which = {"open": "open tasks", "completed": "completed tasks", "all": "tasks"}[status]
            return f"No {which}" + (" match." if words or due_min or due_max else ".")
        tail = f"\n(showing the first {limit} - narrow it to see the rest)" if cut else ""
        return "\n\n".join(blocks) + tail


# -- changing things -----------------------------------------------------------


class Plan:
    """What a change will do, worked out once for the card and used again to run."""

    def __init__(self, account: str, card: str, *, confirm: bool = False) -> None:
        self.account = account
        self.card = card
        self.confirm = confirm
        self.steps: list[dict[str, Any]] = []


class ChangeTool(TasksTool):
    """A change: gated, carded from what is really there, and confirm-tier when
    Google Tasks cannot put it back."""

    gated = True
    action = ""

    def __init__(self, tasks: Tasks) -> None:
        super().__init__(tasks)
        self._looked: dict[str, tuple[float, Plan]] = {}
        """What `subject` worked out, keyed by the call's arguments, so `run` does
        what the card said - the same tasks - and not a later look."""

    async def subject(self, arguments: Mapping[str, Any]) -> Subject | None:
        try:
            plan = await self.plan(arguments)
        except (CredentialError, ToolError):
            return None  # `run` fails the same way, before it changes anything
        except (GoogleError, WebError) as exc:
            # Still a card, and the strict one: a look that failed is no reason
            # to skip the question.
            summary = f"{self.action} in Google Tasks (could not look first: {exc})"
            return Subject(tool=self.name, action=self.action, summary=summary, confirm=True)
        self._looked[_key(arguments)] = (time.monotonic(), plan)
        return Subject(tool=self.name, action=self.action, summary=plan.card, confirm=plan.confirm)

    async def act(self, **arguments: Any) -> str:
        seen = self._looked.pop(_key(arguments), None)
        plan = seen[1] if seen is not None and time.monotonic() - seen[0] < 900 else None
        return await self.carry_out(plan or await self.plan(arguments), arguments)

    def account(self, arguments: Mapping[str, Any]) -> str:
        return self.tasks.pick(str(arguments.get("account", "") or ""), write=True)[0]

    async def plan(self, arguments: Mapping[str, Any]) -> Plan:
        raise NotImplementedError

    async def carry_out(self, plan: Plan, arguments: Mapping[str, Any]) -> str:
        raise NotImplementedError


def _path(list_id: str, task_id: str = "") -> str:
    base = f"/lists/{quote(list_id, safe='@')}/tasks"
    return f"{base}/{quote(task_id, safe='')}" if task_id else base


def _done_so_far(done: Sequence[str]) -> str:
    return ", ".join(f'"{d}"' for d in done) or "nothing"


class Add(ChangeTool):
    name = "tasks_add"
    action = "add"
    description = (
        "Add tasks to a Google Tasks list - the main list unless `list` names another. Each "
        "task has a title, and optionally notes, a due date (YYYY-MM-DD: a date only - Google "
        "Tasks keeps no time of day, so a reminder at a time is not possible here) and a "
        f"parent task's id to make it a subtask. At most {ADD_MAX} at once. New tasks go to "
        "the top of the list. The person is shown each one first."
    )
    parameters = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "notes": {"type": "string"},
                        "due": DUE,
                        "parent": {
                            "type": "string",
                            "description": "A task id in the same list: this becomes its subtask.",
                        },
                    },
                    "required": ["title"],
                },
            },
            "list": LIST_REF,
            "account": ACCOUNT,
        },
        "required": ["tasks"],
    }

    async def plan(self, arguments: Mapping[str, Any]) -> Plan:
        given = [t for t in arguments.get("tasks") or () if isinstance(t, Mapping)]
        if not given:
            raise ToolError("name at least one task, with a title")
        if len(given) > ADD_MAX:
            raise ToolError(f"{len(given)} tasks - at most {ADD_MAX} at once")
        steps: list[dict[str, Any]] = []
        for item in given:
            body: dict[str, Any] = {"title": title_of(item.get("title"))}
            notes = notes_of(item.get("notes"))
            if notes:
                body["notes"] = notes
            if str(item.get("due") or "").strip():
                body["due"] = parse_due(item.get("due"))
            steps.append({"body": body, "parent": str(item.get("parent") or "").strip()})
        account = self.account(arguments)
        info = await self.tasks.resolve(account, str(arguments.get("list", "") or ""))
        listing: Listing | None = None
        if any(s["parent"] for s in steps):
            listing = await self.tasks.listing(account, info)
        title = clean(info.get("title")) or "(no name)"
        lines = [f'Add {len(steps)} task(s) to "{title}" (Google Tasks)']
        for step in steps:
            body = step["body"]
            line = f"  {clean(body['title'])}"
            if "due" in body:
                line += f" · due {day(date.fromisoformat(body['due'][:10]))}"
            if step["parent"]:
                parent = listing.find(step["parent"]) if listing else None
                if parent is None:
                    raise ToolError(
                        f"no task {step['parent']!r} in {title!r} to put a subtask under"
                    )
                if parent.get("parent"):
                    raise ToolError(
                        f"{clean(parent.get('title'))!r} is itself a subtask - Google Tasks "
                        "nests one level deep"
                    )
                line += f' · a subtask of "{clean(parent.get("title"), 60)}"'
            lines.append(line)
            if body.get("notes"):
                lines.append(f"    notes: {card_notes(body['notes'])}")
        if any("due" in s["body"] for s in steps):
            lines.append(f"Due dates are {DATE_NOTE}.")
        plan = Plan(account, "\n".join(lines))
        plan.steps = [{**s, "list": str(info.get("id", "")), "list_title": title} for s in steps]
        return plan

    async def carry_out(self, plan: Plan, arguments: Mapping[str, Any]) -> str:
        made: list[dict[str, Any]] = []
        # Each new task goes to the top, so the last is added first and they
        # read in the order asked for.
        for step in reversed(plan.steps):
            assert_active()
            try:
                task = await self.tasks.call(
                    plan.account,
                    "POST",
                    _path(step["list"]),
                    params={"parent": step["parent"]},
                    body=step["body"],
                    retry=False,
                )
            except (GoogleError, WebError) as exc:
                done = _done_so_far([clean(m.get("title")) for m in made])
                raise ToolError(
                    f"added {done}; then {clean(step['body']['title'])!r} failed: {exc}"
                ) from None
            made.append(task if isinstance(task, dict) else {})
        made.reverse()
        self.tasks.record(
            "tasks_added",
            f"{len(made)} task(s)",
            {
                "account": label_of(plan.account),
                "list": plan.steps[0]["list"],
                "tasks": [str(m.get("id", "")) for m in made],
            },
        )
        title = plan.steps[0]["list_title"]
        return f'Added to "{title}":\n' + "\n".join(task_line(m) for m in made)


class Update(ChangeTool):
    name = "tasks_update"
    action = "update"
    description = (
        "Change tasks in Google Tasks, by id: mark done or open again, rename, change or clear "
        "the notes or the due date (YYYY-MM-DD, or none - a date only; Google Tasks keeps no "
        "time of day), move to another list, make it a subtask of another task or a top-level "
        f"task again, or put it after another task. At most {UPDATE_MAX} tasks at once. The "
        "person is shown every change first."
    )
    parameters = {
        "type": "object",
        "properties": {
            "changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "task_id": TASK_ID,
                        "status": {"type": "string", "enum": ["done", "open"]},
                        "title": {"type": "string"},
                        "notes": {"type": "string", "description": "New notes; empty clears."},
                        "due": {
                            "type": "string",
                            "description": f"YYYY-MM-DD, or none to clear - {DATE_NOTE}.",
                        },
                        "to_list": {
                            "type": "string",
                            "description": "Move it to this list (name or id), at the top.",
                        },
                        "parent": {
                            "type": "string",
                            "description": "A task id to nest it under, or none for top level.",
                        },
                        "after": {
                            "type": "string",
                            "description": "A task id to put it after, or first for the top.",
                        },
                    },
                    "required": ["task_id"],
                },
            },
            "list": {
                "type": "string",
                "description": "The list the tasks are in, if known - it saves reading them all.",
            },
            "account": ACCOUNT,
        },
        "required": ["changes"],
    }

    async def plan(self, arguments: Mapping[str, Any]) -> Plan:
        given = [c for c in arguments.get("changes") or () if isinstance(c, Mapping)]
        if not given:
            raise ToolError("say which tasks to change, by id")
        if len(given) > UPDATE_MAX:
            raise ToolError(f"{len(given)} tasks - at most {UPDATE_MAX} at once")
        ids = [str(c.get("task_id") or "").strip() for c in given]
        if len(set(ids)) != len(ids):
            raise ToolError("a task is named twice - give all its changes in one entry")
        account = self.account(arguments)
        listings = await self.tasks.everything(account, str(arguments.get("list", "") or ""))
        lists_by_ref: dict[str, dict[str, Any]] = {}
        lines: list[str] = []
        steps: list[dict[str, Any]] = []
        for change in given:
            listing, task = locate(listings, str(change.get("task_id") or ""))
            name = clean(task.get("title"), 60) or "(no title)"
            body: dict[str, Any] = {}
            said: list[str] = []
            status = str(change.get("status") or "")
            if status == "done" and not is_done(task):
                body["status"] = "completed"
                said.append("mark done")
            elif status == "open" and is_done(task):
                body["status"], body["completed"] = "needsAction", None
                said.append("mark not done")
            elif status and status not in ("done", "open"):
                raise ToolError("status is done or open")
            if "title" in change and change.get("title") is not None:
                new = title_of(change.get("title"))
                if new != task.get("title"):
                    body["title"] = new
                    said.append(f'rename → "{clean(new, 60)}"')
            if "notes" in change and change.get("notes") is not None:
                notes = notes_of(change.get("notes"))
                if notes != str(task.get("notes") or ""):
                    body["notes"] = notes or None
                    said.append(f"notes → {card_notes(notes)}" if notes else "clear the notes")
            if str(change.get("due") or "").strip():
                before = due_date(task)
                was = day(before) if before else "none"
                if is_none(change.get("due")):
                    if before is not None:
                        body["due"] = None
                        said.append(f"due {was} → none")
                else:
                    due = parse_due(change.get("due"))
                    if before is None or before.isoformat() != due[:10]:
                        body["due"] = due
                        said.append(f"due {was} → {day(date.fromisoformat(due[:10]))}")
            move = await self.move(account, listing, task, change, listings, lists_by_ref)
            if move:
                said.append(move.pop("said"))
            if not said:
                lines.append(f'  "{name}" ({listing.title}): already so - unchanged')
                continue
            lines.append(f'  "{name}" ({listing.title}): ' + "; ".join(said))
            steps.append(
                {
                    "list": listing.id,
                    "task": str(task["id"]),
                    "title": name,
                    "body": body,
                    "move": move,
                }
            )
        if not steps:
            raise ToolError("nothing to change:\n" + "\n".join(lines))
        head = f"Change {len(steps)} task(s) in Google Tasks"
        tail = [f"Due dates are {DATE_NOTE}."] if any("due" in s["body"] for s in steps) else []
        plan = Plan(account, "\n".join([head, *lines, *tail]))
        plan.steps = steps
        return plan

    async def move(
        self,
        account: str,
        listing: Listing,
        task: Mapping[str, Any],
        change: Mapping[str, Any],
        listings: Sequence[Listing],
        lists_by_ref: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """The move a change asks for, as Google's `move` takes it, and what the
        card says of it - or None."""
        to_list = str(change.get("to_list") or "").strip()
        parent = str(change.get("parent") or "").strip()
        after = str(change.get("after") or "").strip()
        if not (to_list or parent or after):
            return None
        target = listing
        destination = ""
        said: list[str] = []
        if to_list:
            if to_list not in lists_by_ref:
                lists_by_ref[to_list] = await self.tasks.resolve(account, to_list)
            info = lists_by_ref[to_list]
            if info.get("id") != listing.id:
                if task.get("assignmentInfo"):
                    raise ToolError(
                        f"{clean(task.get('title'))!r} was assigned from a Doc or a Chat space "
                        "and cannot leave its list"
                    )
                destination = str(info["id"])
                target = next((x for x in listings if x.id == destination), None) or (
                    await self.tasks.listing(account, info)
                )
                said.append(f'move to "{target.title}"')
                subs = listing.children(str(task["id"]))
                if subs:
                    said.append(f"its {len(subs)} subtask(s) go with it")
        # A move without `parent` makes a task top-level, so a task that only
        # changes place keeps the parent it has.
        new_parent = "" if destination else str(task.get("parent") or "")
        if parent:
            if is_none(parent) or parent in ("top", "top-level"):
                new_parent = ""
                if task.get("parent"):
                    said.append("make it a top-level task")
            else:
                above = target.find(parent)
                if above is None or parent == task.get("id"):
                    raise ToolError(f"no task {parent!r} in {target.title!r} to nest it under")
                if above.get("parent"):
                    raise ToolError(
                        f"{clean(above.get('title'))!r} is itself a subtask - Google Tasks "
                        "nests one level deep"
                    )
                if listing.children(str(task["id"])):
                    raise ToolError(
                        f"{clean(task.get('title'))!r} has subtasks of its own, so it cannot "
                        "become a subtask"
                    )
                if destination or parent != task.get("parent"):
                    said.append(f'make it a subtask of "{clean(above.get("title"), 60)}"')
                new_parent = parent
        previous = ""
        if after:
            if after.lower() in ("first", "top", "start"):
                said.append("put it first")
            else:
                before = target.find(after)
                if before is None or after == task.get("id"):
                    raise ToolError(f"no task {after!r} in {target.title!r} to put it after")
                if str(before.get("parent") or "") != new_parent:
                    raise ToolError(
                        f"{clean(before.get('title'))!r} is not at the same level - put a task "
                        "after one that shares its parent"
                    )
                previous = after
                said.append(f'put it after "{clean(before.get("title"), 60)}"')
        if not said:
            return None
        return {
            "parent": new_parent,
            "previous": previous,
            "destinationTasklist": destination,
            "said": "; ".join(said),
        }

    async def carry_out(self, plan: Plan, arguments: Mapping[str, Any]) -> str:
        done: list[str] = []
        lines: list[str] = []
        for step in plan.steps:
            assert_active()
            try:
                task: Any = {}
                if step["body"]:
                    task = await self.tasks.call(
                        plan.account,
                        "PATCH",
                        _path(step["list"], step["task"]),
                        body=step["body"],
                        retry=False,
                    )
                if step["move"]:
                    task = await self.tasks.call(
                        plan.account,
                        "POST",
                        _path(step["list"], step["task"]) + "/move",
                        params=dict(step["move"]),
                        retry=False,
                    )
            except (GoogleError, WebError) as exc:
                raise ToolError(
                    f"changed {_done_so_far(done)}; then {step['title']!r} failed: {exc}"
                ) from None
            done.append(step["title"])
            if isinstance(task, dict) and task.get("id"):
                lines.append(task_line(task))
        self.tasks.record(
            "tasks_updated",
            f"{len(done)} task(s)",
            {
                "account": label_of(plan.account),
                "tasks": [s["task"] for s in plan.steps],
                "changed": sorted({k for s in plan.steps for k in s["body"]}),
                "moved": [s["task"] for s in plan.steps if s["move"]],
            },
        )
        return f"Changed {len(done)} task(s):\n" + "\n".join(lines)


class Remove(ChangeTool):
    name = "tasks_remove"
    action = "remove"
    description = (
        f"Delete tasks from Google Tasks, by id - at most {REMOVE_MAX} at once; a task's "
        "subtasks go with it - or clear a list's completed tasks (`clear_completed`, a list's "
        "name or id). Deleting cannot be undone, so the person is shown every task and asked "
        "in every mode. Clearing hides the completed tasks rather than deleting them."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task_ids": {"type": "array", "items": {"type": "string"}},
            "list": {
                "type": "string",
                "description": "The list the tasks are in, if known - it saves reading them all.",
            },
            "clear_completed": {
                "type": "string",
                "description": "A list's name or id: hide every completed task in it.",
            },
            "account": ACCOUNT,
        },
        "required": [],
    }

    async def plan(self, arguments: Mapping[str, Any]) -> Plan:
        ids = list(
            dict.fromkeys(str(i).strip() for i in arguments.get("task_ids") or () if str(i).strip())
        )
        clear = str(arguments.get("clear_completed", "") or "").strip()
        if not ids and not clear:
            raise ToolError("say which tasks to delete, by id, or which list to clear")
        if len(ids) > REMOVE_MAX:
            raise ToolError(f"{len(ids)} tasks - at most {REMOVE_MAX} at once")
        account = self.account(arguments)
        lines: list[str] = []
        steps: list[dict[str, Any]] = []
        if ids:
            listings = await self.tasks.everything(account, str(arguments.get("list", "") or ""))
            chosen: set[str] = set()
            for task_id in ids:
                listing, task = locate(listings, task_id)
                if task_id in chosen:
                    continue  # already going, with the task above it
                subs = [s for s in listing.children(task_id) if s["id"] not in chosen]
                line = f"  {task_line(task, '').splitlines()[0]} ({listing.title})"
                for sub in subs:
                    line += f"\n    and its subtask {task_line(sub, '').splitlines()[0]}"
                lines.append(line)
                # Subtasks first, so nothing is left behind without the task it was under.
                for item in [*subs, task]:
                    chosen.add(str(item["id"]))
                    steps.append(
                        {
                            "verb": "delete",
                            "list": listing.id,
                            "task": str(item["id"]),
                            "title": clean(item.get("title"), 60),
                        }
                    )
            lines.insert(0, f"Delete {len(steps)} task(s) from Google Tasks - for good:")
            lines.append("Google Tasks cannot undo this.")
        if clear:
            info = await self.tasks.resolve(account, clear)
            found, _ = await self.tasks.tasks(account, str(info.get("id", "")), hidden=False)
            finished = [t for t in found if is_done(t)]
            title = clean(info.get("title")) or "(no name)"
            if not finished and not steps:
                raise ToolError(f'"{title}" has no completed tasks showing to clear')
            if finished:
                lines.append(
                    f'Clear {len(finished)} completed task(s) from "{title}": {titles(finished)}'
                    " - hidden from the list, not deleted"
                )
                steps.append({"verb": "clear", "list": str(info["id"]), "title": title})
        plan = Plan(account, "\n".join(lines), confirm=any(s["verb"] == "delete" for s in steps))
        plan.steps = steps
        return plan

    async def carry_out(self, plan: Plan, arguments: Mapping[str, Any]) -> str:
        deleted: list[str] = []
        cleared = ""
        for step in plan.steps:
            assert_active()
            try:
                if step["verb"] == "clear":
                    await self.tasks.call(
                        plan.account,
                        "POST",
                        f"/lists/{quote(step['list'], safe='@')}/clear",
                        retry=False,
                    )
                    cleared = step["title"]
                    continue
                await self.tasks.call(
                    plan.account, "DELETE", _path(step["list"], step["task"]), retry=False
                )
            except GoogleError as exc:
                if exc.status == 404 and step["verb"] == "delete":
                    deleted.append(step["title"])  # already gone, with the task it was under
                    continue
                raise ToolError(
                    f"deleted {_done_so_far(deleted)}; then {step['title']!r} failed: {exc}"
                ) from None
            except WebError as exc:
                raise ToolError(
                    f"deleted {_done_so_far(deleted)}; then {step['title']!r} failed: {exc}"
                ) from None
            deleted.append(step["title"])
        self.tasks.record(
            "tasks_removed",
            f"{len(deleted)} deleted" + (", completed cleared" if cleared else ""),
            {
                "account": label_of(plan.account),
                "deleted": [s["task"] for s in plan.steps if s["verb"] == "delete"],
                "cleared": [s["list"] for s in plan.steps if s["verb"] == "clear"],
            },
        )
        said: list[str] = []
        if deleted:
            said.append(f"Deleted {len(deleted)} task(s): " + ", ".join(f'"{d}"' for d in deleted))
        if cleared:
            said.append(f'Cleared the completed tasks from "{cleared}".')
        return "\n".join(said)


MANAGE = ("create", "rename", "delete")


class ListManage(ChangeTool):
    name = "tasks_list_manage"
    action = "manage lists"
    description = (
        "Make a new Google Tasks list, rename one, or delete one. Deleting a list deletes every "
        "task in it and cannot be undone, so the person is told how many tasks go and asked in "
        "every mode. The main list cannot be deleted."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(MANAGE)},
            "list": {"type": "string", "description": "For rename or delete: its name or id."},
            "title": {"type": "string", "description": "For create or rename: the name."},
            "account": ACCOUNT,
        },
        "required": ["action"],
    }

    async def plan(self, arguments: Mapping[str, Any]) -> Plan:
        action = str(arguments.get("action", ""))
        if action not in MANAGE:
            raise ToolError(f"action is one of {', '.join(MANAGE)}")
        ref = str(arguments.get("list", "") or "").strip()
        if action != "create" and not ref:
            raise ToolError(f"{action} needs a list, by its name or id")
        title = ""
        if action != "delete":
            title = title_of(arguments.get("title"), "name")
            if len(title) > LIST_TITLE_MAX:
                raise ToolError(f"a list's name is at most {LIST_TITLE_MAX} characters")
        account = self.account(arguments)
        if action == "create":
            plan = Plan(account, f'Create a task list "{clean(title)}" in Google Tasks')
            plan.steps = [{"title": title}]
            return plan
        info = await self.tasks.resolve(account, ref)
        old = clean(info.get("title")) or "(no name)"
        if action == "rename":
            if title == info.get("title"):
                raise ToolError(f'the list is already called "{old}"')
            plan = Plan(account, f'Rename the task list "{old}" → "{clean(title)}"')
            plan.steps = [{"list": str(info["id"]), "title": title, "old": old}]
            return plan
        main = await self.tasks.default_list(account)
        if info.get("id") == main.get("id"):
            raise ToolError(f'"{old}" is the main list, which Google Tasks does not let go')
        found, more = await self.tasks.tasks(account, str(info["id"]))
        count = f"more than {len(found)}" if more else str(len(found))
        if not found:
            card = f'Delete the task list "{old}" - it is empty. Google Tasks cannot undo this.'
        else:
            open_ = [t for t in found if not is_done(t)]
            card = (
                f'Delete the task list "{old}" and all {count} task(s) in it - '
                f"{len(open_)} still open, {len(found) - len(open_)} completed - for good.\n"
                f"  Open: {titles(open_, 10) or 'none'}\n"
                "Google Tasks cannot undo this."
            )
            if any(t.get("assignmentInfo") for t in found):
                card += " Tasks assigned from a Doc or a Chat space are deleted there too."
        plan = Plan(account, card, confirm=True)
        plan.steps = [{"list": str(info["id"]), "old": old, "count": len(found)}]
        return plan

    async def carry_out(self, plan: Plan, arguments: Mapping[str, Any]) -> str:
        action = str(arguments["action"])
        step = plan.steps[0]
        if action == "create":
            made = await self.tasks.call(
                plan.account, "POST", "/users/@me/lists", body={"title": step["title"]}, retry=False
            )
            made = made if isinstance(made, dict) else {}
            self.tasks.record(
                "tasklist_created", "", {"account": label_of(plan.account), "list": made.get("id")}
            )
            return f'Created the list "{clean(made.get("title"))}"  [id: {made.get("id", "")}]'
        path = f"/users/@me/lists/{quote(step['list'], safe='@')}"
        if action == "rename":
            await self.tasks.call(
                plan.account, "PATCH", path, body={"title": step["title"]}, retry=False
            )
            self.tasks.record(
                "tasklist_renamed", "", {"account": label_of(plan.account), "list": step["list"]}
            )
            return f'Renamed "{step["old"]}" to "{clean(step["title"])}".'
        await self.tasks.call(plan.account, "DELETE", path, retry=False)
        self.tasks.record(
            "tasklist_deleted",
            f"{step['count']} task(s)",
            {"account": label_of(plan.account), "list": step["list"]},
        )
        return f'Deleted the list "{step["old"]}" and the {step["count"]} task(s) in it.'


# -- signing in ----------------------------------------------------------------


def _label(response: Mapping[str, Any]) -> Mapping[str, str]:
    """The account's address, from the `id_token` the token endpoint sent.
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
        label="Google Tasks",
    )


def no_client(context: LoginContext) -> Tokens:
    """What signing in does before any client id exists: say so, and stop."""
    raise CredentialError(
        "Google Tasks has no OAuth client id yet. Create a Desktop-app client in a Google "
        "Cloud project with the Tasks API enabled, then set "
        "plugins_settings.google-tasks.client_id to its id in config.json."
    )


class GoogleTasksPlugin(Plugin):
    name = PLUGIN
    description = "Google Tasks: see your to-do lists; add, finish, move and remove with a yes."

    def register(self, ctx: PluginContext) -> None:
        client_id = str(ctx.setting("client_id", "") or "").strip() or DEFAULT_CLIENT_ID
        ctx.register_login(LOGIN, client(client_id) if client_id else no_client)
        tasks = Tasks(ctx)
        for tool in (Lists, Show, Add, Update, Remove, ListManage):
            ctx.register_tool(tool(tasks), toolset="Google Tasks")
