"""Checks a running AgroSense backend against openapi.json. Standard library only.

    python tools/check_contract.py

    API_BASE_URL   where the backend is (default http://localhost:9000)
    API_EMAIL      an account to sign in with; without it only the public routes are checked
    API_PASSWORD   its password
    API_WAKE_S     how long to wait for a sleeping backend to answer /api/health (default 240)

It changes nothing: routes that create or modify data are called with data the backend must refuse
(an empty body, or an identifier that does not exist), which still proves the route is there and answers
as documented. Every answer must carry a status the contract lists for that operation, and every 2xx body
must match its schema.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SPEC = json.loads((Path(__file__).resolve().parent.parent / "openapi.json").read_text(encoding="utf-8"))
BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:9000").rstrip("/")
EMAIL = os.environ.get("API_EMAIL", "")
PASSWORD = os.environ.get("API_PASSWORD", "")
TIMEOUT_S = float(os.environ.get("API_TIMEOUT_S", "30"))
WAKE_S = float(os.environ.get("API_WAKE_S", "240"))

# What the backend answers when no route matches, as opposed to "that record does not exist".
NO_ROUTE_MESSAGE = "La solicitud no es válida."
MISSING_ID = 999_999_999
METHODS = ("get", "post", "put", "patch", "delete")

failures = 0


def report(ok: bool, name: str, detail: str = "") -> None:
    global failures
    failures += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} {name}{' - ' + detail if detail else ''}")


def resolve(node: dict) -> dict:
    while "$ref" in node:
        target = SPEC
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target[part]
        node = target
    return node


def mismatches(value: object, schema: dict, where: str = "body") -> list[str]:
    """Why the value does not fit the schema; empty when it does."""
    schema = resolve(schema)
    if value is None:
        return [] if schema.get("nullable") else [f"{where} is null"]
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return [f"{where} is not an object"]
        problems = [f"{where}.{name} is missing" for name in schema.get("required", []) if name not in value]
        for name, child in schema.get("properties", {}).items():
            if name in value and not (value[name] is None and name not in schema.get("required", [])):
                problems += mismatches(value[name], child, f"{where}.{name}")
        problems += [f"{where}.{name} is not in the contract" for name in value
                     if name not in schema.get("properties", {}) and "additionalProperties" not in schema]
        return problems
    if kind == "array":
        if not isinstance(value, list):
            return [f"{where} is not a list"]
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return [f"{where} has more than {schema['maxItems']} items"]
        return [problem for index, item in enumerate(value[:20])
                for problem in mismatches(item, schema["items"], f"{where}[{index}]")]
    expected = {"string": str, "boolean": bool, "integer": int, "number": (int, float)}.get(kind)
    if expected and (not isinstance(value, expected) or (kind != "boolean" and isinstance(value, bool))):
        return [f"{where} is not a {kind}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{where} = {value!r} is not one of {schema['enum']}"]
    return []


def call(method: str, path: str, token: str | None = None, body: object = None, form: bool = False):
    """Returns (status, decoded JSON body or None); status 0 means the backend gave no answer."""
    headers = {"Accept": "application/json"}
    data = None
    if form:
        boundary = "agrosense-contract-check"
        data = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"check\"\r\n\r\n1\r\n"
                f"--{boundary}--\r\n").encode("utf-8")
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    except OSError:
        # Refused, unreachable or timed out (urllib's own errors are OSErrors too).
        return 0, None
    try:
        return status, json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        return status, None


def check(operation: dict, name: str, status: int, body: object, expected: tuple[int, ...]) -> None:
    documented = operation["responses"]
    if status == 0:
        report(False, name, "no answer from the backend")
        return
    if str(status) not in documented:
        report(False, name, f"answered {status}, which the contract does not list")
        return
    if status == 404 and isinstance(body, dict) and body.get("message") == NO_ROUTE_MESSAGE:
        report(False, name, "the backend has no such route")
        return
    if status not in expected:
        report(False, name, f"answered {status}, expected {' or '.join(map(str, expected))}")
        return
    content = resolve(documented[str(status)]).get("content", {}).get("application/json")
    problems = mismatches(body, content["schema"]) if content else []
    report(not problems, f"{name} -> {status}", "; ".join(problems[:4]))


def main() -> int:
    print(f"Contract: {SPEC['info']['title']} {SPEC['info']['version']}  Backend: {BASE_URL}")
    operations = [(path, method, item[method]) for path, item in SPEC["paths"].items()
                  for method in METHODS if method in item]

    # A backend on a free plan sleeps when idle and takes a minute or two to start answering.
    deadline = time.monotonic() + WAKE_S
    while call("get", "/api/health")[0] != 200:
        if time.monotonic() >= deadline:
            print(f"The backend did not answer /api/health within {WAKE_S:g} seconds.")
            return 1
        time.sleep(3)

    token = None
    if EMAIL and PASSWORD:
        status, body = call("post", "/api/auth/login", body={"email": EMAIL, "password": PASSWORD})
        check(SPEC["paths"]["/api/auth/login"]["post"], "POST /api/auth/login (sign in)", status, body, (200,))
        token = body.get("token") if status == 200 and isinstance(body, dict) else None
        if not token:
            print("Could not sign in: the protected routes are not checked.")
    else:
        print("API_EMAIL and API_PASSWORD are not set: only the public routes are checked.")

    ids = {"cropId": MISSING_ID, "sensorId": MISSING_ID, "alertId": MISSING_ID}
    if token:
        status, crops = call("get", "/api/cultivos", token)
        if status == 200 and crops:
            ids["cropId"] = crops[0]["idCrop"]
            status, sensors = call("get", f"/api/crops/{ids['cropId']}/sensors", token)
            if status == 200 and sensors:
                ids["sensorId"] = sensors[0]["idSensor"]

    for path, method, operation in operations:
        name = f"{method.upper()} {path}"
        public = operation.get("security") == []
        if not public:
            status, body = call(method, _fill(path, ids))
            check(operation, f"{name} (no token)", status, body, (401,))
            if not token:
                continue

        request_body = operation.get("requestBody", {}).get("content", {})
        query = {parameter["name"]: "contract-check@example.invalid" for parameter in operation.get("parameters", [])
                 if resolve(parameter)["in"] == "query" and resolve(parameter).get("required")}
        suffix = "?" + urllib.parse.urlencode(query) if query else ""
        if method == "get":
            target = _fill(path, ids)
            known = all(ids[key] != MISSING_ID for key in ids if "{" + key + "}" in path)
            status, body = call(method, target + suffix, token)
            check(operation, name, status, body, (200,) if known else (404,))
        elif "multipart/form-data" in request_body:
            status, body = call(method, _fill(path, ids), token, form=True)
            check(operation, f"{name} (form without audio)", status, body, (400,))
        elif request_body:
            status, body = call(method, _fill(path, ids), token, body={})
            check(operation, f"{name} (empty body)", status, body, (400,))
        else:
            status, body = call(method, _fill(path, {key: MISSING_ID for key in ids}), token)
            check(operation, f"{name} (unknown id)", status, body, (404,))

    print("CONTRACT HOLDS" if failures == 0 else f"{failures} CHECK(S) FAILED")
    return 0 if failures == 0 else 1


def _fill(path: str, ids: dict) -> str:
    for key, value in ids.items():
        path = path.replace("{" + key + "}", str(value))
    return path


if __name__ == "__main__":
    sys.exit(main())
