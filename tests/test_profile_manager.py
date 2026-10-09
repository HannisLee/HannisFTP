from pathlib import Path


def test_ssh_config_alias_discovery(tmp_path: Path):
    from app.services.profile_manager import ProfileManager
    config = tmp_path / "config"
    config.write_text(
        "Host web-*\n  User ignored\n\n"
        "Host prod\n  HostName 203.0.113.10\n  User alice\n  Port 2222\n  IdentityFile ~/.ssh/prod_key\n\n"
        "Host plain\n  HostName example.com\n",
        encoding="utf-8",
    )
    manager = ProfileManager(str(config))
    aliases = {item.alias: item for item in manager.discover_ssh_hosts()}
    assert set(aliases) == {"prod", "plain"}
    assert aliases["prod"].host == "203.0.113.10"
    assert aliases["prod"].user == "alice"
    assert aliases["prod"].port == 2222
    assert aliases["prod"].identity_file.endswith("prod_key")
