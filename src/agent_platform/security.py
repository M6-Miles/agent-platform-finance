"""Stateless signed access tokens implemented with the Python standard library."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any


class AuthenticationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    username: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_token(*, user_id: str, username: str, role: str, secret: str, ttl_s: int) -> tuple[str, int]:
    now = int(time.time())
    expires_at = now + max(60, int(ttl_s))
    encoded = _b64encode(json.dumps({
        "sub": user_id, "username": username, "role": role,
        "iat": now, "exp": expires_at,
    }, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64encode(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
    return f"v1.{encoded}.{signature}", expires_at


def verify_token(token: str, *, secret: str, now: int | None = None) -> Principal:
    try:
        version, encoded, signature = token.split(".", 2)
        if version != "v1":
            raise AuthenticationError("不支持的访问令牌版本")
        expected = _b64encode(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise AuthenticationError("访问令牌签名无效")
        payload: dict[str, Any] = json.loads(_b64decode(encoded))
        if int(payload["exp"]) <= int(time.time() if now is None else now):
            raise AuthenticationError("访问令牌已过期")
        role = str(payload.get("role", "user"))
        if role not in {"admin", "user"}:
            raise AuthenticationError("访问令牌角色无效")
        return Principal(str(payload["sub"]), str(payload["username"]), role)
    except AuthenticationError:
        raise
    except Exception as exc:
        raise AuthenticationError("访问令牌格式无效") from exc
