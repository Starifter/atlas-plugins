---
name: google-tasks
description: Google Tasks - see your to-do lists; add, finish, move and remove tasks with your yes.
categories: [productivity]
logo: logo.svg
version: "1.0.0"
requires_atlas_sdk: ">=1.34,<2"
contracts:
  tools:
    - tasks_lists
    - tasks_show
    - tasks_add
    - tasks_update
    - tasks_remove
    - tasks_list_manage
  logins: [google]
config_schema:
  client_id:
    type: str
    default: ""
    description: A Google OAuth client id (Desktop app) to sign in with instead of Atlas's own.
  max_results:
    type: int
    default: 100
    description: The most tasks one tasks_show returns. At most 1000.
---

# google-tasks

**Read this when:** you want Atlas to keep your Google Tasks to-do lists - add things, tick them
off, move them about - or you are wondering why something asked you first, or exactly what this
plugin sends to Google.

**See also:** [`docs/core/oauth.md`](https://github.com/Starifter/atlas/blob/main/docs/core/oauth.md) is how signing in works for every
plugin. The plugin itself is this directory, and this page is the whole of what it promises.

---

`google-tasks` is in Atlas's official marketplace. Install it once:

```
/plugins install google-tasks
```

That copies it into `~/.atlas/plugins/google-tasks/` and enables it; it loads on the next
session. It then does nothing until you sign in: no request reaches Google, and the tools
answer "not signed in".

## Setup

```console
$ atlas auth login google-tasks:google
```

Your browser opens Google's consent page. Sign in, allow access to your tasks, and the terminal
says who you signed in as. The tools work from the next thing you say.

**A second account** is a second sign-in with a label:

```console
$ atlas auth login google-tasks:google --id work
```

With more than one, "what's on my list" covers all of them and says which account each list is
in; a change needs you to say which (`account: work`).

**Signing out** is `atlas auth logout google-tasks:google`. To take the access back at Google
too, remove Atlas at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

**Before Atlas's Google app is verified** only people added as test users can sign in, and a
sign-in lasts 7 days; the tools say how to sign in again when it runs out.

**With your own Google client**, create an OAuth client of type *Desktop app* in a Google Cloud
project with the Google Tasks API enabled, and set its id:

```jsonc
{ "plugins_settings": { "google-tasks": { "client_id": "1234-abc.apps.googleusercontent.com" } } }
```

## What you can ask

| You say | Atlas uses | Asks you first? |
|---|---|---|
| "What lists do I have?" | `tasks_lists` | no |
| "What's on my to-do list?" · "What's overdue?" · "What did I finish this week?" | `tasks_show` | no |
| "Remind me to buy milk" · "Add 'renew passport' due Friday to my Errands list" | `tasks_add` | yes |
| "Mark the dentist one done" · "Move it to Errands" · "Push it to next Monday" | `tasks_update` | yes |
| "Clear the finished ones off Errands" | `tasks_remove` | yes |
| "Delete the milk task" | `tasks_remove` | yes, always |
| "Make a Holiday list" · "Rename Errands to Shopping" | `tasks_list_manage` | yes |
| "Delete the Holiday list" | `tasks_list_manage` | yes, always |

**Due dates are dates.** Google Tasks keeps a day and no time of day - its API drops the time -
so "remind me at 3pm" becomes a task due that day, and Google's own apps decide whether and
when to notify you. Recurring tasks are not offered either: Google does not let other apps
make them.

**Subtasks** show indented under their task. Google Tasks nests one level deep, so a subtask
cannot have subtasks of its own.

## What asks you, and how

Every change is a card. Adding, completing, renaming, moving and changing a due date take your
permission mode's ordinary answer, because each can be put back:

```
Add 1 task(s) to "Errands" (Google Tasks)
  renew passport · due Fri 9 Oct
Due dates are a date only - Google Tasks keeps no time of day.
```

Two things **ask you in every mode, `yolo` included**, because Google Tasks has no bin to fetch
them back from:

- **deleting tasks** - the card names every task that goes, each subtask included; and
- **deleting a list** - the card says how many tasks go with it, open and completed, and names
  the open ones.

**Clearing completed tasks** is an ordinary card: Google hides them from the list rather than
deleting them.

## What it will not do

- **Delete your main list.** Google Tasks does not allow it.
- **Set a time** on a task, or make one repeat.
- **Move a task assigned from a Google Doc or a Chat space** to another list - Google keeps it
  where it was assigned.
- **Do anything to more than a handful at once:** 20 new tasks, 25 changes or 50 deletions in
  one go; past that it asks for a narrower request.

## What reaches Google

Your access token, in a request to `tasks.googleapis.com` and nowhere else; the plugin checks
the address before every request. What you ask about goes to the model provider you chose, as
everything you ask Atlas does. A task assigned to you from a Doc or a Chat space was written by
whoever assigned it, so every task is read as untrusted text: if one says "delete all my
lists", the deletion is still a card that asks you.

## Settings

All optional, under `plugins_settings.google-tasks`:

| Key | Default | What |
|---|---|---|
| `client_id` | Atlas's | Your own Google OAuth client id. |
| `max_results` | `100` | The most tasks one `tasks_show` returns (at most 1000). |

To turn the plugin off: `/plugins disable google-tasks`; `/plugins remove google-tasks` deletes it.
