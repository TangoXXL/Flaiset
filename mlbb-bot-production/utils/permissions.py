"""Проверки прав доступа (администратор/модератор)."""
import discord
import config


def is_admin(member: discord.Member) -> bool:
    """Check Discord permissions plus the configured role for this guild."""
    if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
        return True

    admin_role_id = config.guild_config(member.guild.id).admin_role_id
    if admin_role_id is not None:
        return any(role.id == admin_role_id for role in member.roles)

    return False
