"""The Google Maps plugin, driven the way Atlas drives it with Google replaced:
`request` is the plugin's module-level name, and a fake answers the shapes Routes
API, Places API (New) and Geocoding API v4 document. Nothing reaches Google.

Run from a checkout of Atlas (`uv run --project ../Atlas pytest google-maps/tests`).
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from atlas.web.client import Response

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "atlas_plugin_google_maps", HERE.parent / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gm = _load()

KEY = "AIza-test-key-123"
NOW = datetime(2026, 10, 1, 13, 0).astimezone()


class Call(SimpleNamespace):
    method: str
    url: str
    host: str
    path: str
    query: dict[str, str]
    body: Any
    headers: dict[str, str]


Answer = Callable[[Call], tuple[int, Any]]


class Fake:
    """Maps Platform, as far as these tests need it."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.routes: list[tuple[str, Answer]] = []

    def on(self, pattern: str, answer: Answer) -> None:
        self.routes.insert(0, (pattern, answer))

    async def request(self, method: str, url: str, **kwargs: Any) -> Response:
        parts = urlsplit(url)
        call = Call(
            method=method,
            url=url,
            host=parts.hostname or "",
            path=parts.path,
            query=dict(parse_qsl(parts.query)),
            body=kwargs.get("json"),
            headers=dict(kwargs.get("headers") or {}),
        )
        self.calls.append(call)
        status, body = 404, {"error": {"code": 404, "message": f"no route for {call.path}"}}
        for pattern, answer in self.routes:
            if re.fullmatch(pattern, call.path):
                status, body = answer(call)
                break
        raw = json.dumps(body).encode()
        return Response(
            url=url, status=status, headers=(("retry-after", "0"),), body=raw, truncated=False
        )


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> Fake:
    fake = Fake()
    monkeypatch.setattr(gm, "request", fake.request)
    monkeypatch.setattr(gm, "now", lambda: NOW)
    return fake


class Context:
    """A `PluginContext`, as far as the tools use it."""

    def __init__(self, workspace: Path, **settings: Any) -> None:
        self.workspace = workspace
        self.settings = settings


def tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, key: str = KEY, **settings: Any
) -> dict[str, Any]:
    """The four tools, with the key - or an empty `.env` - in the workspace's own
    `.atlas/.env`, so the person's real one is never read."""
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    monkeypatch.delenv("MY_MAPS_KEY", raising=False)
    env = tmp_path / ".atlas"
    env.mkdir(exist_ok=True)
    variable = settings.get("api_key_env") or "GOOGLE_MAPS_API_KEY"
    (env / ".env").write_text(f"{variable}={key}\n" if key else "# empty\n", encoding="utf-8")
    maps = gm.Maps(Context(tmp_path, **settings))
    made = (gm.Route, gm.Places, gm.Place, gm.Geocode)
    return {tool.name: tool(maps) for tool in made}


def route(
    seconds: int = 1440,
    static: int = 1140,
    meters: int = 18200,
    description: str = "M5",
    steps: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "duration": f"{seconds}s",
        "staticDuration": f"{static}s",
        "distanceMeters": meters,
        "description": description,
        "localizedValues": {"distance": {"text": f"{meters / 1000:.1f} km"}},
        "legs": [{"steps": steps or []}],
        **extra,
    }


def turn(text: str) -> dict[str, Any]:
    return {"travelMode": "DRIVE", "navigationInstruction": {"instructions": text}}


PHARMACY = {
    "id": "ChIJpharmacy0001",
    "displayName": {"text": "Corner Pharmacy", "languageCode": "en"},
    "formattedAddress": "1 King St, Newtown NSW 2042, Australia",
    "googleMapsUri": "https://maps.google.com/?cid=111",
    "businessStatus": "OPERATIONAL",
    "utcOffsetMinutes": 600,
    "rating": 4.4,
    "userRatingCount": 210,
    "currentOpeningHours": {
        "openNow": True,
        "weekdayDescriptions": [
            f"{day}: 8:00 AM - 9:00 PM"
            for day in (
                "Monday",
                "Tuesday",
                "Wednesday",
                "Thursday",
                "Friday",
                "Saturday",
                "Sunday",
            )
        ],
    },
    "nationalPhoneNumber": "(02) 9000 0000",
    "websiteUri": "https://corner.example",
}


# -- the key -------------------------------------------------------------------------


async def test_nothing_reaches_google_without_a_key(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = tools(tmp_path, monkeypatch, key="")
    for name, arguments in (
        ("maps_route", {"destination": "Sydney Airport", "origin": "Newtown"}),
        ("maps_places", {"query": "pharmacy"}),
        ("maps_place", {"place_id": "ChIJpharmacy0001"}),
        ("maps_geocode", {"query": "Town Hall, Sydney"}),
    ):
        result = await made[name].run(**arguments)
        assert result.is_error
        assert result.content.startswith("no GOOGLE_MAPS_API_KEY: ")
        assert "Routes API" in result.content and "~/.atlas/.env" in result.content
    assert google.calls == []


async def test_the_key_goes_in_a_header_and_never_in_the_address(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    google.on(r"/v4/geocode/address", lambda c: (200, {"results": []}))
    made = tools(tmp_path, monkeypatch)
    await made["maps_geocode"].run(query="Town Hall, Sydney")
    call = google.calls[0]
    assert call.headers["X-Goog-Api-Key"] == KEY
    assert KEY not in call.url
    assert "key" not in call.query


async def test_api_key_env_renames_the_variable(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    google.on(r"/v4/geocode/address", lambda c: (200, {"results": []}))
    made = tools(tmp_path, monkeypatch, key="other-key", api_key_env="MY_MAPS_KEY")
    await made["maps_geocode"].run(query="x st")
    assert google.calls[0].headers["X-Goog-Api-Key"] == "other-key"

    empty = tools(tmp_path, monkeypatch, key="", api_key_env="MY_MAPS_KEY")
    result = await empty["maps_geocode"].run(query="x st")
    assert result.content.startswith("no MY_MAPS_KEY: ")


def test_every_tool_is_owner_only_untrusted_and_ungated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for tool in tools(tmp_path, monkeypatch).values():
        assert tool.trusted_only and tool.untrusted and not tool.gated


def test_the_plugin_installs_its_four_tools(tmp_path: Path) -> None:
    from atlas.plugins.install import install_one
    from atlas.tools.registry import ToolRegistry

    provision = install_one(gm.GoogleMapsPlugin(), workspace=tmp_path, tools=ToolRegistry())
    assert provision.ok, provision.error
    assert sorted(provision.tools) == ["maps_geocode", "maps_place", "maps_places", "maps_route"]


# -- errors --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error", "said"),
    [
        (
            400,
            {"message": "API key not valid. Please pass a valid API key.", "status": "INVALID"},
            "not valid",
        ),
        (
            403,
            {
                "message": "Routes API has not been used in project 1 before or it is disabled.",
                "status": "PERMISSION_DENIED",
                "details": [{"reason": "SERVICE_DISABLED"}],
            },
            "Routes API is not enabled",
        ),
        (
            403,
            {
                "message": "Requests to this API are blocked.",
                "status": "PERMISSION_DENIED",
                "details": [{"reason": "API_KEY_SERVICE_BLOCKED"}],
            },
            "does not allow the Routes API",
        ),
        (
            403,
            {"message": "x", "details": [{"reason": "BILLING_DISABLED"}]},
            "billing is not enabled",
        ),
    ],
)
async def test_google_errors_become_sentences_that_say_what_to_fix(
    google: Fake,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    error: dict[str, Any],
    said: str,
) -> None:
    google.on(r"/directions/v2:computeRoutes", lambda c: (status, {"error": error}))
    made = tools(tmp_path, monkeypatch)
    result = await made["maps_route"].run(destination="Airport", origin="Newtown")
    assert result.is_error and said in result.content
    assert KEY not in result.content


async def test_a_429_is_retried_once(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter([(429, {"error": {"status": "RESOURCE_EXHAUSTED"}}), (200, {"results": []})])
    google.on(r"/v4/geocode/address", lambda c: next(answers))
    made = tools(tmp_path, monkeypatch)
    result = await made["maps_geocode"].run(query="x st")
    assert not result.is_error and len(google.calls) == 2


# -- maps_route ----------------------------------------------------------------------


async def test_a_drive_is_traffic_aware_and_shows_both_times(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = [turn(f"Turn {n}") for n in range(15)]
    google.on(
        r"/directions/v2:computeRoutes",
        lambda c: (200, {"routes": [route(steps=steps, warnings=["This route has tolls."])]}),
    )
    made = tools(tmp_path, monkeypatch, units="metric", language="en-AU", region="AU")
    result = await made["maps_route"].run(destination="Sydney Airport", origin="Newtown")

    assert not result.is_error, result.content
    call = google.calls[0]
    assert call.host == "routes.googleapis.com" and call.method == "POST"
    assert call.body["travelMode"] == "DRIVE"
    assert call.body["routingPreference"] == "TRAFFIC_AWARE"
    assert call.body["origin"] == {"address": "Newtown"}
    assert call.body["units"] == "METRIC"
    assert call.body["languageCode"] == "en-AU" and call.body["regionCode"] == "au"
    assert "departureTime" not in call.body
    mask = call.headers["X-Goog-FieldMask"]
    assert "routes.staticDuration" in mask and "polyline" not in mask
    text = result.content
    assert "24 min in traffic (19 min without traffic), 18.2 km" in text
    assert "Turn 0" in text and "Turn 11" in text and "Turn 12" not in text
    assert "3 more steps" in text
    assert "Note: This route has tolls." in text
    assert "https://www.google.com/maps/dir/?api=1&origin=Newtown" in text
    assert "travelmode=driving" in text


async def test_walking_sends_no_routing_preference_and_coordinates_as_latlng(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    google.on(r"/directions/v2:computeRoutes", lambda c: (200, {"routes": [route(600, 600)]}))
    made = tools(tmp_path, monkeypatch)
    result = await made["maps_route"].run(
        destination="-33.8688, 151.2093", origin="place_id:ChIJorigin000", mode="walk"
    )
    body = google.calls[0].body
    assert "routingPreference" not in body and body["travelMode"] == "WALK"
    assert body["destination"] == {
        "location": {"latLng": {"latitude": -33.8688, "longitude": 151.2093}}
    }
    assert body["origin"] == {"placeId": "ChIJorigin000"}
    assert "10 min, 18.2 km" in result.content and "without traffic" not in result.content
    assert "origin_place_id=ChIJorigin000" in result.content


async def test_the_origin_defaults_to_home_and_is_needed_without_it(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    google.on(r"/directions/v2:computeRoutes", lambda c: (200, {"routes": [route()]}))
    made = tools(tmp_path, monkeypatch, home="12 Example St, Newtown")
    await made["maps_route"].run(destination="Airport")
    assert google.calls[0].body["origin"] == {"address": "12 Example St, Newtown"}

    google.calls.clear()
    bare = tools(tmp_path, monkeypatch)
    result = await bare["maps_route"].run(destination="Airport")
    assert result.is_error and "home" in result.content
    assert google.calls == []


async def test_depart_at_is_sent_in_utc_and_a_past_one_is_refused(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    google.on(r"/directions/v2:computeRoutes", lambda c: (200, {"routes": [route()]}))
    made = tools(tmp_path, monkeypatch)
    later = NOW + timedelta(hours=2)
    await made["maps_route"].run(destination="A", origin="B", depart_at=later.isoformat())
    assert google.calls[0].body["departureTime"] == gm.rfc3339(later)

    result = await made["maps_route"].run(destination="A", origin="B", depart_at="09:00")
    assert result.is_error and "has passed" in result.content
    assert len(google.calls) == 1


async def test_transit_arrive_by_is_googles_own_and_lists_lines_and_stops(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = [
        {"travelMode": "WALK", "staticDuration": "240s"},
        {"travelMode": "WALK", "staticDuration": "60s"},
        {
            "travelMode": "TRANSIT",
            "staticDuration": "900s",
            "transitDetails": {
                "headsign": "Hornsby",
                "stopCount": 4,
                "stopDetails": {
                    "departureStop": {"name": "Central"},
                    "arrivalStop": {"name": "Wynyard"},
                },
                "localizedValues": {
                    "departureTime": {"time": {"text": "2:05 PM"}},
                    "arrivalTime": {"time": {"text": "2:20 PM"}},
                },
                "transitLine": {
                    "name": "North Shore Line",
                    "nameShort": "T1",
                    "vehicle": {"name": {"text": "Train"}, "type": "HEAVY_RAIL"},
                },
            },
        },
        {"travelMode": "WALK", "staticDuration": "120s"},
    ]
    google.on(
        r"/directions/v2:computeRoutes",
        lambda c: (
            200,
            {
                "routes": [
                    route(
                        1500,
                        1500,
                        steps=steps,
                        localizedValues={
                            "distance": {"text": "9.0 km"},
                            "transitFare": {"text": "A$4.20"},
                        },
                    )
                ]
            },
        ),
    )
    made = tools(tmp_path, monkeypatch)
    result = await made["maps_route"].run(
        destination="Wynyard",
        origin="Newtown",
        mode="transit",
        arrive_by="15:00",
        transit_modes=["train"],
    )
    body = google.calls[0].body
    assert len(google.calls) == 1
    assert body["arrivalTime"] == gm.rfc3339(NOW.replace(hour=15))
    assert body["transitPreferences"] == {"allowedTravelModes": ["TRAIN"]}
    assert "routingPreference" not in body
    text = result.content
    assert "Walk 5 min" in text
    assert "Train T1 toward Hornsby: Central 2:05 PM → Wynyard 2:20 PM, 4 stops" in text
    assert "Walk 2 min" in text
    assert "Fare: A$4.20" in text
    assert "travelmode=transit" in text


async def test_driving_arrive_by_is_an_estimate_from_traffic_then(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Now it takes 30 min; leaving at 14:30 it takes 40; leaving at 14:20, 41.
    Each probe is a small request; the last is the whole route."""
    target = NOW.replace(hour=15)
    by_departure = {
        gm.rfc3339(target - timedelta(minutes=30)): 2400,
        gm.rfc3339(target - timedelta(minutes=40)): 2460,
    }

    def answer(call: Call) -> tuple[int, Any]:
        took = by_departure.get(call.body.get("departureTime", ""), 1800)
        return 200, {"routes": [route(took, 1500)]}

    google.on(r"/directions/v2:computeRoutes", answer)
    made = tools(tmp_path, monkeypatch)
    result = await made["maps_route"].run(
        destination="Airport", origin="Newtown", arrive_by="15:00"
    )

    assert not result.is_error, result.content
    assert len(google.calls) == 4
    assert all("arrivalTime" not in c.body for c in google.calls)
    assert [c.headers["X-Goog-FieldMask"] for c in google.calls[:3]] == [gm.ESTIMATE_FIELDS] * 3
    assert google.calls[-1].body["departureTime"] == gm.rfc3339(target - timedelta(minutes=41))
    assert "Leave by 14:19 to arrive by 15:00 - an estimate" in result.content


async def test_driving_arrive_by_too_soon_says_leave_now_and_how_late(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    google.on(r"/directions/v2:computeRoutes", lambda c: (200, {"routes": [route(3600, 3000)]}))
    made = tools(tmp_path, monkeypatch)
    result = await made["maps_route"].run(destination="A", origin="B", arrive_by="13:30")
    assert len(google.calls) == 2  # one probe, then the route itself
    assert "Leave now" in result.content and "30 min late" in result.content


async def test_alternatives_are_each_shown(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    google.on(
        r"/directions/v2:computeRoutes",
        lambda c: (200, {"routes": [route(description="M5"), route(1600, description="A36")]}),
    )
    made = tools(tmp_path, monkeypatch)
    result = await made["maps_route"].run(destination="A", origin="B", alternatives=True)
    assert google.calls[0].body["computeAlternativeRoutes"] is True
    assert "Route 1 via M5" in result.content and "Route 2 via A36" in result.content


async def test_no_route_is_a_plain_answer(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    google.on(r"/directions/v2:computeRoutes", lambda c: (200, {}))
    made = tools(tmp_path, monkeypatch)
    result = await made["maps_route"].run(destination="A", origin="B", mode="transit")
    assert not result.is_error and "no transit route" in result.content


# -- maps_places and maps_place ------------------------------------------------------


async def test_places_search_near_home_with_open_now(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    google.on(r"/v1/places:searchText", lambda c: (200, {"places": [PHARMACY]}))
    made = tools(tmp_path, monkeypatch, home="Newtown NSW", max_results=3)
    result = await made["maps_places"].run(query="pharmacy", open_now=True)

    call = google.calls[0]
    assert call.host == "places.googleapis.com" and call.method == "POST"
    assert call.body == {"textQuery": "pharmacy near Newtown NSW", "pageSize": 3, "openNow": True}
    mask = call.headers["X-Goog-FieldMask"]
    assert "places.currentOpeningHours" in mask
    assert "reviews" not in mask and "places.photos" not in mask
    text = result.content
    assert "1. Corner Pharmacy" in text
    assert "4.4★ (210) · open now · today 8:00 AM - 9:00 PM" in text
    assert "(02) 9000 0000 · https://corner.example" in text
    assert "place id ChIJpharmacy0001 · https://maps.google.com/?cid=111" in text


async def test_places_near_coordinates_is_a_location_bias(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    google.on(r"/v1/places:searchText", lambda c: (200, {}))
    made = tools(tmp_path, monkeypatch)
    result = await made["maps_places"].run(query="coffee", near="-33.9,151.18", radius_km=2)
    body = google.calls[0].body
    assert body["textQuery"] == "coffee"
    assert body["locationBias"]["circle"] == {
        "center": {"latitude": -33.9, "longitude": 151.18},
        "radius": 2000.0,
    }
    assert body["pageSize"] == 5
    assert "No places match" in result.content


async def test_a_closed_place_says_so(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = {**PHARMACY, "businessStatus": "CLOSED_PERMANENTLY"}
    google.on(r"/v1/places:searchText", lambda c: (200, {"places": [gone]}))
    made = tools(tmp_path, monkeypatch)
    result = await made["maps_places"].run(query="pharmacy", near="Newtown")
    assert "permanently closed" in result.content and "open now" not in result.content


async def test_place_details_gives_the_week_and_skips_reviews_unless_asked(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    google.on(
        r"/v1/places/[^/]+", lambda c: (200, {**PHARMACY, "priceLevel": "PRICE_LEVEL_MODERATE"})
    )
    made = tools(tmp_path, monkeypatch, language="en-AU")
    result = await made["maps_place"].run(place_id="places/ChIJpharmacy0001")

    call = google.calls[0]
    assert call.method == "GET" and call.path == "/v1/places/ChIJpharmacy0001"
    assert call.query == {"languageCode": "en-AU"}
    assert "reviews" not in call.headers["X-Goog-FieldMask"]
    assert "Hours this week:" in result.content and "Sunday: 8:00 AM" in result.content
    assert "price: moderate" in result.content
    assert "Reviews" not in result.content


async def test_place_reviews_are_capped(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviews = [
        {
            "rating": 5,
            "text": {"text": "Great " * 200},
            "relativePublishTimeDescription": "a week ago",
            "authorAttribution": {"displayName": f"Person {n}"},
        }
        for n in range(5)
    ]
    detailed = {
        **PHARMACY,
        "reviews": reviews,
        "reviewSummary": {
            "text": {"text": "People like the friendly staff."},
            "disclosureText": {"text": "Summarized with Gemini"},
        },
    }
    google.on(r"/v1/places/[^/]+", lambda c: (200, detailed))
    made = tools(tmp_path, monkeypatch)
    result = await made["maps_place"].run(place_id="ChIJpharmacy0001", reviews=True)

    assert "reviewSummary" in google.calls[0].headers["X-Goog-FieldMask"]
    text = result.content
    assert "What reviewers say: People like the friendly staff." in text
    assert "(Summarized with Gemini)" in text
    assert "Person 2" in text and "Person 3" not in text
    assert all(len(line) < gm.REVIEW_CHARS + 60 for line in text.splitlines())


async def test_a_place_id_that_is_not_one_is_refused(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = tools(tmp_path, monkeypatch)
    result = await made["maps_place"].run(place_id="../../v1/other")
    assert result.is_error and google.calls == []


# -- maps_geocode --------------------------------------------------------------------


async def test_geocode_forward_and_reverse_use_the_v4_endpoints(
    google: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = {
        "results": [
            {
                "formattedAddress": "483 George St, Sydney NSW 2000, Australia",
                "location": {"latitude": -33.8731, "longitude": 151.2061},
                "placeId": "ChIJtownhall001",
                "granularity": "ROOFTOP",
                "types": ["street_address"],
            }
        ]
    }
    google.on(r"/v4/geocode/(address|location)", lambda c: (200, found))
    made = tools(tmp_path, monkeypatch, region="AU")

    result = await made["maps_geocode"].run(query="Sydney Town Hall")
    call = google.calls[0]
    assert call.host == "geocode.googleapis.com" and call.path == "/v4/geocode/address"
    assert call.query == {"addressQuery": "Sydney Town Hall", "regionCode": "au"}
    assert call.headers["X-Goog-FieldMask"] == gm.GEOCODE_FIELDS
    assert "483 George St" in result.content
    assert "-33.873100,151.206100 · rooftop · place id ChIJtownhall001" in result.content

    await made["maps_geocode"].run(query="-33.8731, 151.2061")
    call = google.calls[1]
    assert call.path == "/v4/geocode/location"
    assert call.query["location.latitude"] == "-33.8731"
    assert call.query["location.longitude"] == "151.2061"


# -- small pieces --------------------------------------------------------------------


def test_latlng_only_takes_real_coordinates() -> None:
    assert gm.latlng("-33.8,151.2") == (-33.8, 151.2)
    assert gm.latlng("95,10") is None
    assert gm.latlng("12 George St") is None


def test_minutes_and_distance_read_naturally() -> None:
    assert gm.minutes(0) == "0 min"
    assert gm.minutes(20) == "1 min"
    assert gm.minutes(3900) == "1 h 5 min"
    assert gm.minutes(7200) == "2 h"
    assert gm.distance(850, "metric") == "850 m"
    assert gm.distance(18200, "imperial") == "11.3 mi"
