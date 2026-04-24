"""Runtime configuration, loaded from environment variables."""

from __future__ import annotations

import os


class Config:
    anthropic_api_key: str | None
    key_dir_url: str

    def __init__(self) -> None:
        self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
        self.key_dir_url = os.environ.get(
            "ESCARP_KEY_DIR_URL", "http://localhost:8000"
        )


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
