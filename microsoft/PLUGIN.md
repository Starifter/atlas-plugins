---
name: microsoft
description: Microsoft - Outlook.com, Hotmail and Microsoft 365. Mail and calendar - read, send and change with your yes, and hear about new Focused mail.
categories: [email, calendar, productivity]
logo: logo.svg
version: "1.0.0"
requires_atlas_sdk: ">=1.34,<2"
contracts:
  tools:
    - outlook_mail_search
    - outlook_mail_read
    - outlook_mail_attachment
    - outlook_mail_folders
    - outlook_mail_draft
    - outlook_mail_send
    - outlook_mail_organise
    - outlook_calendar_list_calendars
    - outlook_calendar_list_events
    - outlook_calendar_get_event
    - outlook_calendar_free_busy
    - outlook_calendar_create_event
    - outlook_calendar_update_event
    - outlook_calendar_delete_event
    - outlook_calendar_respond
  logins: [outlook]
  services: [new-mail]
config_schema:
  client_id:
    type: str
    default: ""
    description: The Application (client) ID of a Microsoft Entra app registration to sign in with.
  tenant:
    type: str
    default: common
    description: Which accounts may sign in - common (personal and work), consumers (Outlook.com only), organizations, or a tenant id.
  notify:
    type: str
    default: focused
    description: What new mail is said to you - focused (Outlook's Focused inbox), all, or off.
  poll_minutes:
    type: int
    default: 2
    description: How often new mail is checked for.
  max_results:
    type: int
    default: 20
    description: The most messages or events one listing returns (at most 100).
  max_chars:
    type: int
    default: 20000
    description: The most text one read returns.
  attachment_max_mb:
    type: int
    default: 25
    description: The largest attachment outlook_mail_attachment fetches.
  sends_per_hour:
    type: int
    default: 20
    description: Sends per account per hour before Atlas refuses.
  attachments_max:
    type: int
    default: 10
    description: Files one send or draft may carry.
  day_start:
    type: str
    default: "08:00"
    description: Where your day starts, for outlook_calendar_free_busy.
  day_end:
    type: str
    default: "18:00"
    description: Where your day ends, for outlook_calendar_free_busy.
---

# microsoft

**Read this when:** you want Atlas to read and send your Outlook.com, Hotmail or Microsoft 365
mail, or read and change your Outlook calendar; or you want to know why something asked you
first, or exactly what Atlas can reach in your Microsoft account.

**See also:** [`docs/spec/microsoft.md`](https://github.com/Starifter/atlas/blob/main/docs/spec/microsoft.md)
is the specification and wins where they disagree. For Gmail, iCloud and other mail, use the
`email` plugin; for Google Calendar, `google-calendar`. All three can be installed at once.

---

`microsoft` is in Atlas's official marketplace. Install it once:

```
/plugins install microsoft
```

It does nothing until you sign in: no connection to Microsoft, the tools say how, and the
new-mail check sleeps.

## Signing in

```
atlas auth login microsoft:outlook
```

A browser opens on Microsoft's sign-in. Pick your account and agree to what Atlas asks for:
read and send mail, and read and change your calendars. Atlas keeps the sign-in and renews it
on its own; you do this once.

A second account - work and personal, say - is a second sign-in with a name:

```
atlas auth login microsoft:outlook --id work
```

With more than one, searches and listings cover all of them; a send or a change needs you to
say which account.

**Until Atlas has its own Microsoft app registration**, signing in needs one of yours. It's
free and takes five minutes:

1. Go to [entra.microsoft.com](https://entra.microsoft.com) → *App registrations* → *New
   registration*.
2. Name it "Atlas". Under *Supported account types*, choose **Accounts in any organizational
   directory and personal Microsoft accounts**.
3. Under *Redirect URI*, choose **Public client/native (mobile & desktop)** and enter
   `http://localhost/callback`.
4. *Register*, then copy the **Application (client) ID**.
5. Put it in `config.json`:

```jsonc
{ "plugins_settings": { "microsoft": { "client_id": "00000000-0000-0000-0000-000000000000" } } }
```

**A work or school account** may show "Need admin approval". That is your organisation's
setting, not Atlas's: ask IT to approve the app for `Mail.ReadWrite`, `Mail.Send` and
`Calendars.ReadWrite`.

## What you can ask

| You say | Atlas uses | Asks you first? |
|---|---|---|
| "Anything from the school this week?" | `outlook_mail_search` | no |
| "What did Sam say about Friday?" | `outlook_mail_search`, `outlook_mail_read` | no |
| "Reply to Sam that Friday works" | `outlook_mail_send` | **yes, always** |
| "Draft a reply, I'll check it" | `outlook_mail_draft` | no - unless it attaches files |
| "Archive the newsletters", "flag the one from the bank" | `outlook_mail_organise` | yes, as your permission mode says |
| "What's on tomorrow?" | `outlook_calendar_list_events` | no |
| "When am I free on Thursday afternoon?" | `outlook_calendar_free_busy` | no |
| "Book lunch with Alex Friday at 12" | `outlook_calendar_create_event` | **yes, always** |
| "Move my dentist to 4pm" | `outlook_calendar_update_event` | **yes, always** |
| "Cancel tomorrow's standup" | `outlook_calendar_delete_event` | **yes, always** |
| "Accept the invite from Priya" | `outlook_calendar_respond` | **yes, always** |

**Every send and every calendar change is a card you answer**, in every permission mode, `yolo`
included, showing what will really happen and **who will be emailed**:

```
Create in Calendar (outlook)
Title: Lunch with Alex
When: Fri 2 Oct 12:00-13:00
Where: Cafe Rosa
Invitations will be emailed to: alex@example.com
```

A run with nobody to answer, such as a scheduled job, cannot send or change anything.

**Mail works the way the `email` plugin's does:** the whole message and every file are on the
card; replies don't quote the original; a link's real address is shown beside its words, and
links that pretend to go somewhere else are refused; files can come only from your workspace,
this chat, a web address, or a forward - never anything that looks like a password or key.

**Outlook's way of tidying:** *archive* moves to your Archive folder; *label* adds an Outlook
category (Red, Family ...); *move* puts mail in a folder; *trash* moves to Deleted Items.
`outlook_mail_folders` lists your folders and categories.

**Attachments you send are limited to 3 MB in all** for now - for anything bigger, share a
link to the file. Attachments you receive can be any size up to `attachment_max_mb`.

**Times are your computer's local time.** "Friday at 3" is 3pm where this machine is.

**Only you can use it.** The tools are offered in your own conversations and nowhere else.

**Mail and invitations are strangers' words.** Atlas marks everything it reads from Microsoft as
untrusted; a message that tells Atlas to send or change something still ends at a card only
you can answer.

## New mail

When mail lands in Outlook's **Focused** inbox, a line arrives on the chat you last wrote to
Atlas from:

```
📧 Sam Lee - Dinner Friday?
📧 3 new emails: Sam Lee, Northside School, ANZ
```

Outlook decides what is Focused, not Atlas; no model runs; nothing is opened or marked. The
first check after you sign in only notes where your inbox is. `notify` in settings: `focused`
(the default), `all`, or `off`.

## Settings

All optional, under `plugins_settings.microsoft`:

| Key | Default | What |
|---|---|---|
| `client_id` | none yet | Your Entra app registration's client id (above). |
| `tenant` | `common` | `common` (any account), `consumers` (Outlook.com only), `organizations` (work and school only), or your tenant's id. |
| `notify` | `focused` | `focused`, `all`, or `off`. |
| `poll_minutes` | `2` | How often new mail is checked for. |
| `max_results` | `20` | The most messages or events one listing returns. |
| `max_chars` | `20000` | The most text one read returns. |
| `attachment_max_mb` | `25` | The largest attachment Atlas fetches. |
| `sends_per_hour` | `20` | Sends per account per hour before Atlas refuses. |
| `attachments_max` | `10` | Files one message may carry. |
| `day_start`, `day_end` | `08:00`, `18:00` | Your day, for finding free time. |

## What reaches Microsoft

Requests to `graph.microsoft.com` and sign-in at `login.microsoftonline.com`, over TLS, with
the access Microsoft's consent screen showed you. Nothing is deleted for good: mail goes no
further than Deleted Items.

What you ask Atlas about a message or an event goes to the model you chose, as everything you
ask it does.

## Troubleshooting

**"not signed in to Microsoft"** - run `atlas auth login microsoft:outlook`.

**"no app registration yet"** - make one (above) and set `client_id`.

**"Need admin approval"** in the browser - your organisation decides; ask IT.

**"refused the sign-in (401)"** - sign in again; Microsoft ends sign-ins after long disuse or a
password change.

**"no message 'm4' - search again"** - ids are kept while Atlas runs; after a restart, search
again for fresh ones.

**"the event changed since it was shown"** - it was edited in Outlook after the card. Ask again.

**"the send may not have gone"** - look in *Sent Items* before asking again: Atlas never retries
a send.
