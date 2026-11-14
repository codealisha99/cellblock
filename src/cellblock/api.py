"""HTTP surface. Thin: validates input, maps domain errors, streams SSE. Policy lives in service.py."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import iterate_in_threadpool

from cellblock.backends.base import Backend, SandboxSpec
from cellblock.backends.docker import DockerBackend
from cellblock.config import Settings
from cellblock.errors import BadRequest, CellblockError
from cellblock.service import Service, collect
from cellblock.store import Store

log = logging.getLogger("cellblock")
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}$")
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_ARG_LEN = 32 * 1024


class CreateSandbox(BaseModel):
    image: str | None = None
    snapshot_id: str | None = None
    cpu: float = Field(1.0, ge=0.1, le=4.0)
    memory_mb: int = Field(512, ge=64, le=4096)
    pids: int = Field(256, ge=16, le=4096)
    network: bool = False
    workspace_mb: int = Field(256, ge=16, le=2048)
    ttl_seconds: int = Field(900, ge=1, le=86400)

    @model_validator(mode="after")
    def _check_image(self) -> "CreateSandbox":
        if self.image is not None and not IMAGE_RE.match(self.image):
            raise ValueError("invalid image reference")
        return self


class ExecRequest(BaseModel):
    argv: list[str] | None = Field(default=None, max_length=256)
    command: str | None = Field(default=None, max_length=MAX_ARG_LEN)
    timeout_s: int = Field(30, ge=1, le=3600)

    @model_validator(mode="after")
    def _one_of(self) -> "ExecRequest":
        if (self.argv is None) == (self.command is None):
            raise ValueError("provide exactly one of argv or command")
        if self.argv is not None and (not self.argv or any("\x00" in a or len(a) > MAX_ARG_LEN for a in self.argv)):
            raise ValueError("argv must be non-empty, without NUL bytes, and under 32 KiB per arg")
        return self

    def to_argv(self) -> list[str]:
        return self.argv if self.argv is not None else ["sh", "-c", self.command or ""]


class CodeRequest(BaseModel):
    language: Literal["python", "node"] = "python"
    code: str = Field(max_length=200_000)
    timeout_s: int = Field(30, ge=1, le=3600)


class SnapshotRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


def _sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


def _sse_response(events: Iterator[dict[str, Any]]) -> StreamingResponse:
    first = next(events)  # validation and the started event happen before we commit to a 200

    async def body() -> AsyncIterator[str]:
        yield _sse(first)
        async for event in iterate_in_threadpool(events):
            yield _sse(event)

    return StreamingResponse(body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


async def _every(fn: Callable[[], Any], interval_s: float) -> None:
    while True:
        try:
            await asyncio.to_thread(fn)
        except Exception:  # keep the loop alive; the next tick retries
            log.exception("background task %s failed", getattr(fn, "__name__", fn))
        await asyncio.sleep(interval_s)


def _sandbox_routes(router: APIRouter, service: Service, auth: Callable[..., str]) -> None:
    @router.post("/sandboxes", status_code=201)
    def create(body: CreateSandbox, key: str = Depends(auth)) -> dict[str, Any]:
        s = service.settings
        spec = SandboxSpec(image=body.image or s.default_image, cpu=body.cpu, memory_mb=body.memory_mb,
                           pids=body.pids, network=body.network, workspace_mb=body.workspace_mb)
        return service.create_sandbox(key, spec, body.ttl_seconds, body.snapshot_id)

    @router.get("/sandboxes")
    def list_all(key: str = Depends(auth)) -> list[dict[str, Any]]:
        return service.list(key)

    @router.get("/sandboxes/{sandbox_id}")
    def get_one(sandbox_id: str, key: str = Depends(auth)) -> dict[str, Any]:
        return service.get(key, sandbox_id)

    @router.delete("/sandboxes/{sandbox_id}", status_code=204)
    def destroy(sandbox_id: str, key: str = Depends(auth)) -> Response:
        service.destroy(key, sandbox_id)
        return Response(status_code=204)

    @router.post("/sandboxes/{sandbox_id}/pause")
    def pause(sandbox_id: str, key: str = Depends(auth)) -> dict[str, Any]:
        return service.pause(key, sandbox_id)

    @router.post("/sandboxes/{sandbox_id}/resume")
    def resume(sandbox_id: str, key: str = Depends(auth)) -> dict[str, Any]:
        return service.resume(key, sandbox_id)


def _exec_routes(router: APIRouter, service: Service, auth: Callable[..., str]) -> None:
    @router.post("/sandboxes/{sandbox_id}/exec")
    def exec_collect(sandbox_id: str, body: ExecRequest, key: str = Depends(auth)) -> dict[str, Any]:
        return service.exec_collect(key, sandbox_id, body.to_argv(), body.timeout_s)

    @router.post("/sandboxes/{sandbox_id}/exec/stream")
    def exec_sse(sandbox_id: str, body: ExecRequest, key: str = Depends(auth)) -> StreamingResponse:
        return _sse_response(service.exec_stream(key, sandbox_id, body.to_argv(), body.timeout_s))

    @router.post("/sandboxes/{sandbox_id}/code")
    def code_collect(sandbox_id: str, body: CodeRequest, key: str = Depends(auth)) -> dict[str, Any]:
        events = service.run_code(key, sandbox_id, body.language, body.code, body.timeout_s)
        return collect(events)

    @router.post("/sandboxes/{sandbox_id}/code/stream")
    def code_sse(sandbox_id: str, body: CodeRequest, key: str = Depends(auth)) -> StreamingResponse:
        return _sse_response(service.run_code(key, sandbox_id, body.language, body.code, body.timeout_s))

    @router.post("/sandboxes/{sandbox_id}/execs/{exec_id}/kill", status_code=202)
    def kill(sandbox_id: str, exec_id: str, key: str = Depends(auth)) -> dict[str, bool]:
        service.kill_exec(key, sandbox_id, exec_id)
        return {"killed": True}

    @router.get("/sandboxes/{sandbox_id}/execs")
    def history(sandbox_id: str, key: str = Depends(auth)) -> list[dict[str, Any]]:
        return service.list_execs(key, sandbox_id)


def _fs_routes(router: APIRouter, service: Service, auth: Callable[..., str]) -> None:
    @router.get("/sandboxes/{sandbox_id}/files")
    def list_files(sandbox_id: str, path: str = Query("/workspace"), key: str = Depends(auth)) -> list[dict[str, Any]]:
        return service.list_files(key, sandbox_id, path)

    @router.get("/sandboxes/{sandbox_id}/files/content")
    def download(sandbox_id: str, path: str = Query(...), key: str = Depends(auth)) -> Response:
        data = service.read_file(key, sandbox_id, path)
        return Response(content=data, media_type="application/octet-stream")

    @router.put("/sandboxes/{sandbox_id}/files/content")
    def upload(sandbox_id: str, path: str = Query(...), data: bytes = Body(..., media_type="application/octet-stream"),
               key: str = Depends(auth)) -> dict[str, Any]:
        if len(data) > MAX_UPLOAD_BYTES:
            raise BadRequest(f"upload exceeds {MAX_UPLOAD_BYTES} bytes")
        return {"path": service.write_file(key, sandbox_id, path, data), "bytes": len(data)}


def _template_routes(router: APIRouter, service: Service, auth: Callable[..., str]) -> None:
    @router.post("/sandboxes/{sandbox_id}/snapshots", status_code=201)
    def snapshot(sandbox_id: str, body: SnapshotRequest, key: str = Depends(auth)) -> dict[str, Any]:
        return service.snapshot(key, sandbox_id, body.name)

    @router.get("/templates")
    def list_templates(key: str = Depends(auth)) -> list[dict[str, Any]]:
        return service.list_templates(key)

    @router.get("/templates/{template_id}")
    def get_template(template_id: str, key: str = Depends(auth)) -> dict[str, Any]:
        return service.get_template(key, template_id)

    @router.delete("/templates/{template_id}", status_code=204)
    def delete_template(template_id: str, key: str = Depends(auth)) -> Response:
        service.delete_template(key, template_id)
        return Response(status_code=204)


def create_app(settings: Settings | None = None, backend: Backend | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    service = Service(settings, Store(settings.db_path), backend or DockerBackend(instance=settings.instance))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(service.reconcile)
        await asyncio.to_thread(service.refill_pool)
        tasks = [
            asyncio.create_task(_every(service.reap_expired, settings.reaper_interval_s)),
            asyncio.create_task(_every(service.refill_pool, 1.0)),
        ]
        yield
        for task in tasks:
            task.cancel()
        await asyncio.to_thread(service.shutdown)

    app = FastAPI(title="cellblock", version="0.1.0", lifespan=lifespan)
    app.state.service = service

    async def auth(authorization: Annotated[str | None, Header()] = None) -> str:
        raw = authorization.removeprefix("Bearer ").strip() if authorization else None
        return service.authenticate(raw)

    router = APIRouter(prefix="/v1")
    for register in (_sandbox_routes, _exec_routes, _fs_routes, _template_routes):
        register(router, service, auth)
    app.include_router(router)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "pool_ready": service.pool_ready(), "pool_size": settings.pool_size}

    @app.exception_handler(CellblockError)
    async def _domain_error(_: Request, exc: CellblockError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)

    return app
