from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from platformdirs import user_data_path


def data_path(value: str | Path | None = None) -> Path:
    return (
        Path(value or os.getenv("APPTRAIL_DATA_DIR") or user_data_path("apptrail", appauthor=False))
        .expanduser()
        .resolve()
    )


class Config:
    def __init__(self, directory: str | Path | None = None):
        self.directory = data_path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.secret_path = self.directory / "credentials.json"
        self.key_override: str | None = None

    @property
    def env_key(self) -> str:
        return os.getenv("SERPAPI_API_KEY") or os.getenv("SERPAPI_KEY", "")

    @property
    def api_key(self) -> str:
        if self.key_override is not None:
            return self.key_override
        if self.env_key:
            return self.env_key
        if not self.secret_path.exists():
            return ""
        return json.loads(self.secret_path.read_text()).get("serpapi_api_key", "")

    def save_key(self, key: str) -> None:
        if self.env_key:
            raise ValueError(
                "The key is managed by the environment. Update SERPAPI_API_KEY and restart."
            )
        fd, filename = tempfile.mkstemp(dir=self.directory, prefix=".credentials-")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump({"serpapi_api_key": key}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(filename, self.secret_path)
        finally:
            Path(filename).unlink(missing_ok=True)
