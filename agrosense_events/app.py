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
BACKEND_REFUSED_US = "Falta la llave interna o no es válida."
# The other form a device may send, {"deviceId", "timestamp", "datos": {...}}, and the measurement each name means.
SPANISH_MEASUREMENTS = {"temperatura": "air_temperature", "humedad_aire": "relative_humidity",
                        "humedad_suelo": "soil_moisture"}
# What the backend answers when it stored nothing: its transaction was rolled back, or Render's proxy got no answer.
RETRIED_STATUSES = (500, 502, 503, 504)


class Json(JSONResponse):
    media_type = "application/json; charset=utf-8"


class Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _message(status: int, message: str, **headers: str) -> Json:
    return Json({"message": message}, status_code=status, headers=headers or None)


def check_event(body: object) -> dict:
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


def from_spanish(body: object) -> object:
    """Turns {"deviceId", "timestamp", "datos": {"temperatura", ...}} into this service's own form; others pass."""
    if not isinstance(body, dict) or ("deviceId" not in body and "datos" not in body):
        return body
    if any(field in body for field in ("device_id", "ts", "data")):
        raise Refused(400, "Usa un solo formato: {deviceId, timestamp, datos} o {device_id, type, ts, data}.")
    datos = body.get("datos")
    if not isinstance(datos, dict) or not datos:
        raise Refused(400, "El campo 'datos' debe ser un objeto JSON con al menos un valor.")
    data = {}
    for name, value in datos.items():
        if name not in SPANISH_MEASUREMENTS:
            raise Refused(400, f"Medida desconocida en 'datos'. Usa: {', '.join(SPANISH_MEASUREMENTS)}.")
        data[SPANISH_MEASUREMENTS[name]] = value
    return {"device_id": body.get("deviceId"), "type": body.get("type", "reading"), "ts": body.get("timestamp"),
            "data": data}


def device_key_of(request: Request) -> str:
    key = request.headers.get("X-Device-Key", "").strip()
    alias = request.headers.get("X-API-KEY", "").strip()
    if key and alias and key != alias:
        raise Refused(400, "X-Device-Key y X-API-KEY traen llaves distintas; envía solo una.")
    return key or alias


def sse(kind: str, data: dict) -> bytes:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def create_app(settings: Settings, hub: Hub | None = None) -> FastAPI:
    hub = hub or Hub(settings.max_streams_per_user, settings.max_streams)
    app = FastAPI(title="AgroSense eventos", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.hub = hub
    timeout = httpx.Timeout(settings.backend_timeout_ms / 1000)

    async def forward(event: dict, device_key: str) -> httpx.Response:
        """Sends the event, again after a growing wait only when the backend surely stored nothing."""
        attempts = settings.backend_retries + 1
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(1, attempts + 1):
                if attempt > 1:
                    await asyncio.sleep(settings.backend_retry_first_ms / 1000 * 2 ** (attempt - 2))
                try:
                    answer = await client.post(f"{settings.backend_url}/api/internal/events", json=event,
                                               headers={"X-Internal-Key": settings.internal_key,
                                                        "X-Device-Key": device_key})
                except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                    # The request never reached the backend. A timeout while it answered is not retried: it may
                    # have stored the event, and the backend would store it twice.
                    if attempt == attempts:
                        raise
                    log.warning("Event from %s: backend unreachable (%s), attempt %d of %d", event["device_id"],
                                type(error).__name__, attempt, attempts)
                    continue
                if answer.status_code not in RETRIED_STATUSES or attempt == attempts:
                    return answer
                log.warning("Event from %s: the backend answered %s, attempt %d of %d", event["device_id"],
                            answer.status_code, attempt, attempts)
        raise AssertionError("unreachable")

    @app.middleware("http")
    async def headers_and_access_log(request: Request, call_next):
        started = time.monotonic()
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
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
                     "events": "POST /events (o POST /webhook/eventos)", "stream": "GET /stream?token="})

    @app.api_route("/health", methods=["GET", "HEAD"])
    async def health():
        return Json({"status": "UP"})

    @app.post("/events")
    @app.post("/webhook/eventos")
    async def receive(request: Request):
        try:
            return await stored_or_refused(request)
        except Refused as error:
            log.info("Event refused: %s %s", error.status, error)
            raise

    async def stored_or_refused(request: Request):
        device_key = device_key_of(request)
        if not device_key:
            raise Refused(401, "Falta la cabecera X-Device-Key (o X-API-KEY).")
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
        event = check_event(from_spanish(body))
        log.info("Event received from %s: %s with %d value(s)", event["device_id"], event["type"], len(event["data"]))
        if not settings.internal_key:
            log.error("EVENTS_INTERNAL_KEY is empty: the event cannot be forwarded to the backend")
            return _message(502, "El servicio de eventos no está configurado; reintenta más tarde.")
        try:
            answer = await forward(event, device_key)
        except httpx.HTTPError as error:
            log.warning("Event from %s: the backend did not answer (%s)", event["device_id"], type(error).__name__)
            return _message(502, "El backend no respondió; reintenta más tarde.")
        message = _backend_message(answer)
        if answer.status_code == 401 and message == BACKEND_REFUSED_US:
            log.error("The backend rejected this service's internal key: check EVENTS_INTERNAL_KEY")
            return _message(502, "El servicio de eventos no está configurado; reintenta más tarde.")
        if answer.status_code >= 500:
            log.warning("Event from %s: the backend answered %s", event["device_id"], answer.status_code)
            return _message(502, "El backend no pudo guardar el evento; reintenta más tarde.")
        if answer.status_code != 200:
            raise Refused(answer.status_code if answer.status_code in (400, 401, 403, 404, 409) else 400,
                          message or "El backend rechazó el evento.")
        try:
            stored = answer.json()
            owner = str(stored["ownerEmail"]).strip().lower()
            delivered_event = stored["event"]
        except (ValueError, KeyError, TypeError):
            log.warning("The backend answered an event without the owner or the stored event")
            return _message(502, "El backend no pudo guardar el evento; reintenta más tarde.")
        delivered = hub.publish(owner, event["type"], delivered_event)
        log.info("Event from %s stored: %d reading(s), %d alert(s), delivered to %d stream(s)", event["device_id"],
                 len(delivered_event.get("readings") or []), len(delivered_event.get("alerts") or []), delivered)
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
                yield b"retry: 5000\n\n"
                yield sse("ready", {"expiresAt": int(expires)})
                while True:
                    left = expires - time.time()
                    if left <= 0:
                        yield sse("expired", {})
                        return
                    try:
                        kind, event = await asyncio.wait_for(queue.get(), timeout=min(settings.heartbeat_s, left))
                        yield sse(kind, event)
                    except asyncio.TimeoutError:
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
