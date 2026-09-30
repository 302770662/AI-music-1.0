import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_ROOT))
import suno_batch_generator as sbg  # noqa: E402
import suno_browser_worker as worker  # noqa: E402


def make_queue(task_root: Path, task_id: str, run_name: str) -> Path:
    output = task_root / task_id / run_name / "suno-generation"
    output.mkdir(parents=True)
    prompt = output.parent / "suno-prompts.jsonl"
    prompt.write_text(json.dumps({
        "title": run_name,
        "suno_style_prompt": "folk, warm",
        "generated_lyrics": "原创歌词",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    jobs = sbg.make_plan(sbg.load_prompts(prompt), output, variants=1)
    sbg.write_plan_and_status(jobs, output, prompt, 1)
    return output


def make_sequence_queue(task_root: Path, task_id: str, run_name: str, count: int = 3) -> Path:
    output = task_root / task_id / run_name / "suno-generation"
    output.mkdir(parents=True)
    prompt = output.parent / "suno-prompts.jsonl"
    prompt.write_text("".join(json.dumps({
        "title": f"连续任务 {index}",
        "suno_style_prompt": "folk, warm",
        "generated_lyrics": f"第 {index} 条原创歌词",
    }, ensure_ascii=False) + "\n" for index in range(1, count + 1)), encoding="utf-8")
    jobs = sbg.make_plan(sbg.load_prompts(prompt), output, variants=1)
    sbg.write_plan_and_status(jobs, output, prompt, 1)
    return output


class SunoBrowserWorkerTests(unittest.TestCase):
    def test_submission_requires_start_authorization(self):
        self.assertFalse(worker.submission_is_authorized({
            "active": False,
            "stop_requested": False,
            "auto_create": True,
            "submission_authorized": True,
        }))
        self.assertTrue(worker.submission_is_authorized({
            "active": True,
            "stop_requested": False,
            "auto_create": True,
            "submission_authorized": True,
        }))
        self.assertFalse(worker.submission_is_authorized({
            "active": True,
            "stop_requested": True,
            "auto_create": True,
            "submission_authorized": True,
        }))

    def test_start_persists_authorization_for_new_queue_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            task_root = Path(temporary) / "tasks"
            state_path = Path(temporary) / "worker-state.json"
            with patch.object(worker, "TASK_ROOT", task_root), patch.object(worker, "STATE_PATH", state_path):
                started = worker.start(worker.StartRequest(
                    task_ids=["weibo-hourly"],
                    scope="all",
                    auto_create=True,
                    follow_future=True,
                ))
                self.assertTrue(started["active"])
                self.assertTrue(started["submission_authorized"])
                self.assertTrue(worker.read_state()["submission_authorized"])

                # A queue created after Start must be discovered and submitted
                # with the same action-time authorization.
                make_queue(task_root, "weibo-hourly", "2026-09-02" + "\\\\" + "18-00")
                command = worker.command()
                self.assertEqual(command["action"], "execute")
                self.assertTrue(command["auto_create"])

                stopped = worker.stop()
                self.assertFalse(stopped["active"])
                self.assertFalse(stopped["submission_authorized"])
                self.assertEqual(worker.command()["action"], "abort")

    def test_autostart_restores_start_authorization_until_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "worker-state.json"
            with patch.object(worker, "STATE_PATH", state_path):
                worker.write_state({
                    "schema": "suno_browser_worker_state_v1",
                    "active": True,
                    "stop_requested": False,
                    "auto_create": True,
                    "submission_authorized": True,
                    "listener_enabled": True,
                    "follow_future": True,
                    "task_ids": ["weibo-hourly"],
                    "queue_paths": [],
                    "known_queue_paths": [],
                    "current": None,
                })
                worker.prepare_autostart_listener()
                restored = worker.read_state()
                self.assertTrue(restored["active"])
                self.assertTrue(restored["submission_authorized"])
                self.assertTrue(worker.submission_is_authorized(restored))

                worker.stop()
                worker.prepare_autostart_listener()
                revoked = worker.read_state()
                self.assertFalse(revoked["active"])
                self.assertFalse(revoked["submission_authorized"])

    def test_generated_song_count_uses_receipts_and_completed_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = make_queue(Path(temporary), "bilibili-daily", "2026-09-02")
            jobs = sbg.load_jobs(output)
            jobs[0]["status"] = "completed"
            jobs[0]["song_id"] = "song-primary"
            jobs[0]["song_url"] = "https://suno.com/song/song-primary"
            sbg.write_plan_and_status(jobs, output, output.parent / "suno-prompts.jsonl", 1)
            (output / worker.RECEIPT_NAME).write_text(json.dumps({
                "song_ids": ["song-secondary"],
                "song_urls": ["https://suno.com/song/song-secondary"],
            }) + "\n", encoding="utf-8")
            self.assertEqual(worker.song_result_count(output), 2)

    def test_future_discovery_keeps_all_new_queues(self):
        with tempfile.TemporaryDirectory() as temporary:
            task_root = Path(temporary) / "tasks"
            first = make_queue(task_root, "weibo-hourly", "2026-09-02" + "\\\\" + "10-00")
            second = make_queue(task_root, "weibo-hourly", "2026-09-02" + "\\\\" + "11-00")
            third = make_queue(task_root, "weibo-hourly", "2026-09-02" + "\\\\" + "12-00")
            state = {
                "follow_future": True,
                "task_ids": ["weibo-hourly"],
                "scope": "latest",
                "queue_paths": [str(first)],
                "known_queue_paths": [str(first)],
                "discovered_queue_paths": 0,
            }
            with patch.object(worker, "TASK_ROOT", task_root):
                changed = worker.refresh_queue_paths(state)
            self.assertTrue(changed)
            self.assertEqual(state["discovered_queue_paths"], 2)
            self.assertEqual(
                {Path(value).resolve() for value in state["queue_paths"]},
                {first.resolve(), second.resolve(), third.resolve()},
            )

    def test_three_sequential_jobs_complete_without_controller_block(self):
        """Regression for the third Suno submission in one continuous run."""
        with tempfile.TemporaryDirectory() as temporary:
            task_root = Path(temporary) / "tasks"
            state_path = Path(temporary) / "worker-state.json"
            output = make_sequence_queue(task_root, "bilibili-daily", "2026-09-03")
            with patch.object(worker, "TASK_ROOT", task_root), patch.object(worker, "STATE_PATH", state_path):
                worker.start(worker.StartRequest(
                    task_ids=["bilibili-daily"], scope="all", auto_create=True, follow_future=False
                ))
                for index in range(1, 4):
                    command = worker.command()
                    self.assertEqual(command["action"], "execute")
                    self.assertTrue(command["auto_create"])
                    song_id = f"song-sequence-{index}"
                    response = worker.result(worker.ResultRequest(
                        task_id=command["task_id"],
                        queue_path=command["queue_path"],
                        song_ids=[song_id],
                        song_urls=[f"https://suno.com/song/{song_id}"],
                    ))
                    self.assertEqual(response["job_status"], "completed")
                self.assertEqual(worker.command()["action"], "idle")
                final = worker.status()
                self.assertFalse(final["active"])
                self.assertEqual(final["counts"]["completed"], 3)


if __name__ == "__main__":
    unittest.main()
