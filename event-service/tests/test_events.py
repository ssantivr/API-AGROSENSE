import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient

from agrosense_events import tokens
from agrosense_events.app import MAX_EVENT_BYTES, create_app
from agrosense_events.config import Settings, from_environment
from agrosense_events.server import ServiceServer

INTERNAL_KEY = "internal-key-of-the-events"
STREAM_SECRET = "stream-secret-of-at-least-32-bytes"
DEVICE_KEY = "device-key"
OWNER = "demo@agrosense.co"
EVENT = {"device_id": "RPI-DEMO-01", "type": "reading", "ts": "2026-10-16T08:00:00Z",
         "data": {"soil_moisture": 31.5, "ph": 6.4}}
STORED = {"ownerEmail": OWNER, "event": {"type": "reading", "deviceId": "RPI-DEMO-01", "cropId": 1,
                                         "cropName": "Café", "readings": [{"sensorCode": "AS-001", "value": 31.5},
                                                                           {"sensorCode": "AS-003", "value": 6.4}],
                                         "alerts": []}}


class FakeBackend:
    def __init__(self):
        self.requests: list[dict] = []
        self.status, self.body, self.delay = 200, STORED, 0.0
        self.queued: list[tuple[int, dict]] = []
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                backend.requests.append({"path": self.path, "json": json.loads(raw),
                                         "headers": {k.lower(): v for k, v in self.headers.items()}})
                if backend.delay:
                    time.sleep(backend.delay)
                status, body = backend.queued.pop(0) if backend.queued else (backend.status, backend.body)
                payload = json.dumps(body).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self, status, body, delay=0.0):
        self.status, self.body, self.delay = status, body, delay

    def answer_in_turn(self, *answers):
        """The first requests get these (status, body) answers; the ones after, the usual answer."""
        self.queued = list(answers)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def backend():
    fake = FakeBackend()
    yield fake
    fake.close()


def settings_for(backend_url: str, **overrides) -> Settings:
    values = {"backend_url": backend_url, "internal_key": INTERNAL_KEY, "stream_secret": STREAM_SECRET,
              "allowed_origins": ("http://localhost:8080",), "backend_timeout_ms": 2000, "backend_retry_first_ms": 10,
              "heartbeat_s": 0.2}
    values.update(overrides)
    return Settings(**values)


def send(client, body=None, key=DEVICE_KEY, raw=None, content_type="application/json"):
    headers = {"Content-Type": content_type}
    if key is not None:
        headers["X-Device-Key"] = key
    return client.post("/events", content=raw if raw is not None else json.dumps(body or EVENT), headers=headers)


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_an_event_is_forwarded_with_both_keys_and_answered_with_what_was_stored(backend):
    client = TestClient(create_app(settings_for(backend.url)))

    response = send(client)

    assert response.status_code == 200
    assert response.json() == {"status": "stored", "readings": 2, "alerts": 0, "delivered": 0}
    sent = backend.requests[0]
    assert sent["path"] == "/api/internal/events"
    assert sent["headers"]["x-internal-key"] == INTERNAL_KEY
    assert sent["headers"]["x-device-key"] == DEVICE_KEY
    assert sent["json"] == EVENT


@pytest.mark.parametrize("body, expected", [
    ({**EVENT, "device_id": "x"}, "device_id"),
    ({**EVENT, "device_id": "rpi demo"}, "device_id"),
    ({**EVENT, "type": "irrigation"}, "'type'"),
    ({**EVENT, "data": {}}, "'data'"),
    ({**EVENT, "data": [1, 2]}, "'data'"),
    ({**EVENT, "data": {f"m{index}": index for index in range(33)}}, "como máximo"),
    ({**EVENT, "ts": True}, "'ts'"),
    ([EVENT], "objeto JSON"),
])
def test_malformed_events_are_refused_without_reaching_the_backend(backend, body, expected):
    response = send(TestClient(create_app(settings_for(backend.url))), body)

    assert response.status_code == 400
    assert expected in response.json()["message"]
    assert backend.requests == []


def test_what_is_not_an_event_is_refused(backend):
    client = TestClient(create_app(settings_for(backend.url)))

    assert send(client, key=None).status_code == 401
    assert send(client, key="  ").status_code == 401
    assert send(client, raw=b"{no es json").status_code == 400
    assert send(client, raw=b"device_id=1", content_type="application/x-www-form-urlencoded").status_code == 415
    too_big = send(client, raw=b" " * (MAX_EVENT_BYTES + 1))
    assert too_big.status_code == 413
    assert backend.requests == []


def test_the_devices_own_faults_come_back_as_they_are_so_it_does_not_retry(backend):
    client = TestClient(create_app(settings_for(backend.url)))
    for status, message in ((400, "El cultivo Fresa tiene 2 sensores activos de soil_moisture (FR-01, FR-02)"),
                            (401, "Dispositivo desconocido o llave incorrecta."),
                            (409, "El sensor está inactivo y no puede registrar lecturas.")):
        backend.answer(status, {"message": message})

        response = send(client)

        assert response.status_code == status
        assert response.json()["message"] == message


@pytest.mark.parametrize("status, body", [
    (500, {"message": "boom"}),
    (503, {}),
    (401, {"message": "Falta la llave interna o no es válida."}),
    (200, {"without": "owner"}),
])
def test_a_backend_that_cannot_store_the_event_is_a_502_so_the_device_retries(backend, status, body):
    backend.answer(status, body)

    response = send(TestClient(create_app(settings_for(backend.url))))

    assert response.status_code == 502
    assert "reintenta" in response.json()["message"]


def test_a_backend_that_is_down_or_too_slow_is_a_502(backend):
    down = TestClient(create_app(settings_for(f"http://127.0.0.1:{closed_port()}")))
    assert send(down).status_code == 502

    backend.answer(200, STORED, delay=1.0)
    slow = TestClient(create_app(settings_for(backend.url, backend_timeout_ms=200)))
    assert send(slow).status_code == 502


SPANISH_EVENT = {"deviceId": "RPI-DEMO-01", "timestamp": "2026-10-20T10:00:00Z",
                 "datos": {"temperatura": 24.5, "humedad_aire": 60, "humedad_suelo": 41}}


def test_the_spanish_form_reaches_the_backend_in_the_services_own_form(backend):
    response = send(TestClient(create_app(settings_for(backend.url))), SPANISH_EVENT)

    assert response.status_code == 200
    assert backend.requests[0]["json"] == {
        "device_id": "RPI-DEMO-01", "type": "reading", "ts": "2026-10-20T10:00:00Z",
        "data": {"air_temperature": 24.5, "relative_humidity": 60, "soil_moisture": 41}}


def test_the_spanish_form_without_a_time_is_stamped_by_the_backend_on_arrival(backend):
    body = {"deviceId": "RPI-DEMO-01", "datos": {"temperatura": 25, "humedad_suelo": 40}}

    assert send(TestClient(create_app(settings_for(backend.url))), body).status_code == 200
    assert backend.requests[0]["json"]["ts"] is None


@pytest.mark.parametrize("body, expected", [
    ({**SPANISH_EVENT, "datos": {"lluvia": 3}}, "temperatura, humedad_aire, humedad_suelo"),
    ({**SPANISH_EVENT, "datos": {}}, "'datos'"),
    ({**SPANISH_EVENT, "data": {"ph": 6}}, "un solo formato"),
    ({**SPANISH_EVENT, "deviceId": "x"}, "device_id"),
])
def test_a_malformed_spanish_event_is_refused_without_reaching_the_backend(backend, body, expected):
    response = send(TestClient(create_app(settings_for(backend.url))), body)

    assert response.status_code == 400
    assert expected in response.json()["message"]
    assert backend.requests == []


def test_the_webhook_path_and_the_x_api_key_header_are_the_same_as_events_and_x_device_key(backend):
    client = TestClient(create_app(settings_for(backend.url)))

    response = client.post("/webhook/eventos", json=SPANISH_EVENT, headers={"X-API-KEY": DEVICE_KEY})

    assert response.status_code == 200
    assert backend.requests[0]["headers"]["x-device-key"] == DEVICE_KEY
    assert "x-api-key" not in backend.requests[0]["headers"]
    same = client.post("/events", json=EVENT, headers={"X-API-KEY": DEVICE_KEY, "X-Device-Key": DEVICE_KEY})
    assert same.status_code == 200
    both = client.post("/events", json=EVENT, headers={"X-API-KEY": "one", "X-Device-Key": "other"})
    assert both.status_code == 400
    assert len(backend.requests) == 2


def test_a_backend_that_stored_nothing_is_tried_again_after_a_growing_wait(backend):
    backend.answer_in_turn((503, {}), (502, {}), (500, {"message": "boom"}))
    started = time.monotonic()

    response = send(TestClient(create_app(settings_for(backend.url, backend_retry_first_ms=100))))

    assert response.status_code == 200
    assert len(backend.requests) == 4
    assert time.monotonic() - started >= 0.1 + 0.2 + 0.4


def test_after_three_retries_the_device_gets_a_502_and_keeps_the_event(backend):
    backend.answer(503, {})

    response = send(TestClient(create_app(settings_for(backend.url))))

    assert response.status_code == 502
    assert len(backend.requests) == 4


def test_retries_follow_the_environment(backend):
    backend.answer(503, {})

    send(TestClient(create_app(settings_for(backend.url, backend_retries=0))))

    assert len(backend.requests) == 1


@pytest.mark.parametrize("status, body", [
    (400, {"message": "Medida desconocida"}),
    (401, {"message": "Dispositivo desconocido o llave incorrecta."}),
    (401, {"message": "Falta la llave interna o no es válida."}),
    (409, {"message": "El sensor está inactivo."}),
])
def test_an_answer_that_a_retry_would_not_change_is_not_retried(backend, status, body):
    backend.answer(status, body)

    send(TestClient(create_app(settings_for(backend.url))))

    assert len(backend.requests) == 1


def test_a_backend_that_times_out_while_answering_is_not_retried_because_it_may_have_stored_it(backend):
    backend.answer(200, STORED, delay=0.5)

    response = send(TestClient(create_app(settings_for(backend.url, backend_timeout_ms=200))))

    assert response.status_code == 502
    assert len(backend.requests) == 1


def test_each_event_and_each_failure_is_logged_without_the_keys(backend, caplog):
    client = TestClient(create_app(settings_for(backend.url)))
    with caplog.at_level(logging.INFO, logger="agrosense_events"):
        send(client, SPANISH_EVENT)
        backend.answer(401, {"message": "Dispositivo desconocido o llave incorrecta."})
        send(client)
        backend.answer(503, {})
        send(client)

    written = "\n".join(record.getMessage() for record in caplog.records)
    assert "Event received from RPI-DEMO-01: reading with 3 value(s)" in written
    assert "Event from RPI-DEMO-01 stored: 2 reading(s), 0 alert(s), delivered to 0 stream(s)" in written
    assert "Event refused: 401 Dispositivo desconocido o llave incorrecta." in written
    assert "Event from RPI-DEMO-01: the backend answered 503, attempt 1 of 4" in written
    assert "Event from RPI-DEMO-01: the backend answered 503" in written.splitlines()
    for secret in (DEVICE_KEY, INTERNAL_KEY):
        assert secret not in written


def test_without_its_internal_key_the_service_does_not_forward_anything(backend):
    response = send(TestClient(create_app(settings_for(backend.url, internal_key=""))))

    assert response.status_code == 502
    assert backend.requests == []


def test_a_token_from_the_backend_names_the_user():
    email, expires = tokens.verify(tokens.sign("Demo@AgroSense.co", STREAM_SECRET), STREAM_SECRET)

    assert email == OWNER
    assert expires > time.time()


@pytest.mark.parametrize("token", [
    "",
    "no.es.token",
    tokens.sign(OWNER, "another-secret-of-at-least-32-bytes!!"),
    tokens.sign(OWNER, STREAM_SECRET, lifetime_s=300, now=time.time() - 3600),
    tokens.sign(OWNER, STREAM_SECRET) + "x",
])
def test_tokens_not_signed_for_this_service_or_expired_are_refused(token):
    with pytest.raises(tokens.InvalidToken):
        tokens.verify(token, STREAM_SECRET)


def test_a_token_that_names_no_algorithm_or_another_audience_is_refused():
    header = tokens._encode(json.dumps({"alg": "none"}).encode())
    payload = tokens._encode(json.dumps({"sub": OWNER, "aud": "agrosense-events", "exp": time.time() + 60}).encode())
    with pytest.raises(tokens.InvalidToken):
        tokens.verify(f"{header}.{payload}.", STREAM_SECRET)

    good = tokens.sign(OWNER, STREAM_SECRET).split(".")
    other = tokens._encode(json.dumps({"sub": OWNER, "aud": "other", "exp": time.time() + 60}).encode())
    with pytest.raises(tokens.InvalidToken):
        tokens.verify(f"{good[0]}.{other}.{good[2]}", STREAM_SECRET)


def test_the_stream_refuses_a_bad_token_and_a_service_without_secret(backend):
    client = TestClient(create_app(settings_for(backend.url)))
    assert client.get("/stream").status_code == 401
    assert client.get("/stream", params={"token": "x"}).status_code == 401

    closed = TestClient(create_app(settings_for(backend.url, stream_secret="")))
    assert closed.get("/stream", params={"token": tokens.sign(OWNER, STREAM_SECRET)}).status_code == 401


class Listener:
    def __init__(self, url: str, token: str):
        self.events: list[tuple[str, dict]] = []
        self.status = None
        self.ended = threading.Event()
        self.ready = threading.Event()
        self._client = httpx.Client(timeout=10)
        threading.Thread(target=self._read, args=(url, token), daemon=True).start()

    def _read(self, url, token):
        kind = None
        try:
            with self._client.stream("GET", f"{url}/stream", params={"token": token}) as response:
                self.status = response.status_code
                if response.status_code != 200:
                    return
                for line in response.iter_lines():
                    if line.startswith("event: "):
                        kind = line[7:]
                    elif line.startswith("data: "):
                        self.events.append((kind, json.loads(line[6:])))
                        if kind == "ready":
                            self.ready.set()
        except httpx.HTTPError:
            pass
        finally:
            self.ready.set()
            self.ended.set()

    def kinds(self):
        return [kind for kind, _ in self.events]

    def close(self):
        self._client.close()


@pytest.fixture
def live(backend):
    server = ServiceServer(create_app(settings_for(backend.url, max_streams_per_user=2))).start()
    yield server
    server.stop()


def test_an_event_reaches_the_owners_streams_and_nobody_else(live):
    mine = [Listener(live.url, tokens.sign(OWNER, STREAM_SECRET)) for _ in range(2)]
    other = Listener(live.url, tokens.sign("otra@agrosense.co", STREAM_SECRET))
    try:
        for listener in (*mine, other):
            assert listener.ready.wait(5)

        response = httpx.post(f"{live.url}/events", json=EVENT, headers={"X-Device-Key": DEVICE_KEY}, timeout=5)

        assert response.json()["delivered"] == 2
        deadline = time.time() + 5
        while time.time() < deadline and not all("reading" in listener.kinds() for listener in mine):
            time.sleep(0.05)
        for listener in mine:
            kind, event = next(item for item in listener.events if item[0] == "reading")
            assert event == STORED["event"]
        time.sleep(0.3)
        assert "reading" not in other.kinds()
    finally:
        for listener in (*mine, other):
            listener.close()


def test_a_stream_ends_when_its_token_does_and_says_so(live):
    listener = Listener(live.url, tokens.sign(OWNER, STREAM_SECRET, lifetime_s=1))
    try:
        assert listener.ended.wait(5)
        assert listener.kinds()[0] == "ready"
        assert listener.kinds()[-1] == "expired"
    finally:
        listener.close()


def test_each_user_may_only_keep_a_few_streams_open(live):
    open_ones = [Listener(live.url, tokens.sign(OWNER, STREAM_SECRET)) for _ in range(2)]
    try:
        for listener in open_ones:
            assert listener.ready.wait(5)
        third = Listener(live.url, tokens.sign(OWNER, STREAM_SECRET))
        assert third.ended.wait(5)
        assert third.status == 429
    finally:
        for listener in open_ones:
            listener.close()


def test_the_stream_token_never_reaches_the_log(live, caplog):
    token = tokens.sign(OWNER, STREAM_SECRET, lifetime_s=1)
    with caplog.at_level(logging.INFO):
        listener = Listener(live.url, token)
        assert listener.ended.wait(5)
        httpx.get(f"{live.url}/stream", params={"token": "guessed"}, timeout=5)
        httpx.post(f"{live.url}/events", json=EVENT, headers={"X-Device-Key": DEVICE_KEY}, timeout=5)
        listener.close()

    written = "\n".join(record.getMessage() for record in caplog.records
                        if record.name.startswith(("agrosense_events", "uvicorn")))
    assert "GET /stream 200" in written and "GET /stream 401" in written
    for secret in (token, "guessed", DEVICE_KEY, INTERNAL_KEY, STREAM_SECRET):
        assert secret not in written


def test_health_is_open_and_only_the_frontend_may_read_the_stream_from_a_browser(backend):
    client = TestClient(create_app(settings_for(backend.url)))

    allowed = client.get("/health", headers={"Origin": "http://localhost:8080"})
    other = client.get("/health", headers={"Origin": "https://otro.example"})

    assert allowed.json() == {"status": "UP"}
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:8080"
    assert "access-control-allow-origin" not in other.headers


def test_settings_come_from_the_environment():
    settings = from_environment({"BACKEND_URL": "http://backend:9000/", "EVENTS_ALLOWED_ORIGINS": "http://a, http://b/",
                                 "EVENTS_INTERNAL_KEY": " k ", "BACKEND_TIMEOUT_MS": "60000",
                                 "BACKEND_RETRIES": "1", "BACKEND_RETRY_FIRST_MS": "250"})

    assert settings.backend_url == "http://backend:9000"
    assert settings.allowed_origins == ("http://a", "http://b")
    assert settings.internal_key == "k"
    assert settings.backend_timeout_ms == 60000
    assert (settings.backend_retries, settings.backend_retry_first_ms) == (1, 250)
    defaults = from_environment({})
    assert defaults.heartbeat_s == 15.0
    assert (defaults.backend_retries, defaults.backend_retry_first_ms) == (3, 500)
