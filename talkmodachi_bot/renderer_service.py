from __future__ import annotations

import asyncio
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .renderer_pool import RenderPayload, RendererPool
from .voices import VoiceParams, cache_key


class RenderRequest(BaseModel):
    text: str = Field(min_length=1)
    voice: dict[str, int | str] = Field(default_factory=dict)
    mode: Literal["text", "sing"] = "text"


app = FastAPI(title="Talkmodachi Renderer", version="0.1.0")
pool: RendererPool | None = None
cache_dir = Path(os.environ.get("TALKMODACHI_CACHE_DIR", "/cache"))
engine_version = os.environ.get("TALKMODACHI_ENGINE_VERSION", "talkmodachi-v1")
inflight_lock = asyncio.Lock()
inflight_tasks: dict[str, asyncio.Task[dict[str, object]]] = {}
render_semaphore: asyncio.Semaphore | None = None
max_inflight_renders = int(os.environ.get("TALKMODACHI_MAX_INFLIGHT_RENDERS", "32"))


@app.on_event("startup")
async def startup() -> None:
    global pool, render_semaphore
    cache_dir.mkdir(parents=True, exist_ok=True)
    pool = RendererPool.from_env()
    render_semaphore = asyncio.Semaphore(max_inflight_renders)
    pool.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    if pool is not None:
        pool.stop()


@app.get("/health")
def health() -> dict[str, object]:
    return {"ok": True, "cache_dir": str(cache_dir), "pool": pool.health() if pool else None}


@app.post("/render")
async def render(request: RenderRequest) -> Response:
    if pool is None:
        raise HTTPException(status_code=503, detail="Renderer pool is not ready")
    try:
        voice = VoiceParams.from_mapping(request.voice)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    text = request.text.replace("\n", " ").strip()
    if len(text) > voice.text_limit():
        raise HTTPException(status_code=400, detail=f"Text is longer than {voice.text_limit()} characters")

    key = cache_key(text, voice, request.mode, engine_version)
    cache_path = cache_dir / f"{key}.wav"
    if cache_path.exists():
        return FileResponse(cache_path, media_type="audio/wav", filename="speech.wav", headers={"X-Cache": "HIT"})

    created = False
    async with inflight_lock:
        task = inflight_tasks.get(key)
        if task is None:
            if len(inflight_tasks) >= max_inflight_renders:
                raise HTTPException(status_code=429, detail="Renderer queue is full")
            task = asyncio.create_task(_render_to_cache(cache_path, text, voice, request.mode))
            inflight_tasks[key] = task
            created = True

    try:
        result = await task
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    finally:
        if created:
            async with inflight_lock:
                if inflight_tasks.get(key) is task:
                    del inflight_tasks[key]

    cache_header = str(result["cache"])
    if cache_header == "MISS" and not created:
        cache_header = "DEDUPED"
    return FileResponse(
        cache_path,
        media_type="audio/wav",
        filename="speech.wav",
        headers={"X-Cache": cache_header, "X-Render-Time-Ms": str(result.get("elapsed_ms", ""))},
    )


async def _render_to_cache(cache_path: Path, text: str, voice: VoiceParams, mode: str) -> dict[str, object]:
    if cache_path.exists():
        return {"cache": "HIT", "elapsed_ms": ""}
    if pool is None or render_semaphore is None:
        raise RuntimeError("Renderer pool is not ready")

    async with render_semaphore:
        if cache_path.exists():
            return {"cache": "HIT", "elapsed_ms": ""}
        result = await asyncio.to_thread(pool.render, RenderPayload(text=text, voice=voice, mode=mode))
        audio = result["audio"]
        with NamedTemporaryFile(dir=cache_dir, delete=False) as temp:
            temp.write(audio)
            temp_path = Path(temp.name)
        temp_path.replace(cache_path)
        return {"cache": "MISS", "elapsed_ms": result.get("elapsed_ms", "")}


def main() -> None:
    import uvicorn

    host = os.environ.get("RENDERER_HOST", "0.0.0.0")
    port = int(os.environ.get("RENDERER_PORT", "8080"))
    uvicorn.run("talkmodachi_bot.renderer_service:app", host=host, port=port)


if __name__ == "__main__":
    main()
