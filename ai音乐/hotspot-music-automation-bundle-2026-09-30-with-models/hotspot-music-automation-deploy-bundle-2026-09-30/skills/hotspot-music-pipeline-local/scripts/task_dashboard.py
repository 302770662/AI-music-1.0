"""Build a standalone local dashboard for the scheduled hotspot tasks.

The dashboard is intentionally dependency-free. Each task writes a small
``task-progress.json`` marker into its own date/hour directory; the builder
also infers progress from output files so a missing marker never looks like a
successful run.
"""

import argparse
import json
import os
import re
from datetime import datetime
from html import escape
from pathlib import Path

RUNTIME_ROOT = Path("D:/projects/local/hotspot-music-runtime")
TASK_ROOT = RUNTIME_ROOT / "data" / "tasks"
DEFAULT_DASHBOARD = RUNTIME_ROOT / "dashboard.html"
SUNO_STATUS_NAME = "suno-generation-status.json"
SUNO_RECEIPT_NAME = "suno-browser-results.jsonl"
SUNO_DOWNLOAD_STATUS_NAME = "suno-download-status.json"
SUNO_URL_RE = re.compile(r"^https://(?:www\.)?suno\.com/song/[A-Za-z0-9-]+/?$")

TASKS = [
    {
        "id": "soda-daily",
        "automation_id": "automation",
        "name": "汽水音乐刷歌",
        "schedule": "每天 00:00，最多 3 小时",
        "kind": "cron",
        "description": "MuMu 汽水音乐可见界面刷歌与公开元数据记录",
        "suno_enabled": False,
        "expected": ["soda-browser.json", "trend-snapshot.jsonl", "hotspot.json", "trend-status.json", "trend-report.md"],
    },
    {
        "id": "netease-daily",
        "automation_id": "automation-3",
        "name": "网易云热歌榜",
        "schedule": "每天 01:00",
        "kind": "cron",
        "description": "公开热歌榜歌曲、歌手、排名和热度采集",
        "suno_enabled": False,
        "expected": ["trend-snapshot.jsonl", "hotspot.json", "trend-status.json", "trend-report.md"],
    },
    {
        "id": "qqmusic-daily",
        "automation_id": "qq",
        "name": "QQ 音乐热歌榜",
        "schedule": "每天 01:10",
        "kind": "cron",
        "description": "公开热歌榜歌曲、歌手、排名和榜单字段采集",
        "suno_enabled": False,
        "expected": ["trend-snapshot.jsonl", "hotspot.json", "trend-status.json", "trend-report.md"],
    },
    {
        "id": "bilibili-daily",
        "automation_id": "b-suno",
        "name": "B 站热点与 Suno 创作包",
        "schedule": "每天 01:20",
        "kind": "cron",
        "description": "全站榜、音乐区榜、DeepSeek 创作包；确认后由 Chrome 提交 Suno",
        "suno_enabled": True,
        "expected": ["trend-snapshot.jsonl", "hotspot.json", "trend-status.json", "trend-report.md", "curation.json", "curation-report.md", "curation-status.json", "suno-prompts.jsonl", "suno-prompts.md", "generated-lyrics.md"],
    },
    {
        "id": "douyin-browse",
        "automation_id": "automation-2",
        "name": "抖音热点视频浏览",
        "schedule": "每天 03:05，最多 3 小时",
        "kind": "cron",
        "description": "MuMu 抖音热点公开元数据与授权录屏",
        "suno_enabled": False,
        "expected": ["douyin-browser.json", "trend-status.json"],
    },
    {
        "id": "douyin-curation",
        "automation_id": "suno",
        "name": "抖音热点梳理与 Suno",
        "schedule": "每天 07:00",
        "kind": "cron",
        "description": "DeepSeek 热点梳理、原创歌词、音乐画像；确认后由 Chrome 提交 Suno",
        "suno_enabled": True,
        "expected": ["curation.json", "curation-report.md", "curation-status.json", "suno-prompts.jsonl", "suno-prompts.md", "generated-lyrics.md"],
    },
    {
        "id": "weibo-hourly",
        "automation_id": "suno-2",
        "name": "微博热搜与 Suno 创作包",
        "schedule": "每小时整点",
        "kind": "cron",
        "description": "每小时公开热搜、DeepSeek 摘要、原创歌词和 Suno 提示词",
        "hourly": True,
        "suno_enabled": True,
        "expected": ["weibo-browser.json", "trend-snapshot.jsonl", "hotspot.json", "trend-status.json", "trend-report.md", "curation.json", "curation-report.md", "curation-status.json", "suno-prompts.jsonl", "suno-prompts.md", "generated-lyrics.md"],
    },
]


def now_iso():
    return datetime.now().astimezone().isoformat()


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def rel_path(path):
    return os.path.relpath(path, RUNTIME_ROOT).replace("\\", "/")


def file_info(path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return {"name": path.name, "path": rel_path(path), "size": stat.st_size, "mtime": stat.st_mtime}


def all_files(path):
    if not path.exists():
        return []
    result = []
    for item in path.rglob("*"):
        if item.is_file() and item.name != ".keep":
            info = file_info(item)
            if info:
                result.append(info)
    return sorted(result, key=lambda item: (item["mtime"], item["path"]), reverse=True)


def suno_result_count(status_path):
    """Count unique Suno song links recorded by the visible browser runner."""
    receipt_path = status_path.parent / SUNO_RECEIPT_NAME
    if not receipt_path.exists():
        return 0
    songs = set()
    for line in receipt_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        values = item.get("song_urls") or []
        for value in values:
            cleaned = str(value).strip()
            if SUNO_URL_RE.fullmatch(cleaned):
                songs.add("song-id:" + cleaned.split("/song/", 1)[1].rstrip("/"))
        for value in item.get("song_ids") or []:
            cleaned = str(value).strip()
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{5,127}", cleaned):
                songs.add("song-id:" + cleaned)
    status = read_json(status_path)
    for group in status.get("prompt_groups") or []:
        for variant in group.get("variants") or []:
            for value in (variant.get("song_url"), variant.get("song_id")):
                cleaned = str(value or "").strip()
                if SUNO_URL_RE.fullmatch(cleaned):
                    songs.add("song-id:" + cleaned.split("/song/", 1)[1].rstrip("/"))
                elif re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{5,127}", cleaned):
                    songs.add("song-id:" + cleaned)
    return len(songs)


def suno_generation_status(run_dir):
    """Read the independent Suno queue attached to one task run.

    The generator owns its schema and remains optional: an ordinary pipeline
    run without a queue still renders normally.  The canonical location is
    ``<run>/suno-generation/suno-generation-status.json``; the root fallback
    keeps the dashboard compatible with manually copied status files.
    """
    candidates = [
        run_dir / "suno-generation" / "suno-generation-status.json",
        run_dir / "suno-generation-status.json",
    ]
    status_path = next((path for path in candidates if path.exists()), None)
    if status_path is None:
        return None
    value = read_json(status_path)
    if not isinstance(value, dict) or value.get("schema") != "suno_generation_status_v1":
        return None
    counts = value.get("counts") if isinstance(value.get("counts"), dict) else {}
    prompt_groups = value.get("prompt_groups") if isinstance(value.get("prompt_groups"), list) else []
    return {
        "status": value.get("status", "queued"),
        "percent": int(value.get("percent") or 0),
        "prompt_count": int(value.get("prompt_count") or len(prompt_groups)),
        "variants_per_prompt": int(value.get("variants_per_prompt") or 6),
        "target_song_count": int(value.get("target_song_count") or counts.get("total") or 0),
        "generated_song_count": suno_result_count(status_path),
        "counts": {key: int(counts.get(key) or 0) for key in ("queued", "running", "completed", "failed", "blocked", "total")},
        "updated_at": value.get("updated_at"),
        "status_path": rel_path(status_path),
        "run_dir": rel_path(status_path.parent),
        "browser_manifest": rel_path(Path(str(value.get("browser_manifest")))) if value.get("browser_manifest") else None,
        "prompt_groups": prompt_groups,
        "download": suno_download_status(run_dir),
    }


def suno_download_status(run_dir):
    """Read the independent MP3/Video download queue for one task run."""
    candidates = [
        run_dir / "suno-generation" / "suno-download" / SUNO_DOWNLOAD_STATUS_NAME,
        run_dir / "suno-download" / SUNO_DOWNLOAD_STATUS_NAME,
    ]
    status_path = next((path for path in candidates if path.exists()), None)
    if status_path is None:
        return None
    value = read_json(status_path)
    if not isinstance(value, dict) or value.get("schema") != "suno_download_status_v1":
        return None
    raw_counts = value.get("counts") if isinstance(value.get("counts"), dict) else {}
    counts = {key: int(raw_counts.get(key) or 0) for key in ("queued", "running", "completed", "failed", "blocked", "total")}
    type_counts = {"mp3": 0, "video": 0}
    for song in value.get("songs") or []:
        for item in song.get("downloads") or []:
            item_type = str(item.get("type") or "")
            if item_type in type_counts and item.get("status") == "completed":
                type_counts[item_type] += 1
    songs = []
    for song in value.get("songs") or []:
        if not isinstance(song, dict):
            continue
        downloads = []
        for item in song.get("downloads") or []:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            raw_file = item.get("download_file")
            if raw_file:
                try:
                    item["download_file"] = rel_path(Path(str(raw_file)))
                except (OSError, ValueError):
                    item["download_file"] = None
            downloads.append(item)
        item = dict(song)
        item["downloads"] = downloads
        songs.append(item)
    return {
        "status": value.get("status", "waiting_for_generation"),
        "percent": int(value.get("percent") or 0),
        "song_count": int(value.get("song_count") or len(songs)),
        "target_download_count": int(value.get("target_download_count") or counts["total"]),
        "completed_download_count": int(value.get("completed_download_count") or counts["completed"]),
        "mp3_completed_count": type_counts["mp3"],
        "video_completed_count": type_counts["video"],
        "counts": counts,
        "updated_at": value.get("updated_at"),
        "status_path": rel_path(status_path),
        "run_dir": rel_path(status_path.parent),
        "songs": songs,
    }


def run_status(task, run_dir):
    files = all_files(run_dir)
    names = {item["name"] for item in files}
    marker = read_json(run_dir / "task-progress.json")
    expected = task["expected"]
    complete_count = sum(name in names for name in expected)
    inferred_percent = round(complete_count / len(expected) * 100) if expected else 0
    percent = marker.get("percent")
    try:
        percent = max(0, min(100, int(percent)))
    except (TypeError, ValueError):
        percent = inferred_percent
    status = str(marker.get("status") or "").lower()
    if status not in {"running", "completed", "failed", "waiting", "partial"}:
        if complete_count == len(expected) and complete_count:
            status = "completed"
        elif complete_count:
            status = "partial"
        else:
            status = "waiting"
    label = {"running": "运行中", "completed": "已完成", "failed": "失败", "partial": "部分完成", "waiting": "等待运行"}[status]
    latest = marker.get("updated_at") or (datetime.fromtimestamp(files[0]["mtime"]).astimezone().isoformat() if files else None)
    return {
        "status": status,
        "status_label": label,
        "percent": percent,
        "stage": marker.get("stage") or ("输出已完成" if status == "completed" else "等待本轮任务"),
        "message": marker.get("message") or (f"已生成 {complete_count}/{len(expected)} 个核心文件" if run_dir.exists() else "今日尚未创建运行目录"),
        "updated_at": latest,
        "files": files,
        "expected": expected,
        "complete_count": complete_count,
        "run_dir": rel_path(run_dir),
        "suno_generation": suno_generation_status(run_dir) if task.get("suno_enabled", False) else None,
        "suno_download": suno_download_status(run_dir) if task.get("suno_enabled", False) else None,
    }


def discover_task(task, date_text):
    date_dir = TASK_ROOT / task["id"] / date_text
    if task.get("hourly"):
        hour_dirs = sorted(
            (item for item in date_dir.iterdir() if item.is_dir() and re.fullmatch(r"\d{2}-\d{2}", item.name)),
            key=lambda item: item.name,
            reverse=True,
        ) if date_dir.exists() else []
        runs = [run_status(task, item) for item in hour_dirs]
        latest = runs[0] if runs else run_status(task, date_dir)
        return {
            "date_dir": rel_path(date_dir),
            "latest": latest,
            "runs": runs,
            "run_count": len(runs),
            "files": [item for run in runs for item in run["files"]][:40],
        }
    latest = run_status(task, date_dir)
    return {"date_dir": rel_path(date_dir), "latest": latest, "runs": [latest] if date_dir.exists() else [], "run_count": 1 if date_dir.exists() else 0, "files": latest["files"][:40]}


def dashboard_data(date_text):
    task_data = []
    for task in TASKS:
        item = dict(task)
        item.update(discover_task(task, date_text))
        task_data.append(item)
    counts = {key: sum(item["latest"]["status"] == key for item in task_data) for key in ("running", "completed", "failed", "partial", "waiting")}
    queues = all_suno_queues()
    return {"generated_at": now_iso(), "date": date_text, "runtime_root": str(RUNTIME_ROOT),
            "counts": counts, "tasks": task_data, "suno_queues": queues}


def all_suno_queues():
    """Return valid queues only for tasks whose Suno workflow is enabled.

    Task cards intentionally remain date-scoped, while this separate index is
    global so an allowed prompt queue is never hidden just because the
    dashboard date changed. Historical queues belonging to metadata-only
    tasks remain on disk but are intentionally not rendered.
    """
    task_map = {task["id"]: task for task in TASKS}
    root = TASK_ROOT.resolve()
    queues = []
    for status_path in sorted(TASK_ROOT.rglob(SUNO_STATUS_NAME)) if TASK_ROOT.exists() else []:
        value = read_json(status_path)
        if value.get("schema") != "suno_generation_status_v1":
            continue
        try:
            relative = status_path.resolve().relative_to(root)
        except ValueError:
            continue
        if len(relative.parts) < 3:
            continue
        task_id = relative.parts[0]
        task = task_map.get(task_id)
        if not task or not task.get("suno_enabled", False):
            continue
        run_dir = status_path.parent.parent
        suno = suno_generation_status(run_dir)
        if not suno:
            continue
        queues.append({
            "task_id": task_id,
            "task_name": task["name"],
            "run_dir": rel_path(run_dir),
            "task_status": "unknown",
            "suno": suno,
            "download": suno_download_status(run_dir),
        })
    queues.sort(key=lambda item: (str(item["suno"].get("updated_at") or ""), item["run_dir"]), reverse=True)
    return queues


def js_data(value):
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>热点音乐自动任务看板</title>
  <style>
    :root { --ink:#17211f; --muted:#687572; --line:#dce6e1; --paper:#f5f7f4; --panel:#ffffff; --teal:#16796f; --teal-soft:#dff2ed; --amber:#b36b12; --amber-soft:#fff0d2; --red:#ae3e42; --red-soft:#fde6e4; --shadow:0 8px 24px rgba(23,33,31,.07); }
    * { box-sizing:border-box; }
    body { margin:0; color:var(--ink); background:var(--paper); font-family:Segoe UI,"Microsoft YaHei",sans-serif; line-height:1.45; }
    .shell { max-width:1440px; margin:0 auto; padding:30px 28px 44px; }
    header { display:flex; align-items:flex-end; justify-content:space-between; gap:20px; padding-bottom:24px; border-bottom:1px solid var(--line); }
    .header-actions { display:flex; align-items:flex-end; gap:10px; flex-wrap:wrap; justify-content:flex-end; }
    .suno-control { min-width:420px; max-width:650px; padding:11px 13px; background:var(--panel); border:1px solid #c8d9d3; border-radius:7px; box-shadow:var(--shadow); }
    .suno-control-title { display:flex; align-items:center; justify-content:space-between; gap:10px; font-size:12px; font-weight:600; }
    .suno-control-row { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-top:8px; }
    .suno-control-row label { color:var(--muted); font-size:11px; }
    .suno-control-row select { border:1px solid var(--line); background:var(--panel); color:var(--ink); border-radius:5px; padding:7px 8px; font:inherit; font-size:11px; }
    .suno-control button { padding:7px 10px; font-size:11px; }
    .suno-control button.primary { background:var(--teal); color:#fff; border-color:var(--teal); }
    .suno-control button.danger { background:#fff6f5; color:var(--red); border-color:#e5aaa4; }
    .suno-control button:disabled { cursor:not-allowed; opacity:.55; }
    .suno-worker-state { margin-top:8px; color:var(--muted); font-size:11px; min-height:16px; }
    .suno-worker-state.active { color:var(--teal); }
    .suno-worker-state.error { color:var(--red); }
    .suno-download-control { min-width:420px; max-width:650px; padding:11px 13px; background:#fffdf8; border:1px solid #e0d3b6; border-radius:7px; box-shadow:var(--shadow); }
    .suno-download-control button.primary { background:#9a6519; color:#fff; border-color:#9a6519; }
    .suno-download-control button.danger { background:#fff6f5; color:var(--red); border-color:#e5aaa4; }
    .suno-download-state { margin-top:8px; color:var(--muted); font-size:11px; min-height:16px; }
    .suno-download-state.active { color:#8a5d16; }
    .suno-download-state.error { color:var(--red); }
    h1 { margin:0; font-size:28px; letter-spacing:0; }
    .sub { margin:7px 0 0; color:var(--muted); font-size:13px; }
    button { border:1px solid var(--line); background:var(--panel); color:var(--ink); border-radius:6px; padding:9px 13px; cursor:pointer; font:inherit; }
    button:hover { border-color:var(--teal); color:var(--teal); }
    .summary { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:12px; margin:22px 0; }
    .metric { background:var(--panel); border:1px solid var(--line); border-radius:7px; padding:16px 17px; box-shadow:var(--shadow); }
    .metric strong { display:block; font-size:25px; line-height:1; }
    .metric span { display:block; margin-top:8px; color:var(--muted); font-size:12px; }
    .metric.running strong { color:var(--teal); } .metric.failed strong { color:var(--red); } .metric.waiting strong { color:var(--amber); }
    .section-title { display:flex; justify-content:space-between; align-items:center; margin:30px 0 12px; }
    h2 { margin:0; font-size:17px; }
    .hint { color:var(--muted); font-size:12px; }
    .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:15px; }
    .task { background:var(--panel); border:1px solid var(--line); border-radius:7px; padding:19px; box-shadow:var(--shadow); min-width:0; }
    .task-top { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }
    .task h3 { margin:0; font-size:17px; }
    .desc { margin:5px 0 0; color:var(--muted); font-size:12px; }
    .badge { flex:0 0 auto; border-radius:999px; padding:4px 9px; font-size:11px; font-weight:600; }
    .badge.running { background:var(--teal-soft); color:var(--teal); } .badge.completed { background:#e5f1e8; color:#2e7747; } .badge.failed { background:var(--red-soft); color:var(--red); } .badge.partial,.badge.waiting { background:var(--amber-soft); color:var(--amber); }
    .bar-row { display:flex; align-items:center; gap:10px; margin:18px 0 10px; }
    .bar { height:8px; flex:1; background:#edf1ef; border-radius:999px; overflow:hidden; }
    .fill { height:100%; background:var(--teal); border-radius:inherit; transition:width .2s ease; }
    .percent { min-width:39px; text-align:right; font-size:12px; font-weight:600; }
    .meta { display:grid; grid-template-columns:1fr 1fr; gap:8px 16px; border-top:1px solid var(--line); padding-top:12px; font-size:12px; }
    .meta dt { color:var(--muted); float:left; margin-right:7px; } .meta dd { margin:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .message { margin:13px 0 0; padding:10px 11px; background:#f6f9f7; border-left:3px solid var(--teal); font-size:12px; }
    .folder { margin-top:13px; font-size:11px; color:var(--muted); word-break:break-all; }
    .folder code { color:var(--ink); }
    details { margin-top:14px; border-top:1px solid var(--line); padding-top:11px; }
    summary { cursor:pointer; font-size:12px; color:var(--teal); }
    .files { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
    .files a, .hour a { display:inline-block; max-width:100%; padding:4px 7px; border:1px solid var(--line); border-radius:4px; color:var(--ink); text-decoration:none; font-size:11px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .files a:hover, .hour a:hover { border-color:var(--teal); color:var(--teal); }
    .hours { display:grid; gap:7px; margin-top:10px; }
    .hour { display:grid; grid-template-columns:66px 1fr auto; align-items:center; gap:9px; font-size:12px; }
    .hour .mini-bar { height:6px; background:#edf1ef; border-radius:999px; overflow:hidden; } .hour .mini-bar i { display:block; height:100%; background:var(--teal); }
    .suno { margin-top:14px; padding:12px; border:1px solid var(--line); border-radius:6px; background:#fbfdfc; }
    .suno-head { display:flex; align-items:center; justify-content:space-between; gap:10px; font-size:12px; }
    .suno-head strong { font-size:13px; }
    .suno-stats { display:flex; flex-wrap:wrap; gap:7px 12px; margin-top:8px; color:var(--muted); font-size:11px; }
    .suno-stats b { color:var(--ink); }
    .suno-bar { height:6px; margin-top:9px; background:#edf1ef; border-radius:999px; overflow:hidden; }
    .suno-bar i { display:block; height:100%; background:#7f5bb5; border-radius:inherit; }
    .suno-groups { display:grid; gap:6px; margin-top:10px; }
    .suno-group { display:grid; grid-template-columns:minmax(120px,1.8fr) minmax(60px,1fr) auto; align-items:center; gap:8px; font-size:11px; }
    .suno-group .mini-bar { height:5px; background:#edf1ef; border-radius:999px; overflow:hidden; }
    .suno-group .mini-bar i { display:block; height:100%; background:#7f5bb5; }
    .suno-group a { color:var(--ink); text-decoration:none; }
    .suno-group a:hover { color:#7f5bb5; }
    .suno-variants { grid-column:1 / -1; display:flex; flex-wrap:wrap; gap:4px; margin:0 0 4px 0; }
    .suno-variant { padding:2px 5px; border:1px solid var(--line); border-radius:4px; color:var(--muted); font-size:10px; }
    .suno-variant.completed { color:#2e7747; border-color:#b8d8c1; background:#f1f9f3; }
    .suno-variant.running { color:var(--teal); border-color:#a9d9d0; background:var(--teal-soft); }
    .suno-variant.failed,.suno-variant.blocked { color:var(--red); border-color:#efc1bd; background:var(--red-soft); }
    .suno-download { margin-top:12px; padding-top:11px; border-top:1px dashed #d6c8a8; }
    .download-songs { display:grid; gap:7px; margin-top:10px; }
    .download-song { display:grid; grid-template-columns:minmax(110px,1fr) minmax(170px,2fr); gap:8px; align-items:center; font-size:11px; }
    .download-song > span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .download-song > div { display:flex; flex-wrap:wrap; gap:4px; }
    .download-file { display:inline-block; padding:2px 5px; border:1px solid var(--line); border-radius:4px; color:var(--muted); text-decoration:none; font-size:10px; }
    .download-file:hover { border-color:#9a6519; color:#8a5d16; }
    .download-file.completed { color:#2e7747; border-color:#b8d8c1; background:#f1f9f3; }
    .download-file.running { color:#8a5d16; border-color:#e0c58d; background:#fff8e9; }
    .download-file.failed,.download-file.blocked { color:var(--red); border-color:#efc1bd; background:var(--red-soft); }
    .suno-empty { margin-top:13px; padding:10px 11px; color:var(--muted); background:#f6f9f7; border-left:3px solid #c7b5df; font-size:12px; }
    .suno-overview { display:grid; gap:10px; }
    .suno-queue { background:var(--panel); border:1px solid var(--line); border-radius:7px; padding:12px 14px; box-shadow:var(--shadow); }
    .suno-queue summary { display:flex; align-items:center; justify-content:space-between; gap:12px; }
    .suno-queue-title { color:var(--ink); font-size:12px; font-weight:600; }
    .suno-queue-meta { color:var(--muted); font-size:11px; font-weight:400; }
    .suno-queue .suno { margin-top:12px; box-shadow:none; }
    @media (max-width:900px) { .download-song { grid-template-columns:1fr; gap:3px; } }
    footer { margin-top:28px; color:var(--muted); font-size:11px; }
    @media (max-width:900px) { .summary { grid-template-columns:repeat(3,1fr); } .grid { grid-template-columns:1fr; } }
    @media (max-width:560px) { .shell { padding:20px 15px 34px; } header { align-items:flex-start; flex-direction:column; } .summary { grid-template-columns:repeat(2,1fr); } .meta { grid-template-columns:1fr; } }
  </style>
</head>
<body>
<main class="shell">
  <header>
    <div><h1>热点音乐自动任务看板</h1><p class="sub">统计日期：<span id="date"></span> · 最后刷新：<span id="updated"></span></p></div>
    <div class="header-actions">
      <section class="suno-control" aria-label="Suno 顺序发送控制">
        <div class="suno-control-title"><span>Chrome · Suno 顺序发送</span><span id="suno-worker-badge" class="badge waiting">未连接</span></div>
        <div class="suno-control-row">
          <label><input id="suno-auto-create" type="checkbox" checked> 开始后自动点击 Create</label>
          <label><input id="suno-follow-future" type="checkbox" checked> 持续监听未来任务</label>
          <select id="suno-scope" aria-label="Suno 队列范围"><option value="latest">每个任务取最新队列</option><option value="all">处理全部历史队列</option></select>
          <button id="suno-start" class="primary" type="button" onclick="startSunoWorker()">开始</button>
          <button id="suno-stop" class="danger" type="button" onclick="stopSunoWorker()" disabled>停止</button>
        </div>
        <div id="suno-worker-state" class="suno-worker-state">需要先启动本地 worker，并在 Chrome 加载 Suno Queue Runner 扩展。</div>
      </section>
      <section class="suno-download-control" aria-label="Suno MP3 和 Video 下载控制">
        <div class="suno-control-title"><span>Chrome · Suno MP3 / Video 下载</span><span id="suno-download-badge" class="badge waiting">未连接</span></div>
        <div class="suno-control-row">
          <label><input id="suno-auto-download" type="checkbox" checked> 开始后自动下载</label>
          <label><input id="suno-download-follow-future" type="checkbox" checked> 持续监听新歌曲</label>
          <select id="suno-download-scope" aria-label="Suno 下载队列范围"><option value="latest">每个任务取最新队列</option><option value="all">处理全部历史队列</option></select>
          <button id="suno-download-start" class="primary" type="button" onclick="startSunoDownloadWorker()">开始下载</button>
          <button id="suno-download-stop" class="danger" type="button" onclick="stopSunoDownloadWorker()" disabled>停止下载</button>
        </div>
        <div id="suno-download-state" class="suno-download-state">需要先启动下载 worker，并在 Chrome 加载新版 Suno Queue Runner 扩展。</div>
      </section>
      <button type="button" onclick="location.reload()">刷新页面</button>
    </div>
  </header>
  <section class="summary" id="summary"></section>
  <div class="section-title"><h2>任务进度</h2><span class="hint">每轮任务结束后自动写入本任务目录并刷新看板数据</span></div>
  <section class="grid" id="tasks"></section>
  <div class="section-title"><h2>已启用任务的 Suno 生成队列</h2><span class="hint">仅显示 B 站、抖音热点梳理和微博；每条提示词独立生成 6 个变体</span></div>
  <section class="suno-overview" id="suno-overview"></section>
  <footer>数据根目录：<code id="root"></code> · 页面为本地静态文件，任务运行后重新打开或刷新即可看到最新状态。</footer>
</main>
<script type="application/json" id="dashboard-data">__DATA__</script>
<script>
  const data = JSON.parse(document.getElementById('dashboard-data').textContent);
  const esc = value => String(value ?? '-').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmtTime = value => value ? new Date(value).toLocaleString('zh-CN', {hour12:false}) : '暂无';
  const fmtSize = value => value < 1024 ? value + ' B' : value < 1048576 ? (value / 1024).toFixed(1) + ' KB' : (value / 1048576).toFixed(1) + ' MB';
  const statusLabel = value => ({queued:'排队',running:'运行中',completed:'完成',failed:'失败',blocked:'阻塞',partial:'部分完成'}[value] || value || '未知');
  const SUNO_WORKER_API = 'http://127.0.0.1:8765';
  const SUNO_DOWNLOAD_API = 'http://127.0.0.1:8766';
  let sunoWorkerPollTimer = null;
  let sunoDownloadPollTimer = null;
  let sunoWorkerRequestInFlight = false;
  let sunoDownloadRequestInFlight = false;
  let sunoPageOpened = false;
  const requestWithTimeout = async (url, options = {}, timeoutMs = 10000) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try { return await fetch(url, {cache:'no-store', signal:controller.signal, ...options}); }
    finally { clearTimeout(timer); }
  };
  const workerRequest = async (path, options = {}) => {
    const response = await requestWithTimeout(`${SUNO_WORKER_API}${path}`, options);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `worker HTTP ${response.status}`);
    return payload;
  };
  const workerBadge = document.getElementById('suno-worker-badge');
  const workerState = document.getElementById('suno-worker-state');
  const startButton = document.getElementById('suno-start');
  const stopButton = document.getElementById('suno-stop');
  const downloadBadge = document.getElementById('suno-download-badge');
  const downloadState = document.getElementById('suno-download-state');
  const downloadStartButton = document.getElementById('suno-download-start');
  const downloadStopButton = document.getElementById('suno-download-stop');
  const downloadRequest = async (path, options = {}) => {
    const response = await requestWithTimeout(`${SUNO_DOWNLOAD_API}${path}`, options);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `download worker HTTP ${response.status}`);
    return payload;
  };
  const workerCounts = counts => {
    const value = counts || {};
    return `待处理 ${value.queued || 0} · 当前处理中 ${value.running || 0} · 已完成队列项 ${value.completed || 0} · 已生成歌曲 ${value.generated_song_count || 0} · 失败 ${value.failed || 0} · 阻塞 ${value.blocked || 0}`;
  };
  const isStaleLifecycleError = value => {
    const text = String(value || '');
    return /用户点击停止|worker was inactive|returned to queue|尚未触发下载/.test(text);
  };
  const renderSunoWorker = status => {
    const active = Boolean(status && status.active);
    const stopping = Boolean(status && status.stop_requested);
    const hasCurrent = Boolean(status && status.current);
    const extensionConnected = Boolean(status && status.extension_connected);
    const rawError = status && status.last_error ? String(status.last_error) : '';
    const error = rawError && !(isStaleLifecycleError(rawError) && !active && !hasCurrent) ? rawError : '';
    workerBadge.className = `badge ${error ? 'failed' : active && extensionConnected ? 'running' : active ? 'partial' : 'waiting'}`;
    workerBadge.textContent = active ? (stopping ? '停止中' : extensionConnected ? '监听中' : '等待扩展') : (error ? '错误' : '已停止');
    const submissionAuthorized = Boolean(status && status.submission_authorized);
    startButton.disabled = (active && submissionAuthorized) || sunoWorkerRequestInFlight;
    startButton.textContent = active && !submissionAuthorized ? '启用提交' : '开始';
    stopButton.disabled = !active && !hasCurrent;
    if (error) {
      workerState.className = 'suno-worker-state error';
      workerState.textContent = `worker 错误：${error} · ${workerCounts(status.counts)}`;
      return;
    }
    workerState.className = `suno-worker-state ${active ? 'active' : ''}`;
    if (status && status.current && status.current.job) {
      const job = status.current.job;
      const phase = status.current.phase || 'claimed';
      workerState.textContent = `${active ? '正在处理' : '当前条目收尾'}：${job.title || job.task_id} · ${phase} · ${workerCounts(status.counts)}${stopping ? ' · 已请求停止，不会提交新条目' : ''}`;
    } else if (active) {
      workerState.textContent = extensionConnected
        ? `Suno Queue Runner 已连接，持续监听未来队列；${workerCounts(status.counts)}`
        : `持续监听已启动，正在打开 Suno 或等待 Chrome 扩展连接；请确认扩展已加载且允许访问文件网址；${workerCounts(status.counts)}`;
    } else {
      workerState.textContent = `未运行；点击“开始”后按顺序处理 B 站、抖音梳理和微博队列。${workerCounts(status && status.counts)}`;
    }
  };
  const refreshSunoWorker = async () => {
    try {
      renderSunoWorker(await workerRequest('/v1/browser/status'));
    } catch (error) {
      workerBadge.className = 'badge failed';
      workerBadge.textContent = '未连接';
      startButton.disabled = false;
      stopButton.disabled = true;
      workerState.className = 'suno-worker-state error';
      workerState.textContent = `无法连接本地 worker：${error.message || error} · 请先运行 run-suno-browser-worker.ps1`;
    }
  };
  const downloadCounts = counts => {
    const value = counts || {};
    return `待下载 ${value.queued || 0} · 当前 ${value.running || 0} · 已完成 ${value.completed || 0} · 失败 ${value.failed || 0} · 阻塞 ${value.blocked || 0}`;
  };
  const renderSunoDownloadWorker = status => {
    const active = Boolean(status && status.active);
    const stopping = Boolean(status && status.stop_requested);
    const current = status && status.current && status.current.job;
    const connected = Boolean(status && status.extension_connected);
    const rawError = status && status.last_error ? String(status.last_error) : '';
    const error = rawError && !(isStaleLifecycleError(rawError) && !active && !current) ? rawError : '';
    downloadBadge.className = `badge ${error ? 'failed' : active && connected ? 'running' : active ? 'partial' : 'waiting'}`;
    downloadBadge.textContent = active ? (stopping ? '停止中' : connected ? '监听中' : '等待扩展') : (error ? '错误' : '已停止');
    downloadStartButton.disabled = (active && status.download_authorized) || sunoDownloadRequestInFlight;
    downloadStartButton.textContent = active && !status.download_authorized ? '启用下载' : '开始下载';
    downloadStopButton.disabled = !active && !current;
    if (error) {
      downloadState.className = 'suno-download-state error';
      downloadState.textContent = `下载 worker 错误：${error} · ${downloadCounts(status.counts)}`;
      return;
    }
    downloadState.className = `suno-download-state ${active ? 'active' : ''}`;
    if (current) {
      const phase = status.current.phase || 'claimed';
      downloadState.textContent = `${active ? '正在下载' : '当前条目收尾'}：${current.title || current.task_id} · ${current.download_label || current.download_type || ''} · ${phase} · ${downloadCounts(status.counts)}${stopping ? ' · 已请求停止' : ''}`;
    } else if (active) {
      downloadState.textContent = connected
        ? `下载 Queue Runner 已连接，持续监听已生成歌曲；${downloadCounts(status.counts)}`
        : `下载监听已启动，等待 Chrome 扩展连接；${downloadCounts(status.counts)}`;
    } else {
      downloadState.textContent = `未运行；点击“开始下载”后按每首歌曲依次获取 MP3 和 Video。${downloadCounts(status && status.counts)}`;
    }
  };
  const refreshSunoDownloadWorker = async () => {
    try {
      renderSunoDownloadWorker(await downloadRequest('/v1/download/status'));
    } catch (error) {
      downloadBadge.className = 'badge failed';
      downloadBadge.textContent = '未连接';
      downloadStartButton.disabled = false;
      downloadStopButton.disabled = true;
      downloadState.className = 'suno-download-state error';
      downloadState.textContent = `无法连接下载 worker：${error.message || error} · 请先运行 run-suno-download-worker.ps1`;
    }
  };
  const ensureSunoDownloadPolling = () => {
    if (sunoDownloadPollTimer) return;
    sunoDownloadPollTimer = setInterval(refreshSunoDownloadWorker, 1500);
  };
  const startSunoDownloadWorker = async ({autoTriggered = false} = {}) => {
    if (sunoDownloadRequestInFlight) return false;
    sunoDownloadRequestInFlight = true;
    downloadStartButton.disabled = true;
    downloadState.className = 'suno-download-state active';
    downloadState.textContent = '正在启动 MP3 / Video 下载队列……';
    try {
      const status = await downloadRequest('/v1/download/start', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          task_ids:['bilibili-daily','douyin-curation','weibo-hourly'],
          scope:document.getElementById('suno-download-scope').value,
          auto_download:document.getElementById('suno-auto-download').checked,
          follow_future:document.getElementById('suno-download-follow-future').checked
        })
      });
      ensureSunoDownloadPolling();
      renderSunoDownloadWorker(status);
      return true;
    } catch (error) {
      downloadBadge.className = 'badge failed';
      downloadBadge.textContent = '启动失败';
      downloadState.className = 'suno-download-state error';
      downloadState.textContent = error.message || String(error);
      downloadStartButton.disabled = false;
      if (autoTriggered) {
        downloadState.textContent = `顺序发送已启动，但自动下载未启动：${error.message || error}`;
      }
      return false;
    } finally {
      sunoDownloadRequestInFlight = false;
      refreshSunoDownloadWorker();
    }
  };
  const stopSunoDownloadWorker = async () => {
    if (sunoDownloadRequestInFlight) return;
    sunoDownloadRequestInFlight = true;
    downloadStopButton.disabled = true;
    downloadState.className = 'suno-download-state';
    downloadState.textContent = '正在请求停止；已经触发的 Chrome 下载会完成并保存，未触发的不会继续。';
    try {
      const status = await downloadRequest('/v1/download/stop', {method:'POST'});
      ensureSunoDownloadPolling();
      renderSunoDownloadWorker(status);
    } catch (error) {
      downloadBadge.className = 'badge failed';
      downloadBadge.textContent = '停止失败';
      downloadState.className = 'suno-download-state error';
      downloadState.textContent = error.message || String(error);
    } finally {
      sunoDownloadRequestInFlight = false;
      refreshSunoDownloadWorker();
    }
  };
  const ensureSunoWorkerPolling = () => {
    if (sunoWorkerPollTimer) return;
    sunoWorkerPollTimer = setInterval(refreshSunoWorker, 1500);
  };
  const openSunoCreatePage = () => {
    if (sunoPageOpened) return;
    try {
      // This must stay synchronous with the dashboard click. Chrome may block
      // a popup opened only after an awaited worker request.
      const tab = window.open('https://suno.com/create', '_blank');
      if (tab) {
        sunoPageOpened = true;
        tab.focus();
      }
    } catch (_error) {
      // The worker status will show the missing visible page/extension.
    }
  };
  const startSunoWorker = async () => {
    if (sunoWorkerRequestInFlight) return;
    // Open the visible Create page during the user action, before any await.
    openSunoCreatePage();
    sunoWorkerRequestInFlight = true;
    startButton.disabled = true;
    workerState.className = 'suno-worker-state active';
    workerState.textContent = '正在启动顺序队列……';
    try {
      // Start download independently; it waits for verified /song/<id>
      // results and never opens or steals the generation /create tab.
      let autoDownloadPromise = Promise.resolve(false);
      if (document.getElementById('suno-auto-download').checked) {
        // Wait for acknowledgement so a failed auto-start is visible.
        autoDownloadPromise = startSunoDownloadWorker({autoTriggered:true});
      }
      const autoDownloadStarted = await autoDownloadPromise;
      const status = await workerRequest('/v1/browser/start', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          task_ids:['bilibili-daily','douyin-curation','weibo-hourly'],
          scope:document.getElementById('suno-scope').value,
          auto_create:document.getElementById('suno-auto-create').checked,
          follow_future:document.getElementById('suno-follow-future').checked
        })
      });
      ensureSunoWorkerPolling();
      renderSunoWorker(status);
      if (document.getElementById('suno-auto-download').checked && !autoDownloadStarted) {
        workerState.textContent += '（提示：自动下载 worker 未成功启动，请查看下载区域）';
      }
    } catch (error) {
      workerBadge.className = 'badge failed';
      workerBadge.textContent = '启动失败';
      workerState.className = 'suno-worker-state error';
      workerState.textContent = error.message || String(error);
      startButton.disabled = false;
    } finally {
      sunoWorkerRequestInFlight = false;
      refreshSunoWorker();
    }
  };
  const stopSunoWorker = async () => {
    if (sunoWorkerRequestInFlight) return;
    sunoWorkerRequestInFlight = true;
    stopButton.disabled = true;
    workerState.className = 'suno-worker-state';
    workerState.textContent = '正在请求停止；当前已提交条目会继续等待结果，未提交条目不会再点击 Create。';
    try {
      const status = await workerRequest('/v1/browser/stop', {method:'POST'});
      ensureSunoWorkerPolling();
      renderSunoWorker(status);
      await stopSunoDownloadWorker();
    } catch (error) {
      workerBadge.className = 'badge failed';
      workerBadge.textContent = '停止失败';
      workerState.className = 'suno-worker-state error';
      workerState.textContent = error.message || String(error);
    } finally {
      sunoWorkerRequestInFlight = false;
      refreshSunoWorker();
    }
  };
  window.startSunoWorker = startSunoWorker;
  window.stopSunoWorker = stopSunoWorker;
  window.startSunoDownloadWorker = startSunoDownloadWorker;
  window.stopSunoDownloadWorker = stopSunoDownloadWorker;
  ensureSunoWorkerPolling();
  refreshSunoWorker();
  ensureSunoDownloadPolling();
  refreshSunoDownloadWorker();
  document.getElementById('date').textContent = data.date;
  document.getElementById('updated').textContent = fmtTime(data.generated_at);
  document.getElementById('root').textContent = data.runtime_root;
  const metrics = [['completed','已完成'],['running','运行中'],['partial','部分完成'],['failed','失败'],['waiting','等待运行']];
  document.getElementById('summary').innerHTML = metrics.map(([key,label]) => `<div class="metric ${key}"><strong>${data.counts[key] || 0}</strong><span>${label}</span></div>`).join('');
  const fileLinks = files => files.length ? files.slice(0,40).map(file => `<a href="${encodeURI(file.path)}" title="${esc(file.path)}">${esc(file.name)} <small>${fmtSize(file.size)}</small></a>`).join('') : '<span class="hint">暂无文件</span>';
  const hourRows = task => task.runs.length ? `<div class="hours">${task.runs.map(run => { const q = run.suno_generation; const qc = q ? q.counts || {} : {}; const queue = q ? ` · Suno ${q.percent || 0}% (${qc.completed || 0}/${q.target_song_count || 0})` : ''; return `<div class="hour"><a href="${encodeURI(run.run_dir)}/">${esc(run.run_dir.split('/').pop())}</a><div class="mini-bar"><i style="width:${run.percent}%"></i></div><span>${run.percent}% · ${esc(run.status_label)}${esc(queue)}</span></div>`; }).join('')}</div>` : '<div class="hint">今天尚未产生小时目录</div>';
  const renderSunoGroups = suno => (suno.prompt_groups || []).map(group => {
    const label = `${group.title || group.prompt_id} · ${group.completed}/${group.target}`;
    const resultLinks = (group.variants || []).filter(v => v.song_url || v.result_file).map(v => v.song_url || v.result_file);
    const variants = (group.variants || []).map(v => {
      const result = v.song_url || v.result_file;
      const text = `v${String(v.variant_index).padStart(2,'0')} ${statusLabel(v.status)}`;
      return result ? `<a class="suno-variant ${esc(v.status)}" href="${encodeURI(result)}" title="${esc(result)}">${esc(text)}</a>` : `<span class="suno-variant ${esc(v.status)}">${esc(text)}</span>`;
    }).join('');
    const suffix = resultLinks.length ? ` · ${resultLinks.length} 个结果` : '';
    return `<div class="suno-group"><div title="${esc(group.source_file || '')}">${esc(label)}${esc(suffix)}</div><div class="mini-bar"><i style="width:${group.percent || 0}%"></i></div><span>${group.percent || 0}%</span><div class="suno-variants">${variants}</div></div>`;
  }).join('');
  const sunoRows = suno => {
    if (!suno) return '<div class="suno-empty">Suno 生成队列尚未建立。提示词文件存在不代表已经提交到 Suno。</div>';
    const c = suno.counts || {};
    const groups = renderSunoGroups(suno);
    const manifest = suno.browser_manifest ? `<a href="${encodeURI(suno.browser_manifest)}">浏览器任务清单</a>` : '';
    const download = suno.download || null;
    const downloadCountsValue = download ? download.counts || {} : {};
    const downloadSongs = download && download.songs ? download.songs.map(song => {
      const files = (song.downloads || []).map(item => {
        const file = item.download_file;
        const label = `${item.label || item.type || '文件'} · ${statusLabel(item.status)}`;
        return file ? `<a class="download-file ${esc(item.status)}" href="${encodeURI(file)}" title="${esc(file)}">${esc(label)}${item.bytes ? ` · ${fmtSize(item.bytes)}` : ''}</a>` : `<span class="download-file ${esc(item.status)}">${esc(label)}</span>`;
      }).join('');
      return `<div class="download-song"><span>${esc(song.title || song.song_id)}</span><div>${files || '<span class="hint">暂无下载项</span>'}</div></div>`;
    }).join('') : '';
    const downloadHtml = download ? `<div class="suno-download"><div class="suno-head"><strong>独立下载 · MP3 / Video</strong><span>${esc(statusLabel(download.status))} · ${download.percent || 0}%</span></div><div class="suno-stats"><span>歌曲 <b>${download.song_count || 0}</b></span><span>目标文件 <b>${download.target_download_count || 0}</b></span><span>MP3 完成 <b>${download.mp3_completed_count || 0}</b></span><span>Video 完成 <b>${download.video_completed_count || 0}</b></span><span>已完成 <b>${downloadCountsValue.completed || 0}</b></span><span>排队 <b>${downloadCountsValue.queued || 0}</b></span><span>失败 <b>${downloadCountsValue.failed || 0}</b></span><span>阻塞 <b>${downloadCountsValue.blocked || 0}</b></span></div><div class="suno-bar"><i style="width:${download.percent || 0}%"></i></div><details><summary>查看每首歌曲的 MP3 / Video 文件</summary><div class="download-songs">${downloadSongs || '<span class="hint">尚未发现已生成歌曲</span>'}</div></details><div class="folder">状态：<code>${esc(download.status_path)}</code></div></div>` : '<div class="suno-empty">下载队列尚未建立；生成歌曲收到真实 Suno 链接后才会出现 MP3 / Video 下载项。</div>';
    return `<div class="suno"><div class="suno-head"><strong>Suno 批量生成</strong><span>${esc(statusLabel(suno.status))} · ${suno.percent || 0}%</span></div><div class="suno-stats"><span>提示词 <b>${suno.prompt_count || 0}</b></span><span>每条 <b>${suno.variants_per_prompt || 6}</b> 首</span><span>目标创建项 <b>${suno.target_song_count || 0}</b></span><span>完成项 <b>${c.completed || 0}</b></span><span>已生成歌曲 <b>${suno.generated_song_count || 0}</b></span><span>排队 <b>${c.queued || 0}</b></span><span>失败 <b>${c.failed || 0}</b></span><span>阻塞 <b>${c.blocked || 0}</b></span></div><div class="suno-bar"><i style="width:${suno.percent || 0}%"></i></div><details><summary>查看每条提示词的 6 个变体（${suno.prompt_count || 0} 条）</summary><div class="suno-groups">${groups || '<span class="hint">暂无提示词分组</span>'}</div></details><div class="folder">状态：<code>${esc(suno.status_path)}</code>${manifest ? ` · ${manifest}` : ''}</div>${downloadHtml}</div>`;
  };
  document.getElementById('tasks').innerHTML = data.tasks.map(task => { const run = task.latest; const sunoScope = task.suno_enabled ? 'Suno：启用' : 'Suno：不适用'; return `<article class="task">
    <div class="task-top"><div><h3>${esc(task.name)}</h3><p class="desc">${esc(task.description)} · ${sunoScope}</p></div><span class="badge ${run.status}">${esc(run.status_label)}</span></div>
    <div class="bar-row"><div class="bar"><div class="fill" style="width:${run.percent}%"></div></div><span class="percent">${run.percent}%</span></div>
    <dl class="meta"><div><dt>计划</dt><dd>${esc(task.schedule)}</dd></div><div><dt>阶段</dt><dd>${esc(run.stage)}</dd></div><div><dt>本日运行</dt><dd>${task.run_count}${task.hourly ? ' 个小时目录' : ' 个日期目录'}</dd></div><div><dt>更新时间</dt><dd>${esc(fmtTime(run.updated_at))}</dd></div></dl>
    <p class="message">${esc(run.message)}</p><div class="folder">目录：<code>${esc(run.run_dir)}</code></div>
    ${task.suno_enabled ? sunoRows(run.suno_generation) : '<div class="suno-empty">本任务仅采集/记录公开数据，不进入歌词生成、Suno 提示词或 Suno 生成队列。</div>'}
    ${task.hourly ? hourRows(task) : ''}<details><summary>查看本日文件（${run.files.length}）</summary><div class="files">${fileLinks(run.files)}</div></details>
  </article>`; }).join('');
  const queueCards = (data.suno_queues || []).map(queue => {
    const suno = queue.suno || {};
    const counts = suno.counts || {};
    const title = `${queue.task_name} · ${queue.run_dir}`;
    const download = queue.download || {};
    const meta = `${statusLabel(suno.status)} · ${suno.prompt_count || 0} 条提示词 · 目标 ${suno.target_song_count || 0} 首 · 完成 ${counts.completed || 0} · 下载 ${download.completed_download_count || 0}/${download.target_download_count || 0}`;
    return `<details class="suno-queue"><summary><span class="suno-queue-title">${esc(title)}</span><span class="suno-queue-meta">${esc(meta)}</span></summary>${sunoRows(suno)}</details>`;
  }).join('');
  document.getElementById('suno-overview').innerHTML = queueCards || '<div class="suno-empty">当前日期尚未建立 Suno 生成队列。</div>';
  setTimeout(() => location.reload(), 60000);
</script>
</body>
</html>
'''


def build_dashboard(date_text, output):
    data = dashboard_data(date_text)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(HTML_TEMPLATE.replace("__DATA__", js_data(data)), encoding="utf-8")
    return data, output


def task_by_id(task_id):
    for task in TASKS:
        if task["id"] == task_id:
            return task
    raise ValueError(f"unknown task id: {task_id}")


def init_date(date_text):
    for task in TASKS:
        date_dir = TASK_ROOT / task["id"] / date_text
        date_dir.mkdir(parents=True, exist_ok=True)
        if task.get("hourly"):
            continue
        marker = date_dir / "task-progress.json"
        if not marker.exists():
            write_json(marker, {"task_id": task["id"], "date": date_text, "status": "waiting", "percent": 0, "stage": "等待本轮任务", "message": "已建立本日任务目录，等待定时任务运行", "updated_at": now_iso()})


def write_progress(args):
    task = task_by_id(args.task_id)
    run_dir = TASK_ROOT / task["id"] / args.date
    if task.get("hourly"):
        if not args.hour:
            raise ValueError("hourly task requires --hour HH-mm")
        run_dir /= args.hour
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "task-progress.json", {"task_id": task["id"], "date": args.date, "hour": args.hour, "status": args.status, "percent": args.percent, "stage": args.stage, "message": args.message, "updated_at": now_iso()})
    return run_dir


def main():
    parser = argparse.ArgumentParser(description="Build the local scheduled-task dashboard")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create task/date directories and waiting markers")
    init.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    build = sub.add_parser("build", help="write a standalone dashboard.html")
    build.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    build.add_argument("--output", default=str(DEFAULT_DASHBOARD))
    progress = sub.add_parser("progress", help="write one task run progress marker and rebuild the dashboard")
    progress.add_argument("--task-id", required=True, choices=[task["id"] for task in TASKS])
    progress.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    progress.add_argument("--hour", default=None, help="HH-mm for the hourly Weibo task")
    progress.add_argument("--status", choices=("waiting", "running", "partial", "completed", "failed"), required=True)
    progress.add_argument("--percent", type=int, required=True)
    progress.add_argument("--stage", required=True)
    progress.add_argument("--message", required=True)
    progress.add_argument("--dashboard", default=str(DEFAULT_DASHBOARD))
    args = parser.parse_args()
    if args.command == "init":
        init_date(args.date)
        data, output = build_dashboard(args.date, DEFAULT_DASHBOARD)
        print(json.dumps({"status": "ok", "task_count": len(data["tasks"]), "dashboard": str(output), "date": args.date}, ensure_ascii=False, indent=2))
    elif args.command == "build":
        data, output = build_dashboard(args.date, args.output)
        print(json.dumps({"status": "ok", "task_count": len(data["tasks"]), "dashboard": str(output), "date": args.date}, ensure_ascii=False, indent=2))
    else:
        run_dir = write_progress(args)
        data, output = build_dashboard(args.date, args.dashboard)
        print(json.dumps({"status": "ok", "run_dir": str(run_dir), "dashboard": str(output), "counts": data["counts"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
