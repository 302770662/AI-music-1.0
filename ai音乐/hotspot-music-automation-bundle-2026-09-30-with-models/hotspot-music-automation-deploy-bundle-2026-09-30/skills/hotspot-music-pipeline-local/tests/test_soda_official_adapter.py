"""Regression tests for the authorized Soda official-chart JSON adapter."""

import json
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from unittest.mock import patch

from trend_ingest import _json_items, fetch_source  # noqa: E402


class SodaOfficialAdapterTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "id": "soda-official-json",
            "platform": "汽水音乐官方实时热歌榜",
            "category": "song_chart",
            "type": "json",
            "url": "https://official.example/soda/realtime-chart.json",
            "official_chart": True,
            "schema": "soda_official_realtime_chart_v1",
            "items_path": "data",
            "rank_mode": "field",
            "required_item_fields": ["title", "artist", "rank", "url"],
            "fields": {
                "title": "name",
                "artist": "artist",
                "url": "url",
                "rank": "rank",
                "heat": "heat",
                "style_tags": "style_tags",
                "item_id": "id",
            },
            "allowed_item_hosts": ["music.douyin.com", "douyin.com", "www.douyin.com"],
            "limit": 30,
        }

    def test_valid_payload_preserves_official_provenance_and_music_fields(self):
        payload = {
            "chart_name": "汽水音乐官方实时热歌榜",
            "updated_at": "2026-09-01T12:00:00+08:00",
            "data": [{
                "id": "song-1", "name": "示例原创歌曲", "artist": "示例歌手",
                "rank": 1, "heat": "12.3万",
                "url": "https://music.douyin.com/qishui/share/track?track_id=song-1",
                "style_tags": ["pop", "warm"],
            }],
        }
        rows = _json_items(self.source, json.dumps(payload).encode(), "utf-8")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rank"], 1)
        self.assertEqual(rows[0]["artist"], "示例歌手")
        self.assertEqual(rows[0]["heat"], 123000.0)
        self.assertTrue(rows[0]["official_chart"])
        self.assertEqual(rows[0]["chart_schema"], "soda_official_realtime_chart_v1")
        self.assertEqual(rows[0]["style_tags"], ["pop", "warm"])

    def test_checked_in_fixture_is_compatible(self):
        fixture = Path(__file__).parent / "fixtures" / "soda-official-realtime-v1.json"
        rows = _json_items(self.source, fixture.read_bytes(), "utf-8")
        self.assertEqual([row["rank"] for row in rows], [1, 2])
        self.assertEqual(rows[1]["artist"], "示例歌手甲, 示例歌手乙")

    def test_missing_required_fields_fail_closed(self):
        payload = {"data": [{"name": "缺少官方链接", "artist": "示例歌手", "rank": 1}]}
        with self.assertRaisesRegex(ValueError, "required-field validation"):
            _json_items(self.source, json.dumps(payload).encode(), "utf-8")

    def test_fetch_status_records_official_schema(self):
        payload = {"data": [{
            "name": "示例歌曲", "artist": "示例歌手", "rank": 1,
            "url": "https://music.douyin.com/qishui/share/track?track_id=1",
        }]}
        with patch("trend_ingest._request", return_value=(
            json.dumps(payload).encode(), "application/json", "utf-8"
        )):
            status, rows = fetch_source(self.source)
        self.assertEqual(status["status"], "ok")
        self.assertTrue(status["official_endpoint"])
        self.assertEqual(status["schema"], "soda_official_realtime_chart_v1")
        self.assertEqual(status["source_mode"], "json")
        self.assertEqual(len(rows), 1)

    def test_invalid_rank_and_non_soda_link_fail_closed(self):
        payload = {"data": [{
            "name": "不可信歌曲", "artist": "示例歌手", "rank": "first",
            "url": "https://example.invalid/song",
        }]}
        with self.assertRaisesRegex(ValueError, "required-field validation"):
            _json_items(self.source, json.dumps(payload).encode(), "utf-8")


if __name__ == "__main__":
    unittest.main()
