from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    web_host: str
    web_port: int
    lookback_days: int
    minimax_api_key: str | None
    minimax_base_url: str
    minimax_model: str
    minimax_max_concurrency: int
    whisper_model: str
    whisper_device: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        host = env.get("WEB_HOST", "127.0.0.1")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Goldbook is local-only and refuses public binding")

        concurrency = int(env.get("MINIMAX_MAX_CONCURRENCY", "3"))
        if concurrency < 1 or concurrency > 20:
            raise ValueError("MINIMAX_MAX_CONCURRENCY must be between 1 and 20")

        return cls(
            data_dir=Path(env.get("GOLDBOOK_DATA_DIR", "data")),
            web_host=host,
            web_port=int(env.get("WEB_PORT", "8765")),
            lookback_days=int(env.get("LOOKBACK_DAYS", "183")),
            minimax_api_key=env.get("MINIMAX_API_KEY"),
            minimax_base_url=env.get(
                "MINIMAX_BASE_URL", "https://api.minimaxi.com/v1"
            ),
            minimax_model=env.get("MINIMAX_MODEL", "MiniMax-M3"),
            minimax_max_concurrency=concurrency,
            whisper_model=env.get("WHISPER_MODEL", "small"),
            whisper_device=env.get("WHISPER_DEVICE", "auto"),
        )
