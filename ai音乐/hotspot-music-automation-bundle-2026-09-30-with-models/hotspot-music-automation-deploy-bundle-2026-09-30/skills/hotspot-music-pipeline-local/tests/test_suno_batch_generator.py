import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_ROOT))
import suno_batch_generator as sbg  # noqa: E402


def write_queue(root: Path, name: str = "queue") -> Path:
    output = root / name
    output.mkdir(parents=True)
    prompt = output.parent / "suno-prompts.jsonl"
    prompt.write_text(json.dumps({"title": "测试", "suno_style_prompt": "folk, warm", "generated_lyrics": "原创歌词"}, ensure_ascii=False) + "\n", encoding="utf-8")
    jobs = sbg.make_plan(sbg.load_prompts(prompt), output, variants=1)
    sbg.write_plan_and_status(jobs, output, prompt, 1)
    return output


class SunoBatchGeneratorTests(unittest.TestCase):
    def test_scheduled_task_scope_skips_metadata_only_queues(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tasks"
            enabled = root / "bilibili-daily" / "2026-09-02" / "suno-generation"
            disabled = root / "qqmusic-daily" / "2026-09-02" / "suno-generation"
            self.assertTrue(sbg.scheduled_suno_task_enabled(enabled, task_root=root))
            self.assertFalse(sbg.scheduled_suno_task_enabled(disabled, task_root=root))

    def test_request_id_or_arbitrary_json_is_not_a_song_result(self):
        with self.assertRaises(RuntimeError):
            sbg.verified_artifacts({"task_id": "request-1", "status": "completed"})
        with self.assertRaises(RuntimeError):
            sbg.verified_artifacts({"message": "accepted"})

    def test_terminal_song_id_is_accepted(self):
        self.assertEqual(
            sbg.verified_artifacts({"status": "completed", "id": "song-1"}),
            {"song_id": "song-1", "song_url": None, "result_file": None},
        )
        self.assertEqual(
            sbg.verified_artifacts({"status": "completed", "id": "song-1", "task_id": "request-1"})["song_id"],
            "song-1",
        )

    def test_worker_polls_and_only_then_completes(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = write_queue(Path(temporary))
            task_id = sbg.load_jobs(output)[0]["task_id"]
            args = argparse.Namespace(poll_timeout=1.0, poll_interval=0.0)
            with patch.dict(os.environ, {"HOTSPOT_SUNO_API_KEY": "test", "HOTSPOT_SUNO_API_URL": "https://authorized.example/create"}), \
                 patch.object(sbg, "api_submit", return_value={"request_id": "req-1", "status": "queued", "status_url": "https://authorized.example/status/req-1"}), \
                 patch.object(sbg, "api_request", return_value={"request_id": "req-1", "status": "completed", "song_url": "https://suno.com/song/song-1"}):
                result = sbg.process_api_job(output, task_id, args)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(sbg.read_json(output / sbg.STATUS_NAME)["counts"]["completed"], 1)

    def test_worker_marks_unverifiable_response_failed(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = write_queue(Path(temporary))
            task_id = sbg.load_jobs(output)[0]["task_id"]
            args = argparse.Namespace(poll_timeout=1.0, poll_interval=0.0)
            with patch.dict(os.environ, {"HOTSPOT_SUNO_API_KEY": "test", "HOTSPOT_SUNO_API_URL": "https://authorized.example/create"}), \
                 patch.object(sbg, "api_submit", return_value={"task_id": "req-1", "status": "completed"}):
                result = sbg.process_api_job(output, task_id, args)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(sbg.read_json(output / sbg.STATUS_NAME)["counts"]["completed"], 0)


if __name__ == "__main__":
    unittest.main()
