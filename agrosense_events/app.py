"""AgroSense's event service: the webhook field devices send their events to, and the live stream of each user.

    GET  /health          -> {"status": "UP"}
    POST /events          -> an event from a device (X-Device-Key); forwarded to the backend, which stores it
    GET  /stream?token=   -> the signed-in user's events as they arrive (Server-Sent Events)

It never touches the database: the backend checks the device's key, stores the event and says whose it is,
and this service delivers it to that user's open streams only.
"""

import asyncio
import json
import logging
import re
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException

from . import tokens
from .config import Settings
from .hub import Hub, TooManyStreams

log = logging.getLogger("agrosense_events")
access_log = logging.getLogger("agrosense_events.access")

MAX_EVENT_BYTES = 16 * 1024
MAX_MEASUREMENTS = 32
EVENT_TYPES = ("reading", "alert")
DEVICE_ID = re.compile(r"[A-Za-z0-9_-]{3,64}")
# What the backend answers when this service's own key is wrong: a fault here, not the device's.
BACKEND_REFUSED_US = "Falta la llave interna o no es válida."


class Json(JSONResponse):
    media_type = "application/json; charset=utf-8"


class Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _message(status: int, message: str, **headers: str) -> Json:
    return Json({"message": message}, status_code=status, headers=headers or None)


def check_event(body: object) -> dict:
    """The event in the shape the backend expects; each refusal names the field to fix."""
    if not isinstance(body, dict):
        raise Refused(400, "El cuerpo debe ser un objeto JSON.")
    device_id = body.get("device_id")
    if not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id.strip()):
        raise Refused(400, "El campo 'device_id' debe tener de 3 a 64 letras, números, guiones o guiones bajos.")
    kind = body.get("type")
    if kind not in EVENT_TYPES:
        raise Refused(400, "El campo 'type' debe ser 'reading' o 'alert'.")
    data = body.get("data")
    if not isinstance(data, dict) or not data:
        raise Refused(400, "El campo 'data' debe ser un objeto JSON con al menos un valor.")
    if len(data) > MAX_MEASUREMENTS:
        raise Refused(400, f"El campo 'data' admite como máximo {MAX_MEASUREMENTS} valores.")
    ts = body.get("ts")
    if ts is not None and (isinstance(ts, bool) or not isinstance(ts, (str, int, float))):
        raise Refused(400, "El campo 'ts' debe ser una fecha ISO-8601 o un número de segundos.")
    return {"device_id": device_id.strip(), "type": kind, "ts": ts, "data": data}


def sse(kind: str, data: dict) -> bytes:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def create_app(settings: Settings, hub: Hub | None = None) -> FastAPI:
    hub = hub or Hub(settings.max_streams_per_user, settings.max_streams)
    app = FastAPI(title="AgroSense eventos", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.hub = hub
    timeout = httpx.Timeout(settings.backend_timeout_ms / 1000)

    @app.middleware("http")
    async def headers_and_access_log(request: Request, call_next):
        started = time.monotonic()
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
        # The path only: the stream's token travels in the query string and must not reach the log.
        access_log.info("%s %s %s %.0fms", request.method, request.url.path, response.status_code,
                        (time.monotonic() - started) * 1000)
        return response

    if settings.allowed_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.allowed_origins), allow_methods=["GET"],
                           allow_credentials=False, allow_headers=[])

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        if error.status_code == 404:
            return _message(404, "No se encontró el recurso solicitado.")
        if error.status_code == 405:
            allowed = (error.headers or {}).get("Allow", "")
            return _message(405, "Método no permitido en esta ruta.", Allow=allowed)
        return _message(error.status_code, str(error.detail))

    @app.exception_handler(Refused)
    async def refused(request: Request, error: Refused):
        return _message(error.status, str(error))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception):
        log.error("Unexpected error answering %s %s", request.method, request.url.path, exc_info=error)
        return _message(500, "Ocurrió un error inesperado. Inténtalo de nuevo.")

    @app.api_route("/", methods=["GET", "HEAD"])
    async def root():
        return Json({"service": "AgroSense eventos", "status": "UP", "health": "/health",
                     "events": "POST /events", "stream": "GET /stream?token="})

    @app.api_route("/health", methods=["GET", "HEAD"])
    async def health():
        return Json({"status": "UP"})

    @app.post("/events")
    async def receive(request: Request):
        device_key = request.headers.get("X-Device-Key", "").strip()
        if not device_key:
            return _message(401, "Falta la cabecera X-Device-Key.")
        if request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise Refused(415, "Envía el evento como application/json.")
        try:
            length = int(request.headers.get("Content-Length", ""))
        except ValueError:
            raise Refused(411, "Falta la cabecera Content-Length.") from None
        if length > MAX_EVENT_BYTES:
            return _message(413, "El evento es demasiado grande.", Connection="close")
        try:
            body = json.loads((await request.body()).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise Refused(400, "El cuerpo no es JSON válido.") from None
        event = check_event(body)
        if not settings.internal_key:
            log.error("EVENTS_INTERNAL_KEY sin valor: no se puede reenviar el evento al backend")
            return _message(502, "El servicio de eventos no está configurado; reintenta más tarde.")
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                answer = await client.post(f"{settings.backend_url}/api/internal/events", json=event,
                                           headers={"X-Internal-Key": settings.internal_key,
                                                    "X-Device-Key": device_key})
        except httpx.HTTPError as error:
            log.warning("El backend no respondió al reenviar un evento: %s", type(error).__name__)
            return _message(502, "El backend no respondió; reintenta más tarde.")
        message = _backend_message(answer)
        if answer.status_code == 401 and message == BACKEND_REFUSED_US:
            log.error("El backend rechazó la llave interna de este servicio: revisa EVENTS_INTERNAL_KEY")
            return _message(502, "El servicio de eventos no está configurado; reintenta más tarde.")
        if answer.status_code >= 500:
            log.warning("El backend respondió %s a un evento", answer.status_code)
            return _message(502, "El backend no pudo guardar el evento; reintenta más tarde.")
        if answer.status_code != 200:
            # The device's own fault (unknown device, wrong key, bad event): retrying would not help.
            return _message(answer.status_code if answer.status_code in (400, 401, 403, 404, 409) else 400,
                            message or "El backend rechazó el evento.")
        try:
            stored = answer.json()
            owner = str(stored["ownerEmail"]).strip().lower()
            delivered_event = stored["event"]
        except (ValueError, KeyError, TypeError):
            log.warning("El backend respondió a un evento sin el dueño o sin el evento guardado")
            return _message(502, "El backend no pudo guardar el evento; reintenta más tarde.")
        delivered = hub.publish(owner, event["type"], delivered_event)
        return Json({"status": "stored", "readings": len(delivered_event.get("readings") or []),
                     "alerts": len(delivered_event.get("alerts") or []), "delivered": delivered})

    @app.get("/stream")
    async def stream(request: Request, token: str = ""):
        try:
            email, expires = tokens.verify(token, settings.stream_secret)
        except tokens.InvalidToken:
            return _message(401, "El token del canal en vivo no es válido o venció.")
        try:
            queue = hub.open(email)
        except TooManyStreams:
            return _message(429, "Hay demasiadas conexiones abiertas; cierra alguna pestaña.")

        async def events():
            try:
                # The browser waits this long before reconnecting after a dropped connection.
                yield b"retry: 5000\n\n"
                yield sse("ready", {"expiresAt": int(expires)})
                while True:
                    left = expires - time.time()
                    if left <= 0:
                        # The token is spent: the browser opens a new stream with a fresh one.
                        yield sse("expired", {})
                        return
                    try:
                        kind, event = await asyncio.wait_for(queue.get(), timeout=min(settings.heartbeat_s, left))
                        yield sse(kind, event)
                    except asyncio.TimeoutError:
                        # A comment line keeps proxies from closing a quiet connection.
                        yield b": ping\n\n"
            finally:
                hub.close(email, queue)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return app


def _backend_message(answer: httpx.Response) -> str:
    try:
        body = answer.json()
        return str(body.get("message", "")).strip() if isinstance(body, dict) else ""
    except ValueError:
        return ""
