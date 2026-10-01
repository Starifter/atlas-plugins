---
name: google-maps
description: Google Maps - how long a journey takes now, when to leave, places near you and their hours, with your own API key.
categories: [maps, travel, productivity]
logo: logo.svg
version: "1.0.0"
requires_atlas_sdk: ">=1.34,<2"
contracts:
  tools:
    - maps_route
    - maps_places
    - maps_place
    - maps_geocode
config_schema:
  api_key_env:
    type: str
    default: GOOGLE_MAPS_API_KEY
    description: The variable the API key is read from, in the environment or ~/.atlas/.env.
  home:
    type: str
    default: ""
    description: Your home address - the origin of a route and the centre of a search when none is said.
  units:
    type: str
    default: ""
    description: metric or imperial. Empty lets Google choose from where the route starts.
  language:
    type: str
    default: ""
    description: The language results come back in, e.g. en-AU. Empty lets Google choose.
  region:
    type: str
    default: ""
    description: A two-letter region code (au, us, gb) that biases results and formatting.
  max_results:
    type: int
    default: 5
    description: The most places one search returns, and the most addresses one lookup shows. At most 20.
---

# google-maps

**Read this when:** you want Atlas to tell you how long it takes to get somewhere, when to leave,
or to find a place and check its hours - or you are setting up the key, or wondering what this
plugin sends to Google and what it costs.

**See also:** [`brave`](https://github.com/Starifter/atlas/tree/main/bundled_plugins/brave) reads
its key the same way. The plugin itself is this directory.

---

`google-maps` is in Atlas's official marketplace. Install it once:

```
/plugins install google-maps
```

That copies it into `~/.atlas/plugins/google-maps/` and enables it; it loads on the next
session. It then does nothing until it has a key: no request reaches Google, and the tools answer
"no GOOGLE_MAPS_API_KEY" with these steps.

## Setup

There is no sign-in. Google Maps Platform works with an **API key** from a Google Cloud project
that is yours, and **the billing is yours** too.

1. Open [console.cloud.google.com](https://console.cloud.google.com/) and create a project (or
   pick one).
2. **Turn on billing** for it under *Billing*. Maps Platform refuses every request without a
   billing account, even inside the free usage.
3. Under *APIs & Services > Library*, enable **Places API (New)**, **Routes API** and
   **Geocoding API**.
4. Under *APIs & Services > Credentials*, *Create credentials > API key*. Edit the key and, under
   *API restrictions*, choose *Restrict key* and tick just those three APIs. A key restricted
   this way is worth little to anyone who finds it.
5. Put it in `~/.atlas/.env`:

   ```
   GOOGLE_MAPS_API_KEY=AIza...
   ```

   or ask Atlas to store it with the `secret` tool, which never shows it to the model. It is read
   fresh on every call, so it works from the next thing you say. To read it from a different
   variable, set `api_key_env`.

**Set your home** so "from home" and "near me" mean something:

```jsonc
{ "plugins_settings": { "google-maps": { "home": "12 Example St, Newtown NSW", "units": "metric" } } }
```

### What it costs

Google gives every billing account a **free monthly allowance for each kind of request**: 10,000
for an *Essentials* one, 5,000 for *Pro* and 1,000 for *Enterprise*. Past that you pay Google
directly, at their [current prices](https://developers.google.com/maps/billing-and-pricing/pricing).
For one person asking a few questions a day, the free allowance is plenty; to be sure, set a
budget alert or a per-API quota in the Cloud console. Each tool asks for as few fields as it can,
because Google bills a request at the dearest field it asks for:

| Tool | Google request | Billed as | Free each month |
|---|---|---|---|
| `maps_route` driving | Compute Routes, traffic-aware | Pro | 5,000 |
| `maps_route` walking, cycling, transit | Compute Routes | Essentials | 10,000 |
| `maps_route` two-wheeler | Compute Routes | Enterprise | 1,000 |
| `maps_places` | Text Search (hours, phone, website, rating) | Enterprise | 1,000 |
| `maps_place` | Place Details | Enterprise | 1,000 |
| `maps_place` with reviews | Place Details | Enterprise + Atmosphere | 1,000 |
| `maps_geocode` | Geocoding | Essentials | 10,000 |

A driving route with **arrive by** makes up to three small Compute Routes requests on the way to
its answer (see below).

## What you can ask

| You say | Atlas uses |
|---|---|
| "How long to drive to the airport right now?" | `maps_route` |
| "When should I leave for my 3pm at 12 George St?" | `maps_route` with `arrive_by` |
| "How far is it by train?" · "Which bus gets me to Bondi by 9?" | `maps_route`, transit |
| "Walk or ride to the beach - which is quicker?" | `maps_route` |
| "Find a pharmacy open now near me" | `maps_places` |
| "What are the hours for Bunnings Alexandria?" | `maps_places`, then `maps_place` |
| "What do people say about that café?" | `maps_place` with reviews |
| "Where exactly is -33.87, 151.21?" · "Coordinates of the town hall?" | `maps_geocode` |

**A route** gives the time with traffic and without it (driving), the distance, the steps - for
transit, each line, where to get on and off and when, and the fare where Google knows it - and a
Google Maps link to open it on your phone.

**"When should I leave"** is exact for transit, which Google plans backwards from your arrival
time. Google does not do that for driving, so Atlas estimates: it asks how long the drive takes
now, works out when you would leave, asks how long it takes in the traffic predicted for *then*,
and corrects once more if that moved it. It says it is an estimate; leave a few minutes' margin.

**Times** are your computer's local time unless you say otherwise.

## What it will not do

- **Change anything.** It does not save places, edit your Maps lists, or touch your Google
  account - it has no access to it. There is nothing to approve.
- **Know where you are.** "Near me" means your `home` setting, or coordinates you give it. Without
  either, Google guesses from the search words alone.
- **Navigate turn by turn** as you drive. It shows the first steps; the Maps link has the rest.
- **Run in a group or for someone else.** The tools are offered only in a session you own, because
  each request bills your project and your home address is in the settings.

## What reaches Google

Your API key, in a request header (never in an address that might be logged), to
`routes.googleapis.com`, `places.googleapis.com` and `geocode.googleapis.com` and nowhere else.
With it go the places you ask about - including your `home` address, when a question uses it -
and the time you want to travel. What you ask goes to the model provider you chose, as everything
you ask Atlas does. Place names and reviews are written by strangers and are read as untrusted
text.

## Settings

All optional, under `plugins_settings.google-maps`:

| Key | Default | What |
|---|---|---|
| `api_key_env` | `GOOGLE_MAPS_API_KEY` | The variable the key is read from. The key itself is never a setting. |
| `home` | none | Your home address: the start of a route and the centre of a search when none is said. |
| `units` | Google's choice | `metric` or `imperial`. |
| `language` | Google's choice | The language of results, e.g. `en-AU`. |
| `region` | none | A two-letter region code (`au`, `us`, `gb`) that biases results. |
| `max_results` | `5` | The most places one search returns (at most 20). |

To turn the plugin off: `/plugins disable google-maps`; `/plugins remove google-maps` deletes it.
