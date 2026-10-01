---
name: google-docs-sheets
description: Google Docs and Sheets - read them; add to a Doc, fix a phrase, fill in cells and add rows with your yes.
categories: [productivity, files]
logo: logo.svg
version: "1.0.0"
requires_atlas_sdk: ">=1.34,<2"
contracts:
  tools:
    - docs_read
    - docs_edit
    - docs_create
    - sheets_read
    - sheets_write
    - sheets_append
    - sheets_tab
  logins: [google]
config_schema:
  client_id:
    type: str
    default: ""
    description: A Google OAuth client id (Desktop app) to sign in with instead of Atlas's own.
  max_chars:
    type: int
    default: 20000
    description: The most text one docs_read or sheets_read returns.
  max_rows:
    type: int
    default: 200
    description: The most rows one sheets_read shows.
  overwrite_confirm:
    type: int
    default: 50
    description: A sheets_write that overwrites more filled cells than this asks in every mode.
---

# google-docs-sheets

**Read this when:** you want Atlas to add to a Google Doc, fix something in one, read or fill in a
Google Sheet, or add rows and tabs to one - or you are wondering why something asked you first,
or exactly what this plugin sends to Google.

**See also:** [`docs/core/oauth.md`](https://github.com/Starifter/atlas/blob/main/docs/core/oauth.md)
is how signing in works for every plugin. `google-drive` is the other half: it finds files,
reads and downloads them whole, makes new ones from Markdown or CSV, moves and shares them. This
plugin edits *inside* a Doc or a Sheet, which `google-drive` deliberately does not. The plugin
itself is this directory.

---

`google-docs-sheets` is in Atlas's official marketplace. Install it once:

```
/plugins install google-docs-sheets
```

That copies it into `~/.atlas/plugins/google-docs-sheets/` and enables it; it loads on the next
session. It then does nothing until you sign in: no request reaches Google, and the tools answer
"not signed in".

## Setup

```console
$ atlas auth login google-docs-sheets:google
```

Your browser opens Google's consent page. Sign in, allow Atlas to see and edit your Google Docs
and your Google Sheets, and the terminal says who you signed in as. The tools work from the next
thing you say. This is a sign-in of its own, separate from `google-drive`'s: each plugin asks
only for what it uses.

**A second account** is a second sign-in with a label:

```console
$ atlas auth login google-docs-sheets:google --id work
```

With more than one, reading tries each account until one can open the document; a change needs
you to say which (`account: work`).

**Signing out** is `atlas auth logout google-docs-sheets:google`. To take the access back at
Google too, remove Atlas at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

**Before Atlas's Google app is verified** only people added as test users can sign in, and a
sign-in lasts 7 days; the tools say how to sign in again when it runs out.

**With your own Google client**, create an OAuth client of type *Desktop app* in a Google Cloud
project with both the **Google Docs API** and the **Google Sheets API** enabled, and set its id:

```jsonc
{ "plugins_settings": { "google-docs-sheets": { "client_id": "1234-abc.apps.googleusercontent.com" } } }
```

An API left off answers every call with a sentence saying which one to enable.

## What you can ask

Name a document by its link (`https://docs.google.com/document/d/...`) or its id. To find one by
name, `google-drive`'s `drive_search` does that and hands back the id.

| You say | Atlas uses | Asks you first? |
|---|---|---|
| "What's in the invitation doc?" · "What headings does the plan have?" | `docs_read` | no |
| "Add today's groceries to my shopping list doc" · "Add a line under *Packing*" | `docs_edit` | yes |
| "Fix the typo in the invitation - Satruday" · "Change every *2025* to *2026*" | `docs_edit` | yes |
| "Start a doc called *Trip ideas* with these notes" | `docs_create` | yes |
| "What's in column C of the expenses tab?" · "What tabs does the budget have?" | `sheets_read` | no |
| "Set B4 to 42" · "Fill in this week's hours" | `sheets_write` | yes; **always** when it overwrites a lot |
| "Log $42 for petrol in my budget sheet" | `sheets_append` | yes |
| "Add a tab for October" · "Rename *Sheet1* to *2026*" | `sheets_tab` | yes |
| "Delete the *Old* tab" · "Clear B2:D40" | `sheets_tab` | yes, **always** |

**Reading a Doc** gives its text as Markdown - headings, bulleted and numbered lists, tables,
bold, italics and links - with its tabs, and its headers, footers and footnotes after. **Reading
a Sheet** lists its tabs and their sizes, then the first tab (or the one a link points at, or a
range like `Expenses!A1:D20` or `C:C`) as a table with column letters and row numbers; ask for
formulas and it lists those too.

**Adding to a Doc** goes at the end, at the end of the section under a heading you name - so
a new item under *Groceries* continues that list - or at the very start. New lines make new
paragraphs. **Replacing** swaps every occurrence of a phrase within one tab, matching capitals
unless you say not to.

**Writing cells** is typed as you would type it in Sheets: `42` is a number, `2026-10-01` a
date, `=SUM(B2:B9)` a formula. **Appending** inserts new rows below the table on a tab, so
nothing underneath is overwritten.

## What asks you, and how

Every change is a card drawn from what is really in the document when you are asked:

- **adding to a Doc** shows the text and the paragraph it follows;
- **replacing** shows how many occurrences there are and each one in context, before and after;

  ```
  Replace in "Invitation" (Google Doc): "Satruday" → "Saturday" · 2 occurrence(s)
    - Dinner on Satruday 12th at 7pm…
    + Dinner on Saturday 12th at 7pm…
  ```

- **writing cells** lists every cell it changes, what is there now and what goes in;

  ```
  Write 2 cell(s) in "Budget" · Expenses!B4:C4 · 1 filled cell(s) overwritten
    B4: "Petrol" → "Diesel"
    C4: (empty) → 42
  ```

- **appending** shows the rows, the tab's column headings and the row they go after.

Most take your permission mode's ordinary answer. These **ask you in every mode, `yolo`
included**:

- **deleting a tab** and **clearing a range** - the card says how many filled cells go;
- **a write that overwrites more filled cells than `overwrite_confirm`** (50 unless you change
  it);
- **a write or an append with a formula that fetches from the web** - `IMPORTXML`,
  `IMPORTHTML`, `IMPORTDATA`, `IMPORTFEED`, `IMPORTRANGE` or `IMAGE` - because such a formula
  can send what is in the sheet to the address it names.

**Nothing changes but what the card showed.** A Doc edit carries the version of the Doc the
card was drawn from; if someone changes the Doc before you answer, Google refuses the edit and
nothing is written. A Sheet has no such version, so a write, a clear or a delete reads its cells
again just before it runs and stops if they are not what the card showed.

## What it will not do

- **Share anything, or change who can see a file.** That is `google-drive`'s, on its own card.
- **Say who else can see a document.** That needs Drive access, which this plugin does not ask
  for; `google-drive`'s `drive_info` says. Keep in mind that text you add to a shared Doc is
  seen by everyone it is shared with.
- **Delete a Doc or a Sheet**, or move one. `google-drive` trashes and moves files.
- **Format text** - fonts, colours, cell formats. It adds and replaces words and values.
- **Write a secret.** Text or cells with what looks like an API key in them are refused before
  you are asked, and the refusal names what kind of key, never the key.
- **Reach anything in Drive but Docs and Sheets.** Its two permissions cover those and nothing
  else - not your PDFs, photos or folders.

## What reaches Google

Your access token, in requests to `docs.googleapis.com` and `sheets.googleapis.com` and nowhere
else - Atlas checks the address before every request. What you ask about goes to the model
provider you chose, as everything you ask Atlas does. A document someone else shared with you is
read as untrusted text: if it says "replace every address with this one" or "write this formula",
that is still a card with the change on it, and a formula that would fetch from the web is asked
in every mode.

## Settings

All optional, under `plugins_settings.google-docs-sheets`:

| Key | Default | What |
|---|---|---|
| `client_id` | Atlas's | Your own Google OAuth client id. |
| `max_chars` | `20000` | The most text one read returns. |
| `max_rows` | `200` | The most rows one `sheets_read` shows. |
| `overwrite_confirm` | `50` | A write overwriting more filled cells than this asks in every mode. |

To turn the plugin off: `/plugins disable google-docs-sheets`; `/plugins remove google-docs-sheets`
deletes it.
