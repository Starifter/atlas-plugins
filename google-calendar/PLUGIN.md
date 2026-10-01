---
name: google-calendar
description: Google Calendar - read it, change it with your yes, and get reminders on your phone.
categories: [calendar, productivity]
logo: logo.svg
version: "1.0.0"
requires_atlas_sdk: ">=1.33,<2"
python_dependencies: [python-dateutil>=2.9]
contracts:
  tools:
    - calendar_list_calendars
    - calendar_list_events
    - calendar_get_event
    - calendar_free_busy
    - calendar_create_event
    - calendar_update_event
    - calendar_delete_event
    - calendar_respond
  logins: [google]
  services: [reminders]
config_schema:
  client_id:
    type: str
    default: ""
    description: A Google OAuth client id (Desktop app) to sign in with instead of Atlas's own.
  ical_url_env:
    type: str
    default: GOOGLE_CALENDAR_ICAL_URL
    description: The variable, in the environment or ~/.atlas/.env, holding calendars' secret iCal addresses - read-only, no sign-in. Several are separated by spaces.
  calendars:
    type: list
    default: [primary]
    description: The calendars reminders are sent for, by id. "*" is every calendar.
  reminders:
    type: bool
    default: true
    description: Send reminders. false stops them; the tools are unaffected.
  use_event_reminders:
    type: bool
    default: true
    description: Use each event's own popup reminders from Google, and lead_minutes only where it has none.
  lead_minutes:
    type: list
    default: [10]
    description: Minutes before an event to remind, for events without their own reminders.
  all_day_at:
    type: str
    default: ""
    description: A local time, e.g. "08:00", to remind of the day's all-day events. Empty is none.
  late_grace_minutes:
    type: int
    default: 5
    description: How late a missed reminder may still go out, e.g. after the computer woke.
  poll_minutes:
    type: int
    default: 5
    description: How often reminders ask Google what is coming up while a reminder is due within the hour. At least 1.
  idle_poll_minutes:
    type: int
    default: 30
    description: How often they ask while nothing is due within the hour. Changes made through Atlas are seen at once either way.
  horizon_hours:
    type: int
    default: 24
    description: How far ahead each look reaches.
  max_results:
    type: int
    default: 50
    description: The most events one listing returns.
---

# google-calendar

**Read this when:** you want Atlas to read or change your Google Calendar, you want
reminders on your phone, you are wondering why a calendar change asked you first, or you want
to know exactly what this plugin sends to Google.

**See also:** [`docs/spec/google-calendar.md`](https://github.com/Starifter/atlas/blob/main/docs/spec/google-calendar.md) is the
specification and wins where they disagree; [`docs/core/oauth.md`](https://github.com/Starifter/atlas/blob/main/docs/core/oauth.md) is how
signing in works for every plugin; [`docs/core/channels.md`](https://github.com/Starifter/atlas/blob/main/docs/core/channels.md) is how a
reminder reaches your phone. The plugin itself is this directory.

---

`google-calendar` is in Atlas's official marketplace. Install it once:

```
/plugins install google-calendar
```

That copies it into `~/.atlas/plugins/google-calendar/` and enables it; it loads on the next
session. It then does nothing until you sign in or store a calendar's iCal address: no request
reaches Google, the tools answer "not signed in", and the reminder service sleeps. It needs
`python-dateutil`, which Atlas itself carries.

## Setup

```console
$ atlas auth login google-calendar:google
```

Your browser opens Google's consent page. Sign in, allow access to your calendars, and the
terminal says who you signed in as. That is all: the tools work from the next thing you say,
and reminders start on their own.

**A second account** - work, say - is a second sign-in with a label:

```console
$ atlas auth login google-calendar:google --id work
```

With more than one account, reading covers all of them and says which each event came from;
a change needs you to say which account (`account: work`), because moving an event on the
wrong one is not a thing Atlas guesses.

**Signing out** is `atlas auth logout google-calendar:google` (or `:work`). Atlas forgets the
token; to take the access back at Google too, remove Atlas at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions).

**Before Atlas's Google app is verified** - while it is in Google's *Testing* status - only
people added as test users can sign in, and a sign-in lasts 7 days. When it runs out, Atlas
tells you once, on the channel you last wrote from, and the tools say how to sign in again.

**Without signing in: the secret iCal address.** Any Google calendar can be read - and remind
you - with no sign-in and nothing to set up in Google Cloud. In Google Calendar on the web,
open *Settings*, pick the calendar under *Settings for my calendars*, and copy *Secret address
in iCal format*. Then store it the way you would store a password:

```console
$ atlas "store my google calendar ical url"
```

or add the line yourself:

```
# ~/.atlas/.env
GOOGLE_CALENDAR_ICAL_URL=https://calendar.google.com/calendar/ical/.../basic.ics
```

Several calendars are several addresses on the one line, separated by spaces. Atlas reads them
as `ical1`, `ical2`, ... and names each by the calendar's own name. What you give up: this is
**read-only** - Atlas can list, search and remind, but asking it to create or move an event
tells you to sign in - and Google may take a while to show a change in the feed. Anyone with
the address can read the calendar, which is why it lives in `.env` and never in `config.json`;
Atlas never shows it, even in an error. If you reset it in Google, Atlas tells you once that
the old one stopped working.

**With your own Google client.** If you would rather use a Google Cloud project of your own,
create an OAuth client of type *Desktop app* with the Calendar API enabled and set its id:

```jsonc
{ "plugins_settings": { "google-calendar": { "client_id": "1234-abc.apps.googleusercontent.com" } } }
```

## What you can ask

| You say | Atlas uses | Asks you first? |
|---|---|---|
| "What's on tomorrow?" | `calendar_list_events` | no |
| "Find the dentist appointment" | `calendar_list_events` with a search | no |
| "When am I free Thursday afternoon?" | `calendar_free_busy` | no |
| "What calendars do I have?" | `calendar_list_calendars` | no |
| "Book lunch with Sam on Friday at 1" | `calendar_create_event` | **yes** |
| "Move the dentist to next Tuesday at 10" | `calendar_update_event` | **yes** |
| "Cancel tomorrow's standup" | `calendar_delete_event` | **yes** |
| "Decline the quarterly review" | `calendar_respond` | **yes** |

**Every change is a card you answer**, in every permission mode, `yolo` included:

```
Move "Dentist" · Tue 1 Oct 09:00-09:30 → Fri 4 Oct 10:00-10:30 · Personal
Delete "Standup" · every occurrence from Mon 7 Oct 10:00-10:15 (the whole series) · Work
Create "Lunch" · Fri 4 Oct 13:00-14:00 · Personal · emails invitations to sam@example.com
```

A run with nobody there to answer - a cron job - cannot change your calendar at all.

**Nobody else is emailed unless you ask.** Adding people to an event does not send them an
invitation unless the change says to, and when it does, the card names every address that
will get mail. Answering an invitation always tells the organiser, and its card says so.

**A repeating event asks which part.** "Delete the standup" on a daily standup is refused
until it says *this occurrence* or *the whole series*.

**Only you can use it.** The calendar tools are offered in your own conversations and
nowhere else: not in a group, not to someone else who messages the bot, not in a scheduled
run.

## Reminders

Ten minutes before an event - or whenever that event's own Google reminders say - a line
arrives on the chat you last wrote to Atlas from:

```
⏰ Dentist at 9:00 (in 10 min) · 12 Smith St
⏰ Standup at 10:00 (in 5 min) · meet.google.com/abc-defg-hij
📅 Today: Mum's birthday
```

No model runs to send it. Atlas checks your calendar for the day ahead - every five minutes
while a reminder is due within the hour, every half hour otherwise - and sleeps until the
next one. A change you make through Atlas is seen at once; one made in Google's own apps is
seen at the next check, so an event added there for less than half an hour away may be
reminded by Google's app rather than Atlas. Each reminder is sent once, across restarts; an event
moved to a new time is reminded at the new one. If the computer was asleep, a reminder up to
five minutes late still goes out; after the event has started it does not.

Skipped: events you declined, cancelled events, and events whose only reminders in Google are
email ones - you chose email for those.

With no channel set up, a reminder goes to Atlas's web page or terminal if one is open. For
reminders on your phone, set up Telegram or Discord (`docs/core/channels.md`) and write to
the bot once.

## Settings

All optional, under `plugins_settings.google-calendar` in `config.json`:

| Key | Default | What |
|---|---|---|
| `client_id` | Atlas's | A Google OAuth client id (Desktop app) to sign in with instead. |
| `ical_url_env` | `GOOGLE_CALENDAR_ICAL_URL` | The variable in `.env` holding secret iCal addresses. |
| `calendars` | `["primary"]` | The calendars reminders are sent for, by id. `"*"` is every calendar. `calendar_list_calendars` shows the ids. |
| `reminders` | `true` | `false` stops reminders; the tools still work. |
| `use_event_reminders` | `true` | Use each event's own popup reminders from Google. |
| `lead_minutes` | `[10]` | Minutes before, for events with no reminders of their own. |
| `all_day_at` | `""` | A time like `"08:00"` to remind of the day's all-day events. Empty is none. |
| `late_grace_minutes` | `5` | How late a missed reminder may still go out. |
| `poll_minutes` | `5` | How often Atlas asks Google what is coming up while a reminder is due within the hour. |
| `idle_poll_minutes` | `30` | How often it asks otherwise. Lower means fewer late reminders for events added in Google's apps, and more requests. |
| `horizon_hours` | `24` | How far ahead it looks. |
| `max_results` | `50` | The most events one listing returns. |

## What reaches Google

Requests to `https://www.googleapis.com/calendar/v3` with your token in the `Authorization`
header, and nothing else of Atlas's: the calendar list, event listings between two times, one
event, free/busy for a range, and - after you said yes - the one change you approved. A change
carries the version of the event you were shown, so if someone else edited it in the
meantime, Google refuses it and Atlas says the event changed rather than overwriting it.

Sign-in asks Google for four things: your events (`calendar.events`), your calendar list
(`calendar.calendarlist.readonly`), and `openid email`, which is only how Atlas labels the
sign-in with your address.

## Troubleshooting

**"not signed in to Google Calendar"** - run `atlas auth login google-calendar:google`, or store
a secret iCal address (above) to read without signing in.

**"ical1 answered HTTP 404"** - the secret address was reset in Google. Copy the new one into
`.env`.

**"the calendar is read through its secret iCal address, which is read-only"** - sign in to
make changes; the iCal address can only be read.

**"Google Calendar has no OAuth client id yet"** - this build of Atlas has no Google app of its
own yet. Set `client_id` to your own (above).

**"a permission was not granted (403)"** - Google's consent page lets you untick boxes. Sign in
again and leave them all ticked.

**"the event changed since it was read"** - someone edited it after Atlas showed you the card.
Ask again; the new card shows the event as it is now.

**No reminders** - check `atlas auth login` lists you as signed in, that the calendar is in
`calendars`, and that a channel is running with you as its owner. The Channels page shows the
last one.
