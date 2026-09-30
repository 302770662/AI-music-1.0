"""Curate authorized Douyin video metadata into event briefs and Suno prompts."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import importlib.util
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save(value, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def public_metadata(item: dict, source_file: str) -> dict:
    """Keep only public metadata; never forward media or full protected text."""
    interaction = item.get("interaction") if isinstance(item.get("interaction"), dict) else {}
    return {
        "video_id": item.get("video_id") or item.get("item_id"),
        "platform": item.get("platform"),
        "source_id": item.get("source_id"),
        "category": item.get("category"),
        "url": item.get("url"),
        "title": item.get("title"),
        "author": item.get("author") or item.get("creator"),
        "genre": item.get("genre"),
        "topic": item.get("topic") or item.get("hot_topic") or item.get("topics"),
        "published": item.get("published") or item.get("published_display"),
        "heat": item.get("heat"),
        "likes": item.get("likes") or item.get("likes_display") or interaction.get("likes_display"),
        "comments": item.get("comments") or item.get("comments_display") or interaction.get("comments_display"),
        "shares": item.get("shares") or item.get("shares_display") or interaction.get("shares_display"),
        "viewers": item.get("viewers") or item.get("viewers_display"),
        "position": item.get("position") or item.get("rank"),
        "captured_at": item.get("captured_at"),
        "source_file": source_file,
        "license_status": item.get("license_status") or "metadata_only_public",
    }


def collect_records(input_dir: Path):
    records = []
    generated_json = {"task-progress.json", "curation.json", "curation-status.json"}
    json_paths = sorted(input_dir.rglob("*.json")) if input_dir.exists() else []
    jsonl_paths = sorted(input_dir.rglob("trend-snapshot.jsonl")) if input_dir.exists() else []
    for path in [*json_paths, *jsonl_paths]:
        # A task may intentionally use one date/hour directory for both
        # ingestion and creative output. Do not feed our own progress or
        # generated package back into the next retry as a source record.
        if path.name in {"trend-status.json", "hotspot.json"} | generated_json:
            continue
        if path.suffix.lower() == ".jsonl":
            try:
                items = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            except (OSError, json.JSONDecodeError):
                continue
            default_license = None
        else:
            data = load_json(path)
            if not isinstance(data, dict):
                continue
            default_license = data.get("license_status")
            items = data.get("items") if isinstance(data.get("items"), list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row["source_file"] = str(path)
            row["license_status"] = row.get("license_status", default_license)
            recording = data.get("recording") if path.suffix.lower() == ".json" and isinstance(data.get("recording"), dict) else {}
            row["recording_status"] = recording.get("status", row.get("recording_status"))
            # Public metadata is sufficient for trend ranking and can be used
            # even when the underlying video/audio is not downloadable.
            records.append(public_metadata(row, str(path)))
    unique = {}
    for row in records:
        key = str(row.get("video_id") or row.get("url") or f"{row.get('author')}|{row.get('title')}")
        unique[key] = row
    return list(unique.values())


def endpoint(base_url: str) -> str:
    base_url = str(base_url or "https://api.deepseek.com").rstrip("/")
    return base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"


def discover_music_analysis(input_dir: str | Path, date: str, cfg: dict):
    """Find the local analysis root beside a trend input when no path is given."""
    configured = cfg.get("music_analysis_path") or (cfg.get("music_analysis") or {}).get("path")
    if configured and Path(configured).exists():
        return str(configured)

    source = Path(input_dir).resolve()
    candidates = []
    for parent in (source, *source.parents):
        analysis_root = parent / "analysis"
        if analysis_root.is_dir():
            candidates.extend([
                analysis_root / date,
                analysis_root / f"{date}-daily",
            ])
            candidates.extend(sorted(
                (p for p in analysis_root.iterdir() if p.is_dir() and date in p.name),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            ))
            candidates.append(analysis_root)
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.rglob("*.analysis.json")):
            return str(candidate)
    return None


def load_music_profile(path: str | None, limit: int | None = 20, min_support: int | None = None):
    """Load abstract local music analysis and build one auditable style profile."""
    if not path:
        return {"status": "unavailable", "reason": "music analysis path was not provided", "song_count": 0}
    pipeline_path = Path(__file__).with_name("pipeline.py")
    try:
        spec = importlib.util.spec_from_file_location("hotspot_pipeline_for_curation", pipeline_path)
        if not spec or not spec.loader:
            raise ImportError("pipeline.py could not be loaded")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        raw_records = module.load_analysis_records(path, None)
        audio_suffixes = {".wav", ".mp3", ".flac", ".mflac", ".m4a", ".ogg"}
        records = []
        for record in raw_records:
            source = str(record.get("source") or "")
            music = record.get("music")
            # Ignore generated reports/batch JSON that happen to contain a
            # music-shaped object; only actual audio analysis is a style source.
            if Path(source).suffix.lower() in audio_suffixes and isinstance(music, dict):
                records.append(record)
        if limit:
            records = records[:limit]
        if not records:
            return {"status": "unavailable", "reason": "no valid music analysis records found", "path": str(path), "song_count": 0}
        aggregate = module.aggregate_analysis_records(records, min_support)
        music = aggregate["music"]
        effnet = music.get("effnet") or {}
        tags = []
        for row in effnet.get("accepted_tags") or []:
            if isinstance(row, dict) and row.get("label"):
                tags.append({
                    "label": row["label"],
                    "probability": row.get("probability"),
                    "mean_probability": row.get("mean_probability"),
                    "support_count": row.get("support_count"),
                    "support_ratio": row.get("support_ratio"),
                })
        raw_tag_stats = {}
        model_names = set()
        model_label_counts = []
        for record in records:
            raw_effnet = ((record.get("music") or {}).get("effnet") or {})
            if raw_effnet.get("tags_model"):
                model_names.add(str(raw_effnet["tags_model"]))
            try:
                if raw_effnet.get("label_count"):
                    model_label_counts.append(int(raw_effnet["label_count"]))
            except (TypeError, ValueError):
                pass
            for row in raw_effnet.get("tags") or []:
                if not isinstance(row, dict) or not row.get("label"):
                    continue
                label = str(row["label"])
                stats = raw_tag_stats.setdefault(label, {"probabilities": [], "support_count": 0, "strong_count": 0})
                try:
                    probability = float(row.get("probability"))
                except (TypeError, ValueError):
                    probability = 0.0
                stats["probabilities"].append(probability)
                stats["support_count"] += 1
                if not row.get("weak_label", False):
                    stats["strong_count"] += 1
        profile_min_support = effnet.get("min_support")
        if profile_min_support is None:
            profile_min_support = max(1, int(min_support)) if min_support is not None else (2 if len(records) >= 5 else 1)
        candidates = []
        for label, stats in raw_tag_stats.items():
            probabilities = stats["probabilities"]
            candidates.append({
                "label": label,
                "probability": round(sum(probabilities) / len(records), 4),
                "mean_probability": round(sum(probabilities) / len(probabilities), 4),
                "support_count": stats["support_count"],
                "support_ratio": round(stats["support_count"] / len(records), 4),
                "weak_label": stats["strong_count"] == 0 or stats["support_count"] < profile_min_support,
            })
        candidates.sort(key=lambda row: (row["probability"], row["support_count"]), reverse=True)
        resolved_model = (next(iter(model_names)) if len(model_names) == 1 else
                          "aggregated_discogs_style_profile" if not model_names else
                          "aggregated:" + ",".join(sorted(model_names)))
        return {
            "status": "ok",
            "mode": "average",
            "path": str(path),
            "song_count": len(records),
            "tags_model": resolved_model,
            "label_count": max(model_label_counts) if model_label_counts else (
                519 if resolved_model == "Discogs-MAEST-519" else effnet.get("label_count", 0)
            ),
            "min_support": profile_min_support,
            "style_tags": tags[:12],
            "candidate_style_tags": candidates[:12],
            "bpm": music.get("bpm"),
            "key": music.get("key_estimate"),
            "energy": music.get("rms_energy"),
            "spectral_centroid": music.get("spectral_centroid"),
        }
    except Exception as exc:
        return {"status": "unavailable", "reason": f"music analysis load failed: {type(exc).__name__}: {exc}",
                "path": str(path), "song_count": 0}


def _profile_for_prompt(profile):
    """Keep DeepSeek input abstract: tags and measurable features, no source text."""
    if not isinstance(profile, dict) or profile.get("status") != "ok":
        return {"status": "unavailable", "reason": (profile or {}).get("reason", "no profile")}
    return {
        "status": "ok",
        "mode": profile.get("mode"),
        "song_count": profile.get("song_count"),
        "tags_model": profile.get("tags_model"),
        "style_tags": profile.get("style_tags", [])[:12],
        "candidate_style_tags": profile.get("candidate_style_tags", [])[:12],
        "candidate_style_tags_policy": "weak_signals_only_max_two_influences",
        "bpm": profile.get("bpm"),
        "key": profile.get("key"),
        "energy": profile.get("energy"),
        "spectral_centroid": profile.get("spectral_centroid"),
    }


def _tag_names(profile):
    names = []
    for row in (profile or {}).get("style_tags", []):
        label = row.get("label") if isinstance(row, dict) else row
        name = str(label or "").split("---")[-1].replace("&", "and").strip().lower()
        if name and name not in names:
            names.append(name)
    return names


def fuse_event_with_music(event, profile):
    """Guarantee that the saved event prompt records the music profile used."""
    if not isinstance(event, dict) or not isinstance(profile, dict) or profile.get("status") != "ok":
        return event
    current = str(event.get("suno_style_prompt") or "").strip()
    event.setdefault("deepseek_suno_style_prompt", current)
    current_lower = current.lower()
    strong_tags = _tag_names(profile)
    weak_tags = _tag_names({"style_tags": (profile.get("candidate_style_tags") or [])[:2]})
    tags = strong_tags or weak_tags
    constraints = []
    if tags:
        constraints.extend(tags[:3])
    bpm = profile.get("bpm")
    if isinstance(bpm, (int, float)) and bpm > 0:
        constraints.append(f"{round(bpm)} BPM")
    if profile.get("key") and not re.search(r"\bkey(?:\s+of)?\b", current, re.I):
        constraints.append(f"key {profile['key']}")
    energy = profile.get("energy")
    if isinstance(energy, (int, float)) and not re.search(r"\b(?:low|medium|high)\s+energy\b", current, re.I):
        constraints.append("low energy" if energy < 0.12 else "high energy" if energy > 0.25 else "medium energy")
    missing = [x for x in constraints if x.lower() not in current_lower]
    if missing:
        event["suno_style_prompt"] = ", ".join(missing + ([current] if current else []))
    event["music_fusion"] = {
        "status": "ok",
        "mode": profile.get("mode"),
        "song_count": profile.get("song_count"),
        "tags_model": profile.get("tags_model"),
        "style_tags_used": profile.get("style_tags", [])[:12],
        "candidate_style_tags": profile.get("candidate_style_tags", [])[:12],
        "weak_style_tags_used": (profile.get("candidate_style_tags") or [])[:2] if not strong_tags else [],
        "music_signal_strength": "strong" if strong_tags else "weak_candidate" if weak_tags else "features_only",
        "bpm_used": profile.get("bpm"),
        "key_used": profile.get("key"),
        "energy_used": profile.get("energy"),
    }
    return event


def call_deepseek(records, cfg, music_profile=None):
    dcfg = cfg.get("deepseek") or {}
    env_name = str(dcfg.get("api_key_env", "HOTSPOT_DEEPSEEK_API_KEY"))
    key = os.environ.get(env_name)
    if not key:
        return {"status": "unavailable", "reason": f"environment variable {env_name} is not configured"}
    # A 30-event curation response is substantially larger than a single
    # lyric-generation response.  Keep a separate budget so the JSON is not
    # truncated at the default creative-generation limit.
    curation_max_tokens = max(
        6000,
        int(dcfg.get("curation_max_tokens", dcfg.get("max_tokens", 1200))),
    )
    payload = {
        "model": str(dcfg.get("model", "deepseek-chat")),
        "temperature": 0.35,
        "max_tokens": curation_max_tokens,
        "stream": False,
        # DeepSeek supports JSON mode; this prevents long curation replies
        # from drifting into invalid prose or partially escaped JSON.
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": (
                "你是热点事件编辑和原创音乐策划。输入只包含公开可见的视频元数据，不包含视频文件、音轨、"
                "完整字幕或完整ASR文本。请根据全部输入记录选择最值得梳理的最多30个事件，按热度信号、"
                "互动量、时效性和跨记录重复出现情况排序，去重、合并同一事件，避免未经证实的断言。"
                "必须只返回JSON对象，不要Markdown。字段为 events 数组；每个事件包含 "
                "rank,title,summary,why_worth_review,keywords,emotion,scene,visual_elements,"
                "audio_mood,source_refs,suno_style_prompt,lyrics_brief。严格限制长度：summary不超过50个汉字，"
                "why_worth_review不超过40个汉字，keywords最多5个，emotion最多2个，scene最多1个，"
                "visual_elements最多2个，audio_mood最多2个，source_refs最多3个，"
                "suno_style_prompt不超过35个英文词，lyrics_brief不超过50个汉字。"
                "只输出必要信息，避免长篇解释，以确保完整返回全部事件。"
                "如果提供 music_profile，suno_style_prompt 必须融合该音乐画像的稳定风格标签、"
                "速度、调性和能量；候选/低置信度标签可以作为最多两个弱风格线索，需转成宽泛英文音乐属性，"
                "不得表述为确定识别或覆盖高置信度标签；"
                "热点只影响主题、情绪和场景，不得覆盖音乐画像。"
                "suno_style_prompt 使用英文音乐属性，不要具体艺人、已有歌曲名、原歌词或声线模仿；"
                "lyrics_brief 只写原创歌词创作方向，不要复述或续写原视频字幕。"
            )},
            {"role": "user", "content": json.dumps({
                "public_video_metadata": records,
                "music_profile": _profile_for_prompt(music_profile or {}),
            }, ensure_ascii=False)},
        ],
    }
    try:
        body = None
        result = None
        last_content = ""
        # One bounded retry handles occasional truncated/invalid JSON from the
        # gateway without duplicating the whole curation workflow.
        for attempt in range(2):
            req_payload = dict(payload)
            if attempt:
                req_payload["max_tokens"] = min(max(int(payload["max_tokens"]) * 2, 12000), 24000)
                req_payload["temperature"] = 0.2
                req_payload["messages"] = list(payload["messages"])
                req_payload["messages"][0] = dict(req_payload["messages"][0])
                req_payload["messages"][0]["content"] += " 这是重试请求：必须输出完整、可解析的JSON对象，最多保留20个事件，进一步压缩字段。"
            request = urllib.request.Request(endpoint(dcfg.get("base_url")),
                data=json.dumps(req_payload, ensure_ascii=False).encode("utf-8"),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=float(dcfg.get("curation_timeout_sec", dcfg.get("timeout_sec", 45)))) as response:
                body = json.loads(response.read().decode("utf-8"))
            content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content", "")
            last_content = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(content).strip(), flags=re.I)
            cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", last_content)
            cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
            try:
                result = json.loads(cleaned)
                break
            except json.JSONDecodeError:
                # Gateways may truncate a long JSON object after several
                # complete events. Salvage only complete event objects rather
                # than treating the whole curation run as empty/failed.
                try:
                    marker = cleaned.find('"events"')
                    array_start = cleaned.find('[', marker)
                    if marker >= 0 and array_start >= 0:
                        decoder = json.JSONDecoder()
                        pos = array_start + 1
                        salvaged = []
                        while pos < len(cleaned):
                            while pos < len(cleaned) and cleaned[pos] in ' \t\r\n,':
                                pos += 1
                            if pos >= len(cleaned) or cleaned[pos] != '{':
                                break
                            item, end = decoder.raw_decode(cleaned, pos)
                            if isinstance(item, dict):
                                salvaged.append(item)
                            pos = end
                        if salvaged:
                            result = {"events": salvaged}
                            break
                except (json.JSONDecodeError, ValueError):
                    pass
                if attempt == 1:
                    Path("deepseek-invalid-response.json").write_text(last_content, encoding="utf-8")
                    raise
        events = result.get("events") if isinstance(result, dict) else None
        if not isinstance(events, list):
            raise ValueError("DeepSeek response has no events array")
        events = [fuse_event_with_music(event, music_profile or {}) for event in events[:30]]
        return {"status": "ok", "model": payload["model"], "events": events, "usage": body.get("usage", {})}
    except urllib.error.HTTPError as exc:
        return {"status": "failed", "reason": f"DeepSeek HTTP {exc.code}"}
    except Exception as exc:
        return {"status": "failed", "reason": f"DeepSeek response error: {type(exc).__name__}: {exc}"}


def generate_event_lyrics(event, profile, cfg):
    """Generate original lyrics from one abstract hotspot event and music profile."""
    pipeline_path = Path(__file__).with_name("pipeline.py")
    try:
        spec = importlib.util.spec_from_file_location("hotspot_pipeline_for_lyrics", pipeline_path)
        if not spec or not spec.loader:
            raise ImportError("pipeline.py could not be loaded")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        dcfg = dict((cfg or {}).get("deepseek") or {})
        lyric_tokens = int(dcfg.get("lyrics_max_tokens", 700))
        lyric_cfg = dict(cfg or {})
        lyric_cfg["deepseek"] = dict(dcfg, max_tokens=lyric_tokens)
        music = {
            "bpm": profile.get("bpm"),
            "key_estimate": profile.get("key"),
            "rms_energy": profile.get("energy"),
            "spectral_centroid": profile.get("spectral_centroid"),
            "effnet": {
                "tags": profile.get("style_tags") or [],
                "tags_model": profile.get("tags_model"),
            },
        }
        lyrics_tags = {
            "theme": [str(x) for x in (event.get("keywords") or [])[:6]],
            "emotion_curve": [str(x) for x in (event.get("emotion") or [])[:3]],
            "imagery": [str(event.get("scene"))] if event.get("scene") else [],
            "sections": [
                {"section": "Verse 1", "markers": ["Verse 1"]},
                {"section": "Pre-Chorus", "markers": ["Pre-Chorus"]},
                {"section": "Chorus", "markers": ["Chorus"]},
                {"section": "Bridge / Instrumental Break", "markers": ["Bridge", "Instrumental Break"]},
                {"section": "Outro", "markers": ["Outro"]},
            ],
            "repetition_terms": [],
            "source": "weibo_public_metadata",
        }
        hotspot = {
            "summary": "；".join(x for x in (str(event.get("title") or ""), str(event.get("summary") or "")) if x),
            "keywords": event.get("keywords") or [],
            "emotion": event.get("emotion") or [],
            "scene": [event.get("scene")] if event.get("scene") else [],
            "trend_style_tags": [],
            "trend_production_cues": [],
            "music_chart_count": 0,
        }
        creative = {
            "suno_prompt": event.get("suno_style_prompt") or "",
            "arrangement": ["[Powerful Intro]", "[Verse 1]", "[Pre-Chorus]", "[Chorus]",
                            "[Bridge]", "[Instrumental Break]", "[Outro]"],
        }
        lyrics_tags = {
            "theme": [event.get("title", "")],
            "emotion_curve": event.get("emotion") or [],
            "imagery": event.get("visual_elements") or [],
            "sections": [],
            "repetition_terms": event.get("keywords") or [],
        }
        hotspot = {
            "summary": event.get("summary", ""),
            "keywords": event.get("keywords") or [],
            "emotion": event.get("emotion") or [],
            "scene": event.get("scene") or [],
        }
        return module.generate_original_lyrics(lyrics_tags, music, creative, hotspot, lyric_cfg)
    except Exception as exc:
        return {"status": "failed", "provider": "deepseek", "text": "",
                "reason": f"lyrics generation error: {type(exc).__name__}: {exc}"}


def attach_event_lyrics(result, profile, cfg):
    """Attach per-event original lyrics while preserving partial failures."""
    if result.get("status") != "ok":
        return result
    events = result.get("events") or []
    generated = 0
    failures = 0
    dcfg = (cfg or {}).get("deepseek") or {}
    # Per-event lyric calls are independent.  A small bounded pool prevents
    # 30 sequential requests (each with a long network timeout) from making
    # the hourly task appear hung, while keeping gateway load controlled.
    workers = max(1, min(4, int(dcfg.get("lyrics_max_concurrency", 4))))
    generations = [None] * len(events)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lyrics") as pool:
        futures = {pool.submit(generate_event_lyrics, event, profile, cfg): i
                   for i, event in enumerate(events)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                generations[index] = future.result()
            except Exception as exc:
                generations[index] = {"status": "failed", "provider": "deepseek",
                                      "text": "", "reason": f"worker error: {type(exc).__name__}: {exc}"}
    for event, generation in zip(events, generations):
        generation = generation or {"status": "failed", "provider": "deepseek", "text": "",
                                    "reason": "lyrics worker returned no result"}
        event["generated_lyrics"] = generation.get("text", "")
        event["lyrics_generation"] = {key: value for key, value in generation.items() if key != "text"}
        if generation.get("status") == "ok" and generation.get("text"):
            generated += 1
        else:
            failures += 1
    result["lyrics_generation_summary"] = {
        "status": "ok" if generated == len(events) else "partial" if generated else "failed",
        "requested": len(events),
        "generated": generated,
        "failed": failures,
        "provider": "deepseek",
    }
    return result


def markdown(date, result):
    lines = [f"# 公开热点梳理 - {date}", "", "> 基于公开热点元数据；不包含视频文件、音轨或完整字幕。机器数据见 `curation.json`，提示词见 `suno-prompts.jsonl`。", ""]
    if result.get("status") != "ok":
        lines += [f"状态：`{result.get('status')}`", "", f"原因：{result.get('reason', '-')}", ""]
        return "\n".join(lines)
    events = result.get("events", [])
    profile = result.get("music_profile") or {}
    if profile.get("status") == "ok":
        lines += [f"音乐画像：**{profile.get('song_count', 0)}** 首，模型 `{profile.get('tags_model', '-')}`，",
                  f"高置信度标签：{', '.join(_tag_names(profile)[:8]) or '无'}；候选标签：{', '.join(_tag_names({'style_tags': profile.get('candidate_style_tags', [])})[:8]) or '无'}；",
                  f"BPM：{profile.get('bpm') or '-'}；调性：{profile.get('key') or '-'}；能量：{profile.get('energy') or '-'}", ""]
    else:
        lines += [f"音乐画像：`unavailable`（{profile.get('reason', '未提供音乐分析')}）", ""]
    lines += [f"共梳理事件：**{len(events)}**", ""]
    def labels(value):
        if isinstance(value, list):
            return ", ".join(str(x.get("label", x)) if isinstance(x, dict) else str(x) for x in value)
        if isinstance(value, dict):
            return str(value.get("label", value))
        return str(value or "-")
    for event in events:
        fusion = event.get("music_fusion") or {}
        fusion_tags = fusion.get("style_tags_used") or fusion.get("weak_style_tags_used") or []
        fusion_strength = fusion.get("music_signal_strength", "features_only")
        lines += [f"## {event.get('rank', '-')}. {event.get('title', '未命名事件')}", "",
                  f"**摘要**：{event.get('summary', '-')}", "",
                  f"**梳理价值**：{event.get('why_worth_review', '-')}", "",
                  f"**关键词**：{labels(event.get('keywords'))}", "",
                  f"**情绪/场景**：{labels(event.get('emotion'))} / {labels(event.get('scene'))}", "",
                  f"**视觉与声音**：{labels(event.get('visual_elements'))}；{labels(event.get('audio_mood'))}", "",
                  f"**Suno 风格**：`{event.get('suno_style_prompt', '-')}`", "",
                  f"**音乐融合**：{labels(fusion_tags)}（{fusion_strength}）；{fusion.get('bpm_used') or '-'} BPM；调性 {fusion.get('key_used') or '-'}；能量 {fusion.get('energy_used') or '-'}", "",
                  f"**歌词方向**：{event.get('lyrics_brief', '-')}", "",
                  f"**原创歌词状态**：{(event.get('lyrics_generation') or {}).get('status', '未请求')}", "",
                  f"**原创歌词**：\n{event.get('generated_lyrics', '') or '（未生成）'}", "", "---", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Curate authorized Douyin videos with DeepSeek")
    parser.add_argument("--input", required=True, help="directory containing authorized video JSON records")
    parser.add_argument("--output", required=True, help="output directory")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--music-analysis", default=None,
                        help="directory or *.analysis.json path; aggregate local music style into every event")
    parser.add_argument("--music-limit", type=int, default=20,
                        help="maximum number of music analysis records used for the average profile")
    parser.add_argument("--min-support", type=int, default=None,
                        help="minimum song support for an aggregated style tag")
    parser.add_argument("--generate-lyrics", action="store_true",
                        help="generate original Chinese lyrics for every curated event via DeepSeek")
    args = parser.parse_args()
    try:
        import yaml
        cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}
    records = collect_records(Path(args.input))
    music_analysis_path = args.music_analysis or discover_music_analysis(args.input, args.date, cfg)
    music_profile = load_music_profile(music_analysis_path, args.music_limit, args.min_support)
    if music_analysis_path and music_profile.get("status") == "unavailable":
        music_profile["path"] = str(music_analysis_path)
    result = call_deepseek(records, cfg, music_profile) if records else {"status": "unavailable", "reason": "no public video records found"}
    print(json.dumps({"stage": "curation", "status": result.get("status"), "events": len(result.get("events", []))}, ensure_ascii=False), flush=True)
    if args.generate_lyrics:
        print(json.dumps({"stage": "lyrics", "status": "starting", "events": len(result.get("events", []))}, ensure_ascii=False), flush=True)
        result = attach_event_lyrics(result, music_profile, cfg)
        print(json.dumps({"stage": "lyrics", "status": (result.get("lyrics_generation_summary") or {}).get("status"), "generated": (result.get("lyrics_generation_summary") or {}).get("generated", 0)}, ensure_ascii=False), flush=True)
    result.update({"date": args.date, "input": str(Path(args.input)), "public_video_count": len(records),
                   "captured_at": datetime.now().astimezone().isoformat(), "music_profile": music_profile})
    out = Path(args.output)
    save(result, out / "curation.json")
    save({"date": args.date, "status": result["status"], "public_video_count": len(records),
          "event_count": len(result.get("events", [])), "reason": result.get("reason"),
          "music_profile_status": music_profile.get("status"),
          "music_song_count": music_profile.get("song_count", 0),
          "music_tags_model": music_profile.get("tags_model"),
          "lyrics_generation": result.get("lyrics_generation_summary", {"status": "not_requested"})}, out / "curation-status.json")
    prompts = [{"rank": event.get("rank"), "title": event.get("title"),
                "suno_style_prompt": event.get("suno_style_prompt", ""),
                "lyrics_brief": event.get("lyrics_brief", ""),
                "generated_lyrics": event.get("generated_lyrics", ""),
                "lyrics_generation": event.get("lyrics_generation", {}),
                "source_refs": event.get("source_refs", []),
                "music_fusion": event.get("music_fusion", {})}
               for event in result.get("events", [])]
    (out / "suno-prompts.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in prompts), encoding="utf-8")
    prompt_lines = [f"# Suno 音乐风格提示词 - {args.date}", "",
                    "> 热点来源为公开元数据；提示词使用抽象音乐属性，不包含艺人、原歌词或声线模仿。", ""]
    for event in result.get("events", []):
        prompt_lines += [f"## {event.get('rank', '-')}. {event.get('title', '未命名事件')}", "",
                         "### Style Prompt", "", f"`{event.get('suno_style_prompt', '-')}`", "",
                         "### Lyrics Brief", "", str(event.get("lyrics_brief", "-")), "",
                         "### Original Lyrics", "", str(event.get("generated_lyrics", "（未生成）")), "", "---", ""]
    (out / "suno-prompts.md").write_text("\n".join(prompt_lines), encoding="utf-8")
    lyrics_lines = [f"# DeepSeek 原创歌词 - {args.date}", "",
                    "> 歌词根据微博公开热点的摘要/关键词与本地音乐画像生成；未发送原歌词、完整字幕或完整 ASR。", ""]
    for event in result.get("events", []):
        lyrics_lines += [f"## {event.get('rank', '-')}. {event.get('title', '未命名事件')}", "",
                         f"生成状态：`{(event.get('lyrics_generation') or {}).get('status', '未请求')}`", "",
                         event.get("generated_lyrics") or "（未生成）", "", "---", ""]
    (out / "generated-lyrics.md").write_text("\n".join(lyrics_lines), encoding="utf-8")
    (out / "curation-report.md").write_text(markdown(args.date, result), encoding="utf-8")
    try:
        from suno_batch_generator import create_queue, queue_result
        queue_status = create_queue(out / "suno-prompts.jsonl", variants=6)
        queue_info = queue_result(queue_status, out / "suno-generation")
    except Exception as exc:
        # Curated metadata and prompts remain available if queue setup fails.
        queue_info = {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
    print(json.dumps({"status": result["status"], "public_video_count": len(records),
                      "event_count": len(result.get("events", [])), "output": str(out),
                      "suno_generation_queue": queue_info}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
