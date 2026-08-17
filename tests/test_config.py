from pathlib import Path

from ppa.config import Config


def test_config_creates_default_file_when_missing(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    assert not config_path.exists()

    config = Config.load(config_path)

    assert config_path.exists()
    assert config.log_level == "INFO"
    assert config.library_directories == []


def test_config_loads_existing_values(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[database]
path = "~/db.sqlite3"

[logging]
level = "DEBUG"
path = "~/log.txt"

[library]
directories = ["~/Pictures"]
""",
        encoding="utf-8",
    )

    config = Config.load(config_path)

    assert config.log_level == "DEBUG"
    assert config.library_directories == [Path("~/Pictures").expanduser()]
