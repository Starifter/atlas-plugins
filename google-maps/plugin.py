"""Google Maps: four read-only tools over Maps Platform's current APIs.

Routes API for a journey, Places API (New) for finding a place and reading its
hours, Geocoding API v4 for an address and its coordinates. Nothing here changes
anything anywhere, so nothing is gated; but every request bills the person's own
Cloud project, and `home` is where they live, so every tool is `trusted_only` -
offered only in a session an owner holds - and `untrusted`, because a place's
name and its reviews are whatever somebody else wrote.

The key is an API key, not a sign-in. It is read fresh from the environment or
`~/.atlas/.env` on every call, the way `brave` reads its own, never stored as a
setting, and sent only in the `X-Goog-Api-Key` header - never in an address,
which is the part of a request that ends up in logs.

Field masks are kept to what a tool shows, because Google bills a request at the
highest tier any one field reaches (`PLUGIN.md` says which tier each tool hits).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from atlas.sdk.auth import SecretRef, read_dotenv, resolve
from atlas.sdk.plugin_entry import Plugin, PluginContext
from atlas.sdk.runtime import CredentialError, ToolError
from atlas.sdk.tool_plugin import Tool, ToolResult
from atlas.sdk.web import Response, WebError, request

PLUGIN = "google-maps"
KEY_ENV = "GOOGLE_MAPS_API_KEY"

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
PLACES_API = "https://places.googleapis.com/v1"
GEOCODE_API = "https://geocode.googleapis.com/v4/geocode"
HOSTS = {
    "routes.googleapis.com": "Routes API",
    "places.googleapis.com": "Places API (New)",
    "geocode.googleapis.com": "Geocoding API",
}
"""The only hosts the key is ever sent to, and the name each has in the Cloud console."""

USER_AGENT = "atlas-google-maps"
RESPONSE_BYTES = 2_000_000
RETRY_CAP = 5.0
"""A 429 or a 5xx is waited out once, for at most this long."""

ROUTE_FIELDS = (
    "routes.duration,routes.staticDuration,routes.distanceMeters,routes.description,"
    "routes.localizedValues,routes.warnings,"
    "routes.legs.steps.travelMode,routes.legs.steps.staticDuration,"
    "routes.legs.steps.navigationInstruction.instructions,routes.legs.steps.transitDetails"
)
"""Compute Routes: Essentials for walking, cycling and transit; Pro for driving,
which is traffic-aware; Enterprise for two-wheeler. None of these fields moves it."""
ESTIMATE_FIELDS = "routes.duration,routes.staticDuration"
"""What an arrive-by drive asks on the way to its answer: the time, nothing else."""

PLACE_LIST_FIELDS = (
    "places.id,places.displayName,places.formattedAddress,places.googleMapsUri,"
    "places.businessStatus,places.utcOffsetMinutes,places.rating,places.userRatingCount,"
    "places.currentOpeningHours,places.nationalPhoneNumber,places.websiteUri"
)
"""Text Search Enterprise: hours, phone, website and rating are Enterprise fields."""
PLACE_FIELDS = (
    "id,displayName,formattedAddress,googleMapsUri,businessStatus,utcOffsetMinutes,"
    "rating,userRatingCount,priceLevel,currentOpeningHours,nationalPhoneNumber,"
    "internationalPhoneNumber,websiteUri"
)
"""Place Details Enterprise."""
REVIEW_FIELDS = "editorialSummary,reviewSummary,reviews"
"""Added only when reviews are asked for: Place Details Enterprise + Atmosphere."""
GEOCODE_FIELDS = (
    "results.formattedAddress,results.location,results.placeId,results.types,results.granularity"
)
"""Geocoding (Essentials)."""

MODES = {
    "drive": "DRIVE",
    "walk": "WALK",
    "bicycle": "BICYCLE",
    "transit": "TRANSIT",
    "two-wheeler": "TWO_WHEELER",
}
LINK_MODES = {
    "drive": "driving",
    "walk": "walking",
    "bicycle": "bicycling",
    "transit": "transit",
    "two-wheeler": "two-wheeler",
}
TRAFFIC_MODES = ("drive", "two-wheeler")
"""Routes API takes a routing preference only for these, and refuses it for the rest."""
TRANSIT_MODES = {
    "bus": "BUS",
    "subway": "SUBWAY",
    "train": "TRAIN",
    "light_rail": "LIGHT_RAIL",
    "rail": "RAIL",
}
UNITS = {"metric": "METRIC", "imperial": "IMPERIAL"}

STEPS_SHOWN = 12
"""The most turn-by-turn instructions a route shows; Maps has the rest."""
REVIEWS_SHOWN = 3
REVIEW_CHARS = 300
NAME_MAX = 120
PAST_GRACE = timedelta(minutes=5)
"""A departure this little in the past is taken as now: the model's clock and ours."""
CLOSE_ENOUGH = timedelta(minutes=2)
"""An arrive-by estimate stops iterating when another pass would move it less than this."""

LATLNG = re.compile(r"\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*")
CLOCK = re.compile(r"\s*(\d{1,2}):(\d{2})\s*")
PLACE_ID = re.compile(r"[A-Za-z0-9_-]{10,}")
SECONDS = re.compile(r"(\d+(?:\.\d+)?)s")


def now() -> datetime:
    """The time, aware and local. A function so a test can stop the clock."""
    return datetime.now().astimezone()


def no_key(variable: str) -> str:
    return (
        f"no {variable}: Google Maps needs an API key. In a Google Cloud project with billing "
        "on, enable Places API (New), Routes API and Geocoding API, create an API key under "
        f"APIs & Services > Credentials, restrict it to those three APIs, then put "
        f"{variable}=<the key> in ~/.atlas/.env"
    )


# -- talking to Google ---------------------------------------------------------


class GoogleError(Exception):
    """A status Google answered with, as a sentence the model can repeat."""


def _error_of(response: Response) -> tuple[str, str, str]:
    """Google's message, its status word, and the first ErrorInfo reason."""
    try:
        data = json.loads(response.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "", "", ""
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return str(error or ""), "", ""
    reasons = [
        str(d.get("reason") or "") for d in error.get("details") or () if isinstance(d, dict)
    ]
    reason = next((r for r in reasons if r), "")
    return str(error.get("message") or ""), str(error.get("status") or ""), reason


def _explain(status: int, api: str, text: str, state: str, reason: str, variable: str) -> str:
    detail = f": {clean(text, 300)}" if text else ""
    if reason == "API_KEY_INVALID" or (status == 400 and "api key not valid" in text.lower()):
        return f"Google says the key in {variable} is not valid - check it in ~/.atlas/.env"
    if reason == "SERVICE_DISABLED" or "has not been used in project" in text:
        return (
            f"the {api} is not enabled in the key's Cloud project - enable it at "
            "https://console.cloud.google.com/apis/library and try again in a minute"
        )
    if reason == "API_KEY_SERVICE_BLOCKED" or "are blocked" in text:
        return (
            f"the key is restricted and does not allow the {api} - add it under the key's "
            "API restrictions at https://console.cloud.google.com/apis/credentials"
        )
    if reason == "BILLING_DISABLED" or "billing" in text.lower():
        return (
            "billing is not enabled on the key's Cloud project - Maps Platform needs a "
            "billing account even inside the free monthly usage"
        )
    if status == 429 or state == "RESOURCE_EXHAUSTED":
        return f"Google's {api} quota is used up for now (429){detail}"
    if status == 404:
        return f"not found (404){detail} - check the place id"
    return f"HTTP {status} from the {api}{detail}"


class Maps:
    """What the tools share: the key, the settings, one request."""

    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.workspace = Path(ctx.workspace)
        self.settings = dict(ctx.settings)

    def setting(self, key: str, default: Any) -> Any:
        value = self.settings.get(key)
        return default if value is None else value

    def text(self, key: str) -> str:
        return str(self.setting(key, "") or "").strip()

    @property
    def variable(self) -> str:
        return self.text("api_key_env") or KEY_ENV

    def key(self) -> str:
        """Read fresh, never cached: the `secret` tool writes `.env` mid-session."""
        try:
            key = resolve(SecretRef("env", self.variable), dotenv=read_dotenv(self.workspace))
        except CredentialError:
            raise CredentialError(no_key(self.variable)) from None
        if not key.strip():
            raise CredentialError(no_key(self.variable))
        return key.strip()

    @property
    def language(self) -> str:
        return self.text("language")

    @property
    def region(self) -> str:
        return self.text("region").lower()

    @property
    def home(self) -> str:
        return self.text("home")

    async def call(
        self,
        method: str,
        url: str,
        *,
        fields: str,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
    ) -> dict[str, Any]:
        """One request to Google, decoded. Raises `GoogleError` for a status that is not 2xx."""
        host = urlsplit(url).hostname or ""
        api = HOSTS.get(host)
        if api is None:  # pragma: no cover - every caller names a constant
            raise GoogleError("refused: the key goes to Google's Maps hosts and nowhere else")
        key = self.key()
        query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        if query:
            url += "?" + urlencode(query, doseq=True)
        headers = {"X-Goog-Api-Key": key, "X-Goog-FieldMask": fields}
        for attempt in range(2):
            response = await request(
                method,
                url,
                json=body,
                headers=headers,
                max_bytes=RESPONSE_BYTES,
                timeout=30.0,
                user_agent=USER_AGENT,
            )
            if 200 <= response.status < 300:
                try:
                    data = json.loads(response.body.decode("utf-8") or "{}")
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise GoogleError(f"the {api} sent something unreadable: {exc}") from None
                return data if isinstance(data, dict) else {}
            if attempt == 0 and (response.status == 429 or response.status >= 500):
                await asyncio.sleep(_retry_after(response))
                continue
            text, state, reason = _error_of(response)
            raise GoogleError(_explain(response.status, api, text, state, reason, self.variable))
        raise AssertionError("unreachable")  # pragma: no cover


def _retry_after(response: Response) -> float:
    try:
        return min(max(float(response.header("retry-after") or 1.0), 0.0), RETRY_CAP)
    except ValueError:
        return 1.0


# -- reading what the person said ----------------------------------------------


def latlng(text: str) -> tuple[float, float] | None:
    """`-33.86, 151.21` as numbers, or None for anything that is not a coordinate pair."""
    match = LATLNG.fullmatch(text or "")
    if not match:
        return None
    lat, lng = float(match.group(1)), float(match.group(2))
    if -90 <= lat <= 90 and -180 <= lng <= 180:
        return lat, lng
    return None


def waypoint(text: str) -> dict[str, Any]:
    """An address, `lat,lng`, or `place_id:<id>`, as Routes API wants it."""
    point = latlng(text)
    if point:
        return {"location": {"latLng": {"latitude": point[0], "longitude": point[1]}}}
    if text.startswith("place_id:"):
        return {"placeId": text[len("place_id:") :].strip()}
    return {"address": text}


def place_id_of(text: str) -> str:
    """The id a search showed, with or without `places/` in front of it."""
    value = str(text or "").strip().removeprefix("places/").removeprefix("place_id:")
    if not PLACE_ID.fullmatch(value):
        raise ToolError("say which place by the id maps_places showed (place id ChIJ...)")
    return value


def when(text: str, field: str) -> datetime:
    """`2026-10-01T15:00`, with or without an offset, or `15:00` for today. A time
    with no offset is this machine's local time."""
    value = str(text or "").strip()
    clock = CLOCK.fullmatch(value)
    try:
        if clock:
            hour, minute = int(clock.group(1)), int(clock.group(2))
            return now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(
            f"{field}: {value!r} is not a time - write 2026-10-01T15:00 or 15:00 for today"
        ) from None
    return parsed if parsed.tzinfo else parsed.astimezone()


def rfc3339(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def seconds(value: Any) -> int:
    """Google's `1234s` as a number of seconds."""
    match = SECONDS.fullmatch(str(value or ""))
    return round(float(match.group(1))) if match else 0


# -- showing things ------------------------------------------------------------


def clean(text: Any, limit: int = NAME_MAX) -> str:
    """One line of someone else's words, capped."""
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def minutes(total: int) -> str:
    whole = max(round(total / 60), 1 if total > 0 else 0)
    hours, rest = divmod(whole, 60)
    if not hours:
        return f"{rest} min"
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def distance(meters: int, units: str) -> str:
    if units == "imperial":
        miles = meters / 1609.344
        return f"{miles:.1f} mi" if miles >= 0.2 else f"{round(meters * 3.28084)} ft"
    return f"{meters / 1000:.1f} km" if meters >= 1000 else f"{meters} m"


def clock(moment: datetime) -> str:
    return moment.astimezone().strftime("%H:%M")


def text_of(value: Any) -> str:
    """A LocalizedText (`{"text": ...}`) or a plain string, as one line."""
    if isinstance(value, dict):
        return clean(value.get("text"))
    return clean(value)


def directions_link(origin: str, destination: str, mode: str) -> str:
    """A google.com/maps/dir address (Maps URLs) that opens the same journey."""
    params: dict[str, str] = {"api": "1"}
    for name, value in (("origin", origin), ("destination", destination)):
        if value.startswith("place_id:"):
            params[f"{name}_place_id"] = value[len("place_id:") :].strip()
            params[name] = value[len("place_id:") :].strip()
        else:
            params[name] = value
    params["travelmode"] = LINK_MODES[mode]
    return "https://www.google.com/maps/dir/?" + urlencode(params)


def today_hours(place: Mapping[str, Any]) -> str:
    """The line of this week's hours for today where the place is."""
    hours = place.get("currentOpeningHours") or {}
    week = hours.get("weekdayDescriptions") if isinstance(hours, dict) else None
    if not isinstance(week, list) or len(week) != 7:
        return ""
    try:
        offset = timedelta(minutes=int(place.get("utcOffsetMinutes")))
    except (TypeError, ValueError):
        return clean(week[now().weekday()])
    # weekdayDescriptions starts on Monday, as `weekday()` does.
    return clean(week[(datetime.now(UTC) + offset).weekday()])


def open_state(place: Mapping[str, Any]) -> str:
    status = str(place.get("businessStatus") or "")
    if status == "CLOSED_PERMANENTLY":
        return "permanently closed"
    if status == "CLOSED_TEMPORARILY":
        return "temporarily closed"
    hours = place.get("currentOpeningHours")
    if not isinstance(hours, dict) or "openNow" not in hours:
        return ""
    return "open now" if hours.get("openNow") else "closed now"


def rating(place: Mapping[str, Any]) -> str:
    stars = place.get("rating")
    if not stars:
        return ""
    count = place.get("userRatingCount")
    return f"{stars}★" + (f" ({count})" if count else "")


def place_block(place: Mapping[str, Any], index: int = 0) -> list[str]:
    name = text_of(place.get("displayName")) or "(no name)"
    head = f"{index}. {name}" if index else name
    lines = [head]
    if place.get("formattedAddress"):
        lines.append(f"   {clean(place['formattedAddress'], 200)}")
    facts = [f for f in (rating(place), open_state(place)) if f]
    today = today_hours(place)
    if today:
        facts.append(f"today {today.partition(': ')[2] or today}")
    if facts:
        lines.append("   " + " · ".join(facts))
    contact = [
        clean(place.get(k), 200) for k in ("nationalPhoneNumber", "websiteUri") if place.get(k)
    ]
    if contact:
        lines.append("   " + " · ".join(contact))
    tail = [f"place id {place['id']}"] if place.get("id") else []
    if place.get("googleMapsUri"):
        tail.append(str(place["googleMapsUri"]))
    if tail:
        lines.append("   " + " · ".join(tail))
    return lines


# -- the tools -----------------------------------------------------------------


class MapsTool(Tool):
    """What all four share: owner-only, marked untrusted, failures as sentences."""

    trusted_only = True
    untrusted = True

    def __init__(self, maps: Maps) -> None:
        self.maps = maps

    async def run(self, **arguments: Any) -> ToolResult:
        try:
            said = await self.act(**arguments)
            return said if isinstance(said, ToolResult) else ToolResult.ok(said)
        except (CredentialError, ToolError, GoogleError, WebError) as exc:
            return ToolResult.error(str(exc))

    async def act(self, *args: Any, **arguments: Any) -> Any:
        raise NotImplementedError


PLACE = {
    "type": "string",
    "description": "An address, a place name, 'lat,lng', or place_id:<id> from maps_places.",
}


class Route(MapsTool):
    name = "maps_route"
    description = (
        "How long a journey takes and how to make it: driving (in current traffic), walking, "
        "cycling, public transport or two-wheeler. Gives the time with and without traffic, "
        "the distance, the steps (lines and stops for transit) and a Google Maps link. "
        "`arrive_by` answers 'when should I leave'. Times are this machine's local time "
        "unless they carry an offset. With no origin, the person's home setting."
    )
    parameters = {
        "type": "object",
        "properties": {
            "destination": PLACE,
            "origin": {**PLACE, "description": PLACE["description"] + " Default: home."},
            "mode": {"type": "string", "enum": sorted(MODES), "default": "drive"},
            "depart_at": {
                "type": "string",
                "description": "When to leave: 2026-10-01T15:00 or 15:00 for today. Default now.",
            },
            "arrive_by": {
                "type": "string",
                "description": (
                    "When to be there, instead of depart_at. Exact for transit; an estimate "
                    "for driving, worked out from traffic at the time it suggests leaving."
                ),
            },
            "alternatives": {"type": "boolean", "description": "Also show other routes."},
            "transit_modes": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(TRANSIT_MODES)},
                "description": "Transit only: prefer these, e.g. ['train'] for 'by train'.",
            },
            "avoid": {
                "type": "array",
                "items": {"type": "string", "enum": ["tolls", "highways", "ferries"]},
                "description": "Driving only.",
            },
        },
        "required": ["destination"],
    }

    async def act(
        self,
        destination: str,
        origin: str = "",
        mode: str = "drive",
        depart_at: str = "",
        arrive_by: str = "",
        alternatives: bool = False,
        transit_modes: Sequence[str] = (),
        avoid: Sequence[str] = (),
    ) -> str:
        mode = (mode or "drive").strip().lower().replace("_", "-")
        if mode not in MODES:
            raise ToolError(f"mode is one of {', '.join(sorted(MODES))}")
        start = self._place(origin, "origin")
        end = self._place(destination, "destination")
        if depart_at and arrive_by:
            raise ToolError("give depart_at or arrive_by, not both")
        body = self._body(start, end, mode, alternatives, transit_modes, avoid)
        current = now()
        leave: datetime | None = None
        target: datetime | None = None
        note = ""
        if depart_at:
            leave = when(depart_at, "depart_at")
            if leave < current - PAST_GRACE and mode != "transit":
                raise ToolError(
                    f"{clock(leave)} has passed - only transit can be looked up in the past"
                )
            if leave > current:
                body["departureTime"] = rfc3339(leave)
        elif arrive_by:
            target = when(arrive_by, "arrive_by")
            if target <= current:
                raise ToolError(f"{clock(target)} has passed - say a time still to come")
            if mode == "transit":
                body["arrivalTime"] = rfc3339(target)
            elif mode in TRAFFIC_MODES:
                leave, note = await self._estimate(body, target, current)
                if leave > current:
                    body["departureTime"] = rfc3339(leave)
        data = await self.maps.call("POST", ROUTES_URL, fields=ROUTE_FIELDS, body=body)
        routes = [r for r in data.get("routes") or () if isinstance(r, dict)]
        if not routes:
            return f"Google found no {mode} route from {start} to {end}." + (
                " Transit may not run then, or there." if mode == "transit" else ""
            )
        units = self.maps.text("units").lower()
        lines = [f"{mode.capitalize()} from {clean(start)} to {clean(end)}:"]
        for number, route in enumerate(routes, 1):
            if number > 1:
                lines.append("")
            lines += self._describe(route, number, len(routes), mode, units)
        if target is not None:
            lines.append("")
            lines.append(self._timing(routes[0], mode, target, leave, current, note))
        elif leave is not None and leave > current:
            lines.append(f"Leaving at {clock(leave)}.")
        lines.append(f"Open in Google Maps: {directions_link(start, end, mode)}")
        return "\n".join(lines)

    def _place(self, text: str, field: str) -> str:
        value = clean(text, 300)
        if not value or value.lower() in ("home", "my place"):
            home = self.maps.home
            if home:
                return home
            if not value:
                raise ToolError(
                    f"say the {field} - no home address is set "
                    f"(plugins_settings.{PLUGIN}.home in config.json)"
                )
            raise ToolError(f"no home address is set (plugins_settings.{PLUGIN}.home)")
        return value

    def _body(
        self,
        start: str,
        end: str,
        mode: str,
        alternatives: bool,
        transit_modes: Sequence[str],
        avoid: Sequence[str],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "origin": waypoint(start),
            "destination": waypoint(end),
            "travelMode": MODES[mode],
            "computeAlternativeRoutes": bool(alternatives),
        }
        if mode in TRAFFIC_MODES:
            # TRAFFIC_AWARE (Pro), not _OPTIMAL (Enterprise): good enough for "how long".
            body["routingPreference"] = "TRAFFIC_AWARE"
            modifiers = {
                f"avoid{a.capitalize()}": True
                for a in avoid
                if a in ("tolls", "highways", "ferries")
            }
            if modifiers:
                body["routeModifiers"] = modifiers
        if mode == "transit":
            wanted = [TRANSIT_MODES[m] for m in transit_modes if m in TRANSIT_MODES]
            if wanted:
                body["transitPreferences"] = {"allowedTravelModes": wanted}
        units = self.maps.text("units").lower()
        if units in UNITS:
            body["units"] = UNITS[units]
        if self.maps.language:
            body["languageCode"] = self.maps.language
        if self.maps.region:
            body["regionCode"] = self.maps.region
        return body

    async def _estimate(
        self, body: Mapping[str, Any], target: datetime, current: datetime
    ) -> tuple[datetime, str]:
        """Routes API takes an arrival time only for transit. For a drive, guess a
        departure from the time it takes now, ask how long it takes leaving then,
        and correct once or twice - at most three small requests."""
        probe = {k: v for k, v in body.items() if k != "computeAlternativeRoutes"}
        took = await self._duration(probe)
        leave = target - timedelta(seconds=took)
        for _ in range(2):
            if leave <= current:
                break
            took = await self._duration({**probe, "departureTime": rfc3339(leave)})
            better = target - timedelta(seconds=took)
            moved = abs(better - leave)
            leave = better
            if moved < CLOSE_ENOUGH:
                break
        return leave, f"traffic predicted for then, {minutes(took)}"

    async def _duration(self, body: Mapping[str, Any]) -> int:
        data = await self.maps.call("POST", ROUTES_URL, fields=ESTIMATE_FIELDS, body=dict(body))
        routes = [r for r in data.get("routes") or () if isinstance(r, dict)]
        if not routes:
            raise ToolError("Google found no route between those two places")
        return seconds(routes[0].get("duration")) or seconds(routes[0].get("staticDuration"))

    def _describe(
        self, route: Mapping[str, Any], number: int, count: int, mode: str, units: str
    ) -> list[str]:
        took = seconds(route.get("duration"))
        still = seconds(route.get("staticDuration"))
        local = route.get("localizedValues") or {}
        far = text_of(local.get("distance")) or distance(
            int(route.get("distanceMeters") or 0), units
        )
        if mode in TRAFFIC_MODES and still:
            time = f"{minutes(took)} in traffic ({minutes(still)} without traffic)"
        else:
            time = minutes(took or still)
        via = f" via {clean(route['description'])}" if route.get("description") else ""
        label = f"Route {number}{via}" if count > 1 else f"Route{via}"
        lines = [f"{label}: {time}, {far}"]
        fare = text_of(local.get("transitFare"))
        if fare:
            lines.append(f"Fare: {fare}")
        steps = [
            s
            for leg in route.get("legs") or ()
            if isinstance(leg, dict)
            for s in leg.get("steps") or ()
            if isinstance(s, dict)
        ]
        lines += self._transit(steps) if mode == "transit" else self._turns(steps)
        for warning in route.get("warnings") or ():
            lines.append(f"Note: {clean(warning, 200)}")
        return lines

    @staticmethod
    def _turns(steps: Sequence[Mapping[str, Any]]) -> list[str]:
        said = [
            clean((s.get("navigationInstruction") or {}).get("instructions"), 160) for s in steps
        ]
        said = [s for s in said if s]
        lines = [f"  {n}. {s}" for n, s in enumerate(said[:STEPS_SHOWN], 1)]
        if len(said) > STEPS_SHOWN:
            lines.append(f"  … and {len(said) - STEPS_SHOWN} more steps in Google Maps")
        return lines

    @staticmethod
    def _transit(steps: Sequence[Mapping[str, Any]]) -> list[str]:
        """Each ride by line and stop; the walking between them as one line each."""
        lines: list[str] = []
        walked = 0
        for step in steps:
            ride = step.get("transitDetails")
            if not isinstance(ride, dict):
                walked += seconds(step.get("staticDuration"))
                continue
            if walked:
                lines.append(f"  Walk {minutes(walked)}")
                walked = 0
            line = ride.get("transitLine") or {}
            vehicle = text_of((line.get("vehicle") or {}).get("name")) or "Transit"
            name = clean(line.get("nameShort") or line.get("name"))
            toward = f" toward {clean(ride['headsign'])}" if ride.get("headsign") else ""
            stops = ride.get("stopDetails") or {}
            times = ride.get("localizedValues") or {}
            board = clean((stops.get("departureStop") or {}).get("name"))
            alight = clean((stops.get("arrivalStop") or {}).get("name"))
            leaves = text_of((times.get("departureTime") or {}).get("time"))
            arrives = text_of((times.get("arrivalTime") or {}).get("time"))
            count = ride.get("stopCount")
            hop = f"{board} {leaves}".strip() + " → " + f"{alight} {arrives}".strip()
            tail = f", {count} stop{'s' if count != 1 else ''}" if count else ""
            lines.append(f"  {vehicle} {name}{toward}: {hop}{tail}".replace("  :", ":"))
        if walked:
            lines.append(f"  Walk {minutes(walked)}")
        return lines

    @staticmethod
    def _timing(
        route: Mapping[str, Any],
        mode: str,
        target: datetime,
        leave: datetime | None,
        current: datetime,
        note: str,
    ) -> str:
        took = seconds(route.get("duration")) or seconds(route.get("staticDuration"))
        if mode == "transit":
            return (
                f"Timetabled to arrive by {clock(target)}: be at the first stop above in time "
                "for its departure, allowing for any walk before it."
            )
        if mode not in TRAFFIC_MODES:
            go = target - timedelta(seconds=took)
            if go <= current:
                late = current + timedelta(seconds=took) - target
                return (
                    f"Leave now - you would arrive about {minutes(int(late.total_seconds()))} late."
                )
            return f"Leave by {clock(go)} to arrive by {clock(target)}."
        assert leave is not None
        if leave <= current:
            arrive = current + timedelta(seconds=took)
            if arrive <= target:
                return f"Leave now - you would arrive around {clock(arrive)} ({note})."
            late = int((arrive - target).total_seconds())
            return (
                f"Leave now - even so you would arrive around {clock(arrive)}, about "
                f"{minutes(late)} late ({note}). This is an estimate."
            )
        return (
            f"Leave by {clock(leave)} to arrive by {clock(target)} - an estimate ({note}); "
            "allow a few minutes' margin."
        )


class Places(MapsTool):
    name = "maps_places"
    description = (
        "Find places: 'a pharmacy open now near me', 'Bunnings Alexandria', 'thai food near "
        "Central Station'. Each result has its address, rating, whether it is open now, "
        "today's hours, phone, website, a Google Maps link and a place id for maps_place or "
        "maps_route. `near` defaults to the person's home setting."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for, in plain words."},
            "near": {
                "type": "string",
                "description": "An address or 'lat,lng' to search around. Default: home.",
            },
            "open_now": {"type": "boolean", "description": "Only places open right now."},
            "radius_km": {
                "type": "number",
                "description": "With near as 'lat,lng': how far around it. Default 5.",
            },
            "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["query"],
    }

    async def act(
        self,
        query: str,
        near: str = "",
        open_now: bool = False,
        radius_km: float = 0,
        max_results: int = 0,
    ) -> str:
        words = clean(query, 300)
        if not words:
            raise ToolError("say what to look for")
        limit = min(max(int(max_results or self.maps.setting("max_results", 5)), 1), 20)
        around = clean(near, 300) or self.maps.home
        body: dict[str, Any] = {"textQuery": words, "pageSize": limit}
        point = latlng(around)
        if point:
            radius = min(max(float(radius_km or 5), 0.1), 50) * 1000
            body["locationBias"] = {
                "circle": {
                    "center": {"latitude": point[0], "longitude": point[1]},
                    "radius": radius,
                }
            }
        elif around:
            # Text Search reads "near <address>" itself; no geocoding request needed.
            body["textQuery"] = f"{words} near {around}"
        if open_now:
            body["openNow"] = True
        if self.maps.language:
            body["languageCode"] = self.maps.language
        if self.maps.region:
            body["regionCode"] = self.maps.region
        data = await self.maps.call(
            "POST", f"{PLACES_API}/places:searchText", fields=PLACE_LIST_FIELDS, body=body
        )
        found = [p for p in data.get("places") or () if isinstance(p, dict)][:limit]
        if not found:
            return f"No places match {body['textQuery']!r}" + (
                " that are open now." if open_now else "."
            )
        lines = [f"{len(found)} place(s) for {body['textQuery']!r}:"]
        for index, place in enumerate(found, 1):
            lines += place_block(place, index)
        return "\n".join(lines)


class Place(MapsTool):
    name = "maps_place"
    description = (
        "One place in detail, by the place id maps_places showed: the week's opening hours "
        "(holidays included), phone, website, rating and a Maps link. Set reviews: true only "
        "when the person asks what people say about it - it costs more."
    )
    parameters = {
        "type": "object",
        "properties": {
            "place_id": {"type": "string", "description": "The id maps_places showed."},
            "reviews": {
                "type": "boolean",
                "description": "Also a summary of reviews and a few short ones.",
            },
        },
        "required": ["place_id"],
    }

    async def act(self, place_id: str, reviews: bool = False) -> str:
        pid = place_id_of(place_id)
        fields = f"{PLACE_FIELDS},{REVIEW_FIELDS}" if reviews else PLACE_FIELDS
        place = await self.maps.call(
            "GET",
            f"{PLACES_API}/places/{pid}",
            fields=fields,
            params={"languageCode": self.maps.language, "regionCode": self.maps.region},
        )
        lines = place_block(place)
        price = str(place.get("priceLevel") or "")
        if price.startswith("PRICE_LEVEL_") and price != "PRICE_LEVEL_UNSPECIFIED":
            lines.append("   price: " + price[len("PRICE_LEVEL_") :].replace("_", " ").lower())
        if place.get("internationalPhoneNumber"):
            lines.append(f"   international: {clean(place['internationalPhoneNumber'])}")
        hours = place.get("currentOpeningHours") or {}
        week = hours.get("weekdayDescriptions") if isinstance(hours, dict) else None
        if isinstance(week, list) and week:
            lines.append("Hours this week:")
            lines += [f"   {clean(day)}" for day in week]
        if reviews:
            lines += self._reviews(place)
        return "\n".join(lines)

    @staticmethod
    def _reviews(place: Mapping[str, Any]) -> list[str]:
        lines: list[str] = []
        about = text_of(place.get("editorialSummary"))
        if about:
            lines.append(f"About: {clean(about, REVIEW_CHARS)}")
        summary = place.get("reviewSummary") or {}
        said = text_of(summary.get("text")) if isinstance(summary, dict) else ""
        if said:
            lines.append(f"What reviewers say: {clean(said, REVIEW_CHARS * 2)}")
            disclosure = text_of(summary.get("disclosureText"))
            if disclosure:
                lines.append(f"   ({disclosure})")
        found = [r for r in place.get("reviews") or () if isinstance(r, dict)]
        if found:
            lines.append("Reviews:")
        for review in found[:REVIEWS_SHOWN]:
            who = text_of((review.get("authorAttribution") or {}).get("displayName")) or "someone"
            stars = f"{review['rating']}★ " if review.get("rating") else ""
            ago = clean(review.get("relativePublishTimeDescription"))
            body = clean(text_of(review.get("text")), REVIEW_CHARS)
            lines.append(f"   {stars}{who}{f', {ago}' if ago else ''}: {body}")
        if not lines:
            lines.append("No reviews or summary for this place.")
        return lines


class Geocode(MapsTool):
    name = "maps_geocode"
    description = (
        "Turn an address into coordinates, or 'lat,lng' into an address. Each result has "
        "the full address, the coordinates, how precise it is, and a place id."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "An address, or 'lat,lng' to look up."},
        },
        "required": ["query"],
    }

    async def act(self, query: str) -> str:
        value = clean(query, 300)
        if not value:
            raise ToolError("say an address or 'lat,lng'")
        point = latlng(value)
        params: dict[str, Any] = {
            "languageCode": self.maps.language,
            "regionCode": self.maps.region,
        }
        if point:
            url = f"{GEOCODE_API}/location"
            params["location.latitude"], params["location.longitude"] = point
        else:
            url = f"{GEOCODE_API}/address"
            params["addressQuery"] = value
        data = await self.maps.call("GET", url, fields=GEOCODE_FIELDS, params=params)
        found = [r for r in data.get("results") or () if isinstance(r, dict)]
        if not found:
            return f"Google found no address for {value!r}."
        most = min(max(int(self.maps.setting("max_results", 5)), 1), 20)
        lines = []
        for result in found[:most]:
            where = result.get("location") or {}
            lat, lng = where.get("latitude"), where.get("longitude")
            line = clean(result.get("formattedAddress"), 200) or "(no address)"
            facts = (
                [f"{lat:.6f},{lng:.6f}"]
                if isinstance(lat, float | int) and isinstance(lng, float | int)
                else []
            )
            if result.get("granularity"):
                facts.append(str(result["granularity"]).lower().replace("_", " "))
            if result.get("placeId"):
                facts.append(f"place id {result['placeId']}")
            lines.append(f"{line}\n   " + " · ".join(facts) if facts else line)
        return "\n".join(lines)


class GoogleMapsPlugin(Plugin):
    name = PLUGIN
    description = "Google Maps: journey times, places and their hours, addresses and coordinates."

    def register(self, ctx: PluginContext) -> None:
        maps = Maps(ctx)
        for tool in (Route, Places, Place, Geocode):
            ctx.register_tool(tool(maps), toolset="Google Maps")
