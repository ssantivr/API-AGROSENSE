# API-AGROSENSE

Contrato de la API REST de AgroSense. El código que la implementa está en
[BACKEND-AGROSENSE](https://github.com/WilliamChavez-Grupo2b/BACKEND-AGROSENSE) (Spring Boot); este
repositorio describe qué ofrece esa API, para quien la consume, y trae una herramienta que comprueba
que un backend en marcha la cumple.

| Archivo | Contenido |
|---|---|
| [`openapi.json`](openapi.json) | Especificación OpenAPI 3.0 de todas las rutas, con sus datos de entrada, respuestas y errores |
| [`requests.http`](requests.http) | Peticiones de ejemplo listas para ejecutar (REST Client de VS Code o IntelliJ) |
| [`tools/check_contract.py`](tools/check_contract.py) | Comprueba un backend en marcha contra la especificación |
| [`tools/validate_spec.py`](tools/validate_spec.py) | Comprueba que la especificación sea coherente consigo misma, sin necesidad de un backend |
| [`event-service/`](event-service/README.md) | El manejador de eventos (Python, FastAPI): el webhook que llaman las Raspberry Pi (`POST /events` o `/webhook/eventos`), que reenvía cada evento al backend y lo empuja en vivo al navegador del dueño (SSE). Es un servicio propio, con su `Dockerfile` y sus pruebas |

Para leer la especificación con formato, ábrela en <https://editor.swagger.io> (File → Import file).

## Dónde está

| Entorno | Dirección |
|---|---|
| Producción (Render) | `https://backend-agrosense-u5mz.onrender.com` |
| Local | `http://localhost:9000` |

El plan gratuito de Render duerme el servicio tras unos 15 minutos sin uso: la primera petición puede
tardar más de un minuto. `GET /api/health` responde `{"status":"UP"}` cuando está listo.

## Autenticación

1. `POST /api/auth/login` con `{email, password}` devuelve `{token, name, role, idUser}`.
2. Las demás rutas llevan la cabecera `Authorization: Bearer <token>`.

El token es un JWT firmado con HS512 que vale 60 minutos. El usuario se vuelve a leer de la base de
datos en cada petición, así que una cuenta desactivada deja de funcionar de inmediato aunque su token no
haya vencido. Sin token, o con uno no válido, la respuesta es 401.

Roles: `ADMIN` y `FARMER` pueden hacer todo; `TECHNICIAN` puede consultar, registrar sensores y enviar
lecturas, pero recibe 403 al crear fincas o cultivos, registrar riegos, atender alertas o pedir
predicciones. Con cualquier rol, cada operación se limita a los datos del propio usuario: lo que es de
otra cuenta se responde como 404, igual que si no existiera.

El navegador solo puede llamar a la API desde los orígenes de `CORS_ALLOWED_ORIGINS` del backend (en
producción, `https://agrosense-frontend-iota.vercel.app`).

## Rutas

| Método y ruta | Uso | Acceso |
|---|---|---|
| `GET /` | Qué servicio es (lo que se ve al abrir la dirección en un navegador) | Pública |
| `GET /api/health` | Estado del servicio | Pública |
| `POST /api/auth/login` | Iniciar sesión | Pública |
| `POST /api/auth/register` | Crear una cuenta (rol `farmer`) | Pública |
| `GET /api/auth/check-user?email=` | Saber si un correo ya tiene cuenta | Pública |
| `GET /api/fincas` | Fincas del usuario | Cualquier rol |
| `POST /api/fincas` | Registrar una finca | `ADMIN`, `FARMER` |
| `GET /api/cultivos?estateId=` | Cultivos activos | Cualquier rol |
| `POST /api/cultivos` | Registrar un cultivo | `ADMIN`, `FARMER` |
| `GET /api/crops/{cropId}/sensors` | Sensores de un cultivo | Cualquier rol |
| `POST /api/sensors` | Registrar un sensor | Cualquier rol |
| `POST /api/readings` | Registrar una lectura | Cualquier rol |
| `GET /api/sensors/{sensorId}/readings?limit=` | Últimas lecturas de un sensor | Cualquier rol |
| `GET /api/alerts?limit=` | Alertas sin atender | Cualquier rol |
| `GET /api/crops/{cropId}/alerts?limit=` | Alertas sin atender de un cultivo | Cualquier rol |
| `PATCH /api/alerts/{alertId}/acknowledge` | Marcar una alerta como atendida | `ADMIN`, `FARMER` |
| `GET /api/riego/cultivo/{cropId}?limit=` | Historial de riegos | Cualquier rol |
| `POST /api/riego` | Registrar un riego manual | `ADMIN`, `FARMER` |
| `GET /api/dashboard` | Resumen general | Cualquier rol |
| `GET /api/dashboard/{cropId}` | Resumen de un cultivo | Cualquier rol |
| `GET /api/crops/{cropId}/predictions?limit=` | Predicciones de riego guardadas | Cualquier rol |
| `POST /api/crops/{cropId}/predictions` | Pedir una predicción de riego | `ADMIN`, `FARMER` |
| `POST /api/ai/chat` | Pregunta escrita al asistente de IA | Cualquier rol |
| `POST /api/ai/voice` | Pregunta hablada al asistente de IA | Cualquier rol |

`/api/auth/email-available` es el nombre anterior de `check-user` y sigue respondiendo.

El asistente (`/api/ai/*`) responde con un modelo de lenguaje: Gemini o un proveedor compatible con la
API de OpenAI, según la llave que tenga el backend. Si el modelo no está configurado o no contesta,
las preguntas escritas se responden igualmente con las lecturas de cada cultivo del usuario, y la
respuesta lo dice con `"source": "local"` en lugar de `"model"`. Las predicciones de riego las calcula
[IA-AGROSENSE](https://github.com/ssantivr/IA-AGROSENSE); el backend solo las pide si tiene
`AI_SERVICE_URL`.

Los parámetros `limit` no se rechazan cuando se pasan de su máximo: se ajustan a él.

## Errores

Todas las respuestas de error tienen la misma forma, con el mensaje escrito para el usuario final:

```json
{ "message": "El correo no es válido.", "fields": { "email": "El correo no es válido." } }
```

`fields` solo aparece en los errores de validación (400) y trae un mensaje por cada campo rechazado.

| Código | Significado |
|---|---|
| 400 | Datos no válidos, o falta un parámetro obligatorio |
| 401 | Sin token, o token no válido, vencido o de una cuenta desactivada; en el login, credenciales incorrectas |
| 403 | El rol no permite la operación |
| 404 | No existe, o pertenece a otro usuario |
| 409 | Choca con el estado actual: correo o código de sensor ya registrados, sensor inactivo |
| 429 | Demasiados intentos de inicio de sesión (cabecera `Retry-After`) o de consultas al asistente |
| 5xx | Fallo del servidor; en el asistente, 502, 503 y 504 indican qué pasó con el servicio de IA |

## Tiempo real

Además de REST, el backend publica las lecturas y las alertas nuevas por WebSocket (STOMP) en `/ws`. El
cliente envía el mismo token en la cabecera `Authorization` de la trama `CONNECT` y solo puede
suscribirse a sus colas privadas (`/user/queue/...`); no puede publicar.

## Comprobar un backend contra el contrato

```bash
API_BASE_URL=http://localhost:9000 API_EMAIL=demo@agrosense.co API_PASSWORD=... python tools/check_contract.py
```

Necesita Python 3.10 o posterior y nada más. Sin `API_EMAIL` y `API_PASSWORD` solo comprueba las rutas
públicas y que las protegidas respondan 401. Si el backend está dormido espera hasta 4 minutos a que
`/api/health` responda (`API_WAKE_S` cambia ese tiempo).

No modifica datos: a las rutas que crean o cambian algo les envía datos que el backend debe rechazar (un
cuerpo vacío o un identificador que no existe), lo que basta para saber que la ruta existe y responde
como dice el contrato. En cada respuesta comprueba que el código esté documentado para esa operación y
que el cuerpo de las respuestas correctas coincida con su esquema: campos obligatorios, tipos, valores
permitidos y campos que el contrato no conoce.

## Mantenerlo al día

Cuando cambie una ruta del backend, cambia `openapi.json` en el mismo trabajo y vuelve a ejecutar
`check_contract.py` contra el backend con el cambio.

Antes de subir un cambio de la especificación:

```bash
python tools/validate_spec.py   # referencias rotas, parámetros sin declarar, rutas protegidas sin su 401...
python -m unittest              # las pruebas de las dos herramientas
```

GitHub ejecuta las dos cosas en cada push (`.github/workflows/tests.yml`), y también las pruebas del
manejador de eventos (`cd event-service && pip install -r requirements-dev.txt && python -m pytest`).
