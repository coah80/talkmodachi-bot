from __future__ import annotations

import multiprocessing as mp
import os
import queue
import socket
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .voices import VoiceParams


ROOT_DIR = Path(__file__).resolve().parents[1]
API_DIR = ROOT_DIR / "api"


@dataclass(frozen=True)
class WorkerSpec:
    rom: str
    lang_id: int
    port: int
    name: str


@dataclass(frozen=True)
class RenderPayload:
    text: str
    voice: VoiceParams
    mode: str = "text"


def find_free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _worker_loop(spec: WorkerSpec, inbox: mp.Queue, outbox: mp.Queue) -> None:
    os.environ.setdefault("CITRA_MAX_RUNTIME_SECONDS", "0")
    os.environ.setdefault("TALKMODACHI_POLL_INTERVAL", "0.01")
    sys.path.insert(0, str(API_DIR))

    import citra  # type: ignore

    citra.CITRA_PORT = spec.port
    import tts  # type: ignore

    try:
        tts.startEmulator(spec.rom, spec.lang_id)
        outbox.put({"type": "ready", "worker": spec.name})
    except Exception:
        outbox.put({"type": "startup_error", "worker": spec.name, "error": traceback.format_exc()})

    while True:
        message = inbox.get()
        if message is None:
            break

        job_id = message["job_id"]
        payload = message["payload"]
        started = time.perf_counter()
        try:
            voice = VoiceParams.from_mapping(payload["voice"])
            if voice.rom() != spec.rom:
                raise ValueError(f"Worker {spec.name} cannot render ROM {voice.rom()}")

            if payload["mode"] == "sing":
                audio = tts.singText(
                    payload["text"],
                    voice.pitch,
                    voice.speed,
                    voice.quality,
                    voice.tone,
                    voice.accent,
                    voice.engine_intonation(),
                    voice.lang_id(),
                )
            else:
                audio = tts.generateText(
                    payload["text"],
                    voice.pitch,
                    voice.speed,
                    voice.quality,
                    voice.tone,
                    voice.accent,
                    voice.engine_intonation(),
                    voice.lang_id(),
                )
            if audio is None:
                raise RuntimeError("Renderer returned no audio")
            outbox.put(
                {
                    "type": "result",
                    "job_id": job_id,
                    "audio": audio,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
        except Exception:
            outbox.put({"type": "error", "job_id": job_id, "error": traceback.format_exc()})

    try:
        tts.killEmulator()
    except Exception:
        pass


class WorkerLane:
    def __init__(self, spec: WorkerSpec) -> None:
        self.spec = spec
        self.inbox: mp.Queue | None = None
        self.outbox: mp.Queue | None = None
        self.pending: dict[str, Future[dict[str, Any]]] = {}
        self.pending_lock = threading.Lock()
        self.lifecycle_lock = threading.RLock()
        self.ready = threading.Event()
        self.startup_failed = False
        self.last_error: str | None = None
        self.process: mp.Process | None = None
        self.results_thread: threading.Thread | None = None

    def start(self) -> None:
        with self.lifecycle_lock:
            if self.process is not None and self.process.is_alive():
                return
            self.ready.clear()
            self.startup_failed = False
            self.last_error = None
            self.inbox = mp.Queue()
            self.outbox = mp.Queue()
            self.process = mp.Process(target=_worker_loop, args=(self.spec, self.inbox, self.outbox), daemon=True)
            self.results_thread = threading.Thread(target=self._result_loop, args=(self.process, self.outbox), daemon=True)
            self.process.start()
            self.results_thread.start()

    def stop(self) -> None:
        process = self.process
        inbox = self.inbox
        if process is not None and process.is_alive():
            if inbox is not None:
                inbox.put(None)
            process.join(timeout=5)
        if process is not None and process.is_alive():
            process.kill()
        self._fail_pending(RuntimeError(f"Renderer worker {self.spec.name} stopped"))

    def restart(self) -> None:
        with self.lifecycle_lock:
            self.stop()
            self.start()

    def render(self, payload: RenderPayload, timeout: float) -> dict[str, Any]:
        if self.process is None or not self.process.is_alive():
            self.start()
        if not self.ready.wait(timeout=min(timeout, 15.0)):
            raise RuntimeError(f"Renderer worker {self.spec.name} is not ready")
        if self.startup_failed:
            raise RuntimeError(f"Renderer worker {self.spec.name} failed startup: {self.last_error}")

        inbox = self.inbox
        if inbox is None:
            raise RuntimeError(f"Renderer worker {self.spec.name} has no command queue")
        job_id = str(uuid.uuid4())
        future: Future[dict[str, Any]] = Future()
        with self.pending_lock:
            self.pending[job_id] = future
        inbox.put(
            {
                "job_id": job_id,
                "payload": {
                    "text": payload.text,
                    "voice": payload.voice.to_dict(),
                    "mode": payload.mode,
                },
            }
        )
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as error:
            with self.pending_lock:
                self.pending.pop(job_id, None)
            self.restart()
            raise TimeoutError(f"Renderer worker {self.spec.name} timed out") from error

    def pending_count(self) -> int:
        with self.pending_lock:
            return len(self.pending)

    def _result_loop(self, process: mp.Process, outbox: mp.Queue) -> None:
        while True:
            try:
                message = outbox.get(timeout=1)
            except queue.Empty:
                if not process.is_alive():
                    self._fail_pending(RuntimeError(f"Renderer worker {self.spec.name} exited"))
                    return
                continue

            message_type = message.get("type")
            if message_type == "ready":
                self.ready.set()
            elif message_type == "startup_error":
                self.startup_failed = True
                self.last_error = message["error"]
                self.ready.set()
            elif message_type in {"result", "error"}:
                job_id = message["job_id"]
                with self.pending_lock:
                    future = self.pending.pop(job_id, None)
                if future is None:
                    continue
                if message_type == "result":
                    future.set_result(message)
                else:
                    future.set_exception(RuntimeError(message["error"]))

    def _fail_pending(self, error: Exception) -> None:
        with self.pending_lock:
            pending = list(self.pending.values())
            self.pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)


class RendererPool:
    def __init__(self, specs: list[WorkerSpec], render_timeout: float = 20.0) -> None:
        self.render_timeout = render_timeout
        self.lanes_by_rom: dict[str, list[WorkerLane]] = {}
        self.next_index: dict[str, int] = {}
        for spec in specs:
            self.lanes_by_rom.setdefault(spec.rom, []).append(WorkerLane(spec))
            self.next_index.setdefault(spec.rom, 0)

    @classmethod
    def from_env(cls) -> "RendererPool":
        render_timeout = float(os.environ.get("TALKMODACHI_RENDER_TIMEOUT", "20"))
        specs: list[WorkerSpec] = []
        worker_roms = [rom.strip().upper() for rom in os.environ.get("TALKMODACHI_WORKER_ROMS", "US").split(",") if rom.strip()]
        for rom in worker_roms:
            count = int(os.environ.get(f"TALKMODACHI_{rom}_WORKERS", "1"))
            lang_id = int(os.environ.get(f"TALKMODACHI_{rom}_LANG_ID", "1"))
            for index in range(count):
                specs.append(WorkerSpec(rom=rom, lang_id=lang_id, port=find_free_udp_port(), name=f"{rom}-{index + 1}"))
        return cls(specs, render_timeout=render_timeout)

    def start(self) -> None:
        for lane in self._lanes():
            lane.start()

    def stop(self) -> None:
        for lane in self._lanes():
            lane.stop()

    def render(self, payload: RenderPayload) -> dict[str, Any]:
        lanes = self.lanes_by_rom.get(payload.voice.rom())
        if not lanes:
            raise RuntimeError(f"No renderer worker configured for ROM {payload.voice.rom()}")
        lane = min(lanes, key=lambda worker: worker.pending_count())
        return lane.render(payload, timeout=self.render_timeout)

    def health(self) -> dict[str, Any]:
        return {
            "workers": [
                {
                    "name": lane.spec.name,
                    "rom": lane.spec.rom,
                    "port": lane.spec.port,
                    "pid": lane.process.pid if lane.process else None,
                    "alive": lane.process.is_alive() if lane.process else False,
                    "ready": lane.ready.is_set(),
                    "last_error": lane.last_error,
                }
                for lane in self._lanes()
            ]
        }

    def _lanes(self) -> list[WorkerLane]:
        return [lane for lanes in self.lanes_by_rom.values() for lane in lanes]
