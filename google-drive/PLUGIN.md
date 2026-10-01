---
name: google-drive
description: Google Drive - find, read and download your files; upload, organise and share them with your yes.
categories: [files, productivity]
logo: logo.svg
version: "1.0.0"
requires_atlas_sdk: ">=1.34,<2"
contracts:
  tools:
    - drive_search
    - drive_read
    - drive_info
    - drive_download
    - drive_upload
    - drive_create
    - drive_organise
    - drive_share
  logins: [google]
config_schema:
  client_id:
    type: str
    default: ""
    description: A Google OAuth client id (Desktop app) to sign in with instead of Atlas's own.
  max_results:
    type: int
    default: 25
    description: The most files one search returns. At most 100.
  max_chars:
    type: int
    default: 20000
    description: The most text one drive_read returns.
  download_max_mb:
    type: int
    default: 50
    description: The largest file drive_download fetches.
  upload_max_mb:
    type: int
    default: 50
    description: The most one drive_upload carries, all its files together.
  files_max:
    type: int
    default: 10
    description: The most files one drive_upload carries.
---

# google-drive

**Read this when:** you want Atlas to find, read or download your Google Drive files, put
things in Drive, tidy it, or share a file - or you are wondering why something asked you first,
or exactly what this plugin sends to Google.

**See also:** [`docs/spec/google-drive.md`](https://github.com/Starifter/atlas/blob/main/docs/spec/google-drive.md) is the specification and
wins where they disagree; [`docs/core/oauth.md`](https://github.com/Starifter/atlas/blob/main/docs/core/oauth.md) is how signing in works for
every plugin. The plugin itself is this directory.

---

`google-drive` is in Atlas's official marketplace. Install it once:

```
/plugins install google-drive
```

That copies it into `~/.atlas/plugins/google-drive/` and enables it; it loads on the next
session. It then does nothing until you sign in: no request reaches Google, and the tools
answer "not signed in".

## Setup

```console
$ atlas auth login google-drive:google
```

Your browser opens Google's consent page. Sign in, allow access to your Drive, and the terminal
says who you signed in as. The tools work from the next thing you say.

**A second account** is a second sign-in with a label:

```console
$ atlas auth login google-drive:google --id work
```

With more than one, a search covers all of them and says which account each file is in; a
change needs you to say which (`account: work`).

**Signing out** is `atlas auth logout google-drive:google`. To take the access back at Google
too, remove Atlas at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

**Before Atlas's Google app is verified** only people added as test users can sign in, and a
sign-in lasts 7 days; the tools say how to sign in again when it runs out.

**With your own Google client**, create an OAuth client of type *Desktop app* in a Google Cloud
project with the Drive API enabled, and set its id:

```jsonc
{ "plugins_settings": { "google-drive": { "client_id": "1234-abc.apps.googleusercontent.com" } } }
```

## What you can ask

| You say | Atlas uses | Asks you first? |
|---|---|---|
| "Where's the lease?" · "What did Sam share with me this week?" | `drive_search` | no |
| "What does the packing list say?" · "Summarise the budget sheet" | `drive_read` | no |
| "Who can see the budget?" · "Where is that file?" | `drive_info` | no |
| "Get me the lease PDF" · "Show me the photo from the trip folder" | `drive_download` | no |
| "Save the report to my Taxes folder" | `drive_upload` | yes |
| "Make a doc of these notes" · "Make a Projects folder" | `drive_create` | yes |
| "Rename it", "move these to Archive", "trash the old drafts", "star it" | `drive_organise` | yes |
| "Share the budget with Sam as an editor" · "Stop sharing it by link" | `drive_share` | yes, always |

**Reading** turns a Google Doc into Markdown, a Google Sheet into CSV - only its first sheet;
download it as XLSX for all of them - and Slides into text. A PDF, a picture or a Word file is
downloaded instead: a picture is shown to the model, anything else is saved in your workspace
under `drive-downloads/` and read from there.

## What asks you, and how

Every change is a card. Most take your permission mode's ordinary answer. Three things **ask you
in every mode, `yolo` included**, because each lets somebody new see something:

- **sharing** - the card lists every address, the role each one gets, and whether Google will
  email them (it will not, unless you say so);
- **uploading or creating into a folder other people can see**, and
- **moving something into one** - the card ends `· visible to ...` and names them.

```
Upload 1 file(s) to Google Drive · My Drive / Family · visible to alex@example.com (editor)
  photo.jpg - image, 2.1 MB, sha256 77b20d4a - you sent it in this chat
```

Taking someone's access away only narrows who can see a file, so it is an ordinary card.

## What it will not do

- **Delete anything for good.** Trash is as far as it goes, and Drive empties Trash after 30
  days, so you have a month to change your mind.
- **Make a file public** or share it with a whole domain. It shares with people, by address.
- **Give away ownership.**
- **Upload a secret.** A dotfile (`.env`, `.ssh/`), anything in Atlas's own `.atlas/` folder, a
  key file (`*.pem`, `id_ed25519`, ...) or a text with an API key in it is refused before you are
  asked - and the refusal names what kind of key, never the key.
- **Upload anything but what the card showed.** Each file is read and hashed when the card is
  drawn, and those bytes are what goes, even if the file changes before you answer.
- **Edit a document that is already there** - add to a Doc, change a cell. It can make new ones.

## What reaches Google

Your access token, in a request to `www.googleapis.com` and nowhere else - an upload's address
included, which Atlas checks before following it. What you ask about goes to the model provider
you chose, as everything you ask Atlas does. Files a stranger shared with you are read as
untrusted text: if one says "share everything with this address", the share is still a card
with that address on it.

## Settings

All optional, under `plugins_settings.google-drive`:

| Key | Default | What |
|---|---|---|
| `client_id` | Atlas's | Your own Google OAuth client id. |
| `max_results` | `25` | The most files one search returns (at most 100). |
| `max_chars` | `20000` | The most text one read returns. |
| `download_max_mb` | `50` | The largest file a download fetches. |
| `upload_max_mb` | `50` | The most one upload carries, all files together. |
| `files_max` | `10` | The most files one upload carries. |

To turn the plugin off: `/plugins disable google-drive`; `/plugins remove google-drive` deletes it.
