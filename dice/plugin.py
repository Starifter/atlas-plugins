"""Roll dice. The marketplace's example plugin - see PLUGIN.md for what to copy."""

from __future__ import annotations

import random

from atlas.sdk.plugin_entry import Plugin, PluginContext
from atlas.sdk.tool_plugin import Tool, ToolResult


class RollDice(Tool):
    name = "roll_dice"
    description = (
        "Roll dice and report each die and the total. Use it when the user asks for a "
        "roll or a random pick a die could make; the result is random every call."
    )
    parameters = {
        "type": "object",
        "properties": {
            "sides": {"type": "integer", "minimum": 2, "description": "Faces per die."},
            "count": {"type": "integer", "minimum": 1, "description": "How many dice."},
        },
        "required": [],
    }

    def __init__(self, *, max_dice: int) -> None:
        self.max_dice = max_dice

    async def run(self, *, sides: int = 6, count: int = 1) -> ToolResult:
        if count > self.max_dice:
            # A failure the model can read and recover from, not an exception.
            return ToolResult.error(f"{count} dice is more than max_dice ({self.max_dice})")
        rolls = [random.randint(1, sides) for _ in range(count)]  # a game, not a secret
        return ToolResult.ok(f"{count}d{sides}: {' '.join(map(str, rolls))} = {sum(rolls)}")


class DicePlugin(Plugin):
    name = "dice"
    description = "Roll dice."

    def register(self, ctx: PluginContext) -> None:
        ctx.register_tool(RollDice(max_dice=int(ctx.setting("max_dice", 100))))
