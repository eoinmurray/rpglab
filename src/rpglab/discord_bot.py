from __future__ import annotations

import logging
import os
from pathlib import Path

import discord
from discord import app_commands

from .runtime import Runtime


class Bot(discord.Client):
    """Small Discord transport: one initialized game and character per channel."""

    def __init__(self, runtime: Runtime):
        intents = discord.Intents(guilds=True, messages=True, message_content=True)
        super().__init__(intents=intents)
        self.runtime = runtime
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    async def setup_hook(self) -> None:
        guild_id = os.environ.get("DISCORD_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.content.strip():
            return
        context = self.runtime.channel_context(message.channel.id)
        if not context:
            return
        try:
            actor = self.runtime.actor_for_user(
                context["game_id"], message.channel.id, str(message.author.id),
            )
            result = await self.runtime.act(
                context["game_id"], message.content, str(message.author.id), actor,
            )
        except (ValueError, FileNotFoundError) as exc:
            await message.reply(str(exc))
            return
        await message.reply(result.display()[:2000])

    def _register_commands(self) -> None:
        @self.tree.command(name="games", description="List initialized games")
        async def games(interaction: discord.Interaction) -> None:
            values = [
                f"`{game_id}` — {self.runtime.game(game_id).title}"
                for game_id in self.runtime.games()
            ]
            await interaction.response.send_message(
                "\n".join(values) or "No initialized games are available.", ephemeral=True,
            )

        @self.tree.command(name="start", description="Start an initialized game here")
        async def start(
            interaction: discord.Interaction, game: str, character: str = "",
        ) -> None:
            if interaction.channel_id is None:
                await interaction.response.send_message("Use this in a channel.", ephemeral=True)
                return
            try:
                character_id = self.runtime.bind(
                    game, "discord", interaction.channel_id, str(interaction.user.id),
                    character or None,
                )
                opening = self.runtime.opening(game, character_id)
            except (ValueError, FileNotFoundError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await interaction.response.send_message(opening[:2000])

        async def run_turn(
            interaction: discord.Interaction, text: str, question: bool,
        ) -> None:
            if interaction.channel_id is None:
                await interaction.response.send_message("Use this in a game channel.", ephemeral=True)
                return
            context = self.runtime.channel_context(interaction.channel_id)
            if not context:
                await interaction.response.send_message(
                    "Start a game in this channel first.", ephemeral=True,
                )
                return
            try:
                actor = self.runtime.actor_for_user(
                    context["game_id"], interaction.channel_id, str(interaction.user.id),
                )
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await interaction.response.defer(thinking=True)
            method = self.runtime.ask if question else self.runtime.act
            result = await method(context["game_id"], text, str(interaction.user.id), actor)
            await interaction.followup.send(result.display()[:2000])

        @self.tree.command(name="act", description="Perform an action")
        async def act(interaction: discord.Interaction, action: str) -> None:
            await run_turn(interaction, action, False)

        @self.tree.command(name="ask", description="Ask an in-world question")
        async def ask(interaction: discord.Interaction, question: str) -> None:
            await run_turn(interaction, question, True)

        @self.tree.command(name="scene", description="Show the current scene")
        async def scene(interaction: discord.Interaction) -> None:
            context = self.runtime.channel_context(interaction.channel_id)
            if not context:
                await interaction.response.send_message("No game is active here.", ephemeral=True)
                return
            value = self.runtime.scene(context["game_id"], context["character"])
            await interaction.response.send_message(value[:2000], ephemeral=True)

        @self.tree.command(name="recap", description="Show recent turns")
        async def recap(interaction: discord.Interaction) -> None:
            context = self.runtime.channel_context(interaction.channel_id)
            if not context:
                await interaction.response.send_message("No game is active here.", ephemeral=True)
                return
            await interaction.response.send_message(
                self.runtime.recap(context["game_id"])[:2000], ephemeral=True,
            )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("DISCORD_BOT_API_KEY")
    if not token:
        raise RuntimeError("DISCORD_BOT_API_KEY is required")
    root = Path(os.environ.get("RPGLAB_ROOT", Path.cwd())).resolve()
    Bot(Runtime(root)).run(token, log_handler=None)


if __name__ == "__main__":
    main()
