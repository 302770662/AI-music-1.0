from pathlib import Path
import tempfile
from fastapi import FastAPI, File, UploadFile
from pipeline import aggregate_analysis_records, asr_lyrics_package, audio_analyze, complete_creative_package, config, empty_lyrics, lyrics_tag, transcribe

app = FastAPI(title="Local Hotspot Music Pipeline", version="0.1.0")
CFG = config(Path(__file__).resolve().parents[1] / "config" / "runtime.yaml")

@app.get("/health")
def health(): return {"status": "ok"}

@app.post("/v1/analyze/lyrics")
async def analyze_lyrics(payload: dict):
    text = str(payload.get("lyrics", ""))
    tags = lyrics_tag(text) if text.strip() else empty_lyrics("request")
    tags["source"] = "request"
    return {"lyrics": tags, "creative": complete_creative_package(tags, payload.get("music"), payload.get("hotspot"), CFG)}


@app.post("/v1/create/brief")
async def create_brief(payload: dict):
    """Fuse precomputed music/lyric/hotspot tags without rerunning ASR."""
    tags = payload.get("lyrics")
    if not isinstance(tags, dict):
        text = str(payload.get("lyrics_text", ""))
        tags = lyrics_tag(text) if text.strip() else empty_lyrics("request")
        tags["source"] = "request"
    return {"lyrics": tags, "creative": complete_creative_package(tags, payload.get("music"), payload.get("hotspot"), CFG)}


@app.post("/v1/create/batch")
async def create_batch(payload: dict):
    """Create one package per song, one aggregate package, or both."""
    records = payload.get("records") or []
    mode = str(payload.get("mode", "both"))
    if mode not in {"separate", "average", "both"}:
        return {"status": "failed", "reason": "mode must be separate, average or both"}
    if not isinstance(records, list) or not records:
        return {"status": "failed", "reason": "records must be a non-empty list"}
    hotspot = payload.get("hotspot") or {}
    separate = []
    if mode in {"separate", "both"}:
        for record in records:
            separate.append({"source": record.get("source"), "sha256": record.get("sha256"),
                             "creative": complete_creative_package(record.get("lyrics") or {}, record.get("music") or {}, hotspot, CFG)})
    average = None
    if mode in {"average", "both"}:
        aggregate = aggregate_analysis_records(records)
        average = {"aggregation": aggregate,
                   "creative": complete_creative_package(aggregate["lyrics"], aggregate["music"], hotspot, CFG)}
    return {"status": "ok", "mode": mode, "song_count": len(records),
            "separate": separate, "average": average}

@app.post("/v1/analyze/audio")
async def analyze_audio(file: UploadFile = File(...)):
    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f: f.write(await file.read()); path = Path(f.name)
    try:
        music = audio_analyze(path, CFG)
        asr = transcribe(path, CFG)
        return {"music": music, "asr": asr, **asr_lyrics_package(asr, music, cfg=CFG)}
    finally: path.unlink(missing_ok=True)
