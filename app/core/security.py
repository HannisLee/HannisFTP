from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, WebSocket


class LocalWebSecurity:
    """Same-origin and anti-CSRF checks for this local-only desktop app."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token or secrets.token_urlsafe(32)

    def check_host(self, request: Request) -> None:
        host = (request.headers.get("host") or "").lower()
        if not host:
            raise HTTPException(status_code=421, detail="Missing Host header")
        hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0].lstrip("[")
        if hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise HTTPException(status_code=421, detail="Unexpected Host header")

    def check_origin(self, request: Request) -> None:
        origin = request.headers.get("origin")
        if not origin:
            return
        expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if secrets.compare_digest(origin, expected):
            return
        raise HTTPException(status_code=403, detail="Cross-origin requests are not allowed")

    def check_token(self, request: Request, token: str | None = None) -> None:
        supplied = token or request.headers.get("X-MiniSFTP-Token")
        if not supplied or not secrets.compare_digest(supplied, self.token):
            raise HTTPException(status_code=401, detail="Invalid local session token")

    def check_http(self, request: Request, *, modification: bool) -> None:
        self.check_host(request)
        self.check_origin(request)
        if modification:
            self.check_token(request)

    async def check_websocket(self, websocket: WebSocket) -> None:
        host = (websocket.headers.get("host") or "").lower()
        hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0].lstrip("[")
        if hostname not in {"127.0.0.1", "localhost", "::1"}:
            await websocket.close(code=421)
            return False
        origin = websocket.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                await websocket.close(code=403)
                return False
        supplied = websocket.query_params.get("token")
        if not supplied or not secrets.compare_digest(supplied, self.token):
            await websocket.close(code=401)
            return False
        return True
