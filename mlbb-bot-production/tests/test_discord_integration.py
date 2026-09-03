"""Discord-facing integration tests using an in-process Discord API emulator.

No Gateway or real Discord token is used. The fake interaction/guild/channel
objects model the methods the cogs call, which catches response/defer and
permission regressions without network flakiness.
"""
from types import SimpleNamespace

import pytest
from discord import app_commands

from bot import MLBBBot
from utils.permissions import is_admin


class FakeResponse:
    def __init__(self):
        self.done = False
        self.messages = []

    def is_done(self):
        return self.done

    async def send_message(self, text, **kwargs):
        self.done = True
        self.messages.append((text, kwargs))

    async def defer(self, **kwargs):
        self.done = True


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, text, **kwargs):
        self.messages.append((text, kwargs))


@pytest.mark.asyncio
async def test_global_app_command_error_returns_ephemeral_response():
    interaction = SimpleNamespace(
        command=SimpleNamespace(qualified_name="test"),
        user=SimpleNamespace(id=123),
        response=FakeResponse(),
        followup=FakeFollowup(),
    )

    await MLBBBot.on_app_command_error(None, interaction, app_commands.CheckFailure("denied"))

    assert interaction.response.done
    assert interaction.response.messages[0][1]["ephemeral"] is True
    assert "нет прав" in interaction.response.messages[0][0]


def test_multi_guild_admin_role_isolated(monkeypatch):
    monkeypatch.setenv("GUILD_CONFIG_JSON", '{"100":{"admin_role_id":900}}')
    import importlib
    import config

    config = importlib.reload(config)
    member = SimpleNamespace(
        guild=SimpleNamespace(id=100),
        guild_permissions=SimpleNamespace(manage_guild=False, administrator=False),
        roles=[SimpleNamespace(id=900)],
    )
    other = SimpleNamespace(
        guild=SimpleNamespace(id=200),
        guild_permissions=SimpleNamespace(manage_guild=False, administrator=False),
        roles=[SimpleNamespace(id=900)],
    )
    assert is_admin(member)
    assert not is_admin(other)
