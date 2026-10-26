"""Runtime settings, read from the environment and an optional .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "sources" / "manifest.json"

load_dotenv(REPO_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    model_api_key: str | None
    model_api_base: str
    hosted_model: str
    local_api_base: str
    local_model: str
    local_api_key: str | None
    sec_user_agent: str | None

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def parsed_dir(self) -> Path:
        return self.data_dir / "parsed"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"


def load_settings() -> Settings:
    data_dir = os.environ.get("TSTT_DATA_DIR")
    if not data_dir:
        raise RuntimeError(
            "Set TSTT_DATA_DIR to a folder outside the repository (see .env.example)."
        )
    return Settings(
        data_dir=Path(data_dir),
        model_api_key=os.environ.get("MODEL_API_KEY") or None,
        model_api_base=os.environ.get("MODEL_API_BASE", "https://api.meta.ai/v1"),
        hosted_model=os.environ.get("HOSTED_MODEL", "muse-spark-1.3"),
        local_api_base=os.environ.get("LOCAL_API_BASE", "http://127.0.0.1:1234/v1"),
        local_model=os.environ.get("LOCAL_MODEL", "muse-glimmer-30b"),
        local_api_key=os.environ.get("LOCAL_API_KEY") or None,
        sec_user_agent=os.environ.get("SEC_USER_AGENT") or None,
    )
