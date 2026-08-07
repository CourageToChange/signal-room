from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import uuid4

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import AccessTokenVerifier, AuthenticationError, Identity, MutationLimiter
from .config import AppSettings
from .core import (
    CoreClient,
    CoreRequestError,
    CoreService,
    CoreTransport,
    CoreUnavailableError,
    InProcessCore,
)
from .models import (
    ActionResponse,
    AssetDetailResponse,
    BootstrapResponse,
    DiagnosticsResponse,
    IncidentPage,
    IncidentState,
    IncidentView,
    MaintenanceActionResponse,
    MaintenanceCreateRequest,
    MaintenanceWindowView,
    MetricsResponse,
    NoteRequest,
    StreamEventView,
    TimelinePage,
    utc_now,
)

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,80}$")
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def _request_id(request: Request) -> str:
    supplied = request.headers.get("X-Request-ID", "")
    return supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else str(uuid4())


def _problem(
    request: Request,
    status: int,
    title: str,
    detail: str,
    *,
    problem_type: str = "about:blank",
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", _request_id(request))
    return JSONResponse(
        {
            "type": problem_type,
            "title": title,
            "status": status,
            "detail": detail,
            "instance": request.url.path,
            "request_id": request_id,
        },
        status_code=status,
        media_type="application/problem+json",
        headers=headers,
    )


def _parse_version(value: str) -> int:
    normalized = value.strip().removeprefix("W/").strip('"')
    try:
        version = int(normalized)
    except ValueError as error:
        raise CoreRequestError(
            "invalid_version", "If-Match must contain an incident version"
        ) from error
    if version < 1:
        raise CoreRequestError("invalid_version", "If-Match must contain a positive version")
    return version


def _validate_idempotency(value: str) -> str:
    if not IDEMPOTENCY_PATTERN.fullmatch(value):
        raise CoreRequestError(
            "invalid_idempotency_key", "Idempotency-Key must be 8-128 safe characters"
        )
    return value


async def _buffer_bounded_body(request: Request, limit: int) -> bool:
    """Buffer at most the configured body limit, including chunked requests."""

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            return False
        body.extend(chunk)
    request._body = bytes(body)
    return True


class EventBroadcaster:
    """One core poller fans events out to every SSE connection."""

    def __init__(self, core: CoreTransport) -> None:
        self.core = core
        self.cursor = 0
        self._subscribers: set[asyncio.Queue[StreamEventView]] = set()
        self._history: deque[StreamEventView] = deque(maxlen=1000)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        bootstrap = BootstrapResponse.model_validate(await self.core.call("bootstrap"))
        self.cursor = bootstrap.last_event_id
        self._task = asyncio.create_task(self._run(), name="sse-broadcaster")

    async def close(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                payload = await self.core.call(
                    "stream_events", {"after": self.cursor, "limit": 200}
                )
                for raw in payload:
                    event = StreamEventView.model_validate(raw)
                    self.cursor = max(self.cursor, event.id)
                    self._history.append(event)
                    for queue in tuple(self._subscribers):
                        if not queue.full():
                            queue.put_nowait(event)
            except (CoreUnavailableError, CoreRequestError):
                pass
            await asyncio.sleep(0.5)

    def subscribe(self) -> asyncio.Queue[StreamEventView]:
        queue: asyncio.Queue[StreamEventView] = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[StreamEventView]) -> None:
        self._subscribers.discard(queue)

    async def replay(self, after: int) -> list[StreamEventView]:
        cached = [event for event in self._history if event.id > after]
        if cached and cached[0].id <= after + 1:
            return cached
        payload = await self.core.call("stream_events", {"after": after, "limit": 1000})
        return [StreamEventView.model_validate(item) for item in payload]


def create_app(
    settings: AppSettings | None = None,
    *,
    core: CoreTransport | None = None,
    core_service: CoreService | None = None,
) -> FastAPI:
    app_settings = settings or AppSettings()
    embedded = core_service
    if core is None:
        if embedded is not None:
            core = InProcessCore(embedded, "query")
        elif app_settings.runtime_role == "all" and app_settings.environment != "production":
            embedded = CoreService(app_settings)
            core = InProcessCore(embedded, "query")
        else:
            core = CoreClient(app_settings.query_socket)
    app_core = core
    limiter = MutationLimiter(app_settings.mutation_limit_per_minute)
    verifier = (
        AccessTokenVerifier(
            app_settings.access_team_domain,
            app_settings.access_audience,
            app_settings.email_allowlist,
            clock_leeway_seconds=app_settings.access_clock_leeway_seconds,
        )
        if app_settings.auth_mode == "access"
        else None
    )
    broadcaster = EventBroadcaster(app_core)
    connection_lock = asyncio.Lock()
    connection_count = 0

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if embedded is not None:
            await embedded.open()
        try:
            await broadcaster.start()
            yield
        finally:
            await broadcaster.close()
            if embedded is not None:
                await embedded.close()

    app = FastAPI(
        title="Signal Room API",
        version="1.0.0",
        docs_url="/api/docs" if app_settings.environment != "production" else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.core = app_core
    app.state.verifier = verifier
    app.state.broadcaster = broadcaster

    @app.exception_handler(StarletteHTTPException)
    async def http_exception(request: Request, error: StarletteHTTPException) -> JSONResponse:
        titles = {404: "Not Found", 405: "Method Not Allowed"}
        return _problem(
            request,
            error.status_code,
            titles.get(error.status_code, "Request Failed"),
            str(error.detail),
            headers=cast(dict[str, str] | None, error.headers),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception(request: Request, _: RequestValidationError) -> JSONResponse:
        return _problem(request, 422, "Invalid Request", "Request validation failed")

    @app.exception_handler(CoreUnavailableError)
    async def core_unavailable(request: Request, _: CoreUnavailableError) -> JSONResponse:
        return _problem(request, 503, "Service Unavailable", "Signal Room core is unavailable")

    @app.exception_handler(CoreRequestError)
    async def core_request_error(request: Request, error: CoreRequestError) -> JSONResponse:
        status = {
            "not_found": 404,
            "conflict": 409,
            "idempotency_conflict": 409,
            "invalid_parameter": 422,
            "invalid_version": 428,
            "invalid_idempotency_key": 422,
        }.get(error.code, 500)
        title = {
            404: "Not Found",
            409: "Conflict",
            422: "Invalid Request",
            428: "Precondition Required",
            500: "Internal Server Error",
        }[status]
        detail = str(error) if status < 500 else "The core could not complete the request"
        return _problem(request, status, title, detail)

    def add_security_headers(request: Request, response: Any) -> None:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/assets/") and re.search(
            r"(?:^|[-.])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$", request.url.path
        ):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        if app_settings.environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"

    @app.middleware("http")
    async def security_boundary(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = _request_id(request)
        request.state.identity = Identity(
            subject="local-development", email="local@signal-room.invalid"
        )
        host = request.headers.get("host", "").split(":", 1)[0].lower()
        response: Any
        if host not in app_settings.host_allowlist:
            response = _problem(request, 400, "Invalid Host", "Request Host is not trusted")
        else:
            health_path = request.url.path in {"/api/health/live", "/api/health/ready"}
            if verifier and not health_path:
                token = request.headers.get("Cf-Access-Jwt-Assertion", "")
                try:
                    request.state.identity = await verifier.verify(token)
                except AuthenticationError:
                    response = _problem(request, 403, "Forbidden", "Access identity was rejected")
                    add_security_headers(request, response)
                    return response
            content_length = request.headers.get("content-length")
            try:
                oversized = bool(
                    content_length and int(content_length) > app_settings.request_body_limit_bytes
                )
            except ValueError:
                oversized = True
            if oversized:
                response = _problem(request, 413, "Content Too Large", "Request body is too large")
            elif request.method not in {"GET", "HEAD", "OPTIONS"}:
                content_type = request.headers.get("content-type", "").lower()
                if not content_type.startswith("application/json"):
                    response = _problem(
                        request, 415, "Unsupported Media Type", "JSON content type is required"
                    )
                elif request.headers.get("origin") != app_settings.public_origin:
                    response = _problem(request, 403, "Forbidden", "Request Origin was rejected")
                elif request.headers.get("X-Signal-Room-CSRF") != "1":
                    response = _problem(request, 403, "Forbidden", "CSRF confirmation is required")
                elif not await _buffer_bounded_body(request, app_settings.request_body_limit_bytes):
                    response = _problem(
                        request, 413, "Content Too Large", "Request body is too large"
                    )
                elif not limiter.allow(request.state.identity.subject):
                    response = _problem(
                        request,
                        429,
                        "Too Many Requests",
                        "Mutation rate limit was exceeded",
                        headers={"Retry-After": "60"},
                    )
                else:
                    response = await call_next(request)
            else:
                response = await call_next(request)
        add_security_headers(request, response)
        return response

    @app.get("/api/health/live")
    async def health_live() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/health/ready")
    async def health_ready(request: Request) -> JSONResponse:
        try:
            status = await app_core.call("readiness")
        except (CoreUnavailableError, CoreRequestError):
            status = {"ok": False, "database": "core unavailable"}
        return JSONResponse(status, status_code=200 if status.get("ok") else 503)

    @app.get("/api/v1/bootstrap", response_model=BootstrapResponse)
    async def get_bootstrap() -> BootstrapResponse:
        return BootstrapResponse.model_validate(await app_core.call("bootstrap"))

    @app.get("/api/v1/assets/{asset_id}", response_model=AssetDetailResponse)
    async def get_asset(asset_id: str) -> AssetDetailResponse:
        return AssetDetailResponse.model_validate(
            await app_core.call("asset_detail", {"asset_id": asset_id})
        )

    @app.get("/api/v1/assets/{asset_id}/metrics", response_model=MetricsResponse)
    async def get_metrics(
        asset_id: str,
        range_name: str = Query(default="1h", alias="range", pattern=r"^(1h|24h|7d|30d|180d)$"),
        resolution: str = Query(default="auto", pattern=r"^(auto|raw|5m|1h|1d)$"),
    ) -> MetricsResponse:
        return MetricsResponse.model_validate(
            await app_core.call(
                "metrics",
                {"asset_id": asset_id, "range": range_name, "resolution": resolution},
            )
        )

    @app.get("/api/v1/incidents", response_model=IncidentPage)
    async def get_incidents(
        state: Annotated[list[IncidentState] | None, Query()] = None,
        cursor: str | None = None,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> IncidentPage:
        return IncidentPage.model_validate(
            await app_core.call(
                "incident_page",
                {
                    "states": [str(item) for item in state] if state else None,
                    "cursor": cursor,
                    "limit": limit,
                },
            )
        )

    @app.get("/api/v1/incidents/{incident_id}", response_model=IncidentView)
    async def get_incident(incident_id: str) -> IncidentView:
        return IncidentView.model_validate(
            await app_core.call("incident", {"incident_id": incident_id})
        )

    @app.get("/api/v1/incidents/{incident_id}/timeline", response_model=TimelinePage)
    async def get_incident_timeline(
        incident_id: str,
        cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> TimelinePage:
        return TimelinePage.model_validate(
            await app_core.call(
                "timeline", {"incident_id": incident_id, "cursor": cursor, "limit": limit}
            )
        )

    def mutation_params(
        request: Request,
        incident_id: str,
        if_match: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        identity: Identity = request.state.identity
        return {
            "incident_id": incident_id,
            "actor_subject": identity.subject,
            "actor_email": identity.email,
            "version": _parse_version(if_match),
            "idempotency_key": _validate_idempotency(idempotency_key),
        }

    @app.post("/api/v1/incidents/{incident_id}/acknowledge", response_model=ActionResponse)
    async def acknowledge_incident(
        incident_id: str,
        request: Request,
        if_match: str = Header(alias="If-Match"),
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> ActionResponse:
        result = await app_core.call(
            "acknowledge",
            mutation_params(request, incident_id, if_match, idempotency_key),
        )
        return ActionResponse(incident=IncidentView.model_validate(result))

    @app.post("/api/v1/incidents/{incident_id}/notes", response_model=ActionResponse)
    async def add_incident_note(
        incident_id: str,
        payload: NoteRequest,
        request: Request,
        if_match: str = Header(alias="If-Match"),
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> ActionResponse:
        params = mutation_params(request, incident_id, if_match, idempotency_key)
        params["body"] = payload.body
        result = await app_core.call("note", params)
        return ActionResponse(incident=IncidentView.model_validate(result))

    @app.post("/api/v1/incidents/{incident_id}/close", response_model=ActionResponse)
    async def close_incident(
        incident_id: str,
        request: Request,
        if_match: str = Header(alias="If-Match"),
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> ActionResponse:
        result = await app_core.call(
            "close", mutation_params(request, incident_id, if_match, idempotency_key)
        )
        return ActionResponse(incident=IncidentView.model_validate(result))

    @app.get("/api/v1/maintenance", response_model=list[MaintenanceWindowView])
    async def get_maintenance(include_expired: bool = False) -> list[MaintenanceWindowView]:
        values = await app_core.call("maintenance_list", {"include_expired": include_expired})
        return [MaintenanceWindowView.model_validate(item) for item in values]

    @app.post("/api/v1/maintenance", response_model=MaintenanceActionResponse)
    async def create_maintenance_window(
        payload: MaintenanceCreateRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> MaintenanceActionResponse:
        identity: Identity = request.state.identity
        result = await app_core.call(
            "maintenance_create",
            {
                "maintenance": payload.model_dump(mode="json"),
                "actor_subject": identity.subject,
                "actor_email": identity.email,
                "idempotency_key": _validate_idempotency(idempotency_key),
            },
        )
        return MaintenanceActionResponse(maintenance=MaintenanceWindowView.model_validate(result))

    @app.post(
        "/api/v1/maintenance/{maintenance_id}/cancel",
        response_model=MaintenanceActionResponse,
    )
    async def cancel_maintenance_window(
        maintenance_id: str,
        request: Request,
        if_match: str = Header(alias="If-Match"),
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> MaintenanceActionResponse:
        identity: Identity = request.state.identity
        result = await app_core.call(
            "maintenance_cancel",
            {
                "maintenance_id": maintenance_id,
                "actor_subject": identity.subject,
                "actor_email": identity.email,
                "version": _parse_version(if_match),
                "idempotency_key": _validate_idempotency(idempotency_key),
            },
        )
        return MaintenanceActionResponse(maintenance=MaintenanceWindowView.model_validate(result))

    @app.get("/api/v1/diagnostics", response_model=DiagnosticsResponse)
    async def get_diagnostics(request: Request) -> DiagnosticsResponse:
        return DiagnosticsResponse.model_validate(
            await app_core.call("diagnostics", {"request_id": request.state.request_id})
        )

    @app.get("/api/v1/stream", response_model=None)
    async def stream_events(request: Request) -> StreamingResponse | JSONResponse:
        nonlocal connection_count
        async with connection_lock:
            if connection_count >= app_settings.sse_connection_limit:
                return _problem(
                    request,
                    429,
                    "Too Many Requests",
                    "SSE connection limit was reached",
                    headers={"Retry-After": "15"},
                )
            connection_count += 1
        try:
            cursor = max(0, int(request.headers.get("Last-Event-ID", "0")))
        except ValueError:
            cursor = 0
        queue = broadcaster.subscribe()

        async def generate() -> AsyncIterator[str]:
            nonlocal cursor, connection_count
            try:
                for event in await broadcaster.replay(cursor):
                    cursor = max(cursor, event.id)
                    yield (
                        f"id: {event.id}\nevent: {event.topic}\ndata: {event.model_dump_json()}\n\n"
                    )
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        heartbeat = json.dumps({"at": utc_now().isoformat()})
                        yield f"event: heartbeat\ndata: {heartbeat}\n\n"
                        continue
                    if event.id <= cursor:
                        continue
                    cursor = event.id
                    yield (
                        f"id: {event.id}\nevent: {event.topic}\ndata: {event.model_dump_json()}\n\n"
                    )
            finally:
                broadcaster.unsubscribe(queue)
                async with connection_lock:
                    connection_count -= 1

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def api_not_found(request: Request, path: str) -> JSONResponse:
        return _problem(request, 404, "Not Found", "API endpoint does not exist")

    packaged_static = Path(__file__).resolve().parent / "static_private"
    source_static = Path(__file__).resolve().parents[2] / "frontend" / "dist-private"
    static_candidates = [app_settings.static_dir, packaged_static, source_static]
    static_dir = next(
        (candidate for candidate in static_candidates if candidate and candidate.is_dir()),
        source_static,
    )
    if static_dir.is_dir():
        assets_dir = static_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            candidate = (static_dir / path).resolve()
            if path and candidate.is_file() and static_dir.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(static_dir / "index.html")

    return app
