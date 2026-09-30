"""Runnable local MVP for audio, lyrics, OCR, DeepSeek lyrics and daily reports."""
from __future__ import annotations
import argparse, hashlib, json, os, re, subprocess, sys, urllib.error, urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "runtime.yaml"
SUPPORTED_INPUT_SUFFIXES = {".wav", ".mp3", ".flac", ".mflac", ".m4a", ".ogg",
                            ".txt", ".md", ".lrc", ".png", ".jpg", ".jpeg", ".webp"}

def config(path):
    try:
        import yaml
        p = Path(path)
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {} if p.exists() else {}
    except ImportError:
        return {}

def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()

def save(value, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _md(value):
    """Keep values safe and readable inside Markdown tables."""
    return str(value if value is not None else "-").replace("|", "\\|").replace("\n", "<br>")


def write_human_reports(date, out, results):
    """Write compact Markdown views while retaining JSON/JSONL as data contracts."""
    out = Path(out)
    rows = []
    for item in results:
        creative = item.get("creative") or {}
        generation = creative.get("lyrics_generation") or {}
        music = item.get("music") or {}
        effnet = music.get("effnet") or {}
        rows.append({
            "source": Path(str(item.get("source", "-"))).name,
            "asr": (item.get("asr") or {}).get("status", "-"),
            "lyrics": (item.get("lyrics") or {}).get("status", "-"),
            "generation": generation.get("status", "-"),
            "model": creative.get("models", {}).get("music_tags", effnet.get("tags_model", "-")),
            "prompt": creative.get("suno_prompt", ""),
            "lyrics_text": creative.get("generated_lyrics", ""),
            "arrangement": creative.get("arrangement") or [],
            "fusion": creative.get("fusion") or {},
            "audio": music,
            "generation_detail": generation,
        })

    report = [f"# 本地音乐创作日报 - {date}", "", "> 机器数据请使用 `daily-report.json` 和 `suno-prompts.jsonl`。本文件用于人工阅读。", "",
              "## 总览", "", "| 项目 | 数值 |", "|---|---:|",
              f"| 处理文件 | {len(results)} |", f"| Suno 创作包 | {sum(bool(x['prompt']) for x in rows)} |",
              f"| DeepSeek 成功 | {sum(x['generation'] == 'ok' for x in rows)} |", ""]
    if rows:
        report += ["## 状态", "", "| 文件 | ASR | 歌词标签 | DeepSeek 歌词 | 音乐模型 |", "|---|---|---|---|---|"]
        report += [f"| {_md(x['source'])} | {_md(x['asr'])} | {_md(x['lyrics'])} | {_md(x['generation'])} | {_md(x['model'])} |" for x in rows]
        report.append("")
    for index, row in enumerate(rows, 1):
        report += [f"## 创作包 {index}：{row['source']}", "", "### Suno 风格提示词", "", "```text", row["prompt"] or "（未生成）", "```", "",
                    "### 段落编排", "", "> " + " → ".join(row["arrangement"]) if row["arrangement"] else "> （无）", ""]
        fusion = row["fusion"]
        tags = [x.get("label") for x in fusion.get("music_tags_used", []) if isinstance(x, dict) and x.get("label")]
        report += ["### 融合依据", "", "| 来源 | 内容 |", "|---|---|",
                   f"| 音乐风格标签 | {_md(', '.join(tags) or '无高置信度标签')} |",
                   f"| 歌词情绪 | {_md(', '.join(map(str, fusion.get('lyrics_emotions_used', []))) or '无')} |",
                   f"| 歌词意象 | {_md(', '.join(map(str, fusion.get('lyrics_imagery_used', []))) or '无')} |",
                   f"| 标签策略 | {_md(fusion.get('music_tag_policy', '默认'))} |", ""]
        audio = row["audio"]
        if audio:
            report += ["### 音频摘要", "", "| 特征 | 值 |", "|---|---:|",
                       f"| BPM | {_md(audio.get('bpm'))} |", f"| 调性估计 | {_md(audio.get('key_estimate'))} |",
                       f"| RMS 能量 | {_md(audio.get('rms_energy'))} |", f"| 谱质心 | {_md(audio.get('spectral_centroid'))} |", ""]
        report += ["### DeepSeek 原创歌词", "", f"状态：`{row['generation']}`", "", "```text", row["lyrics_text"] or "（未生成。请检查 DeepSeek Key、账户余额和 API 状态。）", "```", ""]
    (out / "daily-report.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    prompt_doc = [f"# Suno 创作包 - {date}", "", "> 本文件适合复制到人工审核流程；程序读取请使用同目录的 `suno-prompts.jsonl`。", ""]
    for index, row in enumerate(rows, 1):
        prompt_doc += [f"## {index}. {row['source']}", "", "### Style", "", "```text", row["prompt"] or "（未生成）", "```", "",
                       "### Lyrics", "", "```text", row["lyrics_text"] or "（未生成）", "```", "",
                       f"### Generation status: `{row['generation']}`", "", "---", ""]
    (out / "suno-prompts.md").write_text("\n".join(prompt_doc), encoding="utf-8")

def setup(args):
    cfg = config(args.config); root = Path(cfg.get("data_root", "./data"))
    if not root.is_absolute(): root = Path.cwd() / root
    for name in ("raw", "normalized", "analysis", "music", "creative", "assets", "receipts"): (root / name).mkdir(parents=True, exist_ok=True)
    modules = {}
    for name in ("numpy", "librosa", "soundfile", "essentia", "faster_whisper", "pytesseract", "fastapi"):
        try: __import__(name); modules[name] = "available"
        except Exception as e: modules[name] = f"unavailable: {type(e).__name__}"
    print(json.dumps({"root": str(ROOT), "data_root": str(root), "optional": modules}, ensure_ascii=False, indent=2))

def normalize_audio(src, dst, cfg):
    ac = cfg.get("audio", {}); dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-i", str(src), "-ac", str(ac.get("channels", 1)), "-ar", str(ac.get("sample_rate", 16000)), "-vn", str(dst)]
    try: subprocess.run(cmd, check=True, capture_output=True); return {"status": "ok", "method": "ffmpeg", "path": str(dst)}
    except Exception:
        try:
            import librosa, soundfile as sf
            y, sr = librosa.load(src, sr=int(ac.get("sample_rate", 16000)), mono=True); sf.write(dst, y, sr)
            return {"status": "ok", "method": "librosa", "path": str(dst)}
        except Exception as e: return {"status": "failed", "reason": str(e)}

def effnet(path, cfg):
    ecfg = cfg.get("effnet", {})
    model = ecfg.get("model_path")
    if not model:
        return {"status": "unavailable", "reason": "set effnet.model_path"}
    if ecfg.get("runtime") == "wsl" and sys.platform == "win32":
        worker = Path(__file__).with_name("effnet_worker.py")
        wsl_python = ecfg.get("wsl_python")
        if not wsl_python or not worker.exists():
            return {"status": "unavailable", "reason": "WSL Effnet worker is not configured"}
        def wsl_path(p):
            m = re.match(r"^([A-Za-z]):[\\/](.*)$", str(p))
            return "/mnt/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/") if m else str(p)
        try:
            cmd = ["wsl", "-d", "Ubuntu", "--", wsl_python, str(wsl_path(worker)), "--audio", str(wsl_path(path)), "--model", str(wsl_path(model))]
            run = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=300)
            result = json.loads(run.stdout.strip().splitlines()[-1])
            fallback = maest_tags(path, ecfg)
            if fallback.get("status") == "ok":
                result["effnet_400_status"] = "unavailable"
                result["effnet_400_reason"] = "official genre_discogs400 Effnet head is not reachable; using the configured Discogs-MAEST-519 classifier"
                result["tags_status"] = "ok"
                result["tags_model"] = fallback["model"]
                result["tags_model_role"] = "discogs_genre_classifier"
                result["tags"] = fallback["tags"]
            return result
        except Exception as e:
            fallback = maest_tags(path, ecfg)
            if fallback.get("status") == "ok":
                return {"status": "partial", "model_role": "music_embedding_unavailable",
                        "embedding_status": "unavailable", "embedding_reason": str(e),
                        "effnet_400_status": "unavailable",
                        "tags_status": "ok", "tags_model": fallback["model"],
                        "tags_model_role": "discogs_genre_classifier", "tags": fallback["tags"],
                        "device": fallback.get("device"), "label_count": fallback.get("label_count"),
                        "lyrics_role": "separate_lyrics_module"}
            return {"status": "unavailable", "reason": str(e)}
    try:
        import essentia.standard as es
        audio = es.MonoLoader(filename=str(path), sampleRate=16000)()
        emb = es.TensorflowPredictEffnetDiscogs(graphFilename=str(model), output="PartitionedCall:1")(audio)
        return {"status": "ok", "embedding_shape": list(emb.shape), "model": str(model), "model_role": "music_embedding", "tags_status": "unavailable", "lyrics_role": "separate_lyrics_module"}
    except Exception as e: return {"status": "unavailable", "reason": str(e)}

def maest_tags(path, ecfg):
    """Run the local MTG-UPF Discogs-MAEST-519 classifier on Windows."""
    model_path = ecfg.get("fallback_model_path")
    if not model_path or not Path(model_path).exists():
        return {"status": "unavailable", "reason": "Discogs-MAEST-519 model directory is missing"}
    try:
        import importlib.util
        import librosa, numpy as np, torch
        from transformers import AutoModelForAudioClassification
        extractor_file = Path(model_path) / "feature_extraction_maest.py"
        spec = importlib.util.spec_from_file_location("maest_feature_extractor", extractor_file)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        extractor = module.MAESTFeatureExtractor.from_pretrained(str(model_path), local_files_only=True)
        model = AutoModelForAudioClassification.from_pretrained(str(model_path), local_files_only=True)
        use_cuda = ecfg.get("classifier_device", "auto") in ("auto", "cuda") and torch.cuda.is_available()
        device = torch.device("cuda" if use_cuda else "cpu"); model.to(device).eval()
        audio, sr = librosa.load(str(path), sr=16000, mono=True)
        features = extractor(audio, sampling_rate=sr, return_tensors="pt")
        features = {k: v.to(device) for k, v in features.items() if hasattr(v, "to")}
        with torch.inference_mode():
            logits = model(**features).logits
            probs = torch.sigmoid(logits)[0].detach().float().cpu().numpy()
        labels = model.config.id2label
        threshold = float(ecfg.get("min_confidence", 0.55))
        rows = [{"label": str(labels.get(int(i), labels.get(str(int(i)), int(i)))),
                 "probability": float(probs[int(i)]),
                 "weak_label": bool(probs[int(i)] < threshold)}
                for i in np.argsort(probs)[::-1][:20]]
        return {"status": "ok", "model": "Discogs-MAEST-519", "tags": rows,
                "accepted_tags": [x for x in rows if not x["weak_label"]],
                "device": str(device), "label_count": len(labels), "threshold": threshold}
    except Exception as e:
        return {"status": "unavailable", "reason": f"Discogs-MAEST-519: {e}"}

def audio_analyze(path, cfg):
    result = {"status": "ok", "sha256": digest(path), "effnet": effnet(path, cfg)}
    try:
        import librosa, numpy as np
        y, sr = librosa.load(path, sr=None, mono=True); chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr); keys = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
        result.update({"duration_sec": len(y) / sr, "sample_rate": int(sr), "bpm": float(np.asarray(tempo).reshape(-1)[0]), "rms_energy": float(np.sqrt(np.mean(y * y))), "spectral_centroid": float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr))), "key_estimate": keys[int(np.argmax(np.mean(chroma, axis=1)))]})
    except Exception as e: result.update({"status": "partial", "reason": str(e)})
    return result

def transcribe(path, cfg):
    try:
        from faster_whisper import WhisperModel
        ac = cfg.get("asr", {}); device = ac.get("device", "auto")
        if device == "auto": device = "cuda"
        compute = ac.get("compute_type", "float16" if device == "cuda" else "int8")
        model = WhisperModel(ac.get("model_path") or ac.get("model", "base"), device=device, compute_type=compute,
                             download_root=ac.get("download_root"))
        segments, info = model.transcribe(str(path))
        rows = [{"start": float(s.start), "end": float(s.end), "text": s.text.strip()} for s in segments]
        return {"status": "ok", "language": info.language, "segments": rows}
    except Exception as exc:
        return {"status": "unavailable", "reason": str(exc), "segments": []}

def asr_to_lyrics(asr):
    """Convert ASR segments into a lyric-like document for downstream tagging."""
    if not asr or asr.get("status") != "ok":
        return {"status": "unavailable", "text": "", "source": "asr", "segments": []}
    segments = asr.get("segments") or []
    text = "\n".join(str(row.get("text", "")).strip() for row in segments if str(row.get("text", "")).strip())
    return {"status": "ok" if text else "empty", "text": text, "source": "asr", "segments": segments}


def empty_lyrics(source="asr", reason=None):
    """Return an explicit non-success result when no lyric text is available."""
    result = {"status": "empty", "source": source, "theme": [], "emotion_curve": [],
              "imagery": [], "sections": [], "repetition_terms": []}
    if reason:
        result["reason"] = reason
    return result


def asr_lyrics_package(asr, music=None, hotspot=None, cfg=None):
    """Orchestrate ASR text -> lyric tags -> the fused creative package."""
    asr_text = asr_to_lyrics(asr)
    if asr_text.get("text"):
        lyrics = lyrics_tag(asr_text["text"])
        lyrics.update({"source": "asr", "weak_label": True,
                       "asr_language": (asr or {}).get("language")})
    elif asr_text.get("status") == "unavailable":
        lyrics = empty_lyrics("asr", (asr or {}).get("reason"))
        lyrics["status"] = "unavailable"
    else:
        lyrics = empty_lyrics("asr", "ASR returned no text")
    return {"asr_text": asr_text, "lyrics": lyrics,
            "creative": complete_creative_package(lyrics, music, hotspot, cfg)}

def lyrics_tag(text):
    text = str(text or "")
    sections, current, lines = [], "unsectioned", []
    for line in text.splitlines():
        m = re.match(r"^\s*((?:\[[^]]+\]\s*)+)(.*)$", line)
        if m:
            if lines: sections.append({"section": current, "lines": lines})
            markers = re.findall(r"\[([^]]+)\]", m.group(1))
            current, lines = " / ".join(markers), []
            if m.group(2).strip(): lines.append(m.group(2).strip())
        elif line.strip(): lines.append(line.strip())
    if lines: sections.append({"section": current, "lines": lines})
    joined = "\n".join(x for s in sections for x in s["lines"]); words = re.findall(r"[\u4e00-\u9fff]{2,}", joined)
    common = [w for w, n in Counter(words).most_common(10) if n > 1]
    imagery = [w for w in ("海浪", "星光", "月光", "雪地", "四季", "潮汐", "春天", "夏日", "秋天", "冬日", "银河") if w in joined]
    emotion = ["忧郁" if any(w in joined for w in ("散", "离", "孤", "最后", "回望")) else "温暖"]
    section_rows = [{"section": s["section"], "markers": [x.strip() for x in s["section"].split("/")],
                    "line_count": len(s["lines"])} for s in sections]
    return {"status": "ok" if joined else "empty", "theme": imagery or common[:6], "emotion_curve": emotion if joined else [],
            "imagery": imagery, "sections": section_rows, "repetition_terms": common}

def _tag_name(label):
    return str(label).split("---")[-1].replace("&", "and").strip()

def _genre_family(label):
    return str(label).split("---")[0].strip().lower()

def _english_emotion(value):
    mapping = {"忧郁": "melancholic", "悲伤": "sad", "温暖": "warm", "怀旧": "nostalgic", "希望": "hopeful", "紧张": "tense", "治愈": "comforting", "轻快": "light-hearted"}
    return mapping.get(str(value), str(value).lower())


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]

def _music_tags(music):
    effnet = (music or {}).get("effnet") or {}
    # Only accepted/non-weak predictions may drive a creative prompt. Keep the
    # complete raw MAEST output in music.effnet for audit, but do not turn a
    # low-confidence classifier guess into a style assertion.
    if "accepted_tags" in effnet:
        rows = effnet.get("accepted_tags") or []
    else:
        rows = [row for row in (effnet.get("tags") or [])
                if not isinstance(row, dict) or not row.get("weak_label", False)]
    valid = []
    for row in rows:
        if isinstance(row, str):
            valid.append({"label": row, "probability": None, "weak_label": False})
        elif isinstance(row, dict) and row.get("label"):
            valid.append(row)
    return valid[:8]

def create_suno_prompt(lyrics_tags=None, music=None, hotspot=None):
    """Fuse audio/MAEST labels, lyric tags, audio features and optional hotspot tags.

    This is deliberately deterministic and attribute-based: it never copies lyric text,
    artist names, song titles or melodies from reference material.
    """
    lyrics_tags, music, hotspot = lyrics_tags or {}, music or {}, hotspot or {}
    tag_rows = _music_tags(music)
    families = [_genre_family(row["label"]) for row in tag_rows]
    genre_words = []
    for row in tag_rows[:3]:
        name = _tag_name(row["label"])
        if name and name.lower() not in {x.lower() for x in genre_words}:
            genre_words.append(name.lower())
    trend_words = [_tag_name(value) for value in _as_list(hotspot.get("trend_style_tags")) if str(value).strip()]
    for name in trend_words[:2]:
        if name and name.lower() not in {x.lower() for x in genre_words}:
            genre_words.append(name.lower())
    if not genre_words:
        genre_words = ["cinematic pop"]

    bpm = music.get("bpm")
    try: bpm_value = float(bpm) if bpm is not None else None
    except (TypeError, ValueError): bpm_value = None
    if bpm_value and bpm_value < 75: tempo = "slow tempo"
    elif bpm_value and bpm_value > 115: tempo = "upbeat tempo"
    else: tempo = "medium tempo"
    bpm_text = f"{round(bpm_value)} BPM" if bpm_value and bpm_value > 0 else tempo

    emotions = []
    for value in _as_list(lyrics_tags.get("emotion_curve")) + _as_list(hotspot.get("emotion")):
        word = _english_emotion(value)
        if word and word not in emotions: emotions.append(word)
    if not emotions: emotions = ["warm", "reflective"]
    emotions = emotions[:4]

    instruments = []
    family_set = set(families)
    if "jazz" in family_set or "blues" in family_set:
        instruments += ["warm piano", "upright bass", "brush drums", "expressive saxophone"]
    if "folk, world, and country" in family_set or "folk" in family_set:
        instruments += ["acoustic guitar", "organic percussion"]
    if "pop" in family_set:
        instruments += ["soft synth pads", "clean electric guitar"]
    if "rock" in family_set:
        instruments += ["electric guitar", "live drums"]
    if "classical" in family_set:
        instruments += ["piano", "string ensemble"]
    if not instruments: instruments = ["piano", "acoustic guitar", "soft percussion"]
    instruments = list(dict.fromkeys(instruments))[:5]
    production_cues = [str(value).strip() for value in _as_list(hotspot.get("trend_production_cues")) if str(value).strip()][:3]

    imagery = list(dict.fromkeys([str(x) for x in _as_list(lyrics_tags.get("imagery")) + _as_list(hotspot.get("scene"))]))[:4]
    scene = ", ".join(imagery) if imagery else "intimate cinematic atmosphere"
    has_lyric_evidence = bool(lyrics_tags.get("text") or lyrics_tags.get("theme") or
                              lyrics_tags.get("imagery") or lyrics_tags.get("sections"))
    vocal = "intimate Mandarin vocal" if has_lyric_evidence else "expressive vocal"
    sections = []
    for row in lyrics_tags.get("sections") or []:
        markers = row.get("markers") if isinstance(row, dict) else None
        if not markers:
            markers = re.split(r"\s*/\s*", str(row.get("section", "") if isinstance(row, dict) else row))
        sections.extend(str(x).lower() for x in markers if str(x).strip())
    recognized_sections = [x for x in sections if any(k in x for k in
                        ("intro", "verse", "pre-chorus", "chorus", "bridge", "instrumental", "solo", "outro"))]
    arrangement = []
    if recognized_sections:
        arrangement = ["[Powerful Intro]"]
        for section in recognized_sections:
            if "violin" in section: marker = "[Violin Solo]"
            elif "sax" in section: marker = "[Sax Solo]"
            elif "verse" in section: marker = "[Verse]"
            elif "chorus" in section: marker = "[Chorus]"
            elif "bridge" in section: marker = "[Bridge]"
            elif "outro" in section: marker = "[Outro]"
            elif "solo" in section or "instrumental" in section: marker = "[Instrumental Break]"
            else: continue
            if marker not in arrangement: arrangement.append(marker)
        if "jazz" in family_set or "blues" in family_set: arrangement.insert(-1 if len(arrangement) > 1 else 1, "[Sax Solo]")
        arrangement.append("[Outro]") if "[Outro]" not in arrangement else None
    else:
        arrangement = ["[Powerful Intro]", "[Verse]", "[Chorus]", "[Melodic Instrumental]", "[Bridge]", "[Sax Solo]", "[Outro]"]

    style = ", ".join([", ".join(genre_words[:2]), *emotions, bpm_text, *instruments, vocal,
                        *production_cues, "restrained verses", "emotional chorus", "dynamic build", scene])
    style += ", " + ", ".join(arrangement)
    selected_tags = [{"label": row["label"], "probability": row.get("probability"),
                      "weak_label": bool(row.get("weak_label", False))} for row in tag_rows]
    top_probability = (tag_rows[0].get("probability") or 0.0) if tag_rows else 0.0
    return {
        "status": "ok",
        "suno_prompt": style,
        "style_prompt": style,
        "arrangement": arrangement,
        "avoid": "named artists, existing song titles, quoted lyrics, voice imitation, copyrighted samples, copied lyric lines",
        "confidence": round(min(0.98, 0.45 * top_probability +
                                 0.25 * has_lyric_evidence + 0.20 * bool(hotspot) + 0.10), 3),
        "fusion": {
            "music_tags_used": selected_tags,
            "music_tag_policy": "accepted_tags_or_non_weak_only",
            "trend_style_tags_used": trend_words[:2],
            "trend_production_cues_used": production_cues,
            "trend_chart_count": int(hotspot.get("music_chart_count") or 0),
            "trend_news_count": int(hotspot.get("news_item_count") or 0),
            "lyrics_emotions_used": emotions,
            "lyrics_imagery_used": imagery,
            "hotspot_emotions_used": [str(x) for x in _as_list(hotspot.get("emotion"))],
            "hotspot_scenes_used": [str(x) for x in _as_list(hotspot.get("scene"))],
        },
        "source": {"lyrics": lyrics_tags, "music": music, "hotspot": hotspot},
        "models": {"music_tags": (music.get("effnet") or {}).get("tags_model", "audio_features"),
                   "lyrics_tags": "rule_based", "lyrics_source": lyrics_tags.get("source", "unknown")}
    }


def _deepseek_endpoint(base_url):
    base_url = str(base_url or "https://api.deepseek.com").rstrip("/")
    return base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"


def _deepseek_input(lyrics_tags, music, hotspot, creative):
    """Build an abstract, non-copying input for lyric generation."""
    effnet = (music or {}).get("effnet") or {}
    tags = []
    for row in _music_tags(music):
        if isinstance(row, dict) and row.get("label"):
            tags.append({"label": row["label"], "probability": row.get("probability")})
        elif isinstance(row, str):
            tags.append({"label": row})
    return {
        "style_prompt": creative.get("suno_prompt", ""),
        "arrangement": creative.get("arrangement", []),
        "lyric_tags": {
            "theme": lyrics_tags.get("theme", []),
            "emotion_curve": lyrics_tags.get("emotion_curve", []),
            "imagery": lyrics_tags.get("imagery", []),
            "sections": lyrics_tags.get("sections", []),
            "repetition_terms": lyrics_tags.get("repetition_terms", []),
        },
        "music_features": {
            "bpm": music.get("bpm"),
            "key": music.get("key_estimate"),
            "energy": music.get("rms_energy"),
            "spectral_centroid": music.get("spectral_centroid"),
            "accepted_style_tags": tags,
        },
        "hotspot": {
            "summary": hotspot.get("summary"),
            "keywords": hotspot.get("keywords", []),
            "emotion": hotspot.get("emotion", []),
            "scene": hotspot.get("scene", []),
            "trend_style_tags": hotspot.get("trend_style_tags", []),
            "trend_production_cues": hotspot.get("trend_production_cues", []),
            "music_chart_count": hotspot.get("music_chart_count", 0),
        },
    }


def generate_original_lyrics(lyrics_tags, music, creative, hotspot=None, cfg=None):
    """Generate original Chinese lyrics through DeepSeek's OpenAI-compatible API.

    The API key is read only from the configured environment variable. Raw ASR
    text and reference lyrics are intentionally not sent; only abstract tags
    and measurable music attributes are used to reduce copying risk.
    """
    dcfg = (cfg or {}).get("deepseek") or {}
    if not dcfg.get("enabled", True):
        return {"status": "disabled", "provider": "deepseek", "text": ""}
    env_name = str(dcfg.get("api_key_env", "HOTSPOT_DEEPSEEK_API_KEY"))
    api_key = os.environ.get(env_name)
    if not api_key:
        return {"status": "unavailable", "provider": "deepseek", "text": "",
                "reason": f"environment variable {env_name} is not configured"}

    model = str(dcfg.get("model", "deepseek-chat"))
    system = (
        "你是中文原创歌曲歌词编辑。只根据抽象音乐属性、歌词标签和热点意象创作全新歌词。"
        "不得复用输入歌词或参考歌词的句子、独特比喻、连续表达、旋律、艺人姓名或歌曲标题，"
        "不得模仿具体艺人。输出简体中文歌词，必须包含清晰的段落标记；可使用 [Verse 1]、"
        "[Pre-Chorus]、[Chorus]、[Bridge]、[Instrumental Break]、[Violin Solo]、"
        "[Sax Solo]、[Outro]。只输出歌词正文和段落标记，不要解释、引号、Markdown 代码块或前言。"
    )
    user = json.dumps(_deepseek_input(lyrics_tags or {}, music or {}, hotspot or {}, creative or {}),
                      ensure_ascii=False, separators=(",", ":"))
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        "temperature": float(dcfg.get("temperature", 0.85)),
        "max_tokens": int(dcfg.get("max_tokens", 1200)),
        "stream": False,
    }
    request = urllib.request.Request(
        _deepseek_endpoint(dcfg.get("base_url")),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(dcfg.get("lyrics_timeout_sec", dcfg.get("timeout_sec", 45)))) as response:
            body = json.loads(response.read().decode("utf-8"))
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content", "")
        if isinstance(text, list):
            text = "".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in text)
        text = re.sub(r"^```(?:text|markdown)?\s*|\s*```$", "", str(text).strip(), flags=re.I)
        if not text:
            return {"status": "failed", "provider": "deepseek", "model": model, "text": "",
                    "reason": "DeepSeek returned empty content"}
        return {"status": "ok", "provider": "deepseek", "model": model, "text": text,
                "usage": body.get("usage", {})}
    except urllib.error.HTTPError as exc:
        return {"status": "failed", "provider": "deepseek", "model": model, "text": "",
                "reason": f"DeepSeek HTTP {exc.code}"}
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError) as exc:
        return {"status": "failed", "provider": "deepseek", "model": model, "text": "",
                "reason": f"DeepSeek request failed: {type(exc).__name__}: {exc}"}
    except Exception as exc:
        return {"status": "failed", "provider": "deepseek", "model": model, "text": "",
                "reason": f"DeepSeek request failed: {type(exc).__name__}"}


def complete_creative_package(lyrics_tags, music=None, hotspot=None, cfg=None):
    """Create the fused prompt, then optionally attach DeepSeek lyrics."""
    creative = create_suno_prompt(lyrics_tags, music, hotspot)
    generation = generate_original_lyrics(lyrics_tags or {}, music or {}, creative, hotspot, cfg)
    creative["lyrics_generation"] = {key: value for key, value in generation.items() if key != "text"}
    creative["generated_lyrics"] = generation.get("text", "")
    return creative


def _number(value):
    try:
        result = float(value)
        return result if result == result else None
    except (TypeError, ValueError):
        return None


def _unique(items, limit=None):
    values = []
    for item in items:
        text = str(item).strip()
        if text and text not in values:
            values.append(text)
    return values[:limit] if limit else values


def aggregate_analysis_records(records, min_support=None):
    """Create one auditable centroid-like style profile from many song analyses."""
    if not records:
        raise ValueError("at least one analysis record is required")
    tag_scores, tag_counts = {}, {}
    themes, emotions, imagery, sections = [], [], [], []
    numeric = {"bpm": [], "rms_energy": [], "spectral_centroid": []}
    keys = Counter()
    source_rows = []
    for record in records:
        music = record.get("music") or {}
        lyrics = record.get("lyrics") or {}
        effnet = music.get("effnet") or {}
        source_rows.append({"source": record.get("source"), "sha256": record.get("sha256")})
        for tag in _music_tags(music):
            label = tag.get("label")
            if not label:
                continue
            probability = _number(tag.get("probability"))
            tag_scores[label] = tag_scores.get(label, 0.0) + (probability if probability is not None else 1.0)
            tag_counts[label] = tag_counts.get(label, 0) + 1
        themes += _as_list(lyrics.get("theme")); emotions += _as_list(lyrics.get("emotion_curve"))
        imagery += _as_list(lyrics.get("imagery")); sections += _as_list(lyrics.get("sections"))
        for field in numeric:
            value = _number(music.get(field))
            if value is not None and value > 0:
                numeric[field].append(value)
        key = music.get("key_estimate")
        if key:
            keys[str(key)] += 1
    total = len(records)
    # A tag seen in only one of many songs is usually an outlier. Keep the
    # full profile for audit, but only allow sufficiently supported tags to
    # drive the average creative package.
    if min_support is None:
        min_support = 2 if total >= 5 else 1
    min_support = max(1, int(min_support))
    tag_rows = [{"label": label, "probability": round(score / total, 4),
                 "mean_probability": round(score / tag_counts[label], 4),
                 "support_count": tag_counts[label],
                 "support_ratio": round(tag_counts[label] / total, 4),
                 "weak_label": tag_counts[label] < min_support}
                for label, score in tag_scores.items()]
    tag_rows.sort(key=lambda row: (row["probability"], row["support_count"]), reverse=True)
    accepted = [row for row in tag_rows if not row["weak_label"]]
    average_music = {
        "status": "ok", "aggregation": "mean_of_accepted_or_non_weak_song_tags",
        "song_count": total,
        "bpm": round(sum(numeric["bpm"]) / len(numeric["bpm"]), 2) if numeric["bpm"] else None,
        "rms_energy": round(sum(numeric["rms_energy"]) / len(numeric["rms_energy"]), 6) if numeric["rms_energy"] else None,
        "spectral_centroid": round(sum(numeric["spectral_centroid"]) / len(numeric["spectral_centroid"]), 3) if numeric["spectral_centroid"] else None,
        "key_estimate": keys.most_common(1)[0][0] if keys else None,
        "effnet": {"status": "ok", "tags_model": "aggregated_discogs_style_profile",
                   "accepted_tags": accepted[:12], "aggregated_tags": tag_rows[:50],
                   "label_count": len(tag_rows), "accepted_label_count": len(accepted),
                   "min_support": min_support},
    }
    average_lyrics = {
        "status": "ok", "source": "aggregate", "song_count": total,
        "theme": _unique(themes, 10), "emotion_curve": _unique(emotions, 6),
        "imagery": _unique(imagery, 10), "sections": sections[:20],
        "repetition_terms": [],
    }
    return {"music": average_music, "lyrics": average_lyrics,
            "source_records": source_rows, "min_support": min_support}


def _analysis_candidates(input_path):
    root = Path(input_path)
    if not root.exists():
        raise FileNotFoundError(f"input does not exist: {root}")
    files = [root] if root.is_file() else list(root.rglob("*.analysis.json"))
    return [path for path in files if path.name not in {"daily-report.analysis.json"}]


def load_analysis_records(input_path, limit=None):
    records = []
    seen = set()
    for path in _analysis_candidates(input_path):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("music"):
            identity = record.get("sha256") or str(path.resolve())
            if identity in seen:
                continue
            seen.add(identity)
            records.append(record)
        if limit and len(records) >= limit:
            break
    return records


def load_hotspot(path):
    if not path:
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("hotspot JSON must be an object")
    allowed = {"summary", "keywords", "emotion", "scene", "trend_style_tags",
               "trend_sources", "top_titles", "music_trends", "music_chart_count",
               "news_item_count", "captured_at", "policy"}
    return {key: value[key] for key in allowed if key in value}


def write_batch_reports(out, mode, hotspot, separate, average):
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in separate:
        creative = item["creative"]
        rows.append({"mode": "separate", "source": item.get("source"), "sha256": item.get("sha256"),
                     "suno_prompt": creative.get("suno_prompt"), "generated_lyrics": creative.get("generated_lyrics", ""),
                     "lyrics_generation": creative.get("lyrics_generation", {}), "creative": creative})
    if average:
        creative = average["creative"]
        rows.append({"mode": "average", "source": "aggregate", "sha256": None,
                     "suno_prompt": creative.get("suno_prompt"), "generated_lyrics": creative.get("generated_lyrics", ""),
                     "lyrics_generation": creative.get("lyrics_generation", {}), "creative": creative,
                     "aggregation": average.get("aggregation")})
    Path(out / "batch-suno-prompts.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), encoding="utf-8")
    document = ["# 新闻热点批量 Suno 创作包", "", "## 热点输入", "", "```json", json.dumps(hotspot, ensure_ascii=False, indent=2), "```", ""]
    for index, row in enumerate(rows, 1):
        title = Path(str(row["source"])).name if row["mode"] == "separate" else "20 首歌曲平均风格"
        document += [f"## {index}. {title}", "", f"模式：`{row['mode']}`", "", "### Style", "", "```text", row["suno_prompt"] or "（未生成）", "```", "",
                     "### Lyrics", "", "```text", row["generated_lyrics"] or "（未生成）", "```", "",
                     f"歌词生成状态：`{row['lyrics_generation'].get('status', '-')}`", "", "---", ""]
    (out / "batch-suno-prompts.md").write_text("\n".join(document), encoding="utf-8")
    return rows


def batch_create(args):
    cfg, hotspot = config(args.config), load_hotspot(args.hotspot)
    records = load_analysis_records(args.input, args.limit)
    if not records:
        raise ValueError("no valid *.analysis.json records with music were found")
    separate, average = [], None
    if args.mode in ("separate", "both"):
        for record in records:
            separate.append({"source": record.get("source"), "sha256": record.get("sha256"),
                             "creative": complete_creative_package(record.get("lyrics") or {}, record.get("music") or {}, hotspot, cfg)})
    if args.mode in ("average", "both"):
        aggregate = aggregate_analysis_records(records, args.min_support)
        average = {"aggregation": aggregate, "creative": complete_creative_package(aggregate["lyrics"], aggregate["music"], hotspot, cfg)}
    out = Path(args.output)
    rows = write_batch_reports(out, args.mode, hotspot, separate, average)
    try:
        from suno_batch_generator import create_queue, queue_result
        queue_status = create_queue(out / "batch-suno-prompts.jsonl", variants=6)
        queue_info = queue_result(queue_status, out / "suno-generation")
    except Exception as exc:
        queue_info = {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
    summary = {"mode": args.mode, "song_count": len(records), "separate_prompt_count": len(separate),
               "average_prompt_count": int(average is not None), "output": str(out), "hotspot": hotspot,
               "suno_generation_queue": queue_info}
    save(summary, out / "batch-summary.json")
    print(json.dumps({**summary, "jsonl": str(out / "batch-suno-prompts.jsonl"), "markdown": str(out / "batch-suno-prompts.md")}, ensure_ascii=False, indent=2))


def prompt(tags, music=None, hotspot=None, cfg=None):
    return complete_creative_package(tags, music, hotspot, cfg)

def process(path, cfg, out, hotspot=None):
    path, out = Path(path), Path(out); result = {"source": str(path), "sha256": digest(path), "captured_at": datetime.now().astimezone().isoformat()}; suffix = path.suffix.lower()
    if suffix in (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".mflac"):
        normalized = out / (path.stem + ".normalized.wav"); result["preprocess"] = normalize_audio(path, normalized, cfg)
        if result["preprocess"].get("status") == "ok":
            result["music"] = audio_analyze(normalized, cfg)
            result["asr"] = transcribe(normalized, cfg)
            package = asr_lyrics_package(result["asr"], result["music"], hotspot=hotspot, cfg=cfg)
            result.update(package)
    elif suffix in (".txt", ".md", ".lrc"):
        text = path.read_text(encoding="utf-8", errors="replace")
        result["lyrics"] = lyrics_tag(text) if text.strip() else empty_lyrics("file")
        result["lyrics"]["source"] = "file"
        result["creative"] = complete_creative_package(result["lyrics"], hotspot=hotspot, cfg=cfg)
    elif suffix in (".png", ".jpg", ".jpeg", ".webp"):
        try:
            import pytesseract; from PIL import Image
            result["ocr"] = {"status": "ok", "text": pytesseract.image_to_string(Image.open(path), lang=cfg.get("ocr", {}).get("languages", "chi_sim+eng")).strip()}
        except Exception as e: result["ocr"] = {"status": "unavailable", "reason": str(e)}
    else: result["status"] = "skipped"
    save(result, out / (path.stem + ".analysis.json")); return result

def daily(args):
    cfg, source, out = config(args.config), Path(args.input), Path(args.output) / args.date
    out_resolved = out.resolve()
    hotspot_path = Path(args.hotspot) if args.hotspot else None
    if hotspot_path is None:
        candidates = [source / "hotspot.json", source / "trends" / args.date / "hotspot.json",
                      source.parent / "trends" / args.date / "hotspot.json"]
        hotspot_path = next((path for path in candidates if path.exists()), None)
    hotspot = load_hotspot(hotspot_path) if hotspot_path else {}
    generated_dirs = {out_resolved, (source / "analysis").resolve(),
                      (source / "creative").resolve(), (source / "trends").resolve()}
    generated_names = {"daily-report.json", "suno-prompts.jsonl", "trend-snapshot.jsonl",
                       "trend-status.json", "hotspot.json"}
    # Input roots are often `data/`, which also contains previous analyses.
    # Never feed generated reports or normalized intermediates back into a daily run.
    files = [p for p in source.rglob("*") if p.is_file()
             and out_resolved not in p.resolve().parents
             and not any(root == p.resolve() or root in p.resolve().parents for root in generated_dirs)
             and p.suffix.lower() in SUPPORTED_INPUT_SUFFIXES
             and not p.name.endswith((".analysis.json", ".normalized.wav"))
             and p.name not in generated_names]
    results = [process(p, cfg, out, hotspot) for p in files]
    prompts = [{"source": x.get("source"), "sha256": x.get("sha256"),
                "suno_prompt": (x.get("creative") or {}).get("suno_prompt"),
                "generated_lyrics": (x.get("creative") or {}).get("generated_lyrics", ""),
                "lyrics_generation": (x.get("creative") or {}).get("lyrics_generation", {}),
                "creative": x.get("creative")}
               for x in results if x.get("creative")]
    save({"date": args.date, "processed": len(results), "suno_prompt_count": len(prompts), "results": results}, out / "daily-report.json")
    Path(out / "suno-prompts.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in prompts), encoding="utf-8")
    write_human_reports(args.date, out, results)
    try:
        from suno_batch_generator import create_queue, queue_result
        queue_status = create_queue(out / "suno-prompts.jsonl", variants=6)
        queue_info = queue_result(queue_status, out / "suno-generation")
    except Exception as exc:
        # Prompt files remain usable when queue setup is temporarily unavailable.
        queue_info = {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
    save({"date": args.date, "processed": len(results), "suno_prompt_count": len(prompts),
          "results": results, "suno_generation_queue": queue_info}, out / "daily-report.json")
    print(json.dumps({"date": args.date, "processed": len(results), "suno_prompt_count": len(prompts), "output": str(out),
                      "hotspot": str(hotspot_path) if hotspot_path else None,
                      "suno_prompts": str(out / 'suno-prompts.jsonl'), "human_report": str(out / 'daily-report.md'),
                      "human_prompts": str(out / 'suno-prompts.md'), "suno_generation_queue": queue_info},
                      ensure_ascii=False, indent=2))

def trend_ingest(args):
    from trend_ingest import collect
    if not args.output:
        args.output = str(Path(args.data_root) / "trends" / args.date)
    return collect(args)

def main():
    p = argparse.ArgumentParser(); sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup"); s.add_argument("--config", default=str(CONFIG)); s.set_defaults(fn=setup)
    d = sub.add_parser("daily"); d.add_argument("--input", default="data/assets"); d.add_argument("--output", default="data/analysis"); d.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d")); d.add_argument("--hotspot", default=None, help="optional hotspot.json; otherwise auto-discover it"); d.add_argument("--config", default=str(CONFIG)); d.set_defaults(fn=daily)
    t = sub.add_parser("trend-ingest", help="collect public news, hot-search and music-chart metadata")
    t.add_argument("--sources", default=str(ROOT / "config" / "sources.yaml"))
    t.add_argument("--output", default=None)
    t.add_argument("--data-root", default="D:/projects/local/hotspot-music-runtime/data")
    t.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    t.add_argument("--only", default=None, help="comma-separated source IDs or platform names")
    t.add_argument("--browser-json", default=None, help="JSON snapshot exported from a visible public browser page or client")
    t.add_argument("--browser-source", default=None, help="source ID represented by --browser-json; defaults to weibo-realtime")
    t.add_argument("--per-source-limit", type=int, default=30)
    t.add_argument("--timeout", type=float, default=15)
    t.add_argument("--delay", type=float, default=0.5)
    t.set_defaults(fn=trend_ingest)
    b = sub.add_parser("batch-create", help="create separate and/or aggregate Suno packages from analysis JSON files")
    b.add_argument("--input", required=True, help="directory containing *.analysis.json files")
    b.add_argument("--hotspot", required=True, help="JSON file with summary/keywords/emotion/scene")
    b.add_argument("--output", required=True, help="batch output directory")
    b.add_argument("--mode", choices=("separate", "average", "both"), default="both")
    b.add_argument("--limit", type=int, default=20, help="maximum number of songs to use")
    b.add_argument("--min-support", type=int, default=None,
                   help="minimum number of songs supporting an average tag; default: 2 for 5+ songs, otherwise 1")
    b.add_argument("--config", default=str(CONFIG)); b.set_defaults(fn=batch_create)
    args = p.parse_args(); args.fn(args)

if __name__ == "__main__": main()
