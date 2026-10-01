---
name: email
formerly: gmail
description: Email - Gmail, iCloud, Fastmail, Yahoo or any IMAP account. Search, read, draft, send with your yes, tidy the inbox, and hear about new mail.
categories: [email, productivity]
logo: logo.svg
version: "2.0.0"
requires_atlas_sdk: ">=1.34,<2"
contracts:
  tools:
    - email_search
    - email_read
    - email_attachment
    - email_labels
    - email_draft
    - email_send
    - email_organise
  services: [new-mail]
config_schema:
  address_env:
    type: str
    default: EMAIL_ADDRESS
    description: The variable, in the environment or ~/.atlas/.env, holding the first account's address.
  password_env:
    type: str
    default: EMAIL_APP_PASSWORD
    description: The variable holding its app password, made in your mail provider's account security settings.
  accounts:
    type: list
    default: [personal]
    description: Account labels. After the first, each reads <address_env>_<LABEL> and <password_env>_<LABEL>.
  servers:
    type: dict
    description: Label to a preset (gmail, icloud, fastmail, yahoo) or {"imap" "host:993", "smtp" "host:465"}, for an address whose provider Atlas cannot tell from its domain.
  notify:
    type: str
    default: important
    description: What new mail is said to you - important, primary, all, or off. Outside Gmail, all but off mean every new unread inbox message.
  poll_minutes:
    type: int
    default: 2
    description: How often new mail is checked for.
  max_results:
    type: int
    default: 20
    description: The most messages one search returns (at most 100).
  max_chars:
    type: int
    default: 20000
    description: The most body text one read returns.
  attachment_max_mb:
    type: int
    default: 25
    description: The largest attachment email_attachment fetches.
  sends_per_hour:
    type: int
    default: 20
    description: Sends per account per hour before Atlas refuses.
  attachments_max:
    type: int
    default: 10
    description: Files one send or draft may carry.
---

# email

**Read this when:** you want Atlas to read, write or tidy your email - Gmail, iCloud, Fastmail,
Yahoo or another provider - you want to know why a send asked you first or a file was refused,
or you want to know exactly what reaches your mail provider.

**See also:** [`docs/spec/email.md`](https://github.com/Starifter/atlas/blob/main/docs/spec/email.md) is the specification and wins where they
disagree; [`docs/core/channels.md`](https://github.com/Starifter/atlas/blob/main/docs/core/channels.md) is how a new-mail notice
reaches your phone; [`docs/core/media.md`](https://github.com/Starifter/atlas/blob/main/docs/core/media.md) is how an attached PDF gets read.
The plugin itself is this directory.

---

`email` is in Atlas's official marketplace. Install it once:

```
/plugins install email
```

That copies it into `~/.atlas/plugins/email/` and enables it; it loads on the next session. It
then does nothing until you give it an account: no connection to any mail server, the tools
answer "not set up", and the new-mail check sleeps.

**It used to be called `gmail`.** If you had that installed, see
[Coming from `gmail`](#coming-from-gmail) below - your setup keeps working.

## Setup

Atlas reaches your mailbox with an **app password** - a password your provider makes for one
app, which you can take back at any time without changing your real one. No developer
account, no sign-in page. Every provider below needs two-step sign-in turned on first.

| Your address ends in | Where to make an app password |
|---|---|
| `gmail.com`, `googlemail.com` | [myaccount.google.com](https://myaccount.google.com) → *Security* → *App passwords* |
| `icloud.com`, `me.com`, `mac.com` | [account.apple.com](https://account.apple.com) → *Sign-In and Security* → *App-Specific Passwords* |
| `fastmail.com` | Fastmail → *Settings* → *Privacy & Security* → *App passwords* (choose IMAP and SMTP) |
| `yahoo.com`, `ymail.com` | Yahoo → *Account Security* → *Generate app password* |

Then store it and your address:

```console
$ atlas "store my email app password"
```

or write the lines yourself:

```
# ~/.atlas/.env
EMAIL_ADDRESS=you@icloud.com
EMAIL_APP_PASSWORD=abcd-efgh-ijkl-mnop
```

That is all - Atlas knows the servers from the address. The password lives in `.env`, never in
`config.json`, and Atlas never shows it.

**An address on your own domain** - a Google Workspace account, iCloud custom domain, or any
other mail host - needs one line saying whose servers it is on:

```jsonc
{ "plugins_settings": { "email": { "servers": { "personal": "gmail" } } } }
```

or, for a provider that is not one of the four, its servers:

```jsonc
{ "plugins_settings": { "email": { "servers": {
  "personal": { "imap": "mail.example.com:993", "smtp": "mail.example.com:465" }
} } } }
```

**Outlook, Hotmail and Microsoft 365 don't work yet.** Microsoft no longer accepts passwords
over IMAP; those accounts need Microsoft's own sign-in, which Atlas does not have yet.

**Google doesn't offer app passwords to** work or school accounts, accounts with Advanced
Protection, or accounts whose second step is security keys only.

**A second account** - family, say, on a different provider - is a label and two more lines:

```jsonc
{ "plugins_settings": { "email": { "accounts": ["personal", "family"] } } }
```

```
EMAIL_ADDRESS_FAMILY=family@gmail.com
EMAIL_APP_PASSWORD_FAMILY=...
```

With more than one, searches cover all of them and say which each message came from; a send,
a draft or a change needs you to say which account.

## What you can ask

| You say | Atlas uses | Asks you first? |
|---|---|---|
| "Anything from the school this week?" | `email_search` | no |
| "What did Sam say about Friday?" | `email_search`, `email_read` | no |
| "What's in the PDF the bank sent?" | `email_attachment`, then `read_media` | no |
| "Draft a reply saying Friday works" | `email_draft` | no - unless it attaches files |
| "Reply to Sam that Friday works" | `email_send` | **yes, always** |
| "Send Alex the menu from my documents" | `email_send` with an attachment | **yes, always** |
| "Email Sam the timetable at https://..." | `email_send`, downloading it to attach | **yes, always** |
| "Archive the newsletters" | `email_organise` | yes, as your permission mode says |

Searches are written in Gmail's search syntax for every account - `from:sam is:unread
newer_than:7d has:attachment`. On Gmail, anything that works in Gmail's search box works here.
On other providers the common part works - `from:`, `to:`, `cc:`, `subject:`, `is:unread`,
`is:read`, `is:starred`, `newer_than:`, `older_than:`, `after:`, `before:`, `larger:`,
`smaller:`, `has:attachment`, `in:<folder>` (and `in:anywhere`), a `-` in front to leave
something out, and plain words. Gmail-only things like `is:important` or `category:` are
refused with that list, rather than quietly searching for the words.

**Every send is a card you answer**, in every permission mode, `yolo` included:

```
Send from you@icloud.com
To: sam@example.com
Subject: Re: Dinner Friday?

Friday works - see you at 7. The menu is attached.

Attached (1 file, 1.2 MB):
  menu.pdf - PDF, 1.2 MB, sha256 3f9a1c0e - from the workspace: docs/menu.pdf

(replying to Sam Lee, Thu 1 Oct 18:02)
```

Every recipient, the whole text and every file are on it - never a summary. A run with nobody
to answer, such as a scheduled job, cannot send.

**Replies don't quote the original.** Mail apps show the conversation above a reply anyway, and
a quote is text Atlas didn't write going to someone you didn't see it go to.

**Only you can use it.** The mail tools are offered in your own conversations and nowhere else.

**An email is a stranger's words.** Atlas marks every message it reads as untrusted, and when
your provider could not verify who a message is from, Atlas is told so before it reads the
message. An email that tells Atlas to send something still ends at a card only you can answer.

## Gmail and everyone else

Gmail has labels; other providers have folders. Atlas speaks each one's way:

| | Gmail | iCloud, Fastmail, Yahoo, others |
|---|---|---|
| Archive | takes the message out of the Inbox | moves it to the Archive folder |
| Label | adds a label | moves it to that folder (`email_labels` lists them) |
| Remove a label | removes it | not offered - a message is in one folder; move it back with *inbox* |
| Sent copy | Gmail keeps it | Atlas files a copy in Sent after each send |
| "Important" notices | Gmail's Important, Primary inbox | every new unread inbox message |

## Links

A web address written into an email arrives clickable, as it always has. To make words into a
link - "the menu is **here**" - Atlas writes `[here](https://cafe.example/menu)`, and the email
goes out with the words as a link. On the card you always see the real address beside the
words:

```
The menu is here <https://cafe.example/menu>.
```

Refused before you're asked: a link to anything but a web or email address (`javascript:`,
`data:`, `file:`), and a link whose words name one site while it goes to another -
`paypal.com` that really goes to `paypal.evil.example` - because that is how phishing emails
are made. Links inside a message you forward stay plain text.

## Attachments

A send or a draft can carry files from four places, and no others:

- **Your workspace** - `docs/menu.pdf`, or a file an earlier `email_attachment` saved.
- **A file you sent in this conversation** - "email Sam the lease I just sent you".
- **A web address** - "email Sam the timetable at https://school.example/timetable.pdf". Atlas
  downloads it when it shows you the card, under the same rules as `web_fetch` (never an
  address on your own network unless you allowed it), and the card says where it came from.
- **A forward's own attachments**, kept unless you say otherwise.

Refused before you are ever asked, with the reason named: anything outside the workspace;
Atlas's own `.atlas/` folder; hidden files and folders (`.env`, `.ssh/`, `.git/`); files named
like keys (`*.pem`, `*.key`, `id_rsa`, `*.kdbx` ...); a text file with a password or API key in
it; more than 10 files, or more than your provider takes in one message (25 MB; 20 MB on
iCloud). What was hashed for the card is exactly what is sent - a file changed after you saw
the card still goes as you saw it.

Received attachments come to Atlas one at a time, when asked: a picture is shown to it
directly, anything else is saved under `email-attachments/` in the workspace for it to read.

## New mail

When new mail lands in your inbox, a line arrives on the chat you last wrote to Atlas from:

```
📧 Sam Lee - Dinner Friday?
📧 3 new emails: Sam Lee, Northside School, ANZ
```

On Gmail, only mail Gmail marks **Important** in your Primary inbox is said, by default. Other
providers have no "important", so every new unread message in the inbox is said - the
provider's spam filter has already kept junk out. Your provider decides, not Atlas; no model
runs; Atlas opens, marks and answers nothing. It checks every two minutes. The first check
after you set up an account only notes where the inbox is, so your backlog is not announced.

`notify` in settings: `important` (the default), `primary` (everything in Gmail's Primary),
`all` (every new unread inbox message, Gmail included), or `off`.

## Settings

All optional, under `plugins_settings.email`:

| Key | Default | What |
|---|---|---|
| `address_env` | `EMAIL_ADDRESS` | The variable holding the first account's address. |
| `password_env` | `EMAIL_APP_PASSWORD` | The variable holding its app password. |
| `accounts` | `["personal"]` | Account labels (above). |
| `servers` | none | Per label, a preset (`gmail`, `icloud`, `fastmail`, `yahoo`) or `{"imap": "host:port", "smtp": "host:port"}`. Needed only when the address's domain does not say. |
| `notify` | `important` | `important`, `primary`, `all`, or `off`. |
| `poll_minutes` | `2` | How often new mail is checked for. |
| `max_results` | `20` | The most messages one search returns (at most 100). |
| `max_chars` | `20000` | The most text one read returns. |
| `attachment_max_mb` | `25` | The largest attachment Atlas fetches. |
| `sends_per_hour` | `20` | Sends per account per hour before Atlas refuses. |
| `attachments_max` | `10` | Files one message may carry. |

## What reaches your provider

An IMAP connection and, to send, an SMTP connection to your provider's servers - for iCloud,
`imap.mail.me.com` and `smtp.mail.me.com`; for Gmail, `imap.gmail.com` and `smtp.gmail.com` -
always over TLS, logged in with your app password. Nothing else of Atlas's. Nothing is ever
deleted for good: Trash, which your provider empties on its own schedule (30 days on Gmail and
iCloud), is as far as Atlas moves a message.

What you ask Atlas about a message goes to the model you chose, as everything you ask it does.

## Coming from `gmail`

This plugin was called `gmail` until 1 October 2026. Everything you set up for it still works:

- `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` (and their `_LABEL` versions) are still read when the
  `EMAIL_` ones are not set. Rename them when convenient.
- Settings under `plugins_settings.gmail` are read until there are some under
  `plugins_settings.email`; `/plugins email` reminds you to rename the key.
- If the old `gmail` plugin is still enabled, Atlas sets it aside in favour of this one. Remove
  it with `/plugins remove gmail`.
- The first new-mail check after the switch only notes where your inbox is, so nothing old is
  announced twice.

## Troubleshooting

**"Email is not set up"** - the two lines in `.env` (above) are missing.

**"Atlas does not know where ...'s mail is"** - your address is on your own domain; add a
`servers` line (above).

**"refused the app password"** - it was revoked, or your account's password changed, which
revokes app passwords. Make a new one where the message says.

**"... is Gmail's alone"** - that search operator only works on Gmail; the message lists what
works on your account.

**"that id is out of date - search again"** - your provider renumbered the folder since the
search; search again for fresh ids.

**"refused: ... contains what looks like a credential"** - the file has a key or password in it.
Take it out, or send the file from your mail app yourself.

**"the draft changed since it was shown"** - it was edited in your mail app after Atlas showed
you the card. Ask again; the new card shows it as it is now.

**"the send may not have gone"** - look in *Sent* before asking again: Atlas never retries a send,
so that a message that did go is not sent twice.

**"It went, but was not filed in Sent"** - the message was sent; only the copy in your Sent
folder is missing.
