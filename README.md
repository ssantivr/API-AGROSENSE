# Servicio de eventos de AgroSense

Manejador de eventos (webhook) de AgroSense. Recibe por HTTP los eventos que envían las Raspberry Pi del
campo, los reenvía al backend para que los guarde y los empuja en tiempo real al navegador del dueño del
dispositivo.

```
Raspberry ──POST /events──▶ servicio de eventos ──POST /api/internal/events──▶ backend ──▶ BD
                                     │                                            │
                                     │◀───────────── {ownerEmail, event} ─────────┘
                                     └──GET /stream (SSE)──▶ navegador del dueño
```

Está escrito en Python 3.12 con FastAPI. **No accede a la base de datos**: el backend comprueba la llave del
dispositivo, guarda el evento y dice de quién es; este servicio solo lo entrega.

## Rutas

### `POST /events` (lo llama la Raspberry)

Cabecera `X-Device-Key` con la llave del dispositivo, que se obtiene al registrarlo
(`POST /api/devices` del backend) y solo se muestra esa vez. Cuerpo JSON de hasta 16 kB:

```json
{
  "device_id": "RPI-DEMO-01",
  "type": "reading",
  "ts": "2026-10-16T08:00:00-05:00",
  "data": {"soil_moisture": 31.5, "air_temperature": 24.1, "ph": 6.4}
}
```

| Campo | Obligatorio | Descripción |
|---|---|---|
| `device_id` | sí | Código del dispositivo: de 3 a 64 letras, números, `-` o `_` |
| `type` | sí | `reading` (lecturas) o `alert` (una alerta que el dispositivo detecta) |
| `ts` | no | Cuándo se midió: ISO-8601 o segundos desde 1970. Sin él, la hora de llegada |
| `data` | sí | `reading`: las medidas por tipo de sensor (`soil_moisture`, `air_temperature`, `soil_temperature`, `ph`, `conductivity`, `light`, `rain_gauge`, `relative_humidity`). Si el cultivo tiene varios sensores de ese tipo: `{"soil_moisture": {"value": 31.5, "sensor_code": "AS-001"}}`. `alert`: `{"alert_type": "PEST_DETECTED", "message": "...", "severity": "HIGH"}` |

| Respuesta | Qué hacer |
|---|---|
| 200 `{"status": "stored", "readings": 2, "alerts": 0, "delivered": 1}` | Guardado; `delivered` dice a cuántas pestañas abiertas llegó |
| 400, 401, 404, 409, 413, 415 con `{"message"}` | El evento o la llave están mal: **no reintentar**, corregir |
| 502 | El backend no respondió o no pudo guardarlo: **reintentar** más tarde |

### `GET /stream?token=...` (lo abre el navegador)

Server-Sent Events con los eventos del usuario del token, a medida que llegan. El token lo firma el backend
(`GET /api/events/token`, que el frontend expone en `/api/ui/events/token`) con `EVENTS_STREAM_SECRET`, dura 5
minutos e incluye el correo del usuario.

```
retry: 5000

event: ready
data: {"expiresAt": 1792206449}

event: reading
data: {"type": "reading", "deviceId": "RPI-DEMO-01", "cropId": 1, "cropName": "Café", "readings": [...], "alerts": [...]}

: ping

event: expired
data: {}
```

- Cada evento llega **solo** a las conexiones del dueño del dispositivo.
- Cada 15 s sin eventos se envía un comentario (`: ping`) para que los proxys no cierren la conexión.
- Cuando el token vence, el servicio envía `expired` y cierra; el navegador abre otra con un token nuevo.
- 401 si el token no es válido o venció; 429 si el usuario ya tiene 5 conexiones abiertas (500 en total).

### `GET /health`

`{"status": "UP"}`, sin autenticación.

## Seguridad

- Hacia el backend, cada evento lleva `X-Internal-Key` (`EVENTS_INTERNAL_KEY`) y la `X-Device-Key` de la
  Raspberry; el backend rechaza cualquier otra cosa. Sin `EVENTS_INTERNAL_KEY` el servicio no reenvía nada
  (502).
- El token del canal va en la dirección porque `EventSource` no permite cabeceras. Lo mitigan su vida de 5
  minutos y que **este servicio no registra la parte de la dirección después de `?`**: el registro de
  acceso de uvicorn está apagado y la aplicación anota solo método, ruta, código y duración. El alojamiento
  (Render) puede tener su propio registro de accesos.
- Las firmas se comprueban en tiempo constante; solo se acepta `HS256` con la audiencia `agrosense-events`.
- CORS solo para los orígenes de `EVENTS_ALLOWED_ORIGINS` (el frontend) y solo `GET`.
- Ninguna llave se escribe en el registro ni en archivos.

## Ejecutar

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt     # en Windows: .venv\Scripts\pip
.venv/bin/python -m agrosense_events          # escucha en http://localhost:8100
```

| Variable | Descripción |
|---|---|
| `PORT`, `HOST` | Puerto (8100) y dirección (`0.0.0.0`) |
| `BACKEND_URL` | Dirección del backend (`http://localhost:9000`) |
| `EVENTS_INTERNAL_KEY` | La misma que `EVENTS_INTERNAL_KEY` del backend |
| `EVENTS_STREAM_SECRET` | La misma que `EVENTS_STREAM_SECRET` del backend (al menos 32 bytes) |
| `EVENTS_ALLOWED_ORIGINS` | Orígenes del frontend, separados por comas |
| `BACKEND_TIMEOUT_MS` | Cuánto se espera al backend (30000). En Render gratuito el backend se duerme y la primera petición tarda más |
| `EVENTS_HEARTBEAT_S`, `EVENTS_MAX_STREAMS_PER_USER`, `EVENTS_MAX_STREAMS` | 15 s, 5 y 500 |
| `LOG_LEVEL` | `INFO` |

Las conexiones abiertas viven en la memoria del proceso: el servicio corre **en un solo proceso**. Con
varias instancias, un evento solo llegaría a las conexiones de la instancia que lo recibió.

Con Docker:

```bash
docker build -t agrosense-eventos .
docker run -p 8100:8100 -e BACKEND_URL=... -e EVENTS_INTERNAL_KEY=... -e EVENTS_STREAM_SECRET=... agrosense-eventos
```

## Desplegar en Render

`render.yaml` describe el servicio (plan gratuito, Docker, comprobación en `/health`). `EVENTS_INTERNAL_KEY` y
`EVENTS_STREAM_SECRET` se escriben a mano con los mismos valores que en el backend. En el frontend,
`EVENTS_PUBLIC_URL` debe apuntar a la dirección pública de este servicio.

## Pruebas

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Con el backend representado por un servidor HTTP local: validación del evento, reenvío con las dos llaves,
qué errores del backend se devuelven tal cual y cuáles son 502, backend caído o lento, tokens válidos,
vencidos, mal firmados, sin algoritmo o de otra audiencia, y, con el servicio corriendo de verdad, que un
evento llega a las dos pestañas del dueño y no a las de otro usuario, que el canal se cierra al vencer el
token, el límite de conexiones, CORS y que ni el token ni las llaves llegan al registro.
