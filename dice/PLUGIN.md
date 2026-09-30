---
name: dice
description: Roll dice - a small, honest example of a marketplace plugin.
version: "1.0.0"
requires_atlas_sdk: ">=1,<2"
categories: [example, fun]
contracts:
  tools: [roll_dice]
config_schema:
  max_dice:
    type: int
    default: 100
    description: The most dice one call may roll.
---

# dice

One tool, `roll_dice`, that rolls `count` dice of `sides` sides and reports each die
and the total. It touches nothing on this machine and reaches no network, which is why
it is the example: everything a plugin has to get right is here, and nothing that would
distract from it.

What to copy from it:

- `PLUGIN.md` declares the tool in `contracts` and its one setting in `config_schema`,
  so `/plugins dice` can show both without importing anything.
- `plugin.py` imports from `atlas.sdk.*` only, reads its setting with `ctx.setting`,
  and returns a `ToolResult` for a bad argument rather than raising.
- The tool's `description` carries the guidance the model needs. That is the only
  channel a plugin has into the prompt, and it costs tokens only where the tool exists.
