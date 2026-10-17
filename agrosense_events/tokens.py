import base64
import hashlib
import hmac
import json
import time

AUDIENCE = "agrosense-events"
LEEWAY_S = 30


class InvalidToken(Exception):
    pass


def _decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def verify(token: str, secret: str, now: float | None = None) -> tuple[str, float]:
    if not secret or not token:
        raise InvalidToken()
    parts = token.split(".")
    if len(parts) != 3:
        raise InvalidToken()
    try:
        header = json.loads(_decode(parts[0]))
        payload = json.loads(_decode(parts[1]))
        signature = _decode(parts[2])
    except (ValueError, UnicodeDecodeError):
        raise InvalidToken() from None
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise InvalidToken()
    expected = hmac.new(secret.encode("utf-8"), f"{parts[0]}.{parts[1]}".encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise InvalidToken()
    if not isinstance(payload, dict):
        raise InvalidToken()
    audience = payload.get("aud")
    if AUDIENCE not in (audience if isinstance(audience, list) else [audience]):
        raise InvalidToken()
    expires = payload.get("exp")
    subject = payload.get("sub")
    current = time.time() if now is None else now
    if not isinstance(expires, (int, float)) or expires + LEEWAY_S < current:
        raise InvalidToken()
    if not isinstance(subject, str) or not subject.strip():
        raise InvalidToken()
    return subject.strip().lower(), float(expires)


def sign(email: str, secret: str, lifetime_s: int = 300, now: float | None = None) -> str:
    issued = int(time.time() if now is None else now)
    header = _encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _encode(json.dumps({"sub": email, "aud": [AUDIENCE], "iat": issued,
                                  "exp": issued + lifetime_s}).encode())
    signature = hmac.new(secret.encode("utf-8"), f"{header}.{payload}".encode("ascii"), hashlib.sha256).digest()
    return f"{header}.{payload}.{_encode(signature)}"
