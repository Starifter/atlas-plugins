---
name: gmail
description: Gmail - search, read, draft, send with your yes, tidy the inbox, and hear about important mail.
categories: [email, productivity]
logo: logo.svg
version: "1.0.0"
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
    default: GMAIL_ADDRESS
    description: The variable, in the environment or ~/.atlas/.env, holding the first account's address.
  password_env:
    type: str
    default: GMAIL_APP_PASSWORD
    description: The variable holding its app password - made at myaccount.google.com, Security, App passwords.
  accounts:
    type: list
    default: [personal]
    description: Account labels. After the first, each reads <address_env>_<LABEL> and <password_env>_<LABEL>.
  notify:
    type: str
    default: important
    description: What new mail is said to you - important, primary, or off.
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

# gmail

**Read this when:** you want Atlas to read, write or tidy your Gmail, you want to know why a
send asked you first or a file was refused, or you want to know exactly what reaches Google.

**See also:** [`docs/spec/gmail.md`](https://github.com/Starifter/atlas/blob/main/docs/spec/gmail.md) is the specification and wins where they
disagree; [`docs/core/channels.md`](https://github.com/Starifter/atlas/blob/main/docs/core/channels.md) is how an important-mail notice
reaches your phone; [`docs/core/media.md`](https://github.com/Starifter/atlas/blob/main/docs/core/media.md) is how an attached PDF gets read.
The plugin itself is this directory.

---

`gmail` is in Atlas's official marketplace. Install it once:

```
/plugins install gmail
```

That copies it into `~/.atlas/plugins/gmail/` and enables it; it loads on the next session. It
then does nothing until you give it an account: no connection to Google, the tools answer
"not set up", and the new-mail check sleeps.

## Setup

Atlas reaches Gmail with an **app password** - a 16-letter password Google makes for one app,
which you can take back at any time without changing your real one. No Google Cloud project,
no sign-in page.

1. Turn on **2-Step Verification** on your Google account, if it is not on already.
2. Go to [myaccount.google.com](https://myaccount.google.com) → *Security* → *App passwords*,
   make one called "Atlas", and copy it.
3. Store it and your address:

```console
$ atlas "store my gmail app password"
```

or write the lines yourself:

```
# ~/.atlas/.env
GMAIL_ADDRESS=you@gmail.com
GMAIL_APP_PASSWORD=abcd efgh ijkl mnop
```

That is all. The password lives in `.env`, never in `config.json`, and Atlas never shows it.

**App passwords are not offered to** work or school accounts, accounts with Advanced
Protection, or accounts whose second step is security keys only. Those will need Gmail sign-in,
which is not in Atlas yet.

**A second account** - family, say - is a label and two more lines:

```jsonc
{ "plugins_settings": { "gmail": { "accounts": ["personal", "family"] } } }
```

```
GMAIL_ADDRESS_FAMILY=family@gmail.com
GMAIL_APP_PASSWORD_FAMILY=...
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

Searches take Gmail's own syntax - `from:sam is:unread newer_than:7d has:attachment
label:school` - so anything that works in Gmail's search box works here.

**Every send is a card you answer**, in every permission mode, `yolo` included:

```
Send from you@gmail.com
To: sam@example.com
Subject: Re: Dinner Friday?

Friday works - see you at 7. The menu is attached.

Attached (1 file, 1.2 MB):
  menu.pdf - PDF, 1.2 MB, sha256 3f9a1c0e - from the workspace: docs/menu.pdf

(replying to Sam Lee, Thu 1 Oct 18:02)
```

Every recipient, the whole text and every file are on it - never a summary. A run with nobody
to answer, such as a scheduled job, cannot send.

**Replies don't quote the original.** Gmail shows the conversation above a reply anyway, and a
quote is text Atlas didn't write going to someone you didn't see it go to.

**Only you can use it.** The mail tools are offered in your own conversations and nowhere else.

**An email is a stranger's words.** Atlas marks every message it reads as untrusted, and when
Gmail could not verify who a message is from, Atlas is told so before it reads the message. An
email that tells Atlas to send something still ends at a card only you can answer.

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
it; more than 10 files or 25 MB. What was hashed for the card is exactly what is sent - a file
changed after you saw the card still goes as you saw it.

Received attachments come to Atlas one at a time, when asked: a picture is shown to it
directly, anything else is saved under `email-attachments/` in the workspace for it to read.

## Important mail

When mail Gmail marks **Important** lands in your Primary inbox, a line arrives on the chat you
last wrote to Atlas from:

```
📧 Sam Lee - Dinner Friday?
📧 3 important emails: Sam Lee, Northside School, ANZ
```

Gmail decides what is important, not Atlas; no model runs; Atlas opens, marks and answers
nothing. It checks every two minutes. The first check after you set up an account only notes
where the inbox is, so your backlog is not announced. `notify` in settings: `important` (the
default), `primary` (everything in Primary), or `off`.

## Settings

All optional, under `plugins_settings.gmail`:

| Key | Default | What |
|---|---|---|
| `address_env` | `GMAIL_ADDRESS` | The variable holding the first account's address. |
| `password_env` | `GMAIL_APP_PASSWORD` | The variable holding its app password. |
| `accounts` | `["personal"]` | Account labels (above). |
| `notify` | `important` | `important`, `primary`, or `off`. |
| `poll_minutes` | `2` | How often new mail is checked for. |
| `max_results` | `20` | The most messages one search returns (at most 100). |
| `max_chars` | `20000` | The most text one read returns. |
| `attachment_max_mb` | `25` | The largest attachment Atlas fetches. |
| `sends_per_hour` | `20` | Sends per account per hour before Atlas refuses. |
| `attachments_max` | `10` | Files one message may carry. |

## What reaches Google

An IMAP connection to `imap.gmail.com` and, to send, SMTP to `smtp.gmail.com`, both over TLS,
logged in with your app password. Nothing else of Atlas's. Nothing is ever deleted for good:
Trash, which Gmail empties after 30 days, is as far as Atlas moves a message.

What you ask Atlas about a message goes to the model you chose, as everything you ask it does.

## Troubleshooting

**"Gmail is not set up"** - the two lines in `.env` (above) are missing.

**"refused the app password"** - it was revoked, or your Google password changed, which revokes
app passwords. Make a new one.

**"refused: ... contains what looks like a credential"** - the file has a key or password in it.
Take it out, or send the file from Gmail yourself.

**"the draft changed since it was shown"** - it was edited in Gmail after Atlas showed you the
card. Ask again; the new card shows it as it is now.

**"the send may not have gone"** - look in *Sent* before asking again: Atlas never retries a send,
so that a message that did go is not sent twice.
