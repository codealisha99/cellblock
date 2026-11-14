"""Settings read from environment variables. Immutable once built."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _csv(raw: str | None, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None:
        return fallback
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    home: Path = Path.home() / ".cellblock"
    port: int = 4300
    instance: str = "default"  # scopes Docker ownership so two control planes never touch each other
    default_image: str = "python:3.12-slim"
    allowed_images: tuple[str, ...] = ("python:3.12-slim", "node:22-alpine")  # the default image is always allowed
    pool_size: int = 2
    max_sandboxes_per_key: int = 5
    reaper_interval_s: float = 5.0
    default_cpu: float = 1.0
    default_memory_mb: int = 512
    default_pids: int = 256
    default_workspace_mb: int = 256

    @property
    def db_path(self) -> Path:
        return self.home / "cellblock.db"

    @property
    def template_dir(self) -> Path:
        return self.home / "templates"

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        default_image = env.get("CELLBLOCK_DEFAULT_IMAGE", cls.default_image)
        return cls(
            home=Path(env.get("CELLBLOCK_HOME", str(cls.home))).expanduser(),
            port=int(env.get("CELLBLOCK_PORT", cls.port)),
            instance=env.get("CELLBLOCK_INSTANCE", cls.instance),
            default_image=default_image,
            allowed_images=tuple(dict.fromkeys([default_image, *_csv(env.get("CELLBLOCK_ALLOWED_IMAGES"), cls.allowed_images)])),
            pool_size=int(env.get("CELLBLOCK_POOL_SIZE", cls.pool_size)),
            max_sandboxes_per_key=int(
                env.get("CELLBLOCK_MAX_SANDBOXES_PER_KEY", cls.max_sandboxes_per_key)
            ),
            reaper_interval_s=float(env.get("CELLBLOCK_REAPER_INTERVAL", cls.reaper_interval_s)),
        )
