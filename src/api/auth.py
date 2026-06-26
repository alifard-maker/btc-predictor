from __future__ import annotations

import os
import secrets
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

PUBLIC_PATHS = frozenset({
  "/health",
  "/login",
  "/api/auth/login",
  "/docs",
  "/openapi.json",
  "/redoc",
})

# Read-only dashboard APIs allowed with X-API-Key (same as /api/admin/*).
ADMIN_KEY_PATHS = frozenset({
  "/api/predictions",
  "/api/prediction/latest",
  "/api/calibration",
  "/api/postmortems",
  "/api/slot/monitor",
})


def app_password(cfg: dict[str, Any]) -> str:
  return str(cfg.get("app_password") or "")


def auth_enabled(cfg: dict[str, Any]) -> bool:
  return bool(app_password(cfg))


def session_secret(cfg: dict[str, Any]) -> str:
  return os.getenv("SESSION_SECRET") or app_password(cfg) or secrets.token_hex(32)


def is_authed(request: Request) -> bool:
  return bool(request.session.get("authed"))


def require_session(request: Request, cfg: dict[str, Any]) -> None:
  if not auth_enabled(cfg):
    return
  if not is_authed(request):
    raise HTTPException(401, "Not authenticated")


async def auth_middleware(request: Request, call_next, cfg: dict[str, Any]):
  if not auth_enabled(cfg):
    return await call_next(request)

  path = request.url.path
  if path in PUBLIC_PATHS:
    return await call_next(request)

  admin_key = str(cfg.get("admin_api_key") or os.getenv("ADMIN_API_KEY") or "")
  if admin_key and request.headers.get("x-api-key") == admin_key:
    if path.startswith("/api/admin/") or path in ADMIN_KEY_PATHS:
      return await call_next(request)

  if is_authed(request):
    return await call_next(request)

  if path.startswith("/api/"):
    return JSONResponse({"detail": "Not authenticated"}, status_code=401)

  return RedirectResponse(url="/login", status_code=303)


def add_session_middleware(app, cfg: dict[str, Any]) -> None:
  app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret(cfg),
    same_site="lax",
    https_only=False,
  )
