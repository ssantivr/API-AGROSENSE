import logging
import os
import socket
import threading

import uvicorn

from .app import create_app
from .config import from_environment

log = logging.getLogger("agrosense_events")


class ServiceServer:
    def __init__(self, app, host: str = "127.0.0.1", port: int = 0):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind((host, port))
        self.socket.listen(128)
        self.port = self.socket.getsockname()[1]
        self.url = f"http://{host}:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off", access_log=False,
                                                    server_header=False))
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self.server.run(sockets=[self.socket])
        finally:
            self._stopped.set()

    def start(self) -> "ServiceServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.server.force_exit = True
        self._stopped.wait(10)
        self.socket.close()


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = from_environment()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8100"))
    log.info("AgroSense event service listening on %s:%s; backend %s; internal key %s; stream %s; origins %s", host, port,
             settings.backend_url, "set" if settings.internal_key else "MISSING (events answer 502)",
             "open" if settings.stream_secret else "closed (EVENTS_STREAM_SECRET missing)",
             ", ".join(settings.allowed_origins) or "none")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info", access_log=False,
                server_header=False, proxy_headers=False, workers=1)
