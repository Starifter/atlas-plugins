"""Google Drive: a login and eight tools (`docs/spec/google-drive.md`).

Every tool is `trusted_only` - offered only in a session an owner holds - and
`untrusted`, because a file's name and contents are whatever its author wrote,
and anybody can share a file with anybody. Searching, reading and downloading
are free. Every change is a card: an ordinary one for a change that stays where
the person already keeps things, a confirm card - asked in every mode - for one
that lets somebody new see something, and that card names who.

Nothing is deleted for good. Trash, which Drive empties after 30 days, is as
far as a file goes.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import mimetypes
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlsplit

from atlas.sdk.auth import credentials_in
from atlas.sdk.oauth import LoginContext, OAuthClient, Tokens, connections, grant
from atlas.sdk.plugin_entry import Plugin, PluginContext
from atlas.sdk.runtime import CredentialError, NetworkPolicyError, ToolError, assert_active
from atlas.sdk.tool_plugin import ImageResult, Subject, Tool, ToolResult
from atlas.sdk.web import Response, WebError, extract, get, request

PLUGIN = "google-drive"
LOGIN = "google"
LOGIN_NAME = f"{PLUGIN}:{LOGIN}"
"""What a person types: `atlas auth login google-drive:google`."""

DEFAULT_CLIENT_ID = ""
"""Atlas's own Google OAuth client (a Desktop-app client, `google-drive.md` R4.1) -
the calendar's project, with the Drive API enabled. Empty until it exists;
`client_id` in settings is used instead, and with neither, signing in says what
is missing."""

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
GOOGLE_HOST = "www.googleapis.com"
"""R6.15: the only host the token is ever sent to - an upload's session address
included, which Google chooses and the plugin checks before following it."""
SCOPES = ("https://www.googleapis.com/auth/drive", "openid", "email")
"""R4.2: one restricted scope, by the person's decision, plus the address that
labels a connection."""

USER_AGENT = "atlas-google-drive"
RESPONSE_BYTES = 5_000_000
READ_BYTES = 10 * 1024 * 1024
"""What `drive_read` downloads to make text of - Google's own export limit."""
RETRY_CAP = 10.0
"""R10: a read waits out a 429, a rate-limit 403 or a 5xx once, for at most this long."""

ORGANISE_MAX = 50
"""R6.13: more than this at once asks for a narrower request."""
SHARE_MAX = 10
"""R6.16: the most people one share call adds, changes or removes."""
CARD_TEXT = 3000
"""R6.11: how much of a new Doc or Sheet's text its card shows."""
NAME_MAX = 120
DESCRIPTION_MAX = 2000
SCAN_MAX_BYTES = 5 * 1024 * 1024
"""R6.9: a text file up to this size is scanned for credentials."""
KEY_FILES = re.compile(
    r".*\.(pem|key|p12|pfx|kdbx|keystore|jks)$|^id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$", re.I
)
"""R6.9 (2): names shaped like key material."""
EMAIL = re.compile(r"[^@\s,;<>()]+@[^@\s,;<>()]+\.[A-Za-z]{2,}")
DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")

FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
SLIDES = "application/vnd.google-apps.presentation"
DRAWING = "application/vnd.google-apps.drawing"

NATIVE = {
    DOC: "Google Doc",
    SHEET: "Google Sheet",
    SLIDES: "Google Slides",
    FOLDER: "folder",
    DRAWING: "Google Drawing",
    SHORTCUT: "shortcut",
    "application/vnd.google-apps.form": "Google Form",
    "application/vnd.google-apps.site": "Google Site",
    "application/vnd.google-apps.map": "Google My Map",
    "application/vnd.google-apps.jam": "Jamboard",
}
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
ODT = "application/vnd.oasis.opendocument.text"
ODS = "application/vnd.oasis.opendocument.spreadsheet"
ODP = "application/vnd.oasis.opendocument.presentation"
KNOWN = {
    "application/pdf": "PDF",
    DOCX: "Word document",
    XLSX: "Excel workbook",
    PPTX: "PowerPoint deck",
    "application/msword": "Word document",
    "application/vnd.ms-excel": "Excel workbook",
    "application/zip": "ZIP archive",
    "text/csv": "CSV",
    "text/markdown": "Markdown",
    "text/plain": "text",
    "text/html": "HTML",
    "application/json": "JSON",
}

TYPES = {
    "doc": f"mimeType = '{DOC}'",
    "sheet": f"mimeType = '{SHEET}'",
    "slides": f"mimeType = '{SLIDES}'",
    "folder": f"mimeType = '{FOLDER}'",
    "pdf": "mimeType = 'application/pdf'",
    "image": "mimeType contains 'image/'",
    "video": "mimeType contains 'video/'",
}
"""R6.3: `drive_search`'s `type`, as Drive's own query clause."""

FORMATS = {
    "pdf": "application/pdf",
    "docx": DOCX,
    "odt": ODT,
    "txt": "text/plain",
    "md": "text/markdown",
    "epub": "application/epub+zip",
    "xlsx": XLSX,
    "ods": ODS,
    "csv": "text/csv",
    "pptx": PPTX,
    "odp": ODP,
    "png": "image/png",
    "jpg": "image/jpeg",
    "svg": "image/svg+xml",
}
EXPORTS = {
    DOC: ("pdf", ("pdf", "docx", "odt", "txt", "md", "epub")),
    SHEET: ("xlsx", ("xlsx", "ods", "csv", "pdf")),
    SLIDES: ("pdf", ("pdf", "pptx", "odp", "txt")),
    DRAWING: ("png", ("png", "jpg", "svg", "pdf")),
}
"""R6.6: what each Google-made file exports as - its default, then every choice."""
AS_TEXT = {
    DOC: ("text/markdown", "text/plain"),
    SHEET: ("text/csv",),
    SLIDES: ("text/plain",),
}
"""R6.5: how `drive_read` makes text of a Google-made file, best first."""
CONVERT = {
    DOCX: DOC,
    ODT: DOC,
    "application/msword": DOC,
    "text/plain": DOC,
    "text/markdown": DOC,
    "text/html": DOC,
    "application/rtf": DOC,
    XLSX: SHEET,
    ODS: SHEET,
    "application/vnd.ms-excel": SHEET,
    "text/csv": SHEET,
    PPTX: SLIDES,
    ODP: SLIDES,
}
"""R6.10: what `convert` turns an upload into."""
TEXT_TYPES = (
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-yaml",
    "application/yaml",
    "application/x-sh",
)
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
EXTENSIONS = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".docx": DOCX,
    ".xlsx": XLSX,
    ".pptx": PPTX,
    ".odt": ODT,
    ".ods": ODS,
    ".odp": ODP,
}
"""Named here because `mimetypes` depends on the machine's registry, and Windows'
does not know Markdown."""

ROLES = {"viewer": "reader", "commenter": "commenter", "editor": "writer"}
ROLE_NAMES = {
    "owner": "owner",
    "organizer": "manager",
    "fileOrganizer": "content manager",
    "writer": "editor",
    "commenter": "commenter",
    "reader": "viewer",
}

LIST_FIELDS = (
    "id,name,mimeType,size,modifiedTime,ownedByMe,owners(emailAddress),starred,trashed,"
    "shared,driveId,parents,shortcutDetails(targetId,targetMimeType)"
)
FILE_FIELDS = (
    f"{LIST_FIELDS},createdTime,webViewLink,description,"
    "lastModifyingUser(emailAddress,displayName),capabilities(canShare,canTrash,canRename)"
)
PERMISSION_FIELDS = "permissions(id,type,role,emailAddress,domain,deleted,allowFileDiscovery)"

NOT_SIGNED_IN = f"not signed in to Google Drive: atlas auth login {LOGIN_NAME}"


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
    if status == 403 and reason == "exportSizeLimitExceeded":
        return (
            "the file is too large for Google to export in that format - try drive_download "
            "with another format, or open it in Drive"
        )
    if status == 404:
        return (
            f"not found (404){detail} - check the id; a file shared with you may have "
            "been unshared or deleted"
        )
    if status == 400 and reason == "invalidSharingRequest":
        return (
            f"Google refused the share{detail} - an address with no Google account can only "
            "be shared with by email: ask again with notify: true"
        )
    return f"HTTP {status} from Google Drive{detail}"


def label_of(connection_id: str) -> str:
    """`google-drive:work` -> `work`."""
    return connection_id.partition(":")[2] or connection_id


def escape(value: str) -> str:
    """A value inside a Drive query's single quotes (R6.3)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class Drive:
    """What the tools share: the accounts, the requests, the files on their way in."""

    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.workspace = Path(ctx.workspace)
        self.settings = dict(ctx.settings)
        self._me: dict[str, str] = {}

    def setting(self, key: str, default: Any) -> Any:
        value = self.settings.get(key)
        return default if value is None else value

    # -- accounts -------------------------------------------------------------

    def accounts(self) -> tuple[str, ...]:
        return connections(PLUGIN, workspace=self.workspace)

    def pick(self, account: str, *, write: bool) -> list[str]:
        """R5.3: the only account, the named one, or - for a read - all of them.
        A change with more than one account and none named is refused."""
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

    async def me(self, account: str) -> str:
        """The account's own address, to leave out of "who can see it"."""
        if account not in self._me:
            data = await self.call(
                account, "GET", "/about", params={"fields": "user(emailAddress)"}
            )
            user = data.get("user") if isinstance(data, dict) else None
            self._me[account] = str((user or {}).get("emailAddress") or "").lower()
        return self._me[account]

    # -- one request ----------------------------------------------------------

    async def send(
        self,
        account: str,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        data: bytes | None = None,
        content_type: str = "",
        headers: Mapping[str, str] | None = None,
        retry: bool = True,
        max_bytes: int = RESPONSE_BYTES,
    ) -> Response:
        """One request to Google. `path` is under the API, or a whole address on
        Google's own host (an upload's session). Raises `GoogleError` for a status
        that is not 2xx."""
        url = path if path.startswith("https://") else f"{API}{path}"
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != GOOGLE_HOST:
            # R6.15: the token goes to Google and nowhere else.
            raise GoogleError(0, "Google pointed somewhere other than Google - nothing was sent")
        query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        if query:
            url += ("&" if parts.query else "?") + urlencode(query, doseq=True)
        for attempt in range(2):
            token = await self.bearer(account)
            response = await request(
                method,
                url,
                json=body,
                data=data,
                content_type=content_type,
                headers={"Authorization": f"Bearer {token}", **(headers or {})},
                max_bytes=max_bytes,
                timeout=120.0 if data else 30.0,
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

    # -- what the tools share -------------------------------------------------

    async def file(self, account: str, file_id: str, fields: str = FILE_FIELDS) -> dict[str, Any]:
        if not str(file_id or "").strip():
            raise ToolError("say which file, by the id a search showed")
        data = await self.call(
            account,
            "GET",
            f"/files/{quote(file_id.strip(), safe='')}",
            params={"fields": fields, "supportsAllDrives": "true"},
        )
        return data if isinstance(data, dict) else {}

    async def locate(self, file_id: str, account: str) -> tuple[str, dict[str, Any]]:
        """R5.3: which account sees a file - the named one, or the first that does."""
        found: GoogleError | None = None
        for who in self.pick(account, write=False):
            try:
                return who, await self.file(who, file_id)
            except GoogleError as exc:
                if exc.status != 404:
                    raise
                found = exc
        raise found or ToolError(f"no file {file_id!r}")

    async def follow(self, account: str, meta: dict[str, Any]) -> dict[str, Any]:
        """A shortcut is read, downloaded and described as the file it points at."""
        if meta.get("mimeType") != SHORTCUT:
            return meta
        target = str((meta.get("shortcutDetails") or {}).get("targetId") or "")
        if not target:
            raise ToolError(f"{clean(meta.get('name'))} is a shortcut to nothing")
        return await self.file(account, target)

    async def files(
        self, account: str, query: str, limit: int, *, order: str = ""
    ) -> tuple[list[dict[str, Any]], bool]:
        """R6.3: files matching a Drive query, every drive the account can see. The
        flag says whether `limit` cut the list."""
        found: list[dict[str, Any]] = []
        token = ""
        while True:
            data = await self.call(
                account,
                "GET",
                "/files",
                params={
                    "q": query,
                    "fields": f"nextPageToken,files({LIST_FIELDS})",
                    "pageSize": min(100, limit - len(found) + 1),
                    "orderBy": order,
                    "pageToken": token,
                    "corpora": "allDrives",
                    "includeItemsFromAllDrives": "true",
                    "supportsAllDrives": "true",
                },
            )
            items = data.get("files", []) if isinstance(data, dict) else []
            found.extend(item for item in items if isinstance(item, dict))
            token = str(data.get("nextPageToken") or "") if isinstance(data, dict) else ""
            if len(found) > limit:
                return found[:limit], True
            if not token:
                return found, False

    async def permissions(self, account: str, file_id: str) -> list[dict[str, Any]]:
        data = await self.call(
            account,
            "GET",
            f"/files/{quote(file_id, safe='')}/permissions",
            params={"fields": PERMISSION_FIELDS, "supportsAllDrives": "true", "pageSize": 100},
        )
        found = data.get("permissions", []) if isinstance(data, dict) else []
        return [p for p in found if isinstance(p, dict) and not p.get("deleted")]

    async def audience(self, account: str, meta: Mapping[str, Any]) -> list[str]:
        """R6.12: everybody but the person who can see what is in `meta`, a file or
        a folder, named - or one line saying Drive would not say."""
        if meta.get("driveId"):
            drive = await self.call(
                account,
                "GET",
                f"/drives/{quote(str(meta['driveId']), safe='')}",
                params={"fields": "name"},
            )
            name = clean(drive.get("name") if isinstance(drive, dict) else "") or "a shared drive"
            return [f"everyone in the shared drive {name!r}"]
        try:
            found = await self.permissions(account, str(meta.get("id", "")))
        except GoogleError as exc:
            if exc.status == 403:
                return ["whoever it is shared with (Drive would not say who)"]
            raise
        me = await self.me(account)
        return [
            permission_line(p)
            for p in found
            if not (p.get("type") == "user" and str(p.get("emailAddress", "")).lower() == me)
        ]

    async def path(self, account: str, folder: Mapping[str, Any]) -> str:
        """`My Drive / Finance / Taxes`: a folder and the folders above it."""
        names = [clean(folder.get("name")) or "(no name)"]
        parents = list(folder.get("parents") or ())
        for _ in range(8):
            if not parents:
                break
            try:
                above = await self.file(account, str(parents[0]), fields="id,name,parents")
            except GoogleError:
                names.append("…")  # a folder above that this account cannot see
                break
            names.append(clean(above.get("name")) or "(no name)")
            parents = list(above.get("parents") or ())
        return " / ".join(reversed(names))

    async def destination(self, account: str, folder: str) -> tuple[str, str, list[str]]:
        """Where a new or moved file goes: its id, its path, and who will see it."""
        meta = await self.file(
            account, (folder or "root").strip(), fields="id,name,mimeType,driveId,parents,trashed"
        )
        if meta.get("mimeType") != FOLDER:
            raise ToolError(f"{folder} is not a folder - drive_search with type: folder finds one")
        if meta.get("trashed"):
            raise ToolError(f"the folder {clean(meta.get('name'))!r} is in Trash")
        return str(meta["id"]), await self.path(account, meta), await self.audience(account, meta)

    async def content(
        self, account: str, meta: Mapping[str, Any], *, export: str = "", cap: int
    ) -> Response:
        """A file's bytes, or a Google-made file exported as `export`."""
        path = f"/files/{quote(str(meta.get('id', '')), safe='')}"
        if export:
            return await self.send(
                account, "GET", f"{path}/export", params={"mimeType": export}, max_bytes=cap
            )
        return await self.send(
            account,
            "GET",
            path,
            params={"alt": "media", "supportsAllDrives": "true"},
            max_bytes=cap,
        )

    async def upload(
        self, account: str, metadata: Mapping[str, Any], data: bytes, content_type: str
    ) -> dict[str, Any]:
        """R6.15: a resumable upload in two requests - the metadata, then the bytes
        to the session address Google answered with, on Google's own host."""
        started = await self.send(
            account,
            "POST",
            f"{UPLOAD_API}/files",
            params={"uploadType": "resumable", "supportsAllDrives": "true", "fields": FILE_FIELDS},
            body=dict(metadata),
            headers={
                "X-Upload-Content-Type": content_type,
                "X-Upload-Content-Length": str(len(data)),
            },
            retry=False,
        )
        session = started.header("location")
        if not session:
            raise GoogleError(started.status, "Google did not say where to send the file")
        done = await self.send(
            account, "PUT", session, data=data, content_type=content_type, retry=False
        )
        made = json.loads(done.body.decode("utf-8") or "{}")
        return made if isinstance(made, dict) else {}

    # -- the files an upload may carry (R6.8-R6.9) -----------------------------

    async def gather(self, entries: Iterable[Any]) -> list[Payload]:
        """Each entry a workspace path, `chat:<name>` or a web address, refused before
        any card."""
        names = [str(e).strip() for e in entries if str(e).strip()]
        most = int(self.setting("files_max", 10))
        if not names:
            raise ToolError("name at least one file")
        if len(names) > most:
            raise ToolError(f"{len(names)} files - at most {most} in one upload")
        files: list[Payload] = []
        for name in names:
            if name.startswith("chat:"):
                files.append(self.from_chat(name[len("chat:") :].strip()))
            elif urlsplit(name).scheme.lower() in ("http", "https"):
                files.append(await self.from_url(name))
            else:
                files.append(self.from_workspace(name))
        cap = int(self.setting("upload_max_mb", 50)) * 1024 * 1024
        if sum(len(f.data) for f in files) > cap:
            raise ToolError(f"over {human_size(cap)} in all - upload fewer files at a time")
        return files

    async def from_url(self, address: str) -> Payload:
        """A file downloaded now, through the core's client under the operator's
        address rules, so the card can hash exactly what will be uploaded."""
        policy = self.ctx.web_policy
        cap = int(self.setting("upload_max_mb", 50)) * 1024 * 1024
        try:
            response = await get(
                address,
                allow_private=policy.allow_private,
                hosts=policy.hosts,
                max_redirects=policy.max_redirects,
                max_bytes=cap + 1,
                timeout=60.0,
                user_agent=USER_AGENT,
            )
        except NetworkPolicyError as exc:
            raise ToolError(f"refused: {address} - {exc}") from None
        except WebError as exc:
            raise ToolError(f"could not download {address}: {exc}") from None
        if not 200 <= response.status < 300:
            raise ToolError(f"could not download {address}: HTTP {response.status}")
        if response.truncated or len(response.body) > cap:
            raise ToolError(f"{address} is over {human_size(cap)}")
        _scan(response.body, address, self.workspace)
        return Payload(
            _download_name(response, address), response.body, f"downloaded from {address}"
        )

    def from_workspace(self, entry: str) -> Payload:
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
        return Payload(target.name, data, f"from the workspace: {relative.as_posix()}")

    def from_chat(self, name: str) -> Payload:
        """A file the person sent in this conversation, the latest of that name."""
        media = self.ctx.media
        for block in reversed(self.ctx.session_media()):
            given = str(getattr(block, "name", "") or "")
            source = str(getattr(block, "source", "") or "")
            if name and (given == name or source.split(" (")[0] == name):
                data = media.get(block) if media is not None else None
                if not data:
                    raise ToolError(f"{name} was sent here but is no longer stored")
                _scan(data, name, self.workspace)
                return Payload(given or name, data, "you sent it in this chat")
        raise ToolError(f"no file called {name!r} was sent in this conversation")

    def record(self, event: str, detail: str, arguments: Mapping[str, Any]) -> None:
        """R6.19: what went into Drive, and from where, on the trail - never contents."""
        with contextlib.suppress(Exception):
            self.ctx.audit(event, detail, arguments=dict(arguments))


def _download_name(response: Response, address: str) -> str:
    """The name the server gave the file, else the last part of the address."""
    disposition = response.header("content-disposition")
    match = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)", disposition or "", re.I)
    if match:
        return unquote(match.group(1) or match.group(2))
    tail = unquote(urlsplit(response.url or address).path.rstrip("/").rpartition("/")[2])
    return tail or "download"


def _scan(data: bytes, shown: str, workspace: Path) -> None:
    """R6.9 (3): a text with a credential in it does not leave."""
    if len(data) > SCAN_MAX_BYTES:
        return
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return  # binary: the card is what stands between it and the upload
    found = credentials_in(text, workspace=workspace)
    if found:
        raise ToolError(
            f"refused: {shown} contains what looks like a credential ({', '.join(found)})"
        )


class Payload:
    """One file on its way into Drive: its bytes, read once, and where it came from."""

    def __init__(self, name: str, data: bytes, source: str) -> None:
        self.name = clean_name(name)
        self.data = data
        self.source = source
        self.kind = content_type(data, self.name)
        self.sha256 = hashlib.sha256(data).hexdigest()

    def describe(self) -> str:
        size = human_size(len(self.data))
        return (
            f"{self.name} - {kind_of(self.kind)}, {size}, sha256 {self.sha256[:8]} - {self.source}"
        )

    def record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size": len(self.data),
            "sha256": self.sha256,
            "source": self.source,
        }


# -- showing things ------------------------------------------------------------


def clean(text: Any, limit: int = NAME_MAX) -> str:
    """One line of someone else's words, capped."""
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def clean_name(name: str) -> str:
    """Letters, digits, spaces and `.-_()`; never a path somebody else chose."""
    base = name.replace("\\", "/").rpartition("/")[2]
    kept = "".join(c for c in base if c.isalnum() or c in " .-_()").strip(" .")
    return kept[:NAME_MAX] or "file"


def content_type(data: bytes, name: str) -> str:
    """The file's type from its first bytes, else its name."""
    for signature, kind in SIGNATURES:
        if data.startswith(signature):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    known = EXTENSIONS.get(Path(name).suffix.lower())
    if known:
        return known
    guessed, _ = mimetypes.guess_type(name)
    if guessed:
        return guessed
    return "application/zip" if data.startswith(b"PK\x03\x04") else "application/octet-stream"


def kind_of(value: Any) -> str:
    mime = str(value or "")
    if mime in NATIVE:
        return NATIVE[mime]
    if mime in KNOWN:
        return KNOWN[mime]
    family = mime.partition("/")[0]
    if family in ("image", "video", "audio"):
        return family
    if family == "text":
        return "text"
    return mime or "file"


def human_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def when(stamp: Any) -> str:
    """`Tue 29 Sep 14:05` in this machine's zone, with the year when it is not this one."""
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return str(stamp)
    year = f" {moment.year}" if moment.year != datetime.now().year else ""
    return f"{moment.strftime('%a')} {moment.day} {moment.strftime('%b')}{year} {moment:%H:%M}"


def permission_line(permission: Mapping[str, Any]) -> str:
    role = ROLE_NAMES.get(str(permission.get("role", "")), str(permission.get("role", "")))
    kind = permission.get("type")
    if kind == "anyone":
        if permission.get("allowFileDiscovery"):
            return f"anyone on the internet, findable by search ({role})"
        return f"anyone with the link ({role})"
    if kind == "domain":
        return f"everyone at {permission.get('domain', '?')} ({role})"
    return f"{permission.get('emailAddress', '?')} ({role})"


def file_line(meta: Mapping[str, Any], account: str = "") -> str:
    """R6.4: one line a person can recognise a file by, ending in its id."""
    parts = [clean(meta.get("name")) or "(no name)", kind_of(meta.get("mimeType"))]
    if meta.get("modifiedTime"):
        parts.append(f"changed {when(meta['modifiedTime'])}")
    if meta.get("size"):
        with contextlib.suppress(ValueError):
            parts.append(human_size(int(meta["size"])))
    if meta.get("driveId"):
        parts.append("in a shared drive")
    elif not meta.get("ownedByMe", True):
        owners = [o.get("emailAddress") for o in meta.get("owners") or () if isinstance(o, dict)]
        parts.append(f"owner {owners[0]}" if owners and owners[0] else "shared with you")
    elif meta.get("shared"):
        parts.append("shared")
    if meta.get("starred"):
        parts.append("starred")
    if meta.get("trashed"):
        parts.append("in Trash")
    tags = f"id: {meta.get('id', '')}" + (f" · {label_of(account)}" if account else "")
    return " · ".join(parts) + f"  [{tags}]"


def visible(who: Sequence[str]) -> str:
    """R6.12: the line naming who else will see it, or nothing."""
    if not who:
        return ""
    shown = ", ".join(who[:8]) + (f" and {len(who) - 8} more" if len(who) > 8 else "")
    return f" · visible to {shown}"


def names_of(metas: Sequence[Mapping[str, Any]]) -> str:
    shown = [f'"{clean(m.get("name"), 60)}"' for m in metas[:5]]
    more = f" and {len(metas) - 5} more" if len(metas) > 5 else ""
    return ", ".join(shown) + more


def preview(text: str) -> str:
    if len(text) <= CARD_TEXT:
        return text
    return text[:CARD_TEXT] + f"\n[... and {len(text) - CARD_TEXT} more characters]"


# -- the tools -----------------------------------------------------------------


ACCOUNT = {
    "type": "string",
    "description": (
        "Which signed-in Google account, by its label. Needed for a change when more than "
        "one is signed in."
    ),
}
FILE_ID = {"type": "string", "description": "The file's id, as drive_search showed it."}
FOLDER_ID = {
    "type": "string",
    "description": "A folder's id from drive_search (type: folder). Default: My Drive.",
}


class DriveTool(Tool):
    """What all eight share: owner-only, marked untrusted, failures as sentences."""

    trusted_only = True
    untrusted = True

    def __init__(self, drive: Drive) -> None:
        self.drive = drive

    async def run(self, **arguments: Any) -> ToolResult:
        try:
            assert_active()
            said = await self.act(**arguments)
            return said if isinstance(said, ToolResult) else ToolResult.ok(said)
        except (CredentialError, ToolError, GoogleError, WebError) as exc:
            return ToolResult.error(str(exc))

    async def act(self, *args: Any, **arguments: Any) -> Any:
        raise NotImplementedError


class Search(DriveTool):
    name = "drive_search"
    description = (
        "Find files in Google Drive - the person's own, those shared with them, and shared "
        "drives. With nothing given, the most recently changed. `query` matches words in "
        "names and contents; the other fields narrow it. Each line ends with the file's id "
        "for the other drive tools."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words to find in names and contents."},
            "type": {"type": "string", "enum": sorted(TYPES)},
            "folder": {"type": "string", "description": "Only what is directly in this folder."},
            "shared_with_me": {
                "type": "boolean",
                "description": "Only files other people shared with the person.",
            },
            "starred": {"type": "boolean"},
            "in_trash": {"type": "boolean", "description": "Look in Trash instead."},
            "modified_after": {"type": "string", "description": "YYYY-MM-DD."},
            "drive_query": {
                "type": "string",
                "description": (
                    "Drive's own query syntax, used as given and joined to the rest with and, "
                    "e.g. name contains 'invoice' and 'sam@example.com' in owners."
                ),
            },
            "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
            "account": ACCOUNT,
        },
        "required": [],
    }

    async def act(
        self,
        query: str = "",
        type: str = "",
        folder: str = "",
        shared_with_me: bool = False,
        starred: bool = False,
        in_trash: bool = False,
        modified_after: str = "",
        drive_query: str = "",
        max_results: int = 0,
        account: str = "",
    ) -> str:
        limit = min(max(int(max_results or self.drive.setting("max_results", 25)), 1), 100)
        words, raw = query.strip(), drive_query.strip()
        clauses: list[str] = []
        if "trashed" not in raw:
            clauses.append(f"trashed = {'true' if in_trash else 'false'}")
        if words:
            clauses.append(f"fullText contains '{escape(words)}'")
        if type:
            if type not in TYPES:
                raise ToolError(f"type is one of {', '.join(sorted(TYPES))}")
            clauses.append(TYPES[type])
        if folder.strip():
            clauses.append(f"'{escape(folder.strip())}' in parents")
        if shared_with_me:
            clauses.append("sharedWithMe = true")
        if starred:
            clauses.append("starred = true")
        if modified_after.strip():
            if not DATE_ONLY.fullmatch(modified_after.strip()):
                raise ToolError(f"{modified_after!r} is not a date - write YYYY-MM-DD")
            clauses.append(f"modifiedTime > '{modified_after.strip()}T00:00:00'")
        if raw:
            clauses.append(f"({raw})")
        drive_q = " and ".join(clauses)
        # Drive refuses to sort a full-text search; it answers those by relevance.
        ordered = not words and "fulltext" not in raw.lower()
        accounts = self.drive.pick(account, write=False)
        several = len(accounts) > 1
        lines: list[str] = []
        cut = False
        for who in accounts:
            found, more = await self.drive.files(
                who, drive_q, limit, order="modifiedTime desc" if ordered else ""
            )
            cut = cut or more
            lines += [file_line(f, who if several else "") for f in found]
        if not lines:
            return f"No files match ({drive_q})."
        body = lines[:limit]
        order = ", most recently changed first" if ordered else ", best match first"
        tail = (
            f"\n(showing the first {limit} - narrow the search to see the rest)"
            if cut or len(lines) > limit
            else ""
        )
        return f"{len(body)} file(s){order}:\n" + "\n".join(body) + tail


class Read(DriveTool):
    name = "drive_read"
    description = (
        "Read a file as text: a Google Doc (as Markdown), a Google Sheet (its first sheet, as "
        "CSV), Google Slides, or a text file. A folder lists what is in it. A PDF, a picture "
        "or an Office file is fetched with drive_download instead."
    )
    parameters = {
        "type": "object",
        "properties": {"file_id": FILE_ID, "account": ACCOUNT},
        "required": ["file_id"],
    }

    async def act(self, file_id: str, account: str = "") -> str:
        who, meta = await self.drive.locate(file_id, account)
        meta = await self.drive.follow(who, meta)
        mime = str(meta.get("mimeType", ""))
        head = f"{clean(meta.get('name'))} ({kind_of(mime)}) [id: {meta.get('id', '')}]"
        if mime == FOLDER:
            limit = int(self.drive.setting("max_results", 25))
            query = f"'{escape(str(meta.get('id', '')))}' in parents and trashed = false"
            found, more = await self.drive.files(who, query, limit, order="folder,name")
            lines = [file_line(f) for f in found] or ["(empty)"]
            if more:
                lines.append(f"(the first {limit} - drive_search with folder narrows it)")
            return head + "\n" + "\n".join(lines)
        text, note = await self.text(who, meta)
        most = int(self.drive.setting("max_chars", 20000))
        if len(text) > most:
            note += f" (cut at {most} of {len(text)} characters)"
            text = text[:most]
        return f"{head}{note}\n---\n{text}"

    async def text(self, who: str, meta: Mapping[str, Any]) -> tuple[str, str]:
        """R6.5: the file as text, and a note on what of it this is."""
        mime = str(meta.get("mimeType", ""))
        name = clean(meta.get("name"))
        if mime in AS_TEXT:
            choices = AS_TEXT[mime]
            for index, export in enumerate(choices):
                try:
                    response = await self.drive.content(who, meta, export=export, cap=READ_BYTES)
                except GoogleError as exc:
                    if exc.status == 400 and index + 1 < len(choices):
                        continue  # that export is not offered; the next one is
                    raise
                note = " - the first sheet only; drive_download as xlsx has all of them"
                return response.text(), note if mime == SHEET else ""
        if mime.startswith("text/") or mime in TEXT_TYPES:
            response = await self.drive.content(who, meta, cap=READ_BYTES)
            note = f" - only the first {human_size(READ_BYTES)}" if response.truncated else ""
            if mime == "text/html":
                return extract(response.text(), mode="text", readability=False).text, note
            return response.text(), note
        raise ToolError(
            f"{name} is a {kind_of(mime)}, not text - drive_download saves it to the workspace "
            "(read a PDF or an Office file there with read_media) or shows it (a picture)"
        )


class Info(DriveTool):
    name = "drive_info"
    description = (
        "Everything about one file or folder: where it is, who owns it, who it is shared "
        "with and how, when it changed and by whom, and its link. Look here before sharing "
        "or moving something."
    )
    parameters = {
        "type": "object",
        "properties": {"file_id": FILE_ID, "account": ACCOUNT},
        "required": ["file_id"],
    }

    async def act(self, file_id: str, account: str = "") -> str:
        who, meta = await self.drive.locate(file_id, account)
        lines = [f"{clean(meta.get('name'))} ({kind_of(meta.get('mimeType'))})"]
        if meta.get("mimeType") == SHORTCUT:
            target = (meta.get("shortcutDetails") or {}).get("targetId", "")
            lines.append(f"A shortcut to {target}")
        parents = list(meta.get("parents") or ())
        if parents:
            with contextlib.suppress(GoogleError):
                above = await self.drive.file(who, str(parents[0]), fields="id,name,parents")
                lines.append(f"Where: {await self.drive.path(who, above)}")
        elif not meta.get("ownedByMe", True):
            lines.append("Where: Shared with me")
        owners = [str(o.get("emailAddress")) for o in meta.get("owners") or () if o]
        if meta.get("ownedByMe"):
            lines.append("Owner: you")
        elif owners:
            lines.append(f"Owner: {', '.join(owners)}")
        changed = when(meta.get("modifiedTime")) if meta.get("modifiedTime") else ""
        by = meta.get("lastModifyingUser")
        if changed:
            person = (
                (by.get("displayName") or by.get("emailAddress")) if isinstance(by, dict) else ""
            )
            lines.append(f"Changed: {changed}" + (f" by {clean(person)}" if person else ""))
        if meta.get("createdTime"):
            lines.append(f"Created: {when(meta['createdTime'])}")
        if meta.get("size"):
            with contextlib.suppress(ValueError):
                lines.append(f"Size: {human_size(int(meta['size']))}")
        try:
            people = await self.drive.audience(who, meta)
        except GoogleError as exc:
            people = [f"(could not read the sharing: {exc})"]
        lines.append(
            "Who else can see it: " + (", ".join(people) if people else "nobody - only you")
        )
        if meta.get("webViewLink"):
            lines.append(f"Link: {meta['webViewLink']}")
        description = clean(meta.get("description"), DESCRIPTION_MAX)
        if description:
            lines.append(f"Description: {description}")
        marks = [
            m
            for m, on in (("starred", meta.get("starred")), ("in Trash", meta.get("trashed")))
            if on
        ]
        if marks:
            lines.append("Marked: " + ", ".join(marks))
        lines.append(
            f"[id: {meta.get('id', file_id)}" + (f" · {label_of(who)}" if account else "") + "]"
        )
        return "\n".join(lines)


class Download(DriveTool):
    name = "drive_download"
    description = (
        "Fetch a file from Drive. A picture is shown to you; anything else is saved in the "
        "workspace under drive-downloads/ for read_media and the other tools. A Google Doc or "
        "Slides comes as PDF and a Google Sheet as XLSX unless `format` says otherwise."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_id": FILE_ID,
            "format": {
                "type": "string",
                "enum": sorted(FORMATS),
                "description": "Only for a Google Doc, Sheet, Slides or Drawing: export as.",
            },
            "account": ACCOUNT,
        },
        "required": ["file_id"],
    }

    async def act(self, file_id: str, format: str = "", account: str = "") -> Any:
        who, meta = await self.drive.locate(file_id, account)
        meta = await self.drive.follow(who, meta)
        mime = str(meta.get("mimeType", ""))
        name = clean_name(str(meta.get("name") or "file"))
        cap = int(self.drive.setting("download_max_mb", 50)) * 1024 * 1024
        if mime == FOLDER:
            raise ToolError(f"{name} is a folder - drive_read lists what is in it")
        if mime in EXPORTS:
            default, offered = EXPORTS[mime]
            chosen = format or default
            if chosen not in offered:
                raise ToolError(f"a {kind_of(mime)} exports as {', '.join(offered)}")
            response = await self.drive.content(who, meta, export=FORMATS[chosen], cap=cap + 1)
            name = f"{name}.{chosen}"
        elif mime.startswith("application/vnd.google-apps."):
            raise ToolError(f"a {kind_of(mime)} cannot be downloaded - open it in Drive")
        elif format:
            raise ToolError(
                f"format is for Google Docs, Sheets, Slides and Drawings; {name} is a "
                f"{kind_of(mime)} and comes as it is"
            )
        else:
            response = await self.drive.content(who, meta, cap=cap + 1)
        data = response.body
        if response.truncated or len(data) > cap:
            raise ToolError(f"{name} is over {human_size(cap)}, the download_max_mb limit")
        kind = content_type(data, name)
        media = self.drive.ctx.media
        if kind in IMAGE_TYPES and media is not None:
            block = media.put(data, source=f"{name} (from Google Drive)")
            return ImageResult(f"{name}, {kind}, {human_size(len(data))}", images=(block,))
        folder = self.drive.workspace / "drive-downloads" / re.sub(r"[^\w-]", "", str(meta["id"]))
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        target.write_bytes(data)
        relative = target.relative_to(self.drive.workspace).as_posix()
        return (
            f"Saved {name} ({kind_of(kind)}, {human_size(len(data))}) to {relative} - read it "
            f"with read_media(path={relative!r})."
        )


# -- changing things -----------------------------------------------------------


class Plan:
    """What a change will do, worked out once for the card and used again to run."""

    def __init__(self, account: str, card: str, *, confirm: bool = False) -> None:
        self.account = account
        self.card = card
        self.confirm = confirm
        self.files: list[Payload] = []
        self.folder = ""
        self.targets: list[dict[str, Any]] = []
        self.steps: list[tuple[str, str, str]] = []
        """For a share: (verb, address or permission id, role)."""


def _key(arguments: Mapping[str, Any]) -> str:
    return json.dumps(arguments, sort_keys=True, default=str)


class ChangeTool(DriveTool):
    """A change: gated, carded from what is really there, and confirm-tier when it
    lets somebody new see something (G2)."""

    gated = True
    action = ""

    def __init__(self, drive: Drive) -> None:
        super().__init__(drive)
        self._looked: dict[str, tuple[float, Plan]] = {}
        """What `subject` worked out, keyed by the call's arguments, so `run` does
        what the card said - the same bytes, the same people - and not a later look."""

    async def subject(self, arguments: Mapping[str, Any]) -> Subject | None:
        try:
            plan = await self.plan(arguments)
        except (CredentialError, ToolError):
            return None  # `run` fails the same way, before it changes anything
        except (GoogleError, WebError) as exc:
            # Still a card, and the strict one: a look that failed is no reason
            # to skip the question.
            summary = f"{self.action} in Google Drive (could not look first: {exc})"
            return Subject(tool=self.name, action=self.action, summary=summary, confirm=True)
        self._looked[_key(arguments)] = (time.monotonic(), plan)
        return Subject(tool=self.name, action=self.action, summary=plan.card, confirm=plan.confirm)

    async def act(self, **arguments: Any) -> str:
        seen = self._looked.pop(_key(arguments), None)
        plan = seen[1] if seen is not None and time.monotonic() - seen[0] < 900 else None
        return await self.carry_out(plan or await self.plan(arguments), arguments)

    async def plan(self, arguments: Mapping[str, Any]) -> Plan:
        raise NotImplementedError

    async def carry_out(self, plan: Plan, arguments: Mapping[str, Any]) -> str:
        raise NotImplementedError


def _stem(name: str) -> str:
    """A converted file is named as Drive's own converter names it, without the extension."""
    stem, dot, _ = name.rpartition(".")
    return stem if dot and stem else name


class Upload(ChangeTool):
    name = "drive_upload"
    action = "upload"
    description = (
        "Copy files into Google Drive: a path inside the workspace, chat:<name> for a file the "
        "person sent in this conversation, or an https:// address to download. Into `folder` "
        "or My Drive. `convert` makes Word, Excel, PowerPoint, CSV and text files into Google "
        "Docs, Sheets and Slides. The person is shown every file and who will be able to see it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "reports/q3.pdf, chat:<name>, or https://... - at most 10.",
            },
            "folder": FOLDER_ID,
            "convert": {
                "type": "boolean",
                "description": "Make Office, CSV and text files Google Docs, Sheets and Slides.",
            },
            "account": ACCOUNT,
        },
        "required": ["files"],
    }

    async def plan(self, arguments: Mapping[str, Any]) -> Plan:
        account = self.drive.pick(str(arguments.get("account", "") or ""), write=True)[0]
        files = await self.drive.gather(arguments.get("files") or ())
        folder, where, who = await self.drive.destination(
            account, str(arguments.get("folder", "") or "")
        )
        convert = bool(arguments.get("convert"))
        lines = [f"Upload {len(files)} file(s) to Google Drive · {where}{visible(who)}"]
        for f in files:
            into = CONVERT.get(f.kind) if convert else None
            lines.append(f"  {f.describe()}" + (f" → a {NATIVE[into]}" if into else ""))
        plan = Plan(account, "\n".join(lines), confirm=bool(who))
        plan.files, plan.folder = files, folder
        return plan

    async def carry_out(self, plan: Plan, arguments: Mapping[str, Any]) -> str:
        convert = bool(arguments.get("convert"))
        made: list[dict[str, Any]] = []
        for f in plan.files:
            into = CONVERT.get(f.kind) if convert else None
            metadata: dict[str, Any] = {"name": _stem(f.name) if into else f.name}
            metadata["parents"] = [plan.folder]
            if into:
                metadata["mimeType"] = into
            try:
                made.append(await self.drive.upload(plan.account, metadata, f.data, f.kind))
            except (GoogleError, WebError) as exc:
                done = ", ".join(clean(m.get("name")) for m in made) or "nothing"
                raise ToolError(f"uploaded {done}; then {f.name} failed: {exc}") from None
        self.drive.record(
            "drive_uploaded",
            f"{len(plan.files)} file(s)",
            {
                "account": label_of(plan.account),
                "folder": plan.folder,
                "files": [f.record() for f in plan.files],
            },
        )
        return "Uploaded:\n" + "\n".join(
            file_line(m) + (f"\n  {m['webViewLink']}" if m.get("webViewLink") else "") for m in made
        )


MADE = {"doc": (DOC, "text/markdown"), "sheet": (SHEET, "text/csv"), "folder": (FOLDER, "")}


class Create(ChangeTool):
    name = "drive_create"
    action = "create"
    description = (
        "Make a new Google Doc (from Markdown), Google Sheet (from CSV) or folder, in `folder` "
        "or My Drive. The person is shown what will be made, where, and who will be able to "
        "see it. It never changes a file that is already there."
    )
    parameters = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": sorted(MADE)},
            "name": {"type": "string"},
            "content": {
                "type": "string",
                "description": "A doc's text as Markdown, or a sheet's rows as CSV.",
            },
            "folder": FOLDER_ID,
            "account": ACCOUNT,
        },
        "required": ["kind", "name"],
    }

    async def plan(self, arguments: Mapping[str, Any]) -> Plan:
        kind = str(arguments.get("kind", ""))
        if kind not in MADE:
            raise ToolError(f"kind is one of {', '.join(sorted(MADE))}")
        name = clean(arguments.get("name"))
        if not name:
            raise ToolError("give it a name")
        content = str(arguments.get("content", "") or "")
        if kind == "folder" and content:
            raise ToolError("a folder has no content - make it, then upload or create inside it")
        _scan(content.encode("utf-8"), "the content", self.drive.workspace)
        account = self.drive.pick(str(arguments.get("account", "") or ""), write=True)[0]
        folder, where, who = await self.drive.destination(
            account, str(arguments.get("folder", "") or "")
        )
        what = NATIVE[MADE[kind][0]]
        card = f'Create a {what} "{name}" · {where}{visible(who)}'
        if kind != "folder":
            card += f"\n\n{preview(content)}" if content else " · empty"
        plan = Plan(account, card, confirm=bool(who))
        plan.folder = folder
        return plan

    async def carry_out(self, plan: Plan, arguments: Mapping[str, Any]) -> str:
        kind = str(arguments["kind"])
        native, source = MADE[kind]
        metadata = {
            "name": clean(arguments.get("name")),
            "mimeType": native,
            "parents": [plan.folder],
        }
        if kind == "folder":
            made = await self.drive.call(
                plan.account,
                "POST",
                "/files",
                params={"supportsAllDrives": "true", "fields": FILE_FIELDS},
                body=metadata,
                retry=False,
            )
        else:
            data = str(arguments.get("content", "") or "").encode("utf-8")
            made = await self.drive.upload(plan.account, metadata, data, source)
        link = f"\n  {made['webViewLink']}" if made.get("webViewLink") else ""
        return f"Created: {file_line(made)}{link}"


ACTIONS = {
    "rename": "Rename",
    "move": "Move",
    "trash": "Move to Trash",
    "restore": "Restore from Trash",
    "star": "Star",
    "unstar": "Unstar",
}


class Organise(ChangeTool):
    name = "drive_organise"
    action = "organise"
    description = (
        "Tidy Drive: rename one file, or move, trash, restore, star or unstar up to 50 at once, "
        "by id. Nothing is deleted for good; Drive empties Trash after 30 days. Moving into a "
        "folder other people can see asks the person, naming them."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(ACTIONS)},
            "file_ids": {"type": "array", "items": {"type": "string"}},
            "name": {"type": "string", "description": "For rename: the new name."},
            "folder": {"type": "string", "description": "For move: the folder's id, or root."},
            "account": ACCOUNT,
        },
        "required": ["action", "file_ids"],
    }

    async def plan(self, arguments: Mapping[str, Any]) -> Plan:
        action = str(arguments.get("action", ""))
        if action not in ACTIONS:
            raise ToolError(f"action is one of {', '.join(sorted(ACTIONS))}")
        ids = list(dict.fromkeys(str(i).strip() for i in arguments.get("file_ids") or () if i))
        if not ids:
            raise ToolError("say which files, by id")
        if len(ids) > ORGANISE_MAX:
            raise ToolError(f"{len(ids)} files - at most {ORGANISE_MAX} at once")
        new_name = clean(arguments.get("name"))
        if action == "rename" and (len(ids) != 1 or not new_name):
            raise ToolError("rename takes one file and a name")
        if action == "move" and not str(arguments.get("folder", "") or "").strip():
            raise ToolError("move needs a folder - an id from drive_search, or root for My Drive")
        account = self.drive.pick(str(arguments.get("account", "") or ""), write=True)[0]
        metas = [await self.drive.file(account, i, fields=LIST_FIELDS) for i in ids]
        names = names_of(metas)
        who: list[str] = []
        folder = ""
        if action == "rename":
            card = f'Rename "{clean(metas[0].get("name"))}" → "{new_name}"'
        elif action == "move":
            folder, where, who = await self.drive.destination(account, str(arguments["folder"]))
            card = f"Move {len(metas)} item(s) ({names}) to {where}{visible(who)}"
        else:
            card = f"{ACTIONS[action]} {len(metas)} item(s): {names}"
            if action == "trash":
                if any(m.get("mimeType") == FOLDER for m in metas):
                    card += " · a folder goes with everything in it"
                card += " · Drive empties Trash after 30 days"
        plan = Plan(account, card, confirm=bool(who))
        plan.targets, plan.folder = metas, folder
        return plan

    async def carry_out(self, plan: Plan, arguments: Mapping[str, Any]) -> str:
        action = str(arguments["action"])
        done: list[str] = []
        for meta in plan.targets:
            params: dict[str, Any] = {"supportsAllDrives": "true", "fields": LIST_FIELDS}
            body: dict[str, Any] = {}
            if action == "rename":
                body["name"] = clean(arguments.get("name"))
            elif action == "move":
                parents = [str(p) for p in meta.get("parents") or ()]
                if plan.folder in parents:
                    done.append(clean(meta.get("name")))
                    continue  # already there
                params["addParents"] = plan.folder
                params["removeParents"] = ",".join(parents)
            elif action in ("trash", "restore"):
                body["trashed"] = action == "trash"
            else:
                body["starred"] = action == "star"
            try:
                await self.drive.call(
                    plan.account,
                    "PATCH",
                    f"/files/{quote(str(meta['id']), safe='')}",
                    params=params,
                    body=body,
                    retry=False,
                )
            except (GoogleError, WebError) as exc:
                said = ", ".join(done) or "nothing"
                raise ToolError(
                    f"{ACTIONS[action].lower()}: done for {said}; then "
                    f"{clean(meta.get('name'))} failed: {exc}"
                ) from None
            done.append(clean(meta.get("name")))
        return f"{ACTIONS[action]}: {len(done)} item(s)."


class Share(ChangeTool):
    name = "drive_share"
    action = "share"
    description = (
        "Share a file or folder with people by email address - as viewer, commenter or editor - "
        "change what someone already has, or take access away (an address, or link to turn off "
        "link sharing). Nobody is emailed unless notify is true. Sharing with anyone-with-the-"
        "link or a whole domain is not offered. The person is shown every address and must say "
        "yes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_id": FILE_ID,
            "add": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Email addresses to share with, or whose access to change.",
            },
            "role": {
                "type": "string",
                "enum": sorted(ROLES),
                "description": "For add. Default viewer.",
            },
            "remove": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Email addresses to take access from; link turns off link sharing.",
            },
            "notify": {
                "type": "boolean",
                "description": "Google emails the people added. Default false: nobody is emailed.",
            },
            "message": {"type": "string", "description": "With notify: a note in that email."},
            "account": ACCOUNT,
        },
        "required": ["file_id"],
    }

    async def plan(self, arguments: Mapping[str, Any]) -> Plan:
        add = [str(a).strip().lower() for a in arguments.get("add") or () if str(a).strip()]
        remove = [str(r).strip().lower() for r in arguments.get("remove") or () if str(r).strip()]
        role = str(arguments.get("role", "") or "viewer")
        if role not in ROLES:
            raise ToolError(f"role is one of {', '.join(sorted(ROLES))} - ownership is not given")
        if not add and not remove:
            raise ToolError("say who to add or remove")
        if len(add) + len(remove) > SHARE_MAX:
            raise ToolError(f"{len(add) + len(remove)} people - at most {SHARE_MAX} at once")
        for address in add:
            if not EMAIL.fullmatch(address):
                raise ToolError(
                    f"{address!r} is not an email address - a file is shared with people by "
                    "address; anyone-with-the-link and whole domains are not offered"
                )
        account = self.drive.pick(str(arguments.get("account", "") or ""), write=True)[0]
        meta = await self.drive.file(account, str(arguments.get("file_id", "")))
        name = clean(meta.get("name"))
        if add and not (meta.get("capabilities") or {}).get("canShare", True):
            raise ToolError(f"you cannot share {name!r} - its owner has not allowed it")
        try:
            existing = await self.drive.permissions(account, str(meta["id"]))
        except GoogleError as exc:
            if exc.status == 403:
                raise ToolError(
                    f"Drive will not say who can see {name!r}, so it cannot be shared from here"
                ) from None
            raise
        me = await self.drive.me(account)
        by_address = {
            str(p.get("emailAddress", "")).lower(): p for p in existing if p.get("emailAddress")
        }
        steps: list[tuple[str, str, str]] = []
        lines: list[str] = []
        wanted = ROLES[role]
        for address in dict.fromkeys(add):
            if address == me:
                raise ToolError(f"{address} is you")
            held = by_address.get(address)
            if held is None:
                steps.append(("create", address, wanted))
                lines.append(f"  add {address} as {role}")
            elif held.get("role") == wanted:
                lines.append(f"  {address} is already a {role} - unchanged")
            elif held.get("role") in ("owner", "organizer"):
                raise ToolError(f"{address} is the {ROLE_NAMES[str(held['role'])]} - unchanged")
            else:
                steps.append(("update", str(held["id"]), wanted))
                lines.append(f"  {address}: {ROLE_NAMES.get(str(held.get('role')), '?')} → {role}")
        for address in dict.fromkeys(remove):
            if address in ("link", "anyone"):
                links = [p for p in existing if p.get("type") == "anyone"]
                if not links:
                    raise ToolError(f"{name!r} is not shared by link")
                steps += [("delete", str(p["id"]), "") for p in links]
                lines.append("  turn off link sharing (" + permission_line(links[0]) + ")")
                continue
            held = by_address.get(address)
            if held is None:
                raise ToolError(f"{address} has no access to {name!r} to take away")
            if held.get("role") in ("owner", "organizer") or address == me:
                raise ToolError(f"{address} is the {ROLE_NAMES.get(str(held.get('role')), '')}")
            steps.append(("delete", str(held["id"]), ""))
            lines.append(f"  remove {address} ({ROLE_NAMES.get(str(held.get('role')), '?')})")
        if not steps:
            raise ToolError("nothing to change:\n" + "\n".join(lines))
        folder = meta.get("mimeType") == FOLDER
        what = f'the folder "{name}"' if folder else f'"{name}" ({kind_of(meta.get("mimeType"))})'
        head = f"Share {what}"
        widening = any(verb != "delete" for verb, _, _ in steps)
        if not widening:
            head = f"Take away access to {what}"
        tail: list[str] = []
        if widening and folder:
            tail.append("Everything in the folder, now and later, is shared with them too.")
        if widening:
            message = clean(arguments.get("message"), 500)
            if arguments.get("notify"):
                tail.append(
                    "Google emails them" + (f' with your note: "{message}"' if message else ".")
                )
            else:
                tail.append("Nobody is emailed - send them the link yourself.")
        others = [
            permission_line(p)
            for p in existing
            if str(p.get("emailAddress", "")).lower() not in {me, *add, *remove}
            and not (p.get("type") == "anyone" and ("link" in remove or "anyone" in remove))
        ]
        if others:
            tail.append("Others who can see it: " + ", ".join(others[:8]))
        plan = Plan(account, "\n".join([head, *lines, *tail]), confirm=widening)
        plan.targets, plan.steps = [meta], steps
        return plan

    async def carry_out(self, plan: Plan, arguments: Mapping[str, Any]) -> str:
        meta = plan.targets[0]
        path = f"/files/{quote(str(meta['id']), safe='')}/permissions"
        done: list[str] = []
        for verb, who, role in plan.steps:
            try:
                if verb == "create":
                    params: dict[str, Any] = {
                        "supportsAllDrives": "true",
                        "sendNotificationEmail": "true" if arguments.get("notify") else "false",
                    }
                    if arguments.get("notify") and arguments.get("message"):
                        params["emailMessage"] = clean(arguments.get("message"), 500)
                    await self.drive.call(
                        plan.account,
                        "POST",
                        path,
                        params=params,
                        body={"type": "user", "role": role, "emailAddress": who},
                        retry=False,
                    )
                    done.append(f"added {who} as {ROLE_NAMES[role]}")
                elif verb == "update":
                    await self.drive.call(
                        plan.account,
                        "PATCH",
                        f"{path}/{quote(who, safe='')}",
                        params={"supportsAllDrives": "true"},
                        body={"role": role},
                        retry=False,
                    )
                    done.append(f"made one person {ROLE_NAMES[role]}")
                else:
                    await self.drive.call(
                        plan.account,
                        "DELETE",
                        f"{path}/{quote(who, safe='')}",
                        params={"supportsAllDrives": "true"},
                        retry=False,
                    )
                    done.append("took one access away")
            except (GoogleError, WebError) as exc:
                said = "; ".join(done) or "nothing"
                raise ToolError(f"done: {said}; then Google refused: {exc}") from None
        link = f"\n{meta['webViewLink']}" if meta.get("webViewLink") else ""
        return f"{clean(meta.get('name'))}: " + "; ".join(done) + "." + link


# -- signing in ----------------------------------------------------------------


def _label(response: Mapping[str, Any]) -> Mapping[str, str]:
    """R5.2: the account's address, from the `id_token` the token endpoint sent.
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
        label="Google Drive",
    )


def no_client(context: LoginContext) -> Tokens:
    """What signing in does before any client id exists: say so, and stop."""
    raise CredentialError(
        "Google Drive has no OAuth client id yet. Create a Desktop-app client in a Google "
        "Cloud project with the Drive API enabled, then set "
        "plugins_settings.google-drive.client_id to its id in config.json."
    )


class GoogleDrivePlugin(Plugin):
    name = PLUGIN
    description = "Google Drive: find, read and download; upload, organise and share with a yes."

    def register(self, ctx: PluginContext) -> None:
        client_id = str(ctx.setting("client_id", "") or "").strip() or DEFAULT_CLIENT_ID
        ctx.register_login(LOGIN, client(client_id) if client_id else no_client)
        drive = Drive(ctx)
        for tool in (Search, Read, Info, Download, Upload, Create, Organise, Share):
            ctx.register_tool(tool(drive), toolset="Google Drive")
