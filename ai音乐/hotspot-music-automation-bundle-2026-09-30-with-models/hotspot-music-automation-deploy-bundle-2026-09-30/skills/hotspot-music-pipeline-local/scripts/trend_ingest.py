from __future__ import annotations

"""Collect public trend/news metadata for the local music pipeline.

This module intentionally uses only public RSS/JSON/HTML endpoints. It does
not log in, solve challenges, use cookies, call private mobile APIs, or
download media. Platforms without a stable public endpoint are represented by
an explicit ``unavailable`` source status and can be connected through an
official endpoint configured by the user.
"""

import argparse
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES = ROOT / "config" / "sources.yaml"


def load_yaml(path):
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for trend ingestion") from exc
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"sources config does not exist: {source}")
    return yaml.safe_load(source.read_text(encoding="utf-8")) or {}


def _now():
    return datetime.now().astimezone().isoformat()


def _number(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    # Public UI counters often use a trailing ``+`` (for example ``2w+``).
    # Detect the unit independently of the plus sign so the normalized heat
    # does not become just ``2`` or ``1``.
    lowered = text.lower()
    if "亿" in text:
        multiplier = 100000000
    elif "万" in text or "w" in lowered:
        multiplier = 10000
    elif "千" in text or "k" in lowered:
        multiplier = 1000
    else:
        multiplier = 1
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    try:
        return float(match.group(0)) * multiplier if match else None
    except (TypeError, ValueError):
        return None


def _first(mapping, names, default=None):
    for name in names:
        value = mapping.get(name)
        if value not in (None, ""):
            return value
    return default


def _path(value, dotted):
    current = value
    for part in str(dotted or "").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def _resolve_url(base, value):
    value = html.unescape(str(value or "").strip())
    return urllib.parse.urljoin(base, value) if value else ""


def _person_text(value):
    """Normalize common artist representations without assuming an API shape."""
    if isinstance(value, list):
        values = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("name") or item.get("title") or item.get("artist")
            if item not in (None, ""):
                values.append(str(item).strip())
        return ", ".join(item for item in values if item) or None
    if isinstance(value, dict):
        value = value.get("name") or value.get("title") or value.get("artist")
    return str(value).strip() if value not in (None, "") else None


class LinkParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        attrs = dict(attrs)
        href = _resolve_url(self.base_url, attrs.get("href"))
        if href and not href.startswith(("javascript:", "mailto:", "#")):
            self._current = {"url": href, "text": [], "attrs": attrs}

    def handle_data(self, data):
        if self._current is not None:
            self._current["text"].append(data)

    def handle_endtag(self, tag):
        if tag.lower() != "a" or self._current is None:
            return
        item = self._current
        item["text"] = re.sub(r"\s+", " ", " ".join(item["text"])).strip()
        self.links.append(item)
        self._current = None


class QQChartParser(HTMLParser):
    """Parse song title/artist links from QQ's public server-rendered chart."""
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.rows = []
        self.current = None
        self.anchor_text = None
        self.anchor_kind = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(str(attrs.get("class", "")).split())
        if tag.lower() == "div" and "songlist__item" in classes:
            self.current = {"title": "", "url": "", "author": ""}
        if self.current is None or tag.lower() != "a":
            return
        href = _resolve_url(self.base_url, attrs.get("href"))
        self.anchor_text = []
        if "/songDetail/" in href and not self.current["url"]:
            self.anchor_kind = "song"
            self.current["url"] = href
            self.current["title"] = _clean_title(attrs.get("title"))
        elif "/singer/" in href and not self.current["author"]:
            self.anchor_kind = "singer"
            self.current["author"] = _clean_title(attrs.get("title"))
        else:
            self.anchor_kind = "other"

    def handle_data(self, data):
        if self.anchor_text is not None:
            value = str(data).strip()
            if value:
                self.anchor_text.append(value)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.current is not None and self.anchor_text:
            text = _clean_title(" ".join(self.anchor_text))
            if self.current["url"] and not self.current["title"]:
                self.current["title"] = text
            elif self.anchor_kind == "singer" and self.current["author"] == "":
                self.current["author"] = text
            self.anchor_text = None
            self.anchor_kind = None
        if tag.lower() == "li" and self.current is not None:
            if self.current["title"]:
                self.rows.append(self.current)
            self.current = None


def _qq_items(source, raw, charset):
    parser = QQChartParser(source["url"])
    parser.feed(raw.decode(charset, errors="replace"))
    return [_base_record(source, row["title"], row["url"], index,
                         author=row.get("author"))
            for index, row in enumerate(parser.rows[:int(source.get("limit", 50))], 1)]


def _clean_title(value):
    text = html.unescape(re.sub(r"\s+", " ", str(value or ""))).strip()
    text = re.sub(r"^[#\d\s.、:：|丨-]+", "", text)
    return text[:240]


def _request(url, timeout, max_bytes=5_000_000):
    request = urllib.request.Request(
        url,
        headers={
            # Use a normal browser identification string for public pages that
            # return an empty shell to non-browser clients. No login or bypass.
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
            "Accept": "application/rss+xml, application/atom+xml, application/json, text/html;q=0.9, */*;q=0.1",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"response exceeds {max_bytes} bytes")
        content_type = response.headers.get_content_type()
        charset = response.headers.get_content_charset() or "utf-8"
        return data, content_type, charset


def _base_record(source, title, url, rank=None, heat=None, author=None, extra=None):
    row = {
        "source_id": source["id"],
        "platform": source.get("platform", source["id"]),
        "category": source.get("category", "hot_search"),
        "title": _clean_title(title),
        "url": str(url or source.get("url", "")),
        "rank": int(rank) if rank is not None and str(rank).isdigit() else rank,
        "heat": _number(heat),
        "author": str(author or "").strip() or None,
        "captured_at": _now(),
        "source_url": source.get("url", ""),
        "source_mode": source.get("source_mode") or source.get("type", "unknown"),
        "license_status": source.get("license_status", "metadata_only_public"),
    }
    if extra:
        row.update({key: value for key, value in extra.items() if value not in (None, "")})
    return row


def _rss_items(source, raw):
    root = ET.fromstring(raw)
    rows = []
    # RSS and Atom use different namespaces; local-name matching keeps this
    # parser compatible with common public feeds.
    for item in root.iter():
        name = item.tag.rsplit("}", 1)[-1].lower()
        if name not in {"item", "entry"}:
            continue
        fields = {}
        for child in list(item):
            key = child.tag.rsplit("}", 1)[-1].lower()
            value = "".join(child.itertext()).strip()
            if key == "link" and child.attrib.get("href"):
                value = child.attrib["href"]
            fields[key] = value
        title = _first(fields, ("title", "name"))
        url = _first(fields, ("link", "guid", "id"), source.get("url"))
        if title:
            rows.append(_base_record(source, title, _resolve_url(source["url"], url),
                                     rank=len(rows) + 1,
                                     extra={"published": _first(fields, ("pubdate", "published", "updated")),
                                            "summary": _clean_title(_first(fields, ("description", "summary"), ""))}))
    return rows


def _html_items(source, raw, charset):
    document = raw.decode(charset, errors="replace")
    lowered = document.lower()
    if source.get("id") == "weibo-realtime" and "sina visitor system" in lowered:
        raise ValueError("public page returned Sina Visitor System; no anonymous hot-search data")
    if source.get("id") == "douyin-hot" and re.search(r"<body[^>]*>\s*</body>", lowered):
        raise ValueError("public page returned a dynamic empty shell; no anonymous hot-search data")
    parser = LinkParser(source["url"])
    parser.feed(document)
    rows, seen = [], set()
    skip = {"首页", "登录", "注册", "下载", "更多", "搜索", "首页登录", "隐私", "帮助"}
    for link in parser.links:
        title = _clean_title(link.get("text"))
        if len(title) < 2 or title in skip or title.lower() in skip:
            continue
        if title in seen:
            continue
        seen.add(title)
        rows.append(_base_record(source, title, link.get("url"), rank=len(rows) + 1))
        if len(rows) >= int(source.get("limit", 50)):
            break
    return rows


def _json_items(source, raw, charset):
    payload = json.loads(raw.decode(charset, errors="replace"))
    items = _path(payload, source.get("items_path", "data"))
    if isinstance(items, dict):
        items = items.get("list") or items.get("items") or []
    if not isinstance(items, list):
        raise ValueError(f"items_path did not resolve to a list: {source.get('items_path')}")
    mapping = source.get("fields") or {}
    official_chart = bool(source.get("official_chart"))
    required = set(source.get("required_item_fields") or [])
    if official_chart:
        # An official-chart adapter must opt into the fields it promises to
        # expose.  This prevents a generic JSON list from being mislabeled as
        # a real-time chart when the endpoint returns incomplete metadata.
        required.update({"title", "artist", "rank"})
        if source.get("require_item_url", True):
            required.add("url")
    rows = []
    skipped = Counter()
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            skipped["item_not_object"] += 1
            continue
        title = _path(item, mapping.get("title", "title"))
        if not title:
            skipped["missing_title"] += 1
            continue
        rank_value = _path(item, mapping.get("rank", "rank"))
        try:
            parsed_rank = int(float(rank_value)) if rank_value not in (None, "") else None
        except (TypeError, ValueError):
            parsed_rank = None
        rank = (parsed_rank + int(source.get("rank_base", 0))) if source.get("rank_mode") == "field" and parsed_rank is not None else index
        url = _path(item, mapping.get("url", "url"))
        if not url and source.get("url_template") and item.get("id") is not None:
            url = str(source["url_template"]).format(id=item.get("id"))
        artist = _path(item, mapping.get("artist", mapping.get("author", "artist")))
        heat = _path(item, mapping.get("heat", "heat"))
        author = _person_text(_path(item, mapping.get("author", "author"))) or _person_text(artist)
        artist = _person_text(artist)
        resolved_url = _resolve_url(source["url"], url)
        if official_chart and url not in (None, ""):
            parsed_url = urllib.parse.urlparse(resolved_url)
            if parsed_url.scheme not in {"http", "https"}:
                skipped["invalid_url"] += 1
                continue
        checks = {
            "title": title,
            "artist": artist,
            "rank": rank_value if source.get("rank_mode") == "field" else rank,
            "url": resolved_url,
        }
        if official_chart and "rank" in required and parsed_rank is None:
            skipped["invalid_rank"] += 1
            continue
        allowed_hosts = {str(host).lower() for host in source.get("allowed_item_hosts", [])}
        if official_chart and allowed_hosts:
            if urllib.parse.urlparse(resolved_url).netloc.lower() not in allowed_hosts:
                skipped["item_url_outside_allowed_hosts"] += 1
                continue
        missing = [name for name in required if checks.get(name) in (None, "")]
        if missing:
            skipped["missing_" + "_".join(sorted(missing))] += 1
            continue
        extra = {}
        genre = _path(item, mapping.get("genre", "genre"))
        style_tags = _path(item, mapping.get("style_tags", "style_tags"))
        if genre:
            extra["genre"] = str(genre)
        if style_tags:
            extra["style_tags"] = style_tags if isinstance(style_tags, list) else [str(style_tags)]
        for name, fallback in (("artist", artist), ("album", "album"),
                               ("duration", "duration"), ("is_vip", "is_vip"),
                               ("item_id", "item_id"), ("chart_name", "chart_name")):
            value = fallback if name == "artist" else _path(item, mapping.get(name, fallback))
            if value not in (None, ""):
                extra[name] = value
        if official_chart:
            extra["official_chart"] = True
            extra["chart_schema"] = source.get("schema", "official_json")
        rows.append(_base_record(source, title, resolved_url, rank, heat, author, extra))
        if len(rows) >= int(source.get("limit", 50)):
            break
    if official_chart and not rows:
        detail = ", ".join(f"{key}={value}" for key, value in skipped.most_common()) or "no usable items"
        raise ValueError(f"official Soda chart payload failed required-field validation: {detail}")
    return rows


def _browser_items(path, source):
    """Normalize a browser-captured public snapshot without importing session data."""
    if source.get("official_chart"):
        raise ValueError("official Soda chart requires the authorized JSON endpoint; browser snapshots are not accepted")
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, list):
        items = payload
        captured_at = None
        page_url = None
    elif isinstance(payload, dict):
        items = payload.get("items") or payload.get("rows") or []
        captured_at = payload.get("captured_at")
        page_url = payload.get("page") or payload.get("source_url")
    else:
        raise ValueError("browser snapshot must be a JSON array or object with items")
    if not isinstance(items, list):
        raise ValueError("browser snapshot items must be a list")
    expected_url = source.get("url", "")
    expected_host = urllib.parse.urlparse(expected_url).netloc.lower()
    source_mode = (payload.get("source_mode") if isinstance(payload, dict) else None) or source.get("browser_source_mode", "browser_public_page")
    license_status = (payload.get("license_status") if isinstance(payload, dict) else None) or source.get("browser_license_status", "metadata_only_public")
    is_soda_client = source.get("id") == "soda-song-chart" or source.get("category") == "song_search"
    page_scheme = urllib.parse.urlparse(str(page_url or "")).scheme.lower()
    source_page = (page_url if page_scheme in {"http", "https"} else None) or expected_url or "https://music.douyin.com/"
    rows = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("name")
        item_url = item.get("url") or item.get("share_url")
        if not title:
            continue
        # Visible Soda client captures commonly expose only the current page,
        # not a public per-song detail URL. Keep that row as metadata with a
        # source-page link instead of silently dropping the song.
        item_link_status = "item_link"
        url = item_url or source_page
        parsed = urllib.parse.urlparse(str(url))
        if item_url and parsed.scheme not in {"http", "https"} and not (is_soda_client and parsed.scheme == "soda"):
            raise ValueError("browser snapshot contains a non-HTTP item URL")
        if item_url and parsed.scheme in {"http", "https"} and expected_host and parsed.netloc.lower() != expected_host:
            if not is_soda_client:
                raise ValueError(f"browser snapshot contains a URL outside {expected_host}")
        if not item_url:
            item_link_status = "source_page_only"
        stats = item.get("weibo_stats") or item.get("stats") or {}
        interaction = item.get("interaction") or {}
        likes = _number(stats.get("likes", stats.get("likes_display", interaction.get("likes", interaction.get("likes_display", item.get("likes"))))))
        comments = _number(stats.get("comments", stats.get("comments_display", interaction.get("comments", interaction.get("comments_display", item.get("comments"))))))
        forwards = _number(stats.get("forwards", stats.get("forwards_display", interaction.get("forwards", interaction.get("forwards_display", item.get("forwards"))))))
        shares = _number(stats.get("shares", stats.get("shares_display", interaction.get("shares", interaction.get("shares_display", item.get("shares"))))))
        total = _number(item.get("heat"))
        if total is None:
            total = sum(value for value in (likes, comments, forwards, shares) if value is not None) or None
        extra = {
            "published": item.get("published"),
            "playlist": item.get("playlist") or payload.get("playlist") if isinstance(payload, dict) else None,
            "page": page_url,
            "screenshot": item.get("screenshot"),
            "item_link_status": item_link_status,
            "official_chart": False if is_soda_client else None,
        }
        if source.get("id") == "weibo-realtime" or source.get("category") == "hot_search":
            extra["weibo_stats"] = {
                    "likes": int(likes) if likes is not None else None,
                    "comments": int(comments) if comments is not None else None,
                    "forwards": int(forwards) if forwards is not None else None,
                    "shares": int(shares) if shares is not None else None,
                    "interaction_total": int(total) if total is not None else None,
                }
        if is_soda_client:
            extra["interaction"] = {
                "likes_display": interaction.get("likes_display"),
                "comments_display": interaction.get("comments_display"),
                "shares_display": interaction.get("shares_display"),
                "likes": int(likes) if likes is not None else None,
                "comments": int(comments) if comments is not None else None,
                "shares": int(shares) if shares is not None else None,
                "interaction_total": int(total) if total is not None else None,
            }
        for key in ("item_id", "artist", "album", "duration", "is_vip", "chart_name", "query", "genre", "style_tags"):
            if item.get(key) not in (None, ""):
                extra[key] = item[key]
        author = item.get("author") or item.get("artist")
        row = _base_record(
            dict(source, url=expected_url), title, url,
            item.get("rank") or item.get("position") or index, total, author, extra,
        )
        # Preserve visible hashtag markers for downstream topic extraction and
        # retain the provenance of the browser import after normalization.
        row["title"] = str(title).strip()[:240]
        row["source_mode"] = source_mode
        row["license_status"] = license_status
        rows.append(row)
        if len(rows) >= int(source.get("limit", 50)):
            break
    if not rows:
        raise ValueError(f"browser snapshot contains no usable public {source.get('platform', 'source')} rows")
    return rows, {
        "captured_at": captured_at,
        "page": page_url,
        "source_mode": source_mode,
        "license_status": license_status,
    }


def fetch_source(source, timeout=15):
    started = _now()
    configured_url = source.get("url")
    env_name = source.get("url_env")
    if env_name:
        configured_url = os.environ.get(env_name) or configured_url
    if not configured_url:
        status = {"source_id": source["id"], "platform": source.get("platform"),
                "category": source.get("category"), "status": "unavailable",
                "item_count": 0, "fetched_at": started,
                "reason": "no public endpoint configured; set the official endpoint environment variable"}
        if source.get("official_chart"):
            status["official_endpoint"] = True
            status["schema"] = source.get("schema", "official_json")
            status["source_mode"] = source.get("source_mode", "official_json")
        return status, []
    source = dict(source, url=configured_url)
    try:
        raw, content_type, charset = _request(configured_url, timeout)
        source_type = source.get("type", "rss").lower()
        if source_type == "rss":
            rows = _rss_items(source, raw)
        elif source_type == "json":
            rows = _json_items(source, raw, charset)
        elif source_type == "html_qq_chart":
            rows = _qq_items(source, raw, charset)
        elif source_type == "html_rank":
            rows = _html_items(source, raw, charset)
        else:
            raise ValueError(f"unsupported source type: {source_type}")
        status = {"source_id": source["id"], "platform": source.get("platform"),
                  "category": source.get("category"), "status": "ok" if rows else "empty",
                  "item_count": len(rows), "fetched_at": started, "content_type": content_type,
                  "source_url": configured_url,
                  "source_mode": source.get("source_mode") or source.get("type", "unknown")}
        if source.get("official_chart"):
            status.update({"official_endpoint": True,
                           "schema": source.get("schema", "official_json"),
                           "authorization": "endpoint supplied through environment variable"})
        return status, rows
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError,
            ET.ParseError, json.JSONDecodeError) as exc:
        # Bilibili's public ranking endpoint can briefly return a valid JSON
        # envelope with ``data`` missing/null (often during cache refresh).
        # Retry once without changing the endpoint or using private state.
        if source.get("id", "").startswith("bilibili-") and source.get("type", "").lower() == "json":
            attempt = 0
            while True:
                try:
                    attempt += 1
                    time.sleep(0.4 if attempt == 1 else 300)
                    # Repeat the exact public URL; adding arbitrary query keys
                    # can itself trigger Bilibili's anti-abuse response.
                    raw, content_type, charset = _request(configured_url, timeout)
                    probe = json.loads(raw.decode(charset, errors="replace"))
                    if probe.get("code") == -352:
                        continue
                    rows = _json_items(source, raw, charset)
                    status = {"source_id": source["id"], "platform": source.get("platform"),
                              "category": source.get("category"), "status": "ok" if rows else "empty",
                              "item_count": len(rows), "fetched_at": started, "content_type": content_type,
                              "source_url": configured_url, "source_mode": source.get("source_mode") or "json",
                              "retry": True, "retry_attempts": attempt}
                    return status, rows
                except Exception:
                    break
        reason = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
    return {"source_id": source["id"], "platform": source.get("platform"),
            "category": source.get("category"), "status": "unavailable", "item_count": 0,
            "fetched_at": started, "source_url": configured_url, "reason": reason,
            **({"official_endpoint": True,
                "schema": source.get("schema", "official_json"),
                "source_mode": source.get("source_mode", "official_json")}
               if source.get("official_chart") else {})}, []


STOPWORDS = set("我们 你们 他们 以及 这个 那个 相关 进行 记者 表示 今日 最新 网友 话题 事件 新闻 视频 音乐 歌曲 热门 真的 一个 没有 可以 发生".split())
EMOTION_WORDS = {
    "悲伤": ("离世", "失踪", "事故", "悲痛", "哀悼", "冲突", "灾害", "受伤"),
    "紧张": ("风险", "警报", "争议", "冲突", "调查", "突发", "危机"),
    "希望": ("成功", "突破", "夺冠", "复苏", "救援", "回归", "开通"),
    "温暖": ("互助", "感谢", "团圆", "守护", "志愿", "暖心", "帮助"),
    "振奋": ("冠军", "胜利", "夺冠", "突破", "发射", "金牌", "晋级"),
}
SCENE_WORDS = {
    "城市与公共生活": ("城市", "交通", "地铁", "机场", "学校", "医院"),
    "自然与天气": ("台风", "暴雨", "洪水", "地震", "高温", "降雪", "海洋"),
    "科技与未来": ("人工智能", "AI", "芯片", "机器人", "太空", "卫星", "航天"),
    "体育与现场": ("比赛", "球场", "奥运", "世界杯", "联赛", "夺冠"),
    "社会与民生": ("就业", "消费", "住房", "医疗", "教育", "养老"),
}


def _keywords(titles, limit=12):
    counts = Counter()
    for title in titles:
        chunks = re.findall(r"[\u4e00-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9-]{2,}", title)
        for chunk in chunks:
            if chunk not in STOPWORDS and len(chunk) >= 2:
                counts[chunk] += 1
    return [word for word, _ in counts.most_common(limit)]


def aggregate_hotspot(rows, statuses, captured_at=None):
    music_categories = {"song_chart", "song_search"}
    news_rows = [row for row in rows if row.get("category") not in music_categories]
    news_rows.sort(key=lambda row: (
        row.get("rank") if isinstance(row.get("rank"), int) else 999999,
        -(row.get("heat") or 0),
    ))
    titles = [row["title"] for row in news_rows if row.get("title")]
    emotions = [name for name, words in EMOTION_WORDS.items() if any(any(word in title for word in words) for title in titles)]
    scenes = [name for name, words in SCENE_WORDS.items() if any(any(word in title for word in words) for title in titles)]
    music_rows = [row for row in rows if row.get("category") in music_categories]
    style_tags = Counter()
    for row in music_rows:
        # Chart sections such as "cover" or "music review" are content
        # formats, not genres. Only an explicit style_tags field, or a source
        # that opted into genre mapping, may affect the Suno genre prompt.
        values = row.get("style_tags") or ([row.get("genre")] if row.get("genre") and row.get("style_from_genre") else [])
        for value in values:
            if str(value).strip():
                style_tags[str(value).strip()] += 1
    top_titles = titles[:8]
    summary = "；".join(top_titles[:3]) if top_titles else "暂无可用公开热点摘要"
    return {
        "captured_at": captured_at or _now(),
        "summary": summary,
        "keywords": _keywords(titles),
        "emotion": emotions or ["关注"],
        "scene": scenes,
        "trend_style_tags": [word for word, _ in style_tags.most_common(8)],
        "trend_production_cues": (["hook-forward chorus", "clear vocal lead", "short memorable intro"]
                                   if music_rows else []),
        "trend_sources": statuses,
        "top_titles": top_titles,
        "music_trends": [{key: row.get(key) for key in ("platform", "title", "artist", "author", "rank", "heat", "genre", "style_tags", "album", "duration", "url", "playlist", "page", "screenshot", "item_link_status", "interaction", "official_chart") if row.get(key) is not None}
                         for row in sorted(music_rows, key=lambda item: item.get("rank") or 999999)[:20]],
        "music_chart_count": len(music_rows),
        "news_item_count": len(news_rows),
        "policy": "public_metadata_only; verify facts and rights before publication",
    }


def write_trend_report(out, rows, statuses):
    """Write a compact Markdown report for human review of one collection run."""
    out = Path(out)
    lines = ["# 公开榜单采集报告", "", f"采集时间：{_now()}", "",
             "> 仅包含公开榜单元数据；不下载歌曲，不读取账号信息。", "", "## 来源状态", ""]
    for status in statuses:
        source = status.get("platform") or status.get("source_id") or "未命名来源"
        detail = status.get("reason") or status.get("source_url") or "-"
        lines += [f"- **{source}**：`{status.get('status', '-')}`，条数 {status.get('item_count', 0)}；{detail}"]
    lines += ["", f"## 榜单条目（{len(rows)} 条）", ""]
    if not rows:
        lines += ["暂无可用公开榜单条目。", ""]
    else:
        lines += ["| 排名 | 歌曲 | 歌手/作者 | 歌单 | 热度/播放量 | 互动合计 | 来源 | 链接 |", "|---:|---|---|---|---:|---:|---|---|"]
        for row in rows:
            title = str(row.get("title") or "-").replace("|", "\\|")
            artist = str(row.get("artist") or row.get("author") or "-").replace("|", "\\|")
            playlist = str(row.get("playlist") or "-").replace("|", "\\|")
            heat = row.get("heat") if row.get("heat") is not None else "-"
            interaction = row.get("interaction") or row.get("weibo_stats") or {}
            interaction_total = interaction.get("interaction_total", "-") if isinstance(interaction, dict) else "-"
            url = str(row.get("url") or "-").replace("|", "\\|")
            lines.append(f"| {row.get('rank', '-')} | {title} | {artist} | {playlist} | {heat} | {interaction_total} | {row.get('platform', '-')} | {url} |")
    (out / "trend-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def collect(args):
    config = load_yaml(args.sources)
    sources = config.get("sources") or []
    if args.only:
        wanted = set(args.only.split(","))
        sources = [source for source in sources if source.get("id") in wanted or source.get("platform") in wanted]
    statuses, rows = [], []
    browser_rows = None
    browser_meta = {}
    if args.browser_json:
        browser_source_id = args.browser_source or "weibo-realtime"
        browser_source = next((source for source in sources if source.get("id") == browser_source_id), None)
        if not browser_source:
            raise ValueError(f"--browser-json requires source {browser_source_id!r} in sources.yaml")
        browser_rows, browser_meta = _browser_items(args.browser_json, browser_source)
    for index, source in enumerate(sources):
        env_configured = bool(source.get("url_env") and os.environ.get(source.get("url_env")))
        if source.get("id") == (args.browser_source or "weibo-realtime") and browser_rows is not None:
            source_mode = browser_meta.get("source_mode") or source.get("browser_source_mode", "browser_public_page")
            statuses.append({"source_id": source["id"], "platform": source.get("platform"),
                             "category": source.get("category"), "status": "ok",
                             "item_count": len(browser_rows), "fetched_at": browser_meta.get("captured_at") or _now(),
                             "source_url": source.get("url"), "source_mode": source_mode,
                             "license_status": browser_meta.get("license_status"),
                             "page": browser_meta.get("page"),
                             "reason": "captured from visible public DOM; no cookies or local storage imported"})
            rows.extend(browser_rows[:args.per_source_limit])
            continue
        if not source.get("enabled", True) and not env_configured:
            statuses.append({"source_id": source.get("id"), "platform": source.get("platform"),
                             "category": source.get("category"), "status": "disabled", "item_count": 0,
                             "fetched_at": _now(), "reason": source.get("disabled_reason", "disabled in config")})
            continue
        status, source_rows = fetch_source(source, args.timeout)
        statuses.append(status)
        selected_rows = source_rows[:args.per_source_limit]
        status["item_count"] = len(selected_rows)
        rows.extend(selected_rows)
        if index + 1 < len(sources):
            time.sleep(max(0.0, args.delay))
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    snapshot = out / "trend-snapshot.jsonl"
    snapshot.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    (out / "trend-status.json").write_text(json.dumps({"captured_at": _now(), "sources": statuses}, ensure_ascii=False, indent=2), encoding="utf-8")
    hotspot = aggregate_hotspot(rows, statuses)
    (out / "hotspot.json").write_text(json.dumps(hotspot, ensure_ascii=False, indent=2), encoding="utf-8")
    write_trend_report(out, rows, statuses)
    print(json.dumps({"status": "ok", "item_count": len(rows), "source_count": len(sources),
                      "available_sources": sum(status.get("status") == "ok" for status in statuses),
                      "output": str(out), "snapshot": str(snapshot),
                      "hotspot": str(out / "hotspot.json")}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Collect public news, hot-search and music-chart metadata")
    parser.add_argument("collect", nargs="?", help="collect configured sources")
    parser.add_argument("--sources", default=str(DEFAULT_SOURCES))
    parser.add_argument("--output", required=True, help="output directory")
    parser.add_argument("--only", help="comma-separated source IDs or platform names")
    parser.add_argument("--browser-json", help="JSON snapshot exported from a visible public browser page or client")
    parser.add_argument("--browser-source", help="source ID represented by --browser-json; defaults to weibo-realtime")
    parser.add_argument("--per-source-limit", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--delay", type=float, default=0.5)
    args = parser.parse_args()
    collect(args)


if __name__ == "__main__":
    main()
