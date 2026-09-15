from __future__ import annotations

import logging
import os
from pathlib import Path

import discord
from discord import app_commands

from .runtime import Runtime


class Bot(discord.Client):
    def __init__(self, runtime: Runtime):
        super().__init__(intents=discord.Intents.none())
        self.runtime = runtime
        self.tree = app_commands.CommandTree(self)
        self._commands()

    async def setup_hook(self) -> None:
        guild_id = os.environ.get("DISCORD_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    def _commands(self) -> None:
        @self.tree.command(name="games", description="List playable games")
        async def games(interaction: discord.Interaction) -> None:
            names = [f"`{game_id}` — {self.runtime.game(game_id).title}" for game_id in self.runtime.games()]
            await interaction.response.send_message("\n".join(names) or "No games are installed.")

        @self.tree.command(name="start", description="Bind a game to this channel")
        async def start(interaction: discord.Interaction, game: str) -> None:
            if interaction.channel_id is None:
                await interaction.response.send_message("This command requires a channel.", ephemeral=True)
                return
            try:
                current = self.runtime.game(game)
                self.runtime.bind(game, "discord", interaction.channel_id)
            except (FileNotFoundError, ValueError):
                await interaction.response.send_message("That game does not exist.", ephemeral=True)
                return
            await interaction.response.send_message(
                current.opening if current.turn == 0 else self.runtime.scene(game)
            )

        @self.tree.command(name="act", description="Perform an action")
        async def act(interaction: discord.Interaction, action: str) -> None:
            game = self.runtime.game_for_channel(interaction.channel_id)
            if game is None:
                await interaction.response.send_message("Start a game in this channel first.", ephemeral=True)
                return
            await interaction.response.defer(thinking=True)
            result = await self.runtime.act(game, action, str(interaction.user.id))
            await interaction.followup.send(result.narration[:2000])

        @self.tree.command(name="ask", description="Ask an in-world question")
        async def ask(interaction: discord.Interaction, question: str) -> None:
            game = self.runtime.game_for_channel(interaction.channel_id)
            if game is None:
                await interaction.response.send_message("Start a game in this channel first.", ephemeral=True)
                return
            await interaction.response.defer(thinking=True)
            result = await self.runtime.ask(game, question, str(interaction.user.id))
            await interaction.followup.send(result.narration[:2000])

        @self.tree.command(name="scene", description="Show currently known state")
        async def scene(interaction: discord.Interaction) -> None:
            game = self.runtime.game_for_channel(interaction.channel_id)
            message = "Start a game in this channel first." if game is None else self.runtime.scene(game)
            await interaction.response.send_message(message[:2000], ephemeral=game is None)

        @self.tree.command(name="recap", description="Show the recent transcript")
        async def recap(interaction: discord.Interaction) -> None:
            game = self.runtime.game_for_channel(interaction.channel_id)
            message = "Start a game in this channel first." if game is None else self.runtime.recap(game)
            await interaction.response.send_message(message[:2000], ephemeral=game is None)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("DISCORD_BOT_API_KEY")
    if not token:
        raise RuntimeError("DISCORD_BOT_API_KEY is required")
    root = Path(os.environ.get("RPGLAB_ROOT", Path.cwd())).resolve()
    Bot(Runtime(root)).run(token, log_handler=None)


if __name__ == "__main__":
    main()
