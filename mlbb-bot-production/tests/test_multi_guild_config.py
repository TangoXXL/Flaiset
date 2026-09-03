import importlib
import json


def test_guild_config_overrides_and_fallback(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.setenv(
        "GUILD_CONFIG_JSON",
        json.dumps({"111": {"results_channel_id": 222, "admin_role_id": 333, "players_per_game": 6}}),
    )
    import config

    config = importlib.reload(config)
    cfg = config.guild_config(111)
    assert cfg.results_channel_id == 222
    assert cfg.admin_role_id == 333
    assert cfg.players_per_game == 6

    fallback = config.guild_config(999)
    assert fallback.players_per_game == config.PLAYERS_PER_GAME


def test_guild_config_rejects_odd_game_size(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.setenv("GUILD_CONFIG_JSON", json.dumps({"111": {"players_per_game": 5}}))
    import config

    try:
        importlib.reload(config)
    except RuntimeError as exc:
        assert "чётным" in str(exc)
    else:
        raise AssertionError("Expected invalid odd players_per_game to be rejected")
