"""Independent Suno batch-generation queue.

This module turns local ``suno-prompts.jsonl`` files into an idempotent queue.
Each prompt produces exactly six separately trackable generation jobs.  The
queue is deliberately backend-neutral: an explicitly configured API backend
may submit requests, while the browser backend exposes a small claim/complete
protocol for a visible, already-authorized Suno page.

No cookies, passwords, local storage, private endpoints, CAPTCHA bypass, or
copyrighted source lyrics are read by this module.  It only consumes the
creative packages already written by the local pipeline.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse


RUNTIME_ROOT = Path("D:/projects/local/hotspot-music-runtime")
DEFAULT_TASK_ROOT = RUNTIME_ROOT / "data" / "tasks"
PLAN_NAME = "suno-generation-plan.jsonl"
STATUS_NAME = "suno-generation-status.json"
REPORT_NAME = "suno-generation-report.md"
MANIFEST_NAME = "suno-browser-queue.jsonl"
RESULTS_DIR_NAME = "suno-results"
ALLOWED_STATUSES = {"queued", "running", "completed", "failed", "blocked"}
PROMPT_FILE_NAMES = {"suno-prompts.jsonl", "batch-suno-prompts.jsonl"}
SCHEDULED_SUNO_TASK_IDS = {"bilibili-daily", "douyin-curation", "weibo-hourly"}
PROMPT_FIELDS = ("suno_style_prompt", "suno_prompt", "style_prompt", "style")
LYRICS_FIELDS = ("generated_lyrics", "lyrics", "lyrics_text")
SONG_ID_FIELDS = ("song_id", "clip_id", "track_id", "audio_id", "songId", "clipId", "trackId")
SONG_URL_FIELDS = ("song_url", "audio_url", "video_url", "download_url", "mp4_url", "file_url", "page_url", "permalink")
REQUEST_ID_FIELDS = ("request_id", "generation_id", "job_id", "task_id", "run_id")
STATUS_URL_FIELDS = ("status_url", "poll_url", "result_status_url")
SUCCESS_STATUSES = {"completed", "complete", "succeeded", "success", "done", "finished"}
PENDING_STATUSES = {"queued", "pending", "running", "processing", "in_progress", "in-progress", "submitted"}
FAILED_STATUSES = {"failed", "failure", "error", "cancelled", "canceled", "rejected"}
# Keep both fields comfortably below the page counter's effective boundary.
# Suno may count formatting/line endings and can disable Create near 5000.
# The browser extension applies the same limits again immediately before
# visible submission, while the original creative package remains unchanged.
MAX_SUNO_LYRICS_CHARS = 3200
MAX_SUNO_STYLE_CHARS = 600


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def suno_safe_text(value: Any, limit: int) -> str:
    """Bound visible Suno text without changing the source prompt file."""
    text = text_value(value)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # Prefer a complete line/section boundary, then fall back to a hard cap.
    boundary = max(cut.rfind("\n\n"), cut.rfind("\n"))
    if boundary >= int(limit * 0.7):
        cut = cut[:boundary].rstrip()
    result = cut.rstrip()
    return result if len(result) <= limit else result[:limit].rstrip()


def first_value(row: dict[str, Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        value = text_value(row.get(field))
        if value:
            return value
    creative = row.get("creative")
    if isinstance(creative, dict):
        for field in fields:
            value = text_value(creative.get(field))
            if value:
                return value
        if fields == PROMPT_FIELDS:
            fusion = creative.get("fusion")
            if isinstance(fusion, dict):
                value = text_value(fusion.get("suno_style_prompt"))
                if value:
                    return value
    return ""


def prompt_files(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path] if input_path.name in PROMPT_FILE_NAMES else []
    if not input_path.exists():
        raise FileNotFoundError(f"input does not exist: {input_path}")
    result = []
    for path in sorted(input_path.rglob("*.jsonl")):
        if RESULTS_DIR_NAME in path.parts or "suno-generation" in path.parts:
            continue
        if path.name in PROMPT_FILE_NAMES:
            result.append(path)
    return result


def canonical_prompt_id(source: Path, line_number: int, row: dict[str, Any]) -> str:
    payload = "\n".join((str(source.resolve()), str(line_number), first_value(row, PROMPT_FIELDS), first_value(row, LYRICS_FIELDS), text_value(row.get("title"))))
    return "prompt-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load_prompts(input_path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    rows = []
    for source in prompt_files(input_path):
        try:
            lines = source.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(lines, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            style = suno_safe_text(first_value(row, PROMPT_FIELDS), MAX_SUNO_STYLE_CHARS)
            lyrics = suno_safe_text(first_value(row, LYRICS_FIELDS), MAX_SUNO_LYRICS_CHARS)
            if not style and not lyrics:
                continue
            title = text_value(row.get("title")) or text_value(row.get("name")) or f"热点创作 {line_number}"
            prompt_id = canonical_prompt_id(source, line_number, row)
            rows.append({
                "prompt_id": prompt_id,
                "source_file": str(source.resolve()),
                "source_line": line_number,
                "rank": row.get("rank"),
                "title": title,
                "style_prompt": style,
                "lyrics": lyrics,
                "source_refs": row.get("source_refs") or row.get("sources") or [],
                "input_record": row,
            })
            if limit and len(rows) >= limit:
                return rows
    return rows


def default_output(input_path: Path) -> Path:
    if input_path.is_file():
        return input_path.parent / "suno-generation"
    return input_path / "suno-generation"


def scheduled_suno_task_enabled(path: Path, task_root: Path | None = None) -> bool:
    """Whether a path under the scheduled task root may enter Suno.

    Direct library calls such as ``plan`` and ``create_queue`` remain generic
    for other authorized producers.  The bulk task operations are stricter:
    under ``data/tasks`` only Bilibili, Douyin curation, and Weibo are allowed
    to create or process scheduled Suno queues.
    """
    root = Path(task_root) if task_root is not None else DEFAULT_TASK_ROOT
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return True
    return bool(relative.parts) and relative.parts[0] in SCHEDULED_SUNO_TASK_IDS


def task_id(prompt_id: str, variant: int) -> str:
    return f"{prompt_id}-v{variant:02d}"


def load_existing(output: Path) -> dict[str, dict[str, Any]]:
    existing = {}
    path = output / PLAN_NAME
    if not path.exists():
        return existing
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("task_id"):
            existing[str(item["task_id"])] = item
    return existing


def make_plan(prompts: list[dict[str, Any]], output: Path, variants: int = 6) -> list[dict[str, Any]]:
    previous = load_existing(output)
    created = now_iso()
    jobs = []
    for prompt in prompts:
        for variant in range(1, variants + 1):
            job_id = task_id(prompt["prompt_id"], variant)
            old = previous.get(job_id, {})
            status = old.get("status") if old.get("status") in ALLOWED_STATUSES else "queued"
            jobs.append({
                "task_id": job_id,
                "prompt_id": prompt["prompt_id"],
                "variant_index": variant,
                "variant_total": variants,
                "title": prompt["title"],
                "style_prompt": suno_safe_text(prompt["style_prompt"], MAX_SUNO_STYLE_CHARS),
                "lyrics": suno_safe_text(prompt["lyrics"], MAX_SUNO_LYRICS_CHARS),
                "source_file": prompt["source_file"],
                "source_line": prompt["source_line"],
                "rank": prompt.get("rank"),
                "source_refs": prompt.get("source_refs") or [],
                "status": status,
                "backend": old.get("backend"),
                "song_id": old.get("song_id"),
                "song_url": old.get("song_url"),
                "result_file": old.get("result_file"),
                "api_request_id": old.get("api_request_id"),
                "api_status_url": old.get("api_status_url"),
                "api_status": old.get("api_status"),
                "result_kind": old.get("result_kind"),
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


def build_status(jobs: list[dict[str, Any]], output: Path, input_path: Path, variants: int) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        grouped.setdefault(str(job["prompt_id"]), []).append(job)
    groups = []
    for prompt_id, group in grouped.items():
        group = sorted(group, key=lambda item: item["variant_index"])
        c = counts(group)
        groups.append({
            "prompt_id": prompt_id,
            "title": group[0].get("title"),
            "source_file": group[0].get("source_file"),
            "source_line": group[0].get("source_line"),
            "target": variants,
            "completed": c["completed"],
            "running": c["running"],
            "failed": c["failed"],
            "blocked": c["blocked"],
            "queued": c["queued"],
            "percent": round(c["completed"] / variants * 100) if variants else 0,
            "variants": [{
                "task_id": item["task_id"],
                "variant_index": item["variant_index"],
                "status": item["status"],
                "song_id": item.get("song_id"),
                "song_url": item.get("song_url"),
                "result_file": item.get("result_file"),
                "error": item.get("error"),
            } for item in group],
        })
    c = counts(jobs)
    return {
        "schema": "suno_generation_status_v1",
        "status": "completed" if c["total"] and c["completed"] == c["total"] else "running" if c["running"] else "partial" if c["completed"] or c["failed"] or c["blocked"] else "queued",
        "input": str(input_path.resolve()),
        "output": str(output.resolve()),
        "variants_per_prompt": variants,
        "prompt_count": len(groups),
        "target_song_count": c["total"],
        "counts": c,
        "percent": round(c["completed"] / c["total"] * 100) if c["total"] else 0,
        "updated_at": now_iso(),
        "prompt_groups": groups,
    }


def write_plan_and_status(jobs: list[dict[str, Any]], output: Path, input_path: Path, variants: int) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    atomic_write(output / PLAN_NAME, "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in jobs))
    manifest_rows = []
    for item in jobs:
        manifest_rows.append({
            "task_id": item["task_id"],
            "prompt_id": item["prompt_id"],
            "variant_index": item["variant_index"],
            "variant_total": item.get("variant_total", variants),
            "title": item.get("title"),
            "style_prompt": item.get("style_prompt"),
            "lyrics": item.get("lyrics"),
            "status": item.get("status", "queued"),
            "destination": "https://suno.com/create",
            "source_file": item.get("source_file"),
            "source_line": item.get("source_line"),
        })
    atomic_write(output / MANIFEST_NAME, "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in manifest_rows))
    status = build_status(jobs, output, input_path, variants)
    status["browser_manifest"] = str((output / MANIFEST_NAME).resolve())
    write_json(output / STATUS_NAME, status)
    write_report(status, output / REPORT_NAME)
    refresh_dashboard(output)
    return status


def refresh_dashboard(output: Path) -> None:
    """Refresh the static dashboard when a task queue changes."""
    try:
        relative = output.resolve().relative_to((RUNTIME_ROOT / "data" / "tasks").resolve())
        parts = relative.parts
        if len(parts) < 2:
            return
        from task_dashboard import DEFAULT_DASHBOARD, build_dashboard
        build_dashboard(parts[1], DEFAULT_DASHBOARD)
    except (ValueError, OSError, ImportError):
        # Queues outside data/tasks are valid library use and need no dashboard.
        return


def create_queue(input_path: Path, output: Path | None = None, variants: int = 6,
                 limit: int | None = None) -> dict[str, Any]:
    """Create/update a queue for a producer that already wrote JSONL prompts.

    This small function is the integration point for other Skills.  It keeps
    the queue beside the source run and preserves existing receipts on rerun.
    """
    if variants < 1:
        raise ValueError("variants must be at least 1")
    input_path = Path(input_path)
    output = Path(output) if output else default_output(input_path)
    prompts = load_prompts(input_path, limit)
    if not prompts:
        raise ValueError(f"no usable Suno prompts found: {input_path}")
    jobs = make_plan(prompts, output, variants)
    return write_plan_and_status(jobs, output, input_path, variants)


def queue_result(status: dict[str, Any], output: Path) -> dict[str, Any]:
    """Return a compact integration result without embedding all prompt groups."""
    counts_value = status.get("counts") if isinstance(status.get("counts"), dict) else {}
    return {
        "status": "ok",
        "queue_status": status.get("status"),
        "output": str(output.resolve()),
        "plan": str((output / PLAN_NAME).resolve()),
        "status_path": str((output / STATUS_NAME).resolve()),
        "report": str((output / REPORT_NAME).resolve()),
        "browser_manifest": str((output / MANIFEST_NAME).resolve()),
        "prompt_count": int(status.get("prompt_count") or 0),
        "variants_per_prompt": int(status.get("variants_per_prompt") or 6),
        "target_song_count": int(status.get("target_song_count") or 0),
        "counts": {key: int(counts_value.get(key) or 0)
                   for key in ("queued", "running", "completed", "failed", "blocked", "total")},
    }


def write_report(status: dict[str, Any], path: Path) -> None:
    c = status["counts"]
    lines = [
        "# Suno 批量生成状态", "",
        f"> 每条提示词目标生成 {status['variants_per_prompt']} 首；当前只统计本地队列回执，不把排队误报为已生成。", "",
        "| 项目 | 数量 |", "|---|---:|",
        f"| 提示词 | {status['prompt_count']} |",
        f"| 目标歌曲 | {status['target_song_count']} |",
        f"| 已完成 | {c['completed']} |",
        f"| 运行中 | {c['running']} |",
        f"| 排队 | {c['queued']} |",
        f"| 失败 | {c['failed']} |",
        f"| 阻塞 | {c['blocked']} |", "",
        "## 提示词分组", "",
    ]
    for index, group in enumerate(status["prompt_groups"], 1):
        lines += [f"### {index}. {group.get('title') or group['prompt_id']}", "", f"进度：{group['completed']}/{group['target']}（{group['percent']}%）", ""]
        for variant in group["variants"]:
            link = variant.get("song_url") or variant.get("result_file") or "-"
            error = f"；原因：{variant['error']}" if variant.get("error") else ""
            lines.append(f"- 变体 {variant['variant_index']:02d}：`{variant['status']}`；{link}{error}")
        lines.append("")
    atomic_write(path, "\n".join(lines) + "\n")


def plan(args: argparse.Namespace) -> dict[str, Any]:
    input_path = Path(args.input)
    output = Path(args.output) if args.output else default_output(input_path)
    prompts = load_prompts(input_path, args.limit)
    if not prompts:
        raise ValueError("no usable Suno prompts found")
    jobs = make_plan(prompts, output, args.variants)
    status = write_plan_and_status(jobs, output, input_path, args.variants)
    return {"status": "ok", **status, "plan": str(output / PLAN_NAME), "report": str(output / REPORT_NAME)}


def plan_all(args: argparse.Namespace) -> dict[str, Any]:
    """Create queues beside every allowed scheduled prompt file.

    Keeping queues beside their source run is important for the dashboard:
    each daily/hourly task can show its own six-variant progress without
    mixing results from unrelated runs.  Metadata-only task directories are
    skipped even if an old prompt file remains there.
    """
    root = Path(args.input)
    files = prompt_files(root)
    if not files:
        raise ValueError("no suno-prompts.jsonl files found")
    results = []
    for source in files:
        if not scheduled_suno_task_enabled(source, task_root=root):
            continue
        output = source.parent / "suno-generation"
        prompts = load_prompts(source, args.limit)
        if not prompts:
            continue
        jobs = make_plan(prompts, output, args.variants)
        status = write_plan_and_status(jobs, output, source, args.variants)
        results.append({"source": str(source.resolve()), "output": str(output.resolve()), **status})
    if not results:
        raise ValueError("prompt files were found but none contained usable prompts")
    return {"status": "ok", "queue_count": len(results), "queues": results}


def load_jobs(output: Path) -> list[dict[str, Any]]:
    path = output / PLAN_NAME
    if not path.exists():
        raise FileNotFoundError(f"queue not found: {path}; run plan first")
    jobs = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("task_id"):
            jobs.append(item)
    return jobs


def save_jobs(jobs: list[dict[str, Any]], output: Path, input_path: Path | None = None) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    variants = max((int(item.get("variant_total") or 6) for item in jobs), default=6)
    if input_path is None:
        current = read_json(output / STATUS_NAME, {}) or {}
        input_path = Path(str(current.get("input") or output))
    return write_plan_and_status(jobs, output, input_path, variants)


def find_job(jobs: list[dict[str, Any]], task_id_value: str) -> dict[str, Any]:
    for job in jobs:
        if job.get("task_id") == task_id_value:
            return job
    raise ValueError(f"unknown task_id: {task_id_value}")


def mutate_job(args: argparse.Namespace, status: str) -> dict[str, Any]:
    output = Path(args.output)
    jobs = load_jobs(output)
    job = find_job(jobs, args.task_id)
    song_id_arg = getattr(args, "song_id", None)
    song_url_arg = getattr(args, "song_url", None)
    result_file_arg = getattr(args, "result_file", None)
    reason_arg = getattr(args, "reason", None)
    current_status = str(job.get("status") or "queued")
    if status == "running" and current_status not in {"queued", "running"}:
        raise ValueError(f"task {args.task_id} is already {current_status}")
    if status == "completed" and current_status not in {"running", "completed"}:
        raise ValueError(f"task {args.task_id} must be running before completion")
    if status == "completed" and not any((song_id_arg, song_url_arg, result_file_arg,
                                          job.get("song_id"), job.get("song_url"), job.get("result_file"))):
        raise ValueError("complete requires a verified song_id, song_url, or result_file")
    result_file = result_file_arg or job.get("result_file")
    if status == "completed" and result_file and not (song_id_arg or song_url_arg or job.get("song_id") or job.get("song_url")):
        result_path = Path(result_file)
        if not result_path.is_file() or result_path.stat().st_size <= 0:
            raise ValueError(f"result_file does not exist or is empty: {result_file}")
    job["status"] = status
    job["updated_at"] = now_iso()
    if status == "running":
        job["started_at"] = job.get("started_at") or now_iso()
        job["backend"] = args.backend or job.get("backend") or "browser"
        job["error"] = None
    elif status == "completed":
        job["completed_at"] = now_iso()
        job["song_id"] = song_id_arg or job.get("song_id")
        job["song_url"] = song_url_arg or job.get("song_url")
        job["result_file"] = result_file
        job["error"] = None
    else:
        job["error"] = reason_arg or job.get("error") or "未提供原因"
    result = save_jobs(jobs, output)
    return {"status": "ok", "task_id": args.task_id, "job_status": status, "queue": result}


def next_job(args: argparse.Namespace) -> dict[str, Any]:
    output = Path(args.output)
    jobs = load_jobs(output)
    candidates = [item for item in jobs if item.get("status") == "queued"]
    if args.prompt_id:
        candidates = [item for item in candidates if item.get("prompt_id") == args.prompt_id]
    if not candidates:
        return {"status": "empty", "message": "没有排队中的 Suno 变体", "queue": read_json(output / STATUS_NAME, {})}
    item = sorted(candidates, key=lambda row: (str(row.get("prompt_id")), int(row.get("variant_index") or 0)))[0]
    return {"status": "ok", "job": item, "next_command": f"python suno_batch_generator.py claim --output \"{output}\" --task-id {item['task_id']}"}


def claim_next(args: argparse.Namespace) -> dict[str, Any]:
    """Atomically claim the next queued item for another local worker."""
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    lock_path = output / ".suno-generation.lock"
    try:
        lock_handle = lock_path.open("x", encoding="utf-8")
    except FileExistsError:
        return {"status": "busy", "message": "队列正在被另一个本地 worker 更新"}
    try:
        jobs = load_jobs(output)
        candidates = [item for item in jobs if item.get("status") == "queued"]
        if args.prompt_id:
            candidates = [item for item in candidates if item.get("prompt_id") == args.prompt_id]
        if not candidates:
            return {"status": "empty", "message": "没有排队中的 Suno 变体", "queue": read_json(output / STATUS_NAME, {})}
        job = sorted(candidates, key=lambda row: (str(row.get("prompt_id")), int(row.get("variant_index") or 0)))[0]
        job["status"] = "running"
        job["backend"] = args.backend or "browser"
        job["started_at"] = job.get("started_at") or now_iso()
        job["updated_at"] = now_iso()
        job["error"] = None
        status = save_jobs(jobs, output)
        return {"status": "ok", "job": job, "queue": status}
    finally:
        lock_handle.close()
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def claim(args: argparse.Namespace) -> dict[str, Any]:
    return mutate_job(args, "running")


def complete(args: argparse.Namespace) -> dict[str, Any]:
    return mutate_job(args, "completed")


def fail_or_block(args: argparse.Namespace, status: str) -> dict[str, Any]:
    return mutate_job(args, status)


def requeue_blocked(args: argparse.Namespace) -> dict[str, Any]:
    """Safely restore only blocked jobs matching a known transient reason."""
    output = Path(args.output)
    jobs = load_jobs(output)
    reason_match = str(args.reason_match or "").strip()
    changed = []
    for job in jobs:
        if job.get("status") != "blocked":
            continue
        if reason_match and str(job.get("error") or "") != reason_match:
            continue
        previous_reason = job.get("error")
        job["status"] = "queued"
        job["error"] = None
        job["started_at"] = None
        job["updated_at"] = now_iso()
        job["requeued_from"] = "blocked"
        job["requeue_reason"] = str(args.reason or "transient form readiness recovery")
        changed.append({"task_id": job.get("task_id"), "previous_error": previous_reason})
        if args.limit is not None and len(changed) >= args.limit:
            break
    queue = save_jobs(jobs, output) if changed else read_json(output / STATUS_NAME, {})
    return {"status": "ok", "requeued": len(changed), "jobs": changed, "queue": queue}


@contextmanager
def queue_lock(output: Path, stale_after: float = 86400.0):
    """Serialize updates for one queue and recover an abandoned lock.

    The lock is only a local coordination file.  It contains no credentials
    and is removed when the worker exits normally.  A lock older than the
    generous recovery window is considered abandoned, which lets a later
    scheduled run continue after a machine restart or killed process.
    """
    output.mkdir(parents=True, exist_ok=True)
    lock_path = output / ".suno-generation.lock"
    lock_token = f"{os.getpid()}:{time.time_ns()}"
    try:
        lock_handle = lock_path.open("x", encoding="utf-8")
    except FileExistsError:
        try:
            age = max(0.0, time.time() - lock_path.stat().st_mtime)
        except OSError:
            age = 0.0
        if age <= stale_after:
            raise RuntimeError("队列正在被另一个本地 worker 更新")
        try:
            lock_path.unlink()
            lock_handle = lock_path.open("x", encoding="utf-8")
        except (FileExistsError, OSError) as exc:
            raise RuntimeError("队列锁无法回收，稍后重试") from exc
    try:
        lock_handle.write(json.dumps({"pid": os.getpid(), "token": lock_token, "created_at": now_iso()}, ensure_ascii=False))
        lock_handle.flush()
        yield
    finally:
        lock_handle.close()
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def safe_api_endpoint() -> str:
    endpoint = os.environ.get("HOTSPOT_SUNO_API_URL", "").strip()
    if not endpoint:
        raise RuntimeError("HOTSPOT_SUNO_API_URL 未配置；官方 API 不可假定存在")
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("HOTSPOT_SUNO_API_URL 必须是合法的 http(s) 地址")
    return endpoint


def api_request(url: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    key = os.environ.get("HOTSPOT_SUNO_API_KEY", "").strip()
    if not key:
        raise RuntimeError("HOTSPOT_SUNO_API_KEY 未配置")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response_body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Suno API HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Suno API 请求失败：{exc}") from exc
    if not isinstance(response_body, dict):
        raise RuntimeError("Suno API 返回格式不是 JSON 对象")
    return response_body


def api_submit(job: dict[str, Any]) -> dict[str, Any]:
    endpoint = safe_api_endpoint()
    payload = {
        "title": job.get("title"),
        "style_prompt": job.get("style_prompt"),
        "lyrics": job.get("lyrics"),
        "variant_index": job.get("variant_index"),
        "client_task_id": job.get("task_id"),
    }
    return api_request(endpoint, method="POST", payload=payload)


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _clean_http_url(value: Any) -> str | None:
    candidate = text_value(value)
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return candidate


def response_status(body: dict[str, Any]) -> str:
    for node in _walk_dicts(body):
        for key in ("status", "state", "generation_status"):
            value = text_value(node.get(key)).lower().replace(" ", "_")
            if value:
                return value
    return ""


def response_request_id(body: dict[str, Any]) -> str | None:
    for node in _walk_dicts(body):
        for key in REQUEST_ID_FIELDS:
            value = text_value(node.get(key))
            if value:
                return value
    return None


def response_poll_url(body: dict[str, Any]) -> str | None:
    for node in _walk_dicts(body):
        for key in STATUS_URL_FIELDS:
            value = _clean_http_url(node.get(key))
            if value:
                return value
    template = os.environ.get("HOTSPOT_SUNO_API_POLL_URL_TEMPLATE", "").strip()
    request_id = response_request_id(body)
    if template and request_id:
        try:
            candidate = template.format(request_id=quote(request_id, safe=""), id=quote(request_id, safe=""))
        except (KeyError, ValueError) as exc:
            raise RuntimeError("HOTSPOT_SUNO_API_POLL_URL_TEMPLATE 格式无效") from exc
        return _clean_http_url(candidate)
    return None


def response_artifacts(body: dict[str, Any]) -> dict[str, str | None]:
    """Extract only verifiable song artifacts, never a request/task id.

    Generic ``task_id``/``generation_id`` values are intentionally excluded.
    A generic ``id`` is accepted only from a terminal-success response and
    only when the response has no separate request identifier.
    """
    song_id = None
    song_url = None
    result_file = None
    for node in _walk_dicts(body):
        if not song_id:
            for key in SONG_ID_FIELDS:
                value = text_value(node.get(key))
                if value:
                    song_id = value
                    break
        if not song_url:
            for key in SONG_URL_FIELDS:
                value = _clean_http_url(node.get(key))
                if value:
                    song_url = value
                    break
        if not result_file:
            for key in ("result_file", "media_file", "output_file"):
                value = text_value(node.get(key))
                if value:
                    candidate = Path(value)
                    if candidate.is_file() and candidate.stat().st_size > 0:
                        result_file = str(candidate.resolve())
                        break
    terminal = response_status(body) in SUCCESS_STATUSES
    request_id = response_request_id(body)
    if not song_id and terminal:
        for node in _walk_dicts(body):
            generic = text_value(node.get("id"))
            if generic and generic != request_id:
                song_id = generic
                break
    return {"song_id": song_id, "song_url": song_url, "result_file": result_file}


def verified_artifacts(body: dict[str, Any]) -> dict[str, str | None]:
    current_status = response_status(body)
    if current_status in PENDING_STATUSES:
        raise RuntimeError(f"Suno API 仍在处理中：{current_status}")
    artifacts = response_artifacts(body)
    if artifacts["song_id"] or artifacts["song_url"] or artifacts["result_file"]:
        return artifacts
    raise RuntimeError("Suno API 未返回可验证的歌曲 ID、歌曲链接或非空结果文件")


def await_api_result(initial: dict[str, Any], poll_timeout: float, poll_interval: float, fallback_poll_url: str | None = None) -> tuple[dict[str, Any], dict[str, str | None], str | None]:
    """Return a verified result, polling only an explicit authorized URL."""
    try:
        return initial, verified_artifacts(initial), response_request_id(initial)
    except RuntimeError as initial_error:
        status = response_status(initial)
        if status in FAILED_STATUSES:
            raise RuntimeError(f"Suno API 返回失败状态：{status}") from initial_error
        if status in SUCCESS_STATUSES:
            raise initial_error
        poll_url = response_poll_url(initial) or _clean_http_url(fallback_poll_url)
        if not poll_url:
            raise initial_error
    deadline = time.monotonic() + max(0.0, poll_timeout)
    last = initial
    while time.monotonic() < deadline:
        time.sleep(max(0.0, poll_interval))
        last = api_request(poll_url)
        try:
            return last, verified_artifacts(last), response_request_id(last)
        except RuntimeError as poll_error:
            status = response_status(last)
            if status in FAILED_STATUSES:
                raise RuntimeError(f"Suno API 返回失败状态：{status}") from poll_error
            if status in SUCCESS_STATUSES:
                raise poll_error
            next_url = response_poll_url(last)
            if next_url:
                poll_url = next_url
    raise RuntimeError(f"Suno API 在 {poll_timeout:g} 秒内未返回可验证的歌曲结果")


def recover_stale_running(jobs: list[dict[str, Any]], stale_after: float) -> int:
    recovered = 0
    now = time.time()
    for job in jobs:
        if job.get("status") != "running" or job.get("backend") != "official_api":
            continue
        if job.get("api_request_id") and (job.get("api_status_url") or response_poll_url({"request_id": job.get("api_request_id")})):
            if not job.get("api_status_url"):
                job["api_status_url"] = response_poll_url({"request_id": job.get("api_request_id")})
            continue
        updated = job.get("updated_at") or job.get("started_at")
        try:
            age = now - datetime.fromisoformat(str(updated)).timestamp()
        except (TypeError, ValueError, OverflowError):
            age = stale_after + 1
        if age > stale_after:
            job["status"] = "queued"
            job["error"] = "上次 worker 未正常结束，已自动恢复排队"
            job["api_request_id"] = None
            job["api_status_url"] = None
            job["api_status"] = None
            job["updated_at"] = now_iso()
            recovered += 1
    return recovered


def process_api_job(output: Path, task_id_value: str, args: argparse.Namespace) -> dict[str, Any]:
    try:
        with queue_lock(output):
            jobs = load_jobs(output)
            job = find_job(jobs, task_id_value)
            if job.get("status") not in {"queued", "running"}:
                return {"task_id": task_id_value, "status": job.get("status"), "skipped": True}
            try:
                if job.get("status") == "queued":
                    job["status"] = "running"
                    job["backend"] = "official_api"
                    job["started_at"] = now_iso()
                    job["updated_at"] = now_iso()
                    job["error"] = None
                    save_jobs(jobs, output)
                    initial = api_submit(job)
                    job["api_request_id"] = response_request_id(initial)
                    job["api_status_url"] = response_poll_url(initial)
                    job["api_status"] = response_status(initial) or "submitted"
                    job["updated_at"] = now_iso()
                    save_jobs(jobs, output)
                else:
                    poll_url = _clean_http_url(job.get("api_status_url"))
                    if not poll_url:
                        return {"task_id": task_id_value, "status": "running", "error": "已有 API 任务但没有可轮询的状态地址；请配置 HOTSPOT_SUNO_API_POLL_URL_TEMPLATE", "queue": str((output / STATUS_NAME).resolve())}
                    initial = api_request(poll_url)
                    job["api_status_url"] = response_poll_url(initial) or poll_url
                response, artifacts, request_id = await_api_result(initial, args.poll_timeout, args.poll_interval, _clean_http_url(job.get("api_status_url")))
                job["status"] = "completed"
                job["song_id"] = artifacts.get("song_id")
                job["song_url"] = artifacts.get("song_url")
                job["result_file"] = artifacts.get("result_file")
                job["api_request_id"] = request_id or job.get("api_request_id")
                job["api_status_url"] = response_poll_url(response) or job.get("api_status_url")
                job["api_status"] = response_status(response) or "completed"
                job["result_kind"] = "song_id" if artifacts.get("song_id") else "song_url" if artifacts.get("song_url") else "result_file"
                job["completed_at"] = now_iso()
                job["error"] = None
            except Exception as exc:
                job["status"] = "failed"
                job["error"] = str(exc)
                job["updated_at"] = now_iso()
            save_jobs(jobs, output)
            return {"task_id": job["task_id"], "status": job["status"], "song_id": job.get("song_id"), "song_url": job.get("song_url"), "result_file": job.get("result_file"), "error": job.get("error"), "queue": str((output / STATUS_NAME).resolve())}
    except RuntimeError as exc:
        return {"task_id": task_id_value, "status": "busy", "error": str(exc), "queue": str((output / STATUS_NAME).resolve())}


def run_api(args: argparse.Namespace) -> dict[str, Any]:
    output = Path(args.output)
    safe_api_endpoint()
    with queue_lock(output):
        jobs = load_jobs(output)
        recovered = recover_stale_running(jobs, args.stale_after)
        if recovered:
            save_jobs(jobs, output)
        selected = [item["task_id"] for item in jobs if item.get("status") == "queued" or (item.get("status") == "running" and item.get("backend") == "official_api" and item.get("api_status_url"))]
    if args.limit is not None:
        selected = selected[: max(0, args.limit)]
    done = []
    for task_id_value in selected:
        done.append(process_api_job(output, task_id_value, args))
        if args.delay:
            time.sleep(args.delay)
    return {"status": "ok", "backend": "official_api", "processed": len(done), "recovered": recovered, "results": done, "queue": read_json(output / STATUS_NAME, {})}


def api_queue_outputs(root: Path) -> list[Path]:
    if not root.exists():
        raise FileNotFoundError(f"task root does not exist: {root}")
    outputs = []
    for status_path in sorted(root.rglob(STATUS_NAME)):
        if status_path.parent.name != "suno-generation":
            continue
        if not scheduled_suno_task_enabled(status_path, task_root=root):
            continue
        if (status_path.parent / PLAN_NAME).is_file():
            outputs.append(status_path.parent)
    return outputs


def run_api_all(args: argparse.Namespace) -> dict[str, Any]:
    """Process queued jobs across every daily/hourly task queue.

    Queues are processed in deterministic path order.  The global limit and
    budget are evaluated between jobs, so a scheduled invocation can stop
    cleanly and resume on its next run without duplicating completed work.
    """
    safe_api_endpoint()
    root = Path(args.input)
    outputs = api_queue_outputs(root)
    deadline = time.monotonic() + args.budget_minutes * 60 if args.budget_minutes else None
    remaining = args.limit if args.limit is not None else None
    results = []
    queue_summaries = []
    recovered_total = 0
    for output in outputs:
        if remaining is not None and remaining <= 0:
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
        try:
            with queue_lock(output):
                jobs = load_jobs(output)
                recovered = recover_stale_running(jobs, args.stale_after)
                if recovered:
                    save_jobs(jobs, output)
                selected = [item["task_id"] for item in jobs if item.get("status") == "queued" or (item.get("status") == "running" and item.get("backend") == "official_api" and item.get("api_status_url"))]
        except RuntimeError as exc:
            queue_summaries.append({"output": str(output.resolve()), "status": "busy", "error": str(exc)})
            continue
        recovered_total += recovered
        if remaining is not None:
            selected = selected[:max(0, remaining)]
        for task_id_value in selected:
            if deadline is not None and time.monotonic() >= deadline:
                break
            item = process_api_job(output, task_id_value, args)
            results.append({"queue": str(output.resolve()), **item})
            if remaining is not None:
                remaining -= 1
            if args.delay:
                time.sleep(args.delay)
        queue_summaries.append({"output": str(output.resolve()), "status": "ok", "selected": len(selected), "queue": read_json(output / STATUS_NAME, {})})
    return {"status": "ok", "backend": "official_api", "input": str(root.resolve()), "queue_count": len(outputs), "processed": len(results), "recovered": recovered_total, "limit": args.limit, "budget_minutes": args.budget_minutes, "results": results, "queues": queue_summaries}


def show_status(args: argparse.Namespace) -> dict[str, Any]:
    output = Path(args.output)
    status = read_json(output / STATUS_NAME)
    if not status:
        jobs = load_jobs(output)
        status = save_jobs(jobs, output)
    return status


def browser_instruction(args: argparse.Namespace) -> dict[str, Any]:
    """Return a safe, human/browser-runner submission packet.

    This is intentionally data-only.  A separate browser Skill can claim the
    job, fill the visible Suno form, and then call ``complete`` after the
    success signal.  The packet never contains credentials or browser state.
    """
    output = Path(args.output)
    jobs = load_jobs(output)
    job = find_job(jobs, args.task_id) if args.task_id else next_job(argparse.Namespace(output=str(output), prompt_id=args.prompt_id)) .get("job")
    if not job:
        return {"status": "empty", "message": "没有可提交的排队任务"}
    return {
        "status": "ok",
        "backend": "visible_browser",
        "task_id": job["task_id"],
        "destination": "https://suno.com/create",
        "fields": {"title": job.get("title"), "style_prompt": job.get("style_prompt"), "lyrics": job.get("lyrics")},
        "variant_index": job.get("variant_index"),
        "variant_total": job.get("variant_total", 6),
        "required_sequence": ["claim", "fill_visible_form", "user_confirm_external_submit", "submit", "record_song_id_or_url", "complete"],
        "claim_command": f"python suno_batch_generator.py claim --output \"{output}\" --task-id {job['task_id']} --backend browser",
        "complete_command_template": f"python suno_batch_generator.py complete --output \"{output}\" --task-id {job['task_id']} --song-id <id> --song-url <url>",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Suno prompt -> six-song local generation queue")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="scan prompts and create/update an idempotent six-variant queue")
    p.add_argument("--input", required=True, help="suno-prompts.jsonl file or a directory to scan")
    p.add_argument("--output", default=None, help="queue directory; defaults beside input")
    p.add_argument("--variants", type=int, default=6)
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(fn=plan)
    pa = sub.add_parser("plan-all", help="create a separate queue beside every prompt file under a task root")
    pa.add_argument("--input", required=True, help="task root or directory to scan")
    pa.add_argument("--variants", type=int, default=6)
    pa.add_argument("--limit", type=int, default=None)
    pa.set_defaults(fn=plan_all)
    s = sub.add_parser("status", help="show current queue status")
    s.add_argument("--output", required=True)
    s.set_defaults(fn=show_status)
    n = sub.add_parser("next", help="return the next browser/API job as JSON")
    n.add_argument("--output", required=True)
    n.add_argument("--prompt-id", default=None)
    n.set_defaults(fn=next_job)
    cn = sub.add_parser("claim-next", help="atomically claim the next queued browser/API job")
    cn.add_argument("--output", required=True); cn.add_argument("--prompt-id", default=None); cn.add_argument("--backend", default="browser"); cn.set_defaults(fn=claim_next)
    bi = sub.add_parser("browser-instruction", help="return fields for a visible, authorized Suno browser runner")
    bi.add_argument("--output", required=True); bi.add_argument("--task-id", default=None); bi.add_argument("--prompt-id", default=None); bi.set_defaults(fn=browser_instruction)
    c = sub.add_parser("claim", help="mark one job as running before visible Suno submission")
    c.add_argument("--output", required=True); c.add_argument("--task-id", required=True); c.add_argument("--backend", default="browser"); c.set_defaults(fn=claim)
    done = sub.add_parser("complete", help="record a successful Suno result")
    done.add_argument("--output", required=True); done.add_argument("--task-id", required=True); done.add_argument("--song-id", default=None); done.add_argument("--song-url", default=None); done.add_argument("--result-file", default=None); done.set_defaults(fn=complete)
    for name, status in (("fail", "failed"), ("block", "blocked")):
        x = sub.add_parser(name, help=f"mark a job {status}")
        x.add_argument("--output", required=True); x.add_argument("--task-id", required=True); x.add_argument("--reason", required=True); x.set_defaults(fn=lambda args, value=status: fail_or_block(args, value))
    rq = sub.add_parser("requeue-blocked", help="restore blocked jobs matching an exact transient reason")
    rq.add_argument("--output", required=True)
    rq.add_argument("--reason-match", default="")
    rq.add_argument("--reason", default="transient form readiness recovery")
    rq.add_argument("--limit", type=int, default=None)
    rq.set_defaults(fn=requeue_blocked)
    r = sub.add_parser("run-api", help="submit queued jobs in one queue to an explicitly configured API endpoint")
    r.add_argument("--output", required=True); r.add_argument("--limit", type=int, default=None); r.add_argument("--delay", type=float, default=1.0)
    r.add_argument("--poll-timeout", type=float, default=900.0); r.add_argument("--poll-interval", type=float, default=10.0); r.add_argument("--stale-after", type=float, default=1800.0); r.set_defaults(fn=run_api)
    ra = sub.add_parser("run-api-all", help="submit queued jobs across every task queue")
    ra.add_argument("--input", required=True, help="data/tasks root")
    ra.add_argument("--limit", type=int, default=None, help="maximum jobs for this invocation")
    ra.add_argument("--budget-minutes", type=float, default=None, help="stop after this many minutes and resume next run")
    ra.add_argument("--delay", type=float, default=1.0)
    ra.add_argument("--poll-timeout", type=float, default=900.0); ra.add_argument("--poll-interval", type=float, default=10.0); ra.add_argument("--stale-after", type=float, default=1800.0); ra.set_defaults(fn=run_api_all)
    args = parser.parse_args()
    try:
        print(json.dumps(args.fn(args), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
