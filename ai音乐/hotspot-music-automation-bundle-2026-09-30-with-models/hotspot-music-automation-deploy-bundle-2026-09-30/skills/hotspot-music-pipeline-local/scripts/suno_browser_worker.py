"""Local controller for sequential Suno browser generation.

The controller owns only local queue state.  A separately loaded Chrome
extension performs the visible-page actions on ``suno.com`` and reports
verifiable song links back here.  No cookies, passwords, local storage, or
private Suno endpoints are used.

Start the service with ``run-suno-browser-worker.ps1`` and use the controls
in ``hotspot-music-runtime/dashboard.html``.  The service is idle by default;
the user must press Start in the dashboard before any browser submission is
allowed.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import suno_batch_generator as sbg


RUNTIME_ROOT = Path("D:/projects/local/hotspot-music-runtime")
TASK_ROOT = RUNTIME_ROOT / "data" / "tasks"
STATE_PATH = RUNTIME_ROOT / "data" / "suno-browser-worker-state.json"
RECEIPT_NAME = "suno-browser-results.jsonl"
ALLOWED_TASK_IDS = ("bilibili-daily", "douyin-curation", "weibo-hourly")
SUNO_URL_RE = re.compile(r"^https://(?:www\.)?suno\.com/song/[A-Za-z0-9-]+/?$")
SONG_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,127}$")
LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def extension_connected(state: dict[str, Any]) -> bool:
    """Return whether the visible Suno extension checked in recently."""
    raw = state.get("extension_seen_at")
    if not raw:
        return False
    try:
        seen = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    return (datetime.now().astimezone() - seen).total_seconds() <= 10


def atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def read_state() -> dict[str, Any]:
    value = sbg.read_json(STATE_PATH, {})
    if not isinstance(value, dict) or value.get("schema") != "suno_browser_worker_state_v1":
        return {
            "schema": "suno_browser_worker_state_v1",
            "active": False,
            "stop_requested": False,
            "auto_create": True,
            "scope": "latest",
            "follow_future": True,
            "task_ids": list(ALLOWED_TASK_IDS),
            "queue_paths": [],
            "known_queue_paths": [],
            "discovered_queue_paths": 0,
            "last_queue_discovery_at": None,
            "listener_started_at": None,
            "listener_mode": "manual",
            "submission_authorized": False,
            "listener_enabled": False,
            "current": None,
            "last_result": None,
            "last_error": None,
            "extension_seen_at": None,
            "extension_url": None,
            "updated_at": now_iso(),
        }
    # ``None`` is a deliberate migration marker for v1 state files that were
    # created before the future-queue discovery baseline existed.
    value.setdefault("known_queue_paths", None)
    value.setdefault("listener_started_at", None)
    value.setdefault("listener_mode", "manual")
    value.setdefault("submission_authorized", bool(value.get("active") and value.get("auto_create")))
    value.setdefault("listener_enabled", bool(value.get("active")))
    return value


def write_state(state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    atomic_write(STATE_PATH, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def submission_is_authorized(state: dict[str, Any]) -> bool:
    """Return whether a queued item may be submitted to the visible Suno page.

    ``auto_create`` is the user's saved preference, while
    ``submission_authorized`` is the action-time permission granted by Start.
    Both are required so a stale preference cannot re-enable Create after Stop
    or after a state-file migration.  ``active`` and ``stop_requested`` are
    checked here as the final race-safe guard immediately before dispatch.
    """
    return bool(
        state.get("active")
        and not state.get("stop_requested")
        and state.get("auto_create")
        and state.get("submission_authorized")
    )


def task_ids_or_default(task_ids: list[str] | None) -> list[str]:
    selected = list(dict.fromkeys(task_ids or ALLOWED_TASK_IDS))
    invalid = [item for item in selected if item not in ALLOWED_TASK_IDS]
    if invalid:
        raise HTTPException(400, f"不允许的 Suno 任务: {', '.join(invalid)}")
    return selected


def is_allowed_queue(path: Path) -> bool:
    try:
        relative = path.resolve().relative_to(TASK_ROOT.resolve())
    except ValueError:
        return False
    return bool(relative.parts) and relative.parts[0] in ALLOWED_TASK_IDS and path.name == "suno-generation"


def queue_candidates(task_ids: list[str], scope: Literal["latest", "all"]) -> list[Path]:
    result: list[Path] = []
    for task_id in task_ids:
        root = TASK_ROOT / task_id
        candidates = []
        if root.exists():
            for status_path in root.rglob(sbg.STATUS_NAME):
                output = status_path.parent
                if not is_allowed_queue(output):
                    continue
                value = sbg.read_json(status_path, {})
                if value.get("schema") != "suno_generation_status_v1":
                    continue
                candidates.append((str(value.get("updated_at") or ""), status_path.stat().st_mtime, output))
        candidates.sort(key=lambda item: (item[0], item[1], str(item[2])), reverse=True)
        if scope == "latest" and candidates:
            result.append(candidates[0][2])
        else:
            result.extend(item[2] for item in candidates)
    return result


def refresh_queue_paths(state: dict[str, Any]) -> bool:
    """Discover newly created allowed queues while the listener stays active."""
    if not state.get("follow_future", True):
        return False
    task_ids = task_ids_or_default(list(state.get("task_ids") or ALLOWED_TASK_IDS))
    scope = state.get("scope", "latest")
    if scope not in {"latest", "all"}:
        scope = "latest"
    existing = []
    known_selected: set[str] = set()
    for raw in state.get("queue_paths") or []:
        path = Path(str(raw))
        if not is_allowed_queue(path):
            continue
        resolved = path.resolve()
        if str(resolved) not in known_selected:
            existing.append(resolved)
            known_selected.add(str(resolved))

    all_candidates = queue_candidates(task_ids, "all")
    known_all = {
        str(Path(str(raw)).resolve())
        for raw in (state.get("known_queue_paths") or [])
        if is_allowed_queue(Path(str(raw)))
    }
    # State written by older versions has no discovery baseline. Establishing
    # it here prevents a restart from unexpectedly replaying every old queue.
    changed = False
    if "known_queue_paths" not in state or not isinstance(state.get("known_queue_paths"), list):
        state["known_queue_paths"] = [str(path.resolve()) for path in all_candidates]
        changed = True

    added = []
    for path in all_candidates:
        resolved = path.resolve()
        key = str(resolved)
        if key not in known_all:
            known_all.add(key)
            if key not in known_selected:
                existing.append(resolved)
                known_selected.add(key)
            added.append(resolved)

    state["known_queue_paths"] = sorted(known_all)
    if not added:
        # Keep the selected list clean even when a queue was removed or became
        # invalid while the worker was alive.
        state["queue_paths"] = [str(path) for path in existing]
        return changed
    state["queue_paths"] = [str(path) for path in existing]
    state["discovered_queue_paths"] = int(state.get("discovered_queue_paths") or 0) + len(added)
    state["last_queue_discovery_at"] = now_iso()
    return True


def song_result_count(output: Path) -> int:
    """Count unique verified Suno songs recorded for one queue.

    A browser result normally contains both IDs and URLs, but a page can expose
    one before the other.  Count by canonical song ID when possible so a result
    with both fields is not counted twice and an ID-only receipt is not shown as
    zero.
    """
    receipt_path = output / RECEIPT_NAME
    if not receipt_path.exists():
        return 0
    songs: set[str] = set()
    for line in receipt_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        for url in value.get("song_urls") or []:
            cleaned = str(url).strip()
            if SUNO_URL_RE.fullmatch(cleaned):
                songs.add("id:" + cleaned.split("/song/", 1)[1].rstrip("/"))
        for song_id in value.get("song_ids") or []:
            cleaned = str(song_id).strip()
            if SONG_ID_RE.fullmatch(cleaned):
                songs.add("id:" + cleaned)
    # Some older/partially completed runs wrote the verified primary song to
    # the queue status before the browser receipt was appended. Include only
    # completed variants so the worker and dashboard agree immediately.
    status = sbg.read_json(output / sbg.STATUS_NAME, {})
    for group in status.get("prompt_groups") or []:
        for variant in group.get("variants") or []:
            if variant.get("status") != "completed":
                continue
            url = str(variant.get("song_url") or "").strip()
            if SUNO_URL_RE.fullmatch(url):
                songs.add("id:" + url.split("/song/", 1)[1].rstrip("/"))
            song_id = str(variant.get("song_id") or "").strip()
            if SONG_ID_RE.fullmatch(song_id):
                songs.add("id:" + song_id)
    return len(songs)


def compact_queue(output: Path) -> dict[str, Any]:
    value = sbg.read_json(output / sbg.STATUS_NAME, {})
    counts = value.get("counts") if isinstance(value.get("counts"), dict) else {}
    return {
        "task_id": _task_id_for_queue(output),
        "queue_path": str(output.resolve()),
        "status": value.get("status", "unknown"),
        "prompt_count": int(value.get("prompt_count") or 0),
        "target_song_count": int(value.get("target_song_count") or counts.get("total") or 0),
        "generated_song_count": song_result_count(output),
        "counts": {key: int(counts.get(key) or 0) for key in ("queued", "running", "completed", "failed", "blocked", "total")},
        "updated_at": value.get("updated_at"),
    }


def _task_id_for_queue(output: Path) -> str:
    try:
        relative = output.resolve().relative_to(TASK_ROOT.resolve())
        return relative.parts[0]
    except (ValueError, IndexError):
        return "unknown"


def selected_queues(state: dict[str, Any]) -> list[Path]:
    paths = []
    for raw in state.get("queue_paths") or []:
        path = Path(str(raw))
        if is_allowed_queue(path):
            paths.append(path)
    return paths


def append_receipt(output: Path, receipt: dict[str, Any]) -> str:
    path = output / RECEIPT_NAME
    previous = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    atomic_write(path, previous + json.dumps(receipt, ensure_ascii=False) + "\n")
    return str(path.resolve())


def clean_song_values(song_ids: list[str] | None, song_urls: list[str] | None) -> tuple[list[str], list[str]]:
    ids = [str(value).strip() for value in (song_ids or []) if SONG_ID_RE.fullmatch(str(value).strip())]
    urls = [str(value).strip() for value in (song_urls or []) if SUNO_URL_RE.fullmatch(str(value).strip())]
    return list(dict.fromkeys(ids)), list(dict.fromkeys(urls))


def job_from_state(state: dict[str, Any]) -> tuple[Path, str, dict[str, Any]] | None:
    current = state.get("current")
    if not isinstance(current, dict):
        return None
    output = Path(str(current.get("queue_path") or ""))
    task_id = str(current.get("task_id") or "")
    job = current.get("job")
    if not is_allowed_queue(output) or not task_id or not isinstance(job, dict):
        return None
    return output, task_id, job


def claim_next_for(state: dict[str, Any]) -> tuple[Path, dict[str, Any]] | None:
    for output in selected_queues(state):
        response = sbg.claim_next(SimpleNamespace(output=str(output), prompt_id=None, backend="browser"))
        if response.get("status") == "ok" and isinstance(response.get("job"), dict):
            return output, response["job"]
    return None


def requeue_current(state: dict[str, Any], reason: str) -> None:
    current = job_from_state(state)
    if not current:
        return
    output, task_id, _job = current
    with sbg.queue_lock(output):
        jobs = sbg.load_jobs(output)
        job = sbg.find_job(jobs, task_id)
        job["status"] = "queued"
        job["error"] = None
        job["updated_at"] = now_iso()
        job["started_at"] = None
        sbg.save_jobs(jobs, output)
    state["current"] = None
    state["last_error"] = reason


def recover_inactive_current(state: dict[str, Any]) -> bool:
    """Return an unsubmitted claim to its queue after a worker restart.

    A process restart can leave ``current`` persisted while the action-time
    authorization has already been revoked.  Keeping that claim blocks the
    next Start request and makes the dashboard look stopped with no usable
    command.  Only pre-Create phases are safe to requeue; a submitted job must
    remain available for result observation.
    """
    current = state.get("current")
    if state.get("active") or not isinstance(current, dict):
        return False
    phase = str(current.get("phase") or "claimed")
    if phase not in {"claimed", "filled"}:
        return False
    requeue_current(state, "worker was inactive with an unsubmitted claim; returned to queue")
    state["stop_requested"] = False
    return True


def count_selected(state: dict[str, Any]) -> dict[str, int]:
    result = {key: 0 for key in ("queued", "running", "completed", "failed", "blocked", "total", "generated_song_count")}
    for output in selected_queues(state):
        value = sbg.read_json(output / sbg.STATUS_NAME, {})
        counts = value.get("counts") if isinstance(value.get("counts"), dict) else {}
        for key in result:
            if key != "generated_song_count":
                result[key] += int(counts.get(key) or 0)
        result["generated_song_count"] += song_result_count(output)
    return result


class StartRequest(BaseModel):
    task_ids: list[str] | None = None
    scope: Literal["latest", "all"] = "latest"
    auto_create: bool = True
    follow_future: bool = True


class PhaseRequest(BaseModel):
    task_id: str
    queue_path: str
    phase: Literal["filled", "submitted", "observing"]
    existing_song_ids: list[str] = Field(default_factory=list)


class ResultRequest(BaseModel):
    task_id: str
    queue_path: str
    song_ids: list[str] = Field(default_factory=list)
    song_urls: list[str] = Field(default_factory=list)
    observed_at: str | None = None


class ErrorRequest(BaseModel):
    task_id: str
    queue_path: str
    reason: str
    action: Literal["block", "fail", "requeue"] = "block"


class HeartbeatRequest(BaseModel):
    url: str | None = None


app = FastAPI(title="Hotspot Music Suno Browser Worker", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["null", "http://127.0.0.1:8765", "http://localhost:8765"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "suno-browser-worker", "updated_at": now_iso()}


@app.post("/v1/browser/heartbeat")
def heartbeat(request: HeartbeatRequest) -> dict[str, Any]:
    with LOCK:
        state = read_state()
        state["extension_seen_at"] = now_iso()
        state["extension_url"] = request.url if str(request.url or "").startswith("https://suno.com/") or str(request.url or "").startswith("https://www.suno.com/") else None
        write_state(state)
        return {"status": "ok", "extension_connected": True, "seen_at": state["extension_seen_at"]}


@app.get("/v1/browser/status")
def status() -> dict[str, Any]:
    with LOCK:
        state = read_state()
        # After Stop, preserve a pre-Create claim long enough for the browser
        # extension to receive the abort command. A later Start still invokes
        # recovery if the extension was disconnected.
        recovered = False if state.get("stop_requested") else recover_inactive_current(state)
        if recovered or (state.get("active") and refresh_queue_paths(state)):
            write_state(state)
        queues = [compact_queue(path) for path in selected_queues(state)]
        current = state.get("current") if isinstance(state.get("current"), dict) else None
        return {
            "status": "ok",
            "active": bool(state.get("active")),
            "stop_requested": bool(state.get("stop_requested")),
            "auto_create": bool(state.get("auto_create", True)),
            "submission_authorized": bool(state.get("submission_authorized", False)),
            "listener_enabled": bool(state.get("listener_enabled", state.get("active"))),
            "listener_mode": state.get("listener_mode", "manual"),
            "scope": state.get("scope", "latest"),
            "follow_future": bool(state.get("follow_future", True)),
            "discovered_queue_paths": int(state.get("discovered_queue_paths") or 0),
            "last_queue_discovery_at": state.get("last_queue_discovery_at"),
            "task_ids": state.get("task_ids", list(ALLOWED_TASK_IDS)),
            "current": {
                "task_id": current.get("task_id"),
                "queue_path": current.get("queue_path"),
                "job": current.get("job"),
                "phase": current.get("phase"),
            } if current else None,
            "last_result": state.get("last_result"),
            "last_error": state.get("last_error"),
            "extension_connected": extension_connected(state),
            "extension_seen_at": state.get("extension_seen_at"),
            "extension_url": state.get("extension_url"),
            "counts": count_selected(state),
            "queues": queues,
            "updated_at": state.get("updated_at"),
        }


@app.post("/v1/browser/start")
def start(request: StartRequest) -> dict[str, Any]:
    with LOCK:
        state = read_state()
        recover_inactive_current(state)
        if state.get("current"):
            raise HTTPException(409, "当前已有 Suno 条目正在执行；请先停止并等待当前条目收尾")
        task_ids = task_ids_or_default(request.task_ids)
        paths = queue_candidates(task_ids, request.scope)
        if not paths and not request.follow_future:
            raise HTTPException(404, "没有找到 B站、抖音梳理或微博的 Suno 队列")
        state.update({
            "active": True,
            "stop_requested": False,
            "auto_create": bool(request.auto_create),
            "submission_authorized": bool(request.auto_create),
            "listener_enabled": True,
            "scope": request.scope,
            "follow_future": bool(request.follow_future),
            "task_ids": task_ids,
            "queue_paths": [str(path.resolve()) for path in paths],
            "known_queue_paths": [str(path.resolve()) for path in queue_candidates(task_ids, "all")],
            "discovered_queue_paths": 0,
            "last_queue_discovery_at": now_iso(),
            "listener_started_at": now_iso(),
            "listener_mode": "manual",
            "current": None,
            "last_error": None,
        })
        write_state(state)
        return status()


@app.post("/v1/browser/stop")
def stop() -> dict[str, Any]:
    with LOCK:
        state = read_state()
        current = state.get("current")
        # Keep an unsubmitted claim until the extension receives the abort
        # command. Clearing it here loses the abort command and can leave the
        # visible Suno page busy with the previous task. A later status/start
        # cycle can recover it if the extension is disconnected.
        state["active"] = False
        state["stop_requested"] = True
        state["auto_create"] = False
        state["submission_authorized"] = False
        state["listener_enabled"] = False
        if not state.get("current"):
            state["stop_requested"] = False
        write_state(state)
        return status()


@app.get("/v1/browser/command")
def command() -> dict[str, Any]:
    """Return one command for the extension's visible Suno tab."""
    with LOCK:
        state = read_state()
        if state.get("active") and refresh_queue_paths(state):
            write_state(state)
        current = job_from_state(state)
        if current:
            output, task_id, job = current
            phase = str((state.get("current") or {}).get("phase") or "claimed")
            if state.get("stop_requested") and phase in {"claimed", "filled"}:
                return {"status": "ok", "action": "abort", "task_id": task_id, "queue_path": str(output), "job": job,
                        "existing_song_ids": (state.get("current") or {}).get("existing_song_ids", [])}
            if phase == "submitted":
                return {"status": "ok", "action": "observe", "task_id": task_id, "queue_path": str(output), "job": job,
                        "existing_song_ids": (state.get("current") or {}).get("existing_song_ids", [])}
            return {
                "status": "ok",
                "action": "execute",
                "task_id": task_id,
                "queue_path": str(output),
                "auto_create": submission_is_authorized(state),
                "existing_song_ids": (state.get("current") or {}).get("existing_song_ids", []),
                "job": job,
            }
        if not state.get("active") or state.get("stop_requested"):
            return {"status": "ok", "action": "idle", "message": "worker is stopped"}
        claimed = claim_next_for(state)
        if not claimed:
            if state.get("follow_future", True):
                state["last_error"] = None
                state["listener_status"] = "waiting_for_future_queue"
                write_state(state)
                return {"status": "ok", "action": "idle", "message": "listener is waiting for future queues"}
            state["active"] = False
            state["stop_requested"] = False
            state["last_error"] = None
            write_state(state)
            return {"status": "ok", "action": "idle", "message": "selected queues are empty"}
        output, job = claimed
        state["current"] = {
            "task_id": job["task_id"],
            "queue_path": str(output.resolve()),
            "job": job,
            "phase": "claimed",
            "claimed_at": now_iso(),
        }
        # A retryable form/readback error belongs to the previous item. Once
        # a fresh item is claimed, do not keep rendering that old message as
        # the current worker failure on the dashboard.
        state["last_error"] = None
        write_state(state)
        return {
            "status": "ok",
            "action": "execute",
            "task_id": job["task_id"],
            "queue_path": str(output.resolve()),
            "auto_create": submission_is_authorized(state),
            "job": job,
        }


def validate_current(task_id: str, queue_path: str) -> tuple[dict[str, Any], Path]:
    state = read_state()
    current = job_from_state(state)
    if not current or current[1] != task_id or str(current[0].resolve()) != str(Path(queue_path).resolve()):
        raise HTTPException(409, "浏览器回执与当前 Suno 队列条目不匹配")
    return state, current[0]


@app.post("/v1/browser/phase")
def phase(request: PhaseRequest) -> dict[str, Any]:
    with LOCK:
        state, _output = validate_current(request.task_id, request.queue_path)
        current = state["current"]
        current["phase"] = request.phase
        current["phase_at"] = now_iso()
        if request.phase == "filled":
            ids, _urls = clean_song_values(request.existing_song_ids, [])
            current["existing_song_ids"] = ids
        write_state(state)
        return {"status": "ok", "phase": request.phase}


@app.post("/v1/browser/result")
def result(request: ResultRequest) -> dict[str, Any]:
    with LOCK:
        state, output = validate_current(request.task_id, request.queue_path)
        ids, urls = clean_song_values(request.song_ids, request.song_urls)
        if not ids and not urls:
            raise HTTPException(400, "未收到可验证的 Suno 歌曲 ID 或官方歌曲链接")
        primary_id = ids[0] if ids else None
        primary_url = urls[0] if urls else None
        completed = sbg.complete(SimpleNamespace(
            output=str(output), task_id=request.task_id, song_id=primary_id,
            song_url=primary_url, result_file=None,
        ))
        receipt = {
            "schema": "suno_browser_result_v1",
            "task_id": request.task_id,
            "queue_path": str(output.resolve()),
            "song_ids": ids,
            "song_urls": urls,
            "primary_song_id": primary_id,
            "primary_song_url": primary_url,
            "observed_at": request.observed_at or now_iso(),
        }
        receipt["result_file"] = append_receipt(output, receipt)
        # sbg.complete() rebuilds the static dashboard before the receipt is
        # appended. Rebuild once more so generated_song_count is immediately
        # visible instead of remaining at zero until another task changes.
        try:
            sbg.refresh_dashboard(output)
        except Exception as exc:  # dashboard freshness must not lose a result
            state["dashboard_refresh_error"] = str(exc)
        state["last_result"] = receipt
        state["last_error"] = None
        state["current"] = None
        if state.get("stop_requested"):
            state["active"] = False
            state["stop_requested"] = False
        write_state(state)
        return {"status": "ok", "job_status": completed.get("job_status"), "receipt": receipt, "worker": status()}


@app.post("/v1/browser/error")
def error(request: ErrorRequest) -> dict[str, Any]:
    with LOCK:
        state, output = validate_current(request.task_id, request.queue_path)
        if request.action == "requeue":
            requeue_current(state, request.reason)
        else:
            command_args = SimpleNamespace(output=str(output), task_id=request.task_id, reason=request.reason)
            if request.action == "fail":
                sbg.fail_or_block(command_args, "failed")
            else:
                sbg.fail_or_block(command_args, "blocked")
            state["current"] = None
            state["active"] = False
            state["stop_requested"] = False
            state["auto_create"] = False
            state["submission_authorized"] = False
            state["last_error"] = request.reason
        write_state(state)
        return {"status": "ok", "worker": status()}


def prepare_autostart_listener() -> None:
    """Resume the persistent listener after a Windows logon.

    A previous explicit Start authorization is restored only when the worker
    was still active. A previous Stop revokes it, so login startup cannot
    silently re-enable a user-disabled submission flow.
    """
    with LOCK:
        state = read_state()
        if not bool(state.get("listener_enabled", False)):
            state.update({
                "active": False,
                "stop_requested": False,
                "auto_create": False,
                "submission_authorized": False,
                "listener_mode": "disabled",
            })
            write_state(state)
            return
        was_active = bool(state.get("active"))
        was_authorized = bool(state.get("submission_authorized", False))
        current = state.get("current")
        if isinstance(current, dict) and str(current.get("phase") or "claimed") in {"claimed", "filled"}:
            requeue_current(state, "worker restarted before Create; returned to queue")
        state.update({
            "active": True,
            "stop_requested": False,
            "auto_create": was_active and was_authorized,
            "submission_authorized": was_active and was_authorized,
            "scope": "latest",
            "follow_future": True,
            "task_ids": list(ALLOWED_TASK_IDS),
            "last_error": None,
            "listener_mode": "autostart",
        })
        write_state(state)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local sequential Suno browser worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--autostart-listener", action="store_true", help="resume the persistent listener after Windows logon")
    args = parser.parse_args()
    if args.autostart_listener:
        prepare_autostart_listener()
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
