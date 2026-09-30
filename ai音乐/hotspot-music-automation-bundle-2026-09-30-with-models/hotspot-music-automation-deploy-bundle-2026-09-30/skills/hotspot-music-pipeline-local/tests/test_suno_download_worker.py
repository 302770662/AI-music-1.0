import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_ROOT))
import suno_batch_generator as sbg  # noqa: E402
import suno_download_worker as worker  # noqa: E402


def make_generation_output(task_root: Path, task_id: str = "bilibili-daily", song_id: str = "song-primary") -> Path:
    output = task_root / task_id / "2026-09-03" / "suno-generation"
    output.mkdir(parents=True)
    status = {
        "schema": "suno_generation_status_v1",
        "status": "completed",
        "prompt_count": 1,
        "variants_per_prompt": 1,
        "target_song_count": 1,
        "counts": {"queued": 0, "running": 0, "completed": 1, "failed": 0, "blocked": 0, "total": 1},
        "prompt_groups": [{
            "title": "测试歌曲",
            "variants": [{
                "status": "completed",
                "song_id": song_id,
                "song_url": f"https://suno.com/song/{song_id}",
            }],
        }],
    }
    (output / worker.GENERATION_STATUS_NAME).write_text(json.dumps(status), encoding="utf-8")
    return output


class SunoDownloadWorkerTests(unittest.TestCase):
    def test_failed_or_blocked_item_does_not_stop_following_queue_items(self):
        with tempfile.TemporaryDirectory() as temporary:
            task_root = Path(temporary) / "tasks"
            state_path = Path(temporary) / "state.json"
            generation = make_generation_output(task_root)
            generation_status = worker.read_json(generation / worker.GENERATION_STATUS_NAME)
            generation_status["prompt_groups"][0]["variants"].append({
                "status": "completed",
                "song_id": "song-secondary",
                "song_url": "https://suno.com/song/song-secondary",
            })
            generation_status["target_song_count"] = 2
            (generation / worker.GENERATION_STATUS_NAME).write_text(
                json.dumps(generation_status), encoding="utf-8"
            )
            with patch.object(worker, "TASK_ROOT", task_root), patch.object(worker, "STATE_PATH", state_path), patch.object(worker, "refresh_dashboard", lambda _path: None):
                worker.start(worker.StartRequest(task_ids=["bilibili-daily"], scope="all", auto_download=True, follow_future=True))

                first = worker.command()
                self.assertEqual(first["action"], "execute")
                worker.error(worker.ErrorRequest(
                    task_id=first["task_id"], queue_path=first["queue_path"],
                    reason="temporary browser download error", action="fail",
                ))
                state = worker.read_state()
                self.assertTrue(state["active"])
                self.assertTrue(state["download_authorized"])
                second = worker.command()
                self.assertEqual(second["action"], "execute")
                self.assertNotEqual(second["task_id"], first["task_id"])

                worker.error(worker.ErrorRequest(
                    task_id=second["task_id"], queue_path=second["queue_path"],
                    reason="visible menu blocked", action="block",
                ))
                state = worker.read_state()
                self.assertTrue(state["active"])
                self.assertTrue(state["download_authorized"])

    def test_verified_song_creates_exactly_mp3_and_video_jobs_idempotently(self):
        with tempfile.TemporaryDirectory() as temporary:
            task_root = Path(temporary) / "tasks"
            generation = make_generation_output(task_root)
            with patch.object(worker, "TASK_ROOT", task_root), patch.object(worker, "refresh_dashboard", lambda _path: None):
                download = worker.sync_queue(generation)
                self.assertIsNotNone(download)
                first = worker.load_jobs(download)
                self.assertEqual({item["download_type"] for item in first}, {"mp3", "video"})
                self.assertEqual(len(first), 2)
                worker.sync_queue(generation)
                second = worker.load_jobs(download)
                self.assertEqual([item["task_id"] for item in first], [item["task_id"] for item in second])
                self.assertEqual(worker.read_json(download / worker.STATUS_NAME)["target_download_count"], 2)

    def test_start_command_is_authorized_and_only_allowed_tasks_are_scanned(self):
        with tempfile.TemporaryDirectory() as temporary:
            task_root = Path(temporary) / "tasks"
            state_path = Path(temporary) / "state.json"
            make_generation_output(task_root, "bilibili-daily")
            make_generation_output(task_root, "qqmusic-daily", "song-disabled")
            with patch.object(worker, "TASK_ROOT", task_root), patch.object(worker, "STATE_PATH", state_path), patch.object(worker, "refresh_dashboard", lambda _path: None):
                started = worker.start(worker.StartRequest(task_ids=["bilibili-daily"], scope="all", auto_download=True, follow_future=True))
                self.assertTrue(started["active"])
                self.assertTrue(started["download_authorized"])
                command = worker.command()
                self.assertEqual(command["action"], "execute")
                self.assertTrue(command["auto_download"])
                self.assertEqual(worker.generation_candidates(["qqmusic-daily"], "all"), [])

    def test_one_shot_start_syncs_existing_queue_without_following_future(self):
        with tempfile.TemporaryDirectory() as temporary:
            task_root = Path(temporary) / "tasks"
            state_path = Path(temporary) / "state.json"
            make_generation_output(task_root)
            with patch.object(worker, "TASK_ROOT", task_root), patch.object(worker, "STATE_PATH", state_path), patch.object(worker, "refresh_dashboard", lambda _path: None):
                started = worker.start(worker.StartRequest(
                    task_ids=["bilibili-daily"], scope="all", auto_download=True, follow_future=False
                ))
                self.assertTrue(started["active"])
                self.assertEqual(started["download_queue_count"], 1)
                command = worker.command()
                self.assertEqual(command["action"], "execute")
                self.assertEqual(command["job"]["download_type"], "mp3")

    def test_start_can_limit_download_to_one_verified_song(self):
        with tempfile.TemporaryDirectory() as temporary:
            task_root = Path(temporary) / "tasks"
            state_path = Path(temporary) / "state.json"
            make_generation_output(task_root, song_id="song-primary")
            with patch.object(worker, "TASK_ROOT", task_root), patch.object(worker, "STATE_PATH", state_path), patch.object(worker, "refresh_dashboard", lambda _path: None):
                started = worker.start(worker.StartRequest(
                    task_ids=["bilibili-daily"], scope="all", auto_download=True,
                    follow_future=False, song_ids=["song-primary"]
                ))
                self.assertEqual(started["counts"]["total"], 2)
                command = worker.command()
                self.assertEqual(command["job"]["song_id"], "song-primary")

    def test_empty_or_wrong_extension_cannot_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            task_root = Path(temporary) / "tasks"
            state_path = Path(temporary) / "state.json"
            make_generation_output(task_root)
            with patch.object(worker, "TASK_ROOT", task_root), patch.object(worker, "STATE_PATH", state_path), patch.object(worker, "refresh_dashboard", lambda _path: None):
                worker.start(worker.StartRequest(task_ids=["bilibili-daily"], scope="all", auto_download=True, follow_future=True))
                command = worker.command()
                self.assertEqual(command["action"], "execute")
                empty = Path(temporary) / "empty.mp3"
                empty.write_bytes(b"")
                with self.assertRaises(HTTPException):
                    worker.result(worker.ResultRequest(
                        task_id=command["task_id"], queue_path=command["queue_path"],
                        file_path=str(empty), filename="empty.mp3", bytes_received=0,
                    ))
                wrong = Path(temporary) / "wrong.mp4"
                wrong.write_bytes(b"not an mp3")
                with self.assertRaises(HTTPException):
                    worker.result(worker.ResultRequest(
                        task_id=command["task_id"], queue_path=command["queue_path"],
                        file_path=str(wrong), filename="wrong.mp4", bytes_received=9,
                    ))
                job = worker.load_current_job(worker.read_state())[1]
                self.assertEqual(job["status"], "running")

    def test_nonempty_mp3_and_mp4_complete_independently_and_record_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            task_root = Path(temporary) / "tasks"
            state_path = Path(temporary) / "state.json"
            generation = make_generation_output(task_root)
            with patch.object(worker, "TASK_ROOT", task_root), patch.object(worker, "STATE_PATH", state_path), patch.object(worker, "refresh_dashboard", lambda _path: None):
                worker.start(worker.StartRequest(task_ids=["bilibili-daily"], scope="all", auto_download=True, follow_future=True))
                first = worker.command()
                mp3 = Path(temporary) / "suno.mp3"
                mp3.write_bytes(b"ID3" + b"\x00" * 32)
                result = worker.result(worker.ResultRequest(
                    task_id=first["task_id"], queue_path=first["queue_path"],
                    file_path=str(mp3), filename="suno.mp3", bytes_received=5,
                ))
                self.assertEqual(result["job_status"], "completed")
                second = worker.command()
                self.assertEqual(second["action"], "execute")
                mp4 = Path(temporary) / "suno.mp4"
                mp4.write_bytes(b"\x00\x00\x00\x18ftyp" + b"\x00" * 32)
                result = worker.result(worker.ResultRequest(
                    task_id=second["task_id"], queue_path=second["queue_path"],
                    file_path=str(mp4), filename="suno.mp4", bytes_received=5,
                ))
                self.assertEqual(result["job_status"], "completed")
                status = worker.read_json(Path(first["queue_path"]) / worker.STATUS_NAME)
                self.assertEqual(status["completed_download_count"], 2)
                self.assertEqual(status["counts"]["completed"], 2)
                files = list((Path(first["queue_path"]) / worker.FILES_DIR_NAME / "song-primary").glob("*"))
                self.assertEqual({path.suffix for path in files}, {".mp3", ".mp4"})

    def test_next_song_mp3_is_claimed_before_previous_song_video(self):
        with tempfile.TemporaryDirectory() as temporary:
            task_root = Path(temporary) / "tasks"
            state_path = Path(temporary) / "state.json"
            generation = make_generation_output(task_root, song_id="song-primary")
            generation_status = worker.read_json(generation / worker.GENERATION_STATUS_NAME)
            generation_status["prompt_groups"][0]["variants"].append({
                "status": "completed",
                "song_id": "song-secondary",
                "song_url": "https://suno.com/song/song-secondary",
            })
            generation_status["target_song_count"] = 2
            (generation / worker.GENERATION_STATUS_NAME).write_text(
                json.dumps(generation_status), encoding="utf-8"
            )
            with patch.object(worker, "TASK_ROOT", task_root), patch.object(worker, "STATE_PATH", state_path), patch.object(worker, "refresh_dashboard", lambda _path: None):
                worker.start(worker.StartRequest(task_ids=["bilibili-daily"], scope="all", auto_download=True, follow_future=True))
                first = worker.command()
                self.assertEqual(first["job"]["song_id"], "song-primary")
                self.assertEqual(first["job"]["download_type"], "mp3")
                worker.error(worker.ErrorRequest(task_id=first["task_id"], queue_path=first["queue_path"], reason="test", action="fail"))
                second = worker.command()
                self.assertEqual(second["job"]["song_id"], "song-secondary")
                self.assertEqual(second["job"]["download_type"], "mp3")

    def test_chrome_tmp_suffix_is_accepted_only_after_real_mp3_header_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            task_root = Path(temporary) / "tasks"
            state_path = Path(temporary) / "state.json"
            generation = make_generation_output(task_root)
            with patch.object(worker, "TASK_ROOT", task_root), patch.object(worker, "STATE_PATH", state_path), patch.object(worker, "refresh_dashboard", lambda _path: None):
                worker.start(worker.StartRequest(task_ids=["bilibili-daily"], scope="all", auto_download=True, follow_future=True))
                command = worker.command()
                tmp_mp3 = Path(temporary) / "random.tmp"
                tmp_mp3.write_bytes(b"ID3" + b"\x00" * 32)
                result = worker.result(worker.ResultRequest(
                    task_id=command["task_id"], queue_path=command["queue_path"],
                    file_path=str(tmp_mp3), filename=tmp_mp3.name, bytes_received=35,
                ))
                self.assertEqual(result["job_status"], "completed")

    def test_stop_does_not_claim_new_jobs_and_current_can_be_requeued(self):
        with tempfile.TemporaryDirectory() as temporary:
            task_root = Path(temporary) / "tasks"
            state_path = Path(temporary) / "state.json"
            make_generation_output(task_root)
            with patch.object(worker, "TASK_ROOT", task_root), patch.object(worker, "STATE_PATH", state_path), patch.object(worker, "refresh_dashboard", lambda _path: None):
                worker.start(worker.StartRequest(task_ids=["bilibili-daily"], scope="all", auto_download=True, follow_future=True))
                current = worker.command()
                stopped = worker.stop()
                self.assertTrue(stopped["stop_requested"])
                abort = worker.command()
                self.assertEqual(abort["action"], "abort")
                worker.error(worker.ErrorRequest(
                    task_id=current["task_id"], queue_path=current["queue_path"],
                    reason="test stop", action="requeue",
                ))
                self.assertIsNone(worker.read_state()["current"])
                self.assertEqual(worker.load_jobs(Path(current["queue_path"]))[0]["status"], "queued")


if __name__ == "__main__":
    unittest.main()
