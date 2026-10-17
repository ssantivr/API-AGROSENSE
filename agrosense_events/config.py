"""Settings of the event service, read once from the environment. No secret has a default or is logged."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Where the backend is, and the key it asks of this service in X-Internal-Key.
    backend_url: str = "http://localhost:9000"
    internal_key: str = ""
    # Shared with the backend only: it signs the tokens that open a user's live stream.
    stream_secret: str = ""
    # Browser origins allowed to open the stream (the frontend's address).
    allowed_origins: tuple[str, ...] = ()
    # A free host puts the backend to sleep; the first call after a while waits for it to start.
    backend_timeout_ms: int = 30000
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
        heartbeat_s=number("EVENTS_HEARTBEAT_S", 15.0),
        max_streams_per_user=int(number("EVENTS_MAX_STREAMS_PER_USER", 5)),
        max_streams=int(number("EVENTS_MAX_STREAMS", 500)),
    )
