"""Local Suno download queue for verified, user-generated songs.

The generation worker records verified Suno song links.  This worker turns
those links into two independent visible-browser download jobs per song:
MP3 Audio and Video.  The Chrome extension performs the clicks on the
visible Suno page; this service only owns local queue state and copies the
completed browser download into the task's ``suno-download/files`` folder.

No cookies, passwords, local storage, private Suno endpoints, or direct media
URLs are used.  The service must be explicitly started from the dashboard
before it can request a download.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import suno_batch_generator as sbg


RUNTIME_ROOT = Path("D:/projects/local/hotspot-music-runtime")
TASK_ROOT = RUNTIME_ROOT / "data" / "tasks"
STATE_PATH = RUNTIME_ROOT / "data" / "suno-download-worker-state.json"
GENERATION_DIR_NAME = "suno-generation"
GENERATION_STATUS_NAME = "suno-generation-status.json"
GENERATION_RECEIPT_NAME = "suno-browser-results.jsonl"
DOWNLOAD_DIR_NAME = "suno-download"
PLAN_NAME = "suno-download-plan.jsonl"
STATUS_NAME = "suno-download-status.json"
REPORT_NAME = "suno-download-report.md"
RESULTS_NAME = "suno-download-results.jsonl"
FILES_DIR_NAME = "files"
# Chrome may first place the browser download in its default/temporary
# location. The verified worker archive is always this fixed D: root.
ARCHIVE_ROOT = Path("D:/Suno歌曲下载")
ALLOWED_TASK_IDS = ("bilibili-daily", "douyin-curation", "weibo-hourly")
ALLOWED_STATUSES = {"queued", "running", "completed", "failed", "blocked"}
DOWNLOAD_LEASE_TIMEOUT_SEC = 45 * 60
SONG_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,127}$")
SONG_URL_RE = re.compile(r"^https://(?:www\.)?suno\.com/song/([A-Za-z0-9][A-Za-z0-9_-]{5,127})/?$")
LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def valid_song_url(value: Any) -> bool:
    return bool(SONG_URL_RE.fullmatch(str(value or "").strip()))


def song_id_from_url(value: Any) -> str:
    match = SONG_URL_RE.fullmatch(str(value or "").strip())
    return match.group(1) if match else ""


def canonical_song_url(song_id: str, song_url: Any = None) -> str:
    value = str(song_url or "").strip()
    if valid_song_url(value):
        return value.rstrip("/")
    if SONG_ID_RE.fullmatch(song_id):
        return f"https://suno.com/song/{song_id}"
    return ""


def task_ids_or_default(task_ids: list[str] | None) -> list[str]:
    selected = list(dict.fromkeys(task_ids or ALLOWED_TASK_IDS))
    invalid = [item for item in selected if item not in ALLOWED_TASK_IDS]
    if invalid:
        raise HTTPException(400, f"不允许的 Suno 下载任务: {', '.join(invalid)}")
    return selected


def is_generation_output(path: Path) -> bool:
    try:
        relative = path.resolve().relative_to(TASK_ROOT.resolve())
    except ValueError:
        return False
    return bool(relative.parts) and relative.parts[0] in ALLOWED_TASK_IDS and path.name == GENERATION_DIR_NAME


def is_download_output(path: Path) -> bool:
    return path.name == DOWNLOAD_DIR_NAME and is_generation_output(path.parent)


def safe_resolve_under(root: Path, value: Any) -> Path | None:
    """Resolve a worker-owned path without accepting traversal outside root."""
    try:
        candidate = Path(str(value)).resolve()
        candidate.relative_to(root.resolve())
        return candidate
    except (OSError, TypeError, ValueError):
        return None


def archive_download(source: Path, title: str, song_id: str, extension: str) -> Path:
    """Copy a verified browser download to the user's fixed D: archive."""
    target_dir = ARCHIVE_ROOT / song_id
    target = target_dir / f"{title}--{song_id[:12]}.{extension}"
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(source, target)
    except OSError as exc:
        raise HTTPException(500, f"保存到 D 盘歌曲目录失败: {exc}") from exc
    if not target.is_file() or target.stat().st_size <= 0:
        raise HTTPException(500, "D 盘归档文件为空")
    return target


def generation_candidates(task_ids: list[str], scope: Literal["latest", "all"]) -> list[Path]:
    result: list[Path] = []
    for task_id in task_ids:
        root = TASK_ROOT / task_id
        candidates: list[tuple[str, float, Path]] = []
        if root.exists():
            for status_path in root.rglob(GENERATION_STATUS_NAME):
                output = status_path.parent
                if not is_generation_output(output):
                    continue
                value = read_json(status_path, {})
                if not isinstance(value, dict) or value.get("schema") != "suno_generation_status_v1":
                    continue
                try:
                    modified = status_path.stat().st_mtime
                except OSError:
                    modified = 0.0
                candidates.append((str(value.get("updated_at") or ""), modified, output.resolve()))
        candidates.sort(key=lambda item: (item[0], item[1], str(item[2])), reverse=True)
        if scope == "latest" and candidates:
            result.append(candidates[0][2])
        else:
            result.extend(item[2] for item in candidates)
    return result


def completed_songs(generation_output: Path, song_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """Collect only verified song IDs/URLs from a generation queue."""
    songs: dict[str, dict[str, Any]] = {}

    def add(song_id: Any, song_url: Any, title: Any = None, source: str = "") -> None:
        raw_id = str(song_id or "").strip()
        raw_url = str(song_url or "").strip()
        resolved_id = song_id_from_url(raw_url) or (raw_id if SONG_ID_RE.fullmatch(raw_id) else "")
        resolved_url = canonical_song_url(resolved_id, raw_url)
        if not resolved_id or not resolved_url:
            return
        if song_ids and resolved_id not in song_ids:
            return
        row = songs.setdefault(resolved_id, {
            "song_id": resolved_id,
            "song_url": resolved_url,
            "title": "",
            "sources": [],
        })
        if title and not row["title"]:
            row["title"] = str(title).strip()
        if source and source not in row["sources"]:
            row["sources"].append(source)

    status = read_json(generation_output / GENERATION_STATUS_NAME, {})
    for group in status.get("prompt_groups") or []:
        if not isinstance(group, dict):
            continue
        group_title = group.get("title") or "Suno 生成歌曲"
        for variant in group.get("variants") or []:
            if not isinstance(variant, dict) or variant.get("status") != "completed":
                continue
            add(variant.get("song_id"), variant.get("song_url"), group_title, "generation_status")

    receipt_path = generation_output / GENERATION_RECEIPT_NAME
    if receipt_path.exists():
        for line in receipt_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            title = value.get("title") or value.get("name") or "Suno 生成歌曲"
            ids = value.get("song_ids") or []
            urls = value.get("song_urls") or []
            for index, url in enumerate(urls):
                add(ids[index] if index < len(ids) else "", url, title, "browser_receipt")
            for index, song_id in enumerate(ids):
                add(song_id, urls[index] if index < len(urls) else "", title, "browser_receipt")

    return sorted(songs.values(), key=lambda item: (item.get("title") or "", item["song_id"]))


def load_jobs(output: Path) -> list[dict[str, Any]]:
    path = output / PLAN_NAME
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("task_id"):
            result.append(value)
    return result


def sanitize_filename(value: Any, fallback: str) -> str:
    text = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", str(value or "")).strip(" .")
    text = re.sub(r"\s+", " ", text)
    return (text[:80] or fallback).strip(" .")


def make_plan(songs: list[dict[str, Any]], output: Path) -> list[dict[str, Any]]:
    previous = {str(item.get("task_id")): item for item in load_jobs(output)}
    created = now_iso()
    jobs: list[dict[str, Any]] = []
    for song in songs:
        for download_type, extension, label in (("mp3", "mp3", "MP3 Audio"), ("video", "mp4", "Video")):
            seed = "|".join((str(output.resolve()), song["song_id"], song["song_url"], download_type))
            task_id = "download-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
            old = previous.get(task_id, {})
            status = old.get("status") if old.get("status") in ALLOWED_STATUSES else "queued"
            jobs.append({
                "task_id": task_id,
                "song_id": song["song_id"],
                "song_url": song["song_url"],
                "title": song.get("title") or "Suno 生成歌曲",
                "download_type": download_type,
                "download_label": label,
                "extension": extension,
                "status": status,
                "backend": old.get("backend"),
                "download_id": old.get("download_id"),
                "download_file": old.get("download_file"),
                "source_filename": old.get("source_filename"),
                "bytes": old.get("bytes"),
                "error": old.get("error"),
                "created_at": old.get("created_at") or created,
                "updated_at": old.get("updated_at") or created,
                "started_at": old.get("started_at"),
                "completed_at": old.get("completed_at"),
            })
    return jobs


def counts(jobs: list[dict[str, Any]]) -> dict[str, int]:
    result = {status: 0 for status in sorted(ALLOWED_STATUSES)}
    for job in jobs:
        status = str(job.get("status") or "queued")
        result[status if status in result else "queued"] += 1
    result["total"] = len(jobs)
    return result


def build_status(jobs: list[dict[str, Any]], output: Path, generation_output: Path) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        grouped.setdefault(str(job.get("song_id") or "unknown"), []).append(job)
    songs = []
    for song_id, group in sorted(grouped.items()):
        group = sorted(group, key=lambda item: item.get("download_type") or "")
        c = counts(group)
        songs.append({
            "song_id": song_id,
            "song_url": group[0].get("song_url"),
            "title": group[0].get("title"),
            "target": len(group),
            "completed": c["completed"],
            "running": c["running"],
            "queued": c["queued"],
            "failed": c["failed"],
            "blocked": c["blocked"],
            "percent": round(c["completed"] / len(group) * 100) if group else 0,
            "downloads": [{
                "task_id": item["task_id"],
                "type": item.get("download_type"),
                "label": item.get("download_label"),
                "status": item.get("status"),
                "download_file": item.get("download_file"),
                "bytes": item.get("bytes"),
                "error": item.get("error"),
            } for item in group],
        })
    c = counts(jobs)
    status = "waiting_for_generation" if not jobs else "completed" if c["completed"] == c["total"] else "running" if c["running"] else "partial" if c["completed"] or c["failed"] or c["blocked"] else "queued"
    return {
        "schema": "suno_download_status_v1",
        "status": status,
        "generation_output": str(generation_output.resolve()),
        "output": str(output.resolve()),
        "song_count": len(songs),
        "target_download_count": c["total"],
        "completed_download_count": c["completed"],
        "counts": c,
        "percent": round(c["completed"] / c["total"] * 100) if c["total"] else 0,
        "updated_at": now_iso(),
        "songs": songs,
    }


def write_report(status: dict[str, Any], path: Path) -> None:
    c = status["counts"]
    lines = [
        "# Suno MP3 / Video 下载状态", "",
        "> 仅统计已验证的 Suno 歌曲；MP3 Audio 和 Video 各自独立记录。", "",
        "| 项目 | 数量 |", "|---|---:|",
        f"| 歌曲 | {status['song_count']} |",
        f"| 目标下载项 | {status['target_download_count']} |",
        f"| 已完成 | {c['completed']} |",
        f"| 运行中 | {c['running']} |",
        f"| 排队 | {c['queued']} |",
        f"| 失败 | {c['failed']} |",
        f"| 阻塞 | {c['blocked']} |", "",
    ]
    for index, song in enumerate(status["songs"], 1):
        lines += [f"## {index}. {song.get('title') or song['song_id']}", "", f"歌曲：{song.get('song_url')}", ""]
        for item in song["downloads"]:
            target = item.get("download_file") or "-"
            reason = f"；原因：{item['error']}" if item.get("error") else ""
            lines.append(f"- {item.get('label') or item.get('type')}：`{item.get('status')}`；{target}{reason}")
        lines.append("")
    atomic_write(path, "\n".join(lines) + "\n")


def refresh_dashboard(output: Path) -> None:
    try:
        relative = output.resolve().relative_to(TASK_ROOT.resolve())
        if len(relative.parts) < 2:
            return
        from task_dashboard import DEFAULT_DASHBOARD, build_dashboard
        build_dashboard(relative.parts[1], DEFAULT_DASHBOARD)
    except (ValueError, OSError, ImportError):
        return


def write_plan_and_status(jobs: list[dict[str, Any]], output: Path, generation_output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    atomic_write(output / PLAN_NAME, "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in jobs))
    manifest = [{
        "task_id": item["task_id"],
        "song_id": item["song_id"],
        "song_url": item["song_url"],
        "title": item["title"],
        "download_type": item["download_type"],
        "download_label": item["download_label"],
        "extension": item["extension"],
        "status": item["status"],
        "destination": "https://suno.com/song/",
    } for item in jobs]
    atomic_write(output / "suno-download-manifest.jsonl", "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in manifest))
    status = build_status(jobs, output, generation_output)
    write_json(output / STATUS_NAME, status)
    write_report(status, output / REPORT_NAME)
    refresh_dashboard(output)
    return status


def sync_queue(generation_output: Path, song_ids: set[str] | None = None) -> Path | None:
    generation_output = generation_output.resolve()
    if not is_generation_output(generation_output):
        return None
    songs = completed_songs(generation_output, song_ids)
    if not songs:
        return None
    output = generation_output / DOWNLOAD_DIR_NAME
    jobs = make_plan(songs, output)
    write_plan_and_status(jobs, output, generation_output)
    return output


def download_outputs(state: dict[str, Any]) -> list[Path]:
    result = []
    for raw in state.get("queue_paths") or []:
        path = Path(str(raw))
        if is_download_output(path):
            result.append(path)
    return result


def sync_state_queues(state: dict[str, Any]) -> bool:
    task_ids = task_ids_or_default(list(state.get("task_ids") or ALLOWED_TASK_IDS))
    scope = state.get("scope", "latest")
    if scope not in {"latest", "all"}:
        scope = "latest"
    # A one-shot run still needs to materialize the queues selected by
    # ``/start``.  ``follow_future`` only controls whether newly discovered
    # generation directories are added after startup; it must not disable
    # syncing the initial directories themselves.
    follow_future = bool(state.get("follow_future", True))
    all_paths = generation_candidates(task_ids, "all") if follow_future else []
    known = {str(Path(str(raw)).resolve()) for raw in (state.get("known_generation_paths") or []) if is_generation_output(Path(str(raw)))}
    selected: list[Path] = []
    selected_keys: set[str] = set()
    for raw in state.get("generation_paths") or []:
        path = Path(str(raw))
        if is_generation_output(path) and str(path.resolve()) not in selected_keys:
            selected.append(path.resolve())
            selected_keys.add(str(path.resolve()))
    changed = False
    if follow_future and not isinstance(state.get("known_generation_paths"), list):
        known = {str(path.resolve()) for path in all_paths}
        changed = True
    if follow_future:
        for path in all_paths:
            key = str(path.resolve())
            if key not in known:
                known.add(key)
                selected.append(path.resolve())
                selected_keys.add(key)
                changed = True
    state["known_generation_paths"] = sorted(known)
    state["generation_paths"] = [str(path) for path in selected]
    queue_paths: list[str] = []
    for path in selected:
        selected_song_ids = set(str(item) for item in (state.get("song_ids") or []) if SONG_ID_RE.fullmatch(str(item)))
        output = sync_queue(path, selected_song_ids or None)
        if output:
            queue_paths.append(str(output.resolve()))
    if queue_paths != list(state.get("queue_paths") or []):
        state["queue_paths"] = queue_paths
        changed = True
    if changed:
        state["last_queue_sync_at"] = now_iso()
    return changed


def default_state() -> dict[str, Any]:
    return {
        "schema": "suno_download_worker_state_v1",
        "active": False,
        "stop_requested": False,
        "auto_download": True,
        "download_authorized": False,
        "listener_enabled": False,
        "scope": "latest",
        "follow_future": True,
        "task_ids": list(ALLOWED_TASK_IDS),
        "song_ids": [],
        "generation_paths": [],
        "known_generation_paths": [],
        "queue_paths": [],
        "current": None,
        "last_result": None,
        "last_error": None,
        "last_queue_sync_at": None,
        "extension_seen_at": None,
        "extension_url": None,
        "updated_at": now_iso(),
    }


def read_state() -> dict[str, Any]:
    value = read_json(STATE_PATH, {})
    if not isinstance(value, dict) or value.get("schema") != "suno_download_worker_state_v1":
        return default_state()
    defaults = default_state()
    for key, item in defaults.items():
        value.setdefault(key, item)
    return value


def write_state(state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    write_json(STATE_PATH, state)


def extension_connected(state: dict[str, Any]) -> bool:
    raw = state.get("extension_seen_at")
    if not raw:
        return False
    try:
        seen = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    return (datetime.now().astimezone() - seen).total_seconds() <= 10


def download_is_authorized(state: dict[str, Any]) -> bool:
    return bool(state.get("active") and not state.get("stop_requested") and state.get("auto_download") and state.get("download_authorized"))


def load_current_job(state: dict[str, Any]) -> tuple[Path, dict[str, Any]] | None:
    current = state.get("current")
    if not isinstance(current, dict):
        return None
    output = Path(str(current.get("queue_path") or ""))
    job = current.get("job")
    if not is_download_output(output) or not isinstance(job, dict):
        return None
    return output, job


def claim_next(state: dict[str, Any]) -> tuple[Path, dict[str, Any]] | None:
    now_value = datetime.now().astimezone().timestamp()
    for output in download_outputs(state):
        jobs = load_jobs(output)
        candidates = [item for item in jobs if item.get("status") == "queued" and
                      float(item.get("retry_after_ts") or 0) <= now_value]
        if not candidates:
            continue
        # Keep the visible page moving to the next song after each primary
        # audio download.  The old song-first ordering selected the same
        # song's Video immediately after its MP3, so users saw the first song
        # page repeatedly and never reached the next song.  Finish the MP3
        # pass first, then process the queued Video pass.
        job = sorted(candidates, key=lambda item: (
            0 if str(item.get("download_type")) == "mp3" else 1,
            str(item.get("song_id")),
            str(item.get("download_type")),
        ))[0]
        job["status"] = "running"
        job["backend"] = "visible_browser"
        job["started_at"] = job.get("started_at") or now_iso()
        job["updated_at"] = now_iso()
        write_plan_and_status(jobs, output, Path(str(read_json(output / STATUS_NAME, {}).get("generation_output") or output.parent)))
        return output, job
    return None


def count_selected(state: dict[str, Any]) -> dict[str, int]:
    result = {key: 0 for key in ("queued", "running", "completed", "failed", "blocked", "total", "song_count", "completed_download_count")}
    song_ids: set[str] = set()
    for output in download_outputs(state):
        for job in load_jobs(output):
            status = str(job.get("status") or "queued")
            result[status if status in ALLOWED_STATUSES else "queued"] += 1
            result["total"] += 1
            if job.get("song_id"):
                song_ids.add(str(job["song_id"]))
    result["song_count"] = len(song_ids)
    result["completed_download_count"] = result["completed"]
    return result


class StartRequest(BaseModel):
    task_ids: list[str] | None = None
    scope: Literal["latest", "all"] = "latest"
    auto_download: bool = True
    follow_future: bool = True
    song_ids: list[str] | None = None


class HeartbeatRequest(BaseModel):
    url: str | None = None


class PhaseRequest(BaseModel):
    task_id: str
    queue_path: str
    phase: Literal["navigating", "menu_open", "preparing", "download_started", "observing"]
    download_id: str | None = None


class ResultRequest(BaseModel):
    task_id: str
    queue_path: str
    download_id: str | None = None
    filename: str | None = None
    file_path: str
    bytes_received: int = 0


class ErrorRequest(BaseModel):
    task_id: str
    queue_path: str
    reason: str
    action: Literal["block", "fail", "requeue"] = "block"


class RetryRequest(BaseModel):
    task_id: str
    queue_path: str


app = FastAPI(title="Hotspot Music Suno Download Worker", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["null", "http://127.0.0.1:8766", "http://localhost:8766"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "suno-download-worker", "updated_at": now_iso()}


@app.post("/v1/download/heartbeat")
def heartbeat(request: HeartbeatRequest) -> dict[str, Any]:
    with LOCK:
        state = read_state()
        state["extension_seen_at"] = now_iso()
        url = str(request.url or "")
        state["extension_url"] = url if url.startswith("https://suno.com/") or url.startswith("https://www.suno.com/") else None
        write_state(state)
        return {"status": "ok", "extension_connected": True, "seen_at": state["extension_seen_at"]}


@app.get("/v1/download/status")
def status() -> dict[str, Any]:
    with LOCK:
        state = read_state()
        recovered = recover_stuck_current(state)
        if state.get("active") and sync_state_queues(state):
            recovered = True
        if recovered:
            write_state(state)
        current = state.get("current") if isinstance(state.get("current"), dict) else None
        return {
            "status": "ok",
            "active": bool(state.get("active")),
            "stop_requested": bool(state.get("stop_requested")),
            "auto_download": bool(state.get("auto_download", True)),
            "download_authorized": bool(state.get("download_authorized", False)),
            "listener_enabled": bool(state.get("listener_enabled", state.get("active"))),
            "scope": state.get("scope", "latest"),
            "follow_future": bool(state.get("follow_future", True)),
            "task_ids": state.get("task_ids", list(ALLOWED_TASK_IDS)),
            "generation_queue_count": len(state.get("generation_paths") or []),
            "download_queue_count": len(download_outputs(state)),
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
            "updated_at": state.get("updated_at"),
        }


@app.post("/v1/download/start")
def start(request: StartRequest) -> dict[str, Any]:
    with LOCK:
        state = read_state()
        if state.get("current"):
            raise HTTPException(409, "当前已有 Suno 下载项正在执行")
        task_ids = task_ids_or_default(request.task_ids)
        song_ids = list(dict.fromkeys(str(item).strip() for item in (request.song_ids or []) if str(item).strip()))
        invalid_song_ids = [item for item in song_ids if not SONG_ID_RE.fullmatch(item)]
        if invalid_song_ids:
            raise HTTPException(400, f"无效的 Suno 歌曲 ID: {', '.join(invalid_song_ids)}")
        paths = generation_candidates(task_ids, request.scope)
        if not paths and not request.follow_future:
            raise HTTPException(404, "没有找到 B站、抖音梳理或微博的 Suno 生成队列")
        state.update({
            "active": True,
            "stop_requested": False,
            "auto_download": bool(request.auto_download),
            "download_authorized": bool(request.auto_download),
            "listener_enabled": True,
            "scope": request.scope,
            "follow_future": bool(request.follow_future),
            "task_ids": task_ids,
            "song_ids": song_ids,
            "generation_paths": [str(path.resolve()) for path in paths],
            "known_generation_paths": [str(path.resolve()) for path in generation_candidates(task_ids, "all")],
            "queue_paths": [],
            "current": None,
            "last_error": None,
            "listener_started_at": now_iso(),
        })
        sync_state_queues(state)
        write_state(state)
        return status()


@app.post("/v1/download/stop")
def stop() -> dict[str, Any]:
    with LOCK:
        state = read_state()
        current = load_current_job(state)
        # If Chrome is disconnected, it cannot acknowledge the abort message.
        # Keep a pre-download claim until the extension receives the abort
        # command. Clearing it here loses that command and makes the download
        # page/worker appear blocked. The extension's requeue acknowledgement
        # clears it; a later start can recover it if Chrome is disconnected.
        state["active"] = False
        state["stop_requested"] = True
        state["auto_download"] = False
        state["download_authorized"] = False
        state["listener_enabled"] = False
        if not state.get("current"):
            state["stop_requested"] = False
        write_state(state)
        return status()


@app.get("/v1/download/command")
def command() -> dict[str, Any]:
    with LOCK:
        state = read_state()
        recovered = recover_stuck_current(state)
        if state.get("active") and sync_state_queues(state):
            recovered = True
        if recovered:
            write_state(state)
        current = load_current_job(state)
        if current:
            output, job = current
            phase = str((state.get("current") or {}).get("phase") or "claimed")
            if state.get("stop_requested") and phase in {"claimed", "navigating", "menu_open", "preparing"}:
                return {"status": "ok", "action": "abort", "task_id": job["task_id"], "queue_path": str(output), "job": job}
            if phase in {"download_started", "observing"}:
                return {"status": "ok", "action": "observe", "task_id": job["task_id"], "queue_path": str(output), "job": job}
            return {
                "status": "ok",
                "action": "execute",
                "task_id": job["task_id"],
                "queue_path": str(output),
                "auto_download": download_is_authorized(state),
                "job": job,
            }
        if not state.get("active") or state.get("stop_requested"):
            return {"status": "ok", "action": "idle", "message": "download worker is stopped"}
        claimed = claim_next(state)
        if not claimed:
            if state.get("follow_future", True):
                state["last_error"] = None
                state["listener_status"] = "waiting_for_generated_songs"
                write_state(state)
                return {"status": "ok", "action": "idle", "message": "waiting for verified Suno songs"}
            state["active"] = False
            state["download_authorized"] = False
            state["auto_download"] = False
            write_state(state)
            return {"status": "ok", "action": "idle", "message": "download queues are empty"}
        output, job = claimed
        state["current"] = {
            "task_id": job["task_id"],
            "queue_path": str(output.resolve()),
            "job": job,
            "phase": "claimed",
            "claimed_at": now_iso(),
        }
        # Clear a previous stop/requeue message as soon as a new download
        # item is actually claimed. It must not remain a red current error.
        state["last_error"] = None
        write_state(state)
        return {
            "status": "ok",
            "action": "execute",
            "task_id": job["task_id"],
            "queue_path": str(output.resolve()),
            "auto_download": download_is_authorized(state),
            "job": job,
        }


def validate_current(request_task_id: str, queue_path: str) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    state = read_state()
    current = load_current_job(state)
    output = Path(queue_path).resolve()
    if not current or current[1].get("task_id") != request_task_id or current[0].resolve() != output:
        raise HTTPException(409, "浏览器回执与当前 Suno 下载项不匹配")
    return state, current[0], current[1]


def save_current_job(output: Path, task_id: str, update: dict[str, Any]) -> dict[str, Any]:
    jobs = load_jobs(output)
    for job in jobs:
        if job.get("task_id") == task_id:
            job.update(update)
            job["updated_at"] = now_iso()
            generation_output = Path(str(read_json(output / STATUS_NAME, {}).get("generation_output") or output.parent))
            write_plan_and_status(jobs, output, generation_output)
            return job
    raise HTTPException(404, f"找不到下载任务: {task_id}")


def recover_stuck_current(state: dict[str, Any]) -> bool:
    """Release a browser lease that can no longer make progress.

    A browser-side error used to be written into the plan while the worker's
    ``current`` pointer stayed populated.  That made every later command
    observe the same item forever.  Only a recorded error or an expired lease
    is eligible here; an actively preparing Video download is left alone.
    """
    current = load_current_job(state)
    if not current:
        return False
    output, job = current
    phase = str((state.get("current") or {}).get("phase") or "claimed")
    error_text = str(job.get("error") or "").strip()
    stamp = (state.get("current") or {}).get("phase_at") or job.get("updated_at") or job.get("started_at")
    try:
        age = datetime.now().astimezone().timestamp() - datetime.fromisoformat(str(stamp)).timestamp()
    except (TypeError, ValueError):
        age = DOWNLOAD_LEASE_TIMEOUT_SEC + 1
    if not error_text and age < DOWNLOAD_LEASE_TIMEOUT_SEC:
        return False
    attempts = int(job.get("retry_count") or 0) + 1
    jobs = load_jobs(output)
    for item in jobs:
        if item.get("task_id") != job.get("task_id"):
            continue
        if attempts >= 5:
            item.update({
                "status": "blocked", "backend": "visible_browser",
                "started_at": None, "retry_count": attempts,
                "retry_after_ts": None,
                "error": f"自动释放下载租约：{error_text or '超过最大执行时间'}",
            })
        else:
            item.update({
                "status": "queued", "backend": None,
                "started_at": None, "retry_count": attempts,
                "retry_after_ts": datetime.now().astimezone().timestamp() + 5,
                "download_id": None, "download_file": None,
                "source_filename": None, "bytes": None,
                "completed_at": None,
                "error": f"自动释放下载租约：{error_text or '超过最大执行时间'}",
            })
        item["updated_at"] = now_iso()
        break
    else:
        state["current"] = None
        state["last_error"] = "当前下载项已不存在，已释放下载租约"
        return True
    generation_output = Path(str(read_json(output / STATUS_NAME, {}).get("generation_output") or output.parent))
    write_plan_and_status(jobs, output, generation_output)
    state["current"] = None
    state["last_error"] = None if attempts < 5 else jobs[-1].get("error")
    return True


@app.post("/v1/download/phase")
def phase(request: PhaseRequest) -> dict[str, Any]:
    with LOCK:
        recover_stuck_current(read_state())
        state, output, job = validate_current(request.task_id, request.queue_path)
        current = state["current"]
        current["phase"] = request.phase
        current["phase_at"] = now_iso()
        if request.download_id:
            current["download_id"] = request.download_id
            if isinstance(current.get("job"), dict):
                current["job"]["download_id"] = request.download_id
        save_current_job(output, request.task_id, {"download_id": request.download_id or job.get("download_id")})
        write_state(state)
        return {"status": "ok", "phase": request.phase}


@app.post("/v1/download/result")
def result(request: ResultRequest) -> dict[str, Any]:
    with LOCK:
        state, output, job = validate_current(request.task_id, request.queue_path)
        source = Path(request.file_path)
        # Chrome reports an absolute filename.  It must be a regular local
        # file; the extension is not allowed to submit a URL or a directory.
        if not source.is_absolute() or not source.is_file():
            raise HTTPException(400, f"浏览器下载文件不存在: {request.file_path}")
        try:
            source_size = source.stat().st_size
        except OSError as exc:
            raise HTTPException(400, f"无法读取浏览器下载文件: {exc}") from exc
        if source_size <= 0:
            raise HTTPException(400, "浏览器下载文件为空")
        expected_extension = ".mp4" if job.get("download_type") == "video" else ".mp3"
        # Chrome can expose a completed Suno download with a generated
        # temporary suffix. The magic-byte check below remains mandatory and
        # rejects cover images or unrelated files.
        if source.suffix.lower() not in {expected_extension, ".tmp", ".crdownload"}:
            raise HTTPException(400, f"浏览器文件类型与任务不匹配: 需要 {expected_extension}")
        with source.open("rb") as handle:
            header = handle.read(16)
        if expected_extension == ".mp3" and not (header.startswith(b"ID3") or header[:2] in {b"\\xff\\xfb", b"\\xff\\xf3", b"\\xff\\xf2"}):
            raise HTTPException(400, "浏览器文件不是有效 MP3，拒绝保存")
        if expected_extension == ".mp4" and header[4:8] != b"ftyp":
            raise HTTPException(400, "浏览器文件不是有效 MP4，拒绝保存")
        song_id = str(job.get("song_id") or "song")
        title = sanitize_filename(job.get("title"), song_id)
        extension = "mp4" if job.get("download_type") == "video" else "mp3"
        target_dir = output / FILES_DIR_NAME / song_id
        target = target_dir / f"{title}--{song_id[:12]}.{extension}"
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source, target)
        except OSError as exc:
            raise HTTPException(500, f"保存 Suno 下载文件失败: {exc}") from exc
        if not target.is_file() or target.stat().st_size <= 0:
            raise HTTPException(500, "保存后的 Suno 下载文件为空")
        archive = archive_download(source, title, song_id, extension)
        updated = save_current_job(output, request.task_id, {
            "status": "completed",
            "download_id": request.download_id or job.get("download_id"),
            "download_file": str(target.resolve()),
            "archive_file": str(archive.resolve()),
            "source_filename": request.filename or source.name,
            "bytes": int(target.stat().st_size),
            "error": None,
            "completed_at": now_iso(),
        })
        receipt = {
            "schema": "suno_download_result_v1",
            "task_id": request.task_id,
            "queue_path": str(output.resolve()),
            "song_id": job.get("song_id"),
            "song_url": job.get("song_url"),
            "title": job.get("title"),
            "download_type": job.get("download_type"),
            "download_id": request.download_id or job.get("download_id"),
            "source_filename": request.filename or source.name,
            "download_file": str(target.resolve()),
            "archive_file": str(archive.resolve()),
            "bytes": int(target.stat().st_size),
            "completed_at": now_iso(),
        }
        receipt_path = output / RESULTS_NAME
        previous = receipt_path.read_text(encoding="utf-8", errors="replace") if receipt_path.exists() else ""
        atomic_write(receipt_path, previous + json.dumps(receipt, ensure_ascii=False) + "\n")
        state["last_result"] = receipt
        state["last_error"] = None
        state["current"] = None
        if state.get("stop_requested"):
            state["active"] = False
            state["stop_requested"] = False
        write_state(state)
        return {"status": "ok", "job_status": updated.get("status"), "receipt": receipt, "worker": status()}


@app.post("/v1/download/error")
def error(request: ErrorRequest) -> dict[str, Any]:
    with LOCK:
        state, output, _job = validate_current(request.task_id, request.queue_path)
        # A browser-side USER_CANCELED can be transient (for example, a
        # save dialog was dismissed). Requeue it without silently disabling
        # the user's still-active download authorization. An explicit
        # dashboard stop clears listener_enabled/download_authorized, so it
        # remains stopped.
        resume_authorized = bool(
            state.get("listener_enabled") and
            state.get("download_authorized") and
            not state.get("stop_requested")
        )
        if request.action == "requeue":
            current_attempts = int(_job.get("retry_count") or 0) + 1
            # Never spin on a page that is still remounting or has not exposed
            # the visible menu yet. A short backoff prevents repeated clicks;
            # after several consecutive failures, leave the item blocked for
            # manual inspection instead of downloading indefinitely.
            if current_attempts >= 5:
                save_current_job(output, request.task_id, {
                    "status": "blocked", "started_at": None,
                    "retry_count": current_attempts, "retry_after_ts": None,
                    "error": f"连续重试 {current_attempts} 次后停止：{request.reason}",
                })
                request.action = "block"
            else:
                save_current_job(output, request.task_id, {
                    "status": "queued", "started_at": None,
                    "retry_count": current_attempts,
                    "retry_after_ts": datetime.now().astimezone().timestamp() + 45,
                    "error": request.reason,
                    # A requeue must not inherit the previous Chrome
                    # download binding. Reusing that ID makes the extension
                    # observe an old item and can leave the next attempt
                    # stuck at claimed/running.
                    "download_id": None, "download_file": None,
                    "source_filename": None, "bytes": None,
                    "completed_at": None, "backend": None,
                })
        else:
            target_status = "failed" if request.action == "fail" else "blocked"
            save_current_job(output, request.task_id, {"status": target_status, "error": request.reason})
        state["current"] = None
        state["last_error"] = request.reason
        # A failure or a blocked item belongs to that item only.  The user
        # explicitly authorized continuous downloading, so keep the listener
        # alive and let the next queued MP3/Video item proceed.  Only the
        # explicit /stop endpoint may withdraw that authorization.
        state["active"] = resume_authorized
        state["stop_requested"] = False
        if resume_authorized and request.action == "requeue":
            state["last_error"] = None
        write_state(state)
        return {"status": "ok", "worker": status()}


@app.post("/v1/download/retry")
def retry(request: RetryRequest) -> dict[str, Any]:
    """Requeue one item after a transient visible-menu error."""
    with LOCK:
        output = safe_resolve_under(TASK_ROOT, request.queue_path)
        if output is None or not is_download_output(output):
            raise HTTPException(400, "无效的 Suno 下载队列路径")
        jobs = load_jobs(output)
        found = False
        for job in jobs:
            if job.get("task_id") == request.task_id:
                if job.get("status") == "completed":
                    raise HTTPException(409, "该下载项已经完成")
                job.update({"status": "queued", "backend": None, "download_id": None,
                            "download_file": None, "source_filename": None,
                            "bytes": None, "error": None, "started_at": None,
                            "completed_at": None, "updated_at": now_iso()})
                found = True
                break
        if not found:
            raise HTTPException(404, "找不到下载任务")
        state = read_state()
        state.update({"active": True, "stop_requested": False, "auto_download": True,
                      "download_authorized": True, "listener_enabled": True,
                      "last_error": None})
        write_plan_and_status(jobs, output, Path(str(read_json(output / STATUS_NAME, {}).get("generation_output") or output.parent)))
        write_state(state)
        return status()


def prepare_autostart_listener() -> None:
    with LOCK:
        state = read_state()
        if not state.get("listener_enabled", False):
            state["active"] = False
            state["stop_requested"] = False
            state["auto_download"] = False
            state["download_authorized"] = False
            write_state(state)
            return
        was_active = bool(state.get("active"))
        was_authorized = bool(state.get("download_authorized"))
        state["active"] = True
        state["stop_requested"] = False
        state["auto_download"] = was_active and was_authorized
        state["download_authorized"] = was_active and was_authorized
        state["follow_future"] = True
        write_state(state)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Suno MP3/Video download worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--autostart-listener", action="store_true")
    args = parser.parse_args()
    if args.autostart_listener:
        prepare_autostart_listener()
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
