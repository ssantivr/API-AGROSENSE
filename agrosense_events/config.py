import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    backend_url: str = "http://localhost:9000"
    internal_key: str = ""
    stream_secret: str = ""
    allowed_origins: tuple[str, ...] = ()
    backend_timeout_ms: int = 30000
    backend_retries: int = 3
    backend_retry_first_ms: int = 500
    heartbeat_s: float = 15.0
    max_streams_per_user: int = 5
    max_streams: int = 500


def from_environment(environ: dict | None = None) -> Settings:
    env = os.environ if environ is None else environ

    def text(name: str, default: str = "") -> str:
        return env.get(name, "").strip() or default

    def number(name: str, default: float) -> float:
        value = env.get(name, "").strip()
        return float(value) if value else default

    return Settings(
        backend_url=text("BACKEND_URL", "http://localhost:9000").rstrip("/"),
        internal_key=text("EVENTS_INTERNAL_KEY"),
        stream_secret=text("EVENTS_STREAM_SECRET"),
        allowed_origins=tuple(origin.strip().rstrip("/") for origin in env.get("EVENTS_ALLOWED_ORIGINS", "").split(",")
                              if origin.strip()),
        backend_timeout_ms=int(number("BACKEND_TIMEOUT_MS", 30000)),
        backend_retries=max(0, int(number("BACKEND_RETRIES", 3))),
        backend_retry_first_ms=max(0, int(number("BACKEND_RETRY_FIRST_MS", 500))),
        heartbeat_s=number("EVENTS_HEARTBEAT_S", 15.0),
        max_streams_per_user=int(number("EVENTS_MAX_STREAMS_PER_USER", 5)),
        max_streams=int(number("EVENTS_MAX_STREAMS", 500)),
    )
