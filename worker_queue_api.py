import asyncio
import os
import time

from aiohttp import web

from config import COMFY_WORKER_API_TOKEN
from db import claim_next_job, delete_job, get_job, init_db, set_job_status

AUTH_HEADER = "X-Worker-Auth"


def _auth_ok(request: web.Request) -> bool:
    token = request.headers.get(AUTH_HEADER, "").strip()
    return token and COMFY_WORKER_API_TOKEN and token == COMFY_WORKER_API_TOKEN


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "ts": time.time()})


async def get_next_job(request: web.Request) -> web.StreamResponse:
    if not _auth_ok(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    lock_seconds = int(os.getenv("COMFY_WORKER_JOB_LOCK_SEC", "2400"))
    job = await claim_next_job(lock_seconds=lock_seconds)
    if job is None:
        return web.Response(status=204)
    # Worker забирает job и выполняет локальную генерацию.
    return web.json_response(job)


async def post_job_status(request: web.Request) -> web.StreamResponse:
    if not _auth_ok(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    job_id = request.match_info["job_id"]
    payload = await request.json()
    status = str(payload.get("status", "")).strip().lower()
    result_url = payload.get("result_url")
    error = payload.get("error")

    if status not in {"completed", "failed"}:
        return web.json_response({"error": "invalid status"}, status=400)

    # В этой архитектуре только bot удаляет job после доставки.
    await set_job_status(job_id=job_id, status=status, result_url=result_url, error=error)
    return web.json_response({"ok": True})


async def delete_job_endpoint(request: web.Request) -> web.StreamResponse:
    # На всякий случай, но обычно бот удаляет сам.
    if not _auth_ok(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    job_id = request.match_info["job_id"]
    await delete_job(job_id)
    return web.json_response({"ok": True})


async def get_job_endpoint(request: web.Request) -> web.StreamResponse:
    if not _auth_ok(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    job_id = request.match_info["job_id"]
    job = await get_job(job_id)
    if not job:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(job)


async def make_app() -> web.Application:
    await init_db()
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/worker/next", get_next_job)
    app.router.add_post("/worker/jobs/{job_id}/status", post_job_status)
    app.router.add_get("/worker/jobs/{job_id}", get_job_endpoint)
    app.router.add_delete("/worker/jobs/{job_id}", delete_job_endpoint)
    return app


def main() -> None:
    host = os.getenv("COMFY_VPS_WORKER_QUEUE_HOST", "0.0.0.0")
    port = int(os.getenv("COMFY_VPS_WORKER_QUEUE_PORT", "8001"))

    app = asyncio.run(make_app())
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()

