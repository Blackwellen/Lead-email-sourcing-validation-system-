"""FastAPI application factory."""
from __future__ import annotations

import importlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import metrics
from .bootstrap import ensure_initial_admin
from .config import Settings, ensure_secret_key, get_settings
from .db import dispose_engine, sessionmaker
from .logging_setup import mask_pii, setup_logging
from .middleware import (
    BodySizeLimitMiddleware,
    MaintenanceMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
)
from .security import SESSION_COOKIE, ApiError, SlidingWindowLimiter, resolve_session

log = logging.getLogger("api")

ROUTER_MODULES = (
    "auth", "dashboard", "runs", "presets", "categories", "geo", "sources", "businesses", "contacts",
    "verification", "exports", "system", "settings",
    "source_explorer", "discovery", "network", "signals", "data_quality", "organisations",
)

_HTTP_CODES = {
    400: "bad_request", 401: "not_authenticated", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
    409: "conflict", 413: "payload_too_large", 415: "unsupported_media_type", 422: "validation_error", 429: "rate_limited",
    501: "not_implemented", 503: "unavailable",
}


class LivenessOut(BaseModel):
    status: str


class WorkerSummary(BaseModel):
    alive: int
    stale: int
    last_seen_seconds_ago: float | None = None


class ReadyOut(BaseModel):
    ready: bool
    database: bool | None = None
    migration: dict[str, Any] | None = None
    workers: WorkerSummary | None = None
    maintenance: bool | None = None


def _err(status: int, code: str, message: str, detail: Any = None, headers: dict[str, str] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message}
    if detail is not None:
        body["detail"] = detail
    return JSONResponse({"error": body}, status_code=status, headers=headers)


def _alembic_head() -> str | None:
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        ini = Path(__file__).resolve().parent.parent / "alembic.ini"
        cfg = Config(str(ini))
        cfg.set_main_option("script_location", str(ini.parent / "alembic"))
        return ScriptDirectory.from_config(cfg).get_current_head()
    except Exception:
        return None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = ensure_secret_key(settings or get_settings())  # production + default/short key -> RuntimeError
    production = settings.env == "production"
    if production and not settings.cookie_secure:
        raise RuntimeError("LEAD_COOKIE_SECURE must be true in production (session cookies must never travel over plain HTTP)")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging("DEBUG" if settings.env == "development" else "INFO")
        try:
            async with sessionmaker()() as s:
                await ensure_initial_admin(s, settings)
        except Exception:
            log.exception("startup bootstrap failed (is the database migrated?)")
        yield
        await dispose_engine()

    app = FastAPI(
        title="Lead Intelligence API",
        version="1.0.0",
        description="REST API for the lead-intelligence platform (auth, runs, businesses, contacts, verification, exports).",
        lifespan=lifespan,
        # interactive docs and the schema are not served in production (scripts/dump_openapi.py builds it offline)
        docs_url=None if production else "/api/docs",
        redoc_url=None if production else "/api/redoc",
        openapi_url=None if production else "/api/openapi.json",
        generate_unique_id_function=lambda route: route.name,
    )
    app.state.settings = settings
    app.state.login_limiter = SlidingWindowLimiter(settings.login_rate_limit_per_minute)
    # targeted guessing against one account from one address is throttled harder than the per-address limit
    app.state.login_account_limiter = SlidingWindowLimiter(max(3, settings.login_rate_limit_per_minute // 2))
    app.state.api_limiter = SlidingWindowLimiter(settings.rate_limit_per_minute)

    # ---- middleware (last added = outermost)
    app.add_middleware(MaintenanceMiddleware)
    app.add_middleware(RateLimitMiddleware, limiter=app.state.api_limiter)
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Content-Disposition"],
        max_age=600,
    )
    app.add_middleware(BodySizeLimitMiddleware, json_limit=settings.max_json_body_kb * 1024,
                       upload_limit=settings.max_upload_mb * 1024 * 1024 + 1024 * 1024)  # + multipart framing
    app.add_middleware(RequestContextMiddleware, settings=settings)

    # ---- error handlers
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return _err(exc.status_code, exc.code, exc.message, exc.detail, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        code = _HTTP_CODES.get(exc.status_code, "error")
        msg = exc.detail if isinstance(exc.detail, str) else "Request failed."
        if exc.status_code == 404 and msg == "Not Found":
            msg = "The requested resource was not found."
        elif exc.status_code == 405:
            msg = "That HTTP method is not allowed for this endpoint."
        return _err(exc.status_code, code, msg, None, dict(exc.headers) if exc.headers else None)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        items = []
        for e in exc.errors():
            loc = [str(p) for p in e.get("loc", ()) if p not in ("body",)]
            items.append({"field": ".".join(loc), "message": mask_pii(str(e.get("msg", "invalid value")))})
        first = items[0] if items else {"field": "", "message": "invalid request"}
        where = f" ({first['field']})" if first["field"] else ""
        return _err(422, "validation_error", f"The request is invalid{where}: {first['message']}", items)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        rid = request.scope.get("state", {}).get("request_id", "-")
        log.error("unhandled error rid=%s path=%s", rid, request.url.path, exc_info=exc)
        return _err(500, "internal_error", f"Something went wrong on our side. Quote request id {rid} if you contact support.")

    # ---- ops endpoints
    @app.get("/healthz", response_model=LivenessOut, tags=["ops"])
    async def healthz() -> LivenessOut:
        return LivenessOut(status="ok")

    @app.get("/readyz", response_model=ReadyOut, tags=["ops"])
    async def readyz(request: Request, response: Response) -> ReadyOut:
        """Readiness. In production an anonymous caller only learns ``ready``; details need a signed-in session."""
        db_ok, current = False, None
        alive = stale = 0
        last_seen = None
        maint = False
        try:
            async with sessionmaker()() as s:
                await s.execute(text("SELECT 1"))
                db_ok = True
                has = (await s.execute(text("SELECT to_regclass('alembic_version') IS NOT NULL"))).scalar()
                if has:
                    current = (await s.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))).scalar()
                hb = (await s.execute(text(
                    "SELECT count(*) FILTER (WHERE last_seen_at > now() - make_interval(secs => :t)), "
                    "count(*) FILTER (WHERE last_seen_at <= now() - make_interval(secs => :t)), "
                    "extract(epoch FROM now() - max(last_seen_at)) FROM worker_heartbeats"), {"t": settings.worker_heartbeat_seconds * 4})).one()
                alive, stale, last_seen = int(hb[0] or 0), int(hb[1] or 0), (float(hb[2]) if hb[2] is not None else None)
                from .settings_store import maintenance_state

                maint = (await maintenance_state(s))["enabled"]
        except Exception as e:
            log.warning("readiness check failed: %s", type(e).__name__)
        head = _alembic_head()
        mig_ok = (current == head) if head else True
        if current is None and settings.env != "production":
            mig_ok = True  # dev/test databases may be created straight from the models
        ready = db_ok and mig_ok
        if not ready:
            response.status_code = 503
        if production:
            authed = False
            token = request.cookies.get(SESSION_COOKIE)
            if token and db_ok:
                try:
                    async with sessionmaker()() as s:
                        authed = await resolve_session(s, token) is not None
                except Exception:
                    authed = False
            if not authed:
                return ReadyOut(ready=ready)
        return ReadyOut(
            ready=ready, database=db_ok, migration={"current": current, "head": head, "ok": mig_ok},
            workers=WorkerSummary(alive=alive, stale=stale, last_seen_seconds_ago=last_seen), maintenance=maint,
        )

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        try:
            async with sessionmaker()() as s:
                rows = (await s.execute(text("SELECT stage, count(*) FROM sourcing_jobs WHERE state='queued' GROUP BY stage"))).all()
            for stage, n in rows:
                metrics.QUEUE_DEPTH.labels(stage).set(n)
        except Exception:
            pass
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    for name in ROUTER_MODULES:
        mod = importlib.import_module(f"app.api.{name}")
        app.include_router(mod.router)
        for extra in getattr(mod, "extra_routers", ()):
            app.include_router(extra)
    return app


def get_app() -> FastAPI:  # for `uvicorn app.main:get_app --factory`
    return create_app()


__all__ = ["create_app", "get_app", "HTTPException"]
