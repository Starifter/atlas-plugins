---
name: caldav
description: iCloud Calendar - and Fastmail, Yahoo, Nextcloud or any CalDAV calendar - with the app password you already gave the email plugin. Read it, find free time, and change it with your yes.
categories: [calendar, productivity]
logo: logo.svg
version: "1.0.0"
requires_atlas_sdk: ">=1.34,<2"
contracts:
  tools:
    - caldav_calendar_list_calendars
    - caldav_calendar_list_events
    - caldav_calendar_get_event
    - caldav_calendar_free_busy
    - caldav_calendar_create_event
    - caldav_calendar_update_event
    - caldav_calendar_delete_event
    - caldav_calendar_respond
config_schema:
  address_env:
    type: str
    default: EMAIL_ADDRESS
    description: The variable, in the environment or ~/.atlas/.env, holding the first account's address - the email plugin's.
  password_env:
    type: str
    default: EMAIL_APP_PASSWORD
    description: The variable holding its app password - the email plugin's.
  accounts:
    type: list
    default: [personal]
    description: Account labels, as the email plugin has them. After the first, each reads <address_env>_<LABEL> and <password_env>_<LABEL>.
  servers:
    type: dict
    description: Label to a preset (icloud, fastmail, yahoo) or a CalDAV server's https address, for an address whose provider Atlas cannot tell from its domain.
  default_calendar:
    type: str
    default: ""
    description: The calendar a new event goes in when none is named, by name. Empty means your server's default.
  time_zone:
    type: str
    default: ""
    description: An IANA time zone (Australia/Sydney) for the times you write and read. Empty means this computer's.
  max_results:
    type: int
    default: 50
    description: The most events one listing returns.
---

# caldav

**Read this when:** you want Atlas to read or change your iCloud calendar - or a Fastmail,
Yahoo, Nextcloud or other CalDAV calendar - or you want to know why a change asked you first,
or exactly what Atlas sends where.

**See also:** [`docs/spec/caldav.md`](https://github.com/Starifter/atlas/blob/main/docs/spec/caldav.md)
is the specification and wins where they disagree. For Google Calendar use `google-calendar`;
for Outlook and Microsoft 365, `microsoft`. All three can be installed at once.

---

`caldav` is in Atlas's official marketplace. Install it once:

```
/plugins install caldav
```

It does nothing until it has an account: no connection to any server, and the tools say how
to set one up.

## Setup

**If the `email` plugin already reads your iCloud mail, there is nothing to do.** This plugin
reads the same two lines from `~/.atlas/.env`, and an Apple app-specific password opens your
calendar as well as your mail:

```
# ~/.atlas/.env
EMAIL_ADDRESS=you@icloud.com
EMAIL_APP_PASSWORD=abcd-efgh-ijkl-mnop
```

Otherwise, make an app password - a password for one app, which you can take back at any
time without changing your real one. Two-step sign-in has to be on first.

| Your address ends in | Where to make an app password |
|---|---|
| `icloud.com`, `me.com`, `mac.com` | [account.apple.com](https://account.apple.com) → *Sign-In and Security* → *App-Specific Passwords* |
| `fastmail.com` | Fastmail → *Settings* → *Privacy & Security* → *App passwords* - tick **Calendars (CalDAV)**; one made for mail alone won't open calendars |
| `yahoo.com`, `ymail.com` | Yahoo → *Account Security* → *Generate app password* |

Then store it with `atlas "store my email app password"`, or write the two lines above
yourself. The password lives in `.env`, never in `config.json`, and Atlas never shows it.

**An address on your own domain** - iCloud custom email domain, say - or **your own server**
needs one line saying where the calendars are:

```jsonc
{ "plugins_settings": { "caldav": { "servers": { "personal": "icloud" } } } }
```

```jsonc
{ "plugins_settings": { "caldav": { "servers": {
  "personal": "https://cloud.example.com/remote.php/dav/"
} } } }
```

The address must be `https://` - your password would otherwise cross the network in the clear.

**Gmail and Outlook addresses are skipped**, with a note: Google's calendar takes no app
password (use `google-calendar`), and Microsoft's needs Microsoft's sign-in (use `microsoft`).
So if your email accounts mix providers, this plugin reads the iCloud ones and leaves the rest.

**Several accounts** are the `email` plugin's labels - copy its `accounts` line here:

```jsonc
{ "plugins_settings": { "caldav": { "accounts": ["personal", "family"] } } }
```

```
EMAIL_ADDRESS_FAMILY=family@icloud.com
EMAIL_APP_PASSWORD_FAMILY=...
```

With more than one, listings cover all of them; a change needs you to say which account.

## What you can ask

| You say | Atlas uses | Asks you first? |
|---|---|---|
| "What's on tomorrow?" | `caldav_calendar_list_events` | no |
| "When am I free on Thursday afternoon?" | `caldav_calendar_free_busy` | no |
| "What's the review on Friday about?" | `caldav_calendar_get_event` | no |
| "Book lunch with Alex Friday at 12" | `caldav_calendar_create_event` | **yes, always** |
| "Move my dentist to 3pm" | `caldav_calendar_update_event` | **yes, always** |
| "Cancel next Monday's standup" | `caldav_calendar_delete_event` | **yes, always** |
| "Decline Priya's review" | `caldav_calendar_respond` | **yes, always** |

**Every change is a card you answer**, in every permission mode, `yolo` included, showing what
will really happen and **who will be emailed**:

```
Create in Home (personal)
Title: Lunch with Alex
When: Fri 2 Oct 12:00-13:00
Where: Cafe Rosa
iCloud will email invitations to: alex@example.com
```

Your calendar server does the emailing, the way it does when you add someone in the Calendar
app: invitations for a new event, the change when you move one you organise, a cancellation
when you cancel it, and your answer to the organiser when you reply. Deleting an invitation
you received, instead of declining it, tells nobody - the card says so.

A run with nobody to answer, such as a scheduled job, cannot change anything.

**Repeating events ask which part.** "Move Monday's standup" asks: this occurrence, or every
one? Nothing is guessed. Moving a whole series whose occurrences you've already changed one by
one is refused - change the one occurrence, or move the series in the Calendar app.

**A change made on your phone after the card wins.** If the event changed between the card and
your yes, nothing is changed and Atlas says to look again.

**Times are local.** "Friday at 3" is 3pm in your computer's time zone, or `time_zone` if you
set it. A repeating event is saved in that zone, so 9am every Monday stays 9am after daylight
saving changes.

**Only you can use it.** The tools are offered in your own conversations and nowhere else.

**An invitation is a stranger's words.** Atlas marks everything it reads from your calendar as
untrusted; an event that tells Atlas to do something still ends at a card only you can answer.

## Settings

All optional, under `plugins_settings.caldav`:

| Key | Default | What |
|---|---|---|
| `address_env` | `EMAIL_ADDRESS` | The variable holding the first account's address. |
| `password_env` | `EMAIL_APP_PASSWORD` | The variable holding its app password. |
| `accounts` | `["personal"]` | Account labels (above). |
| `servers` | none | Per label, a preset (`icloud`, `fastmail`, `yahoo`) or an `https://` address. |
| `default_calendar` | your server's default | Where a new event goes when you don't say. |
| `time_zone` | this computer's | An IANA zone, such as `Australia/Sydney`. |
| `max_results` | `50` | The most events one listing returns. |

## What reaches your calendar server

Requests to your provider's CalDAV server - for iCloud, `caldav.icloud.com` and the
`pNN-caldav.icloud.com` host it sends Atlas on to - over TLS, with your address and app
password. **The password goes to that server's own domain and nowhere else:** a redirect or an
address pointing anywhere else is refused before anything is sent. Nothing else of Atlas's.

What you ask Atlas about an event goes to the model you chose, as everything you ask it does.

## Not yet

- **Reminders on your phone** from Atlas, like `google-calendar` sends. Your Calendar app
  already reminds you.
- **Contacts** over CardDAV.

## Troubleshooting

**"CalDAV is not set up"** - the two lines in `.env` (above) are missing.

**"refused the app password"** - it was revoked, or your account's password changed. On
Fastmail, make one with *Calendars (CalDAV)* ticked.

**"Atlas does not know where ...'s calendars are"** - your address is on your own domain; add a
`servers` line (above).

**"refused: ... is not iCloud's"** - the server pointed Atlas somewhere else, and Atlas didn't
follow with your password. If it keeps happening, tell us.

**"no event 'e4' - list again"** - ids are kept while Atlas runs; after a restart, list again.

**"the event changed since it was shown"** - it was edited elsewhere after the card. Ask again.

**"a repeating event needs a time zone"** - set `time_zone`, such as `Australia/Sydney`.
