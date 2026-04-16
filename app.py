"""TubeIntel — simplified Flask API

Exposes two operations:
- /api/channel/analyze  (POST): runs Channel Intelligence pipeline (fetch list, deep-fetch top N, save CSV/JSON)
- /api/video/process    (POST): runs Video Intelligence (enrich single video: metadata + transcript)

This file is a server-adapted version of the provided Colab script.
"""

import os
import re
import json
import csv
import logging
import hashlib
from datetime import datetime
from threading import Lock
from itertools import count
from typing import List, Dict, Optional
from urllib.parse import urlparse, parse_qs

import yt_dlp
from flask import Flask, request, jsonify, send_from_directory, abort
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# Quiet noisy third-party loggers
logging.getLogger("yt_dlp").setLevel(logging.ERROR)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

# Stepwise sequencer for high-level operations
_step_counter = count(1)
def log_step(msg: str) -> None:
    n = next(_step_counter)
    log.info("STEP %02d: %s", n, msg)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

app = Flask(__name__, static_folder=None)
CORS(app)

# Default transcript languages (can be overridden by request body)
TRANSCRIPT_LANGS = ["en"]

# Path to cookies file — export from browser using a cookies.txt extension
# Place cookies.txt next to app.py on the server
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")

# Chrome user-agent to avoid bot detection
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# -------------------------
# yt-dlp base options
# -------------------------
def base_ydl_opts() -> dict:
    """
    Returns the baseline yt-dlp options applied to every call.
    - cookies.txt: bypasses sign-in / 429 bot blocks
    - user_agent: impersonates Chrome
    - extractor_args: skips webpage + JS player parsing (faster, fewer bot signals)
    - retries: handles transient 429s gracefully
    """
    opts = {
        "quiet": True,
        "skip_download": True,
        "ignoreerrors": True,
        "user_agent": CHROME_UA,
        "extractor_args": {
            "youtube": {
                "skip": ["webpage"],
                "player_skip": ["js"],
            }
        },
        "retries": 5,
    }
    if os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
        log.debug("Using cookies file: %s", COOKIES_FILE)
    else:
        log.warning("cookies.txt not found at %s — requests may hit 429/bot errors", COOKIES_FILE)
    return opts


# -------------------------
# Utility helpers
# -------------------------
def format_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def extract_video_id_from_url(url: str) -> Optional[str]:
    """
    Robustly extract YouTube video ID directly from the URL string.
    Handles all common YouTube URL formats without needing yt-dlp:
      - https://www.youtube.com/watch?v=VIDEO_ID
      - https://youtu.be/VIDEO_ID
      - https://www.youtube.com/embed/VIDEO_ID
      - https://www.youtube.com/shorts/VIDEO_ID
      - https://m.youtube.com/watch?v=VIDEO_ID
    """
    if not url:
        return None

    # youtu.be short links
    short_match = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", url)
    if short_match:
        return short_match.group(1)

    # embed / shorts / v= param
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "v" in qs:
        vid = qs["v"][0]
        if re.match(r"^[A-Za-z0-9_-]{11}$", vid):
            return vid

    # /embed/ID or /shorts/ID or /v/ID
    path_match = re.search(r"/(embed|shorts|v)/([A-Za-z0-9_-]{11})", parsed.path)
    if path_match:
        return path_match.group(2)

    return None


def safe_extract_info(url: str, ydl_opts: Optional[dict] = None) -> dict:
    opts = base_ydl_opts()           # start with anti-bot base
    opts.update(ydl_opts or {})      # layer caller overrides on top
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except Exception as e:
        log.warning("yt_dlp extract_info failed for %s: %s", url, e)
        return {}


# -------------------------
# Data pipeline functions
# -------------------------
def get_channel_videos(channel_url: str, max_videos: Optional[int] = None) -> List[Dict]:
    """Fast-fetch flattened video list from a channel or playlist URL."""
    ydl_opts = {
        "extract_flat": True,
        "playlistend": max_videos,
    }
    info = safe_extract_info(channel_url, ydl_opts)
    entries = info.get("entries") or []
    videos: List[Dict] = []
    for entry in entries:
        if not entry:
            continue
        vid = entry.get("id") or entry.get("url")
        if not vid:
            continue
        url = entry.get("url") or f"https://www.youtube.com/watch?v={vid}"
        videos.append({
            "video_id": vid,
            "title": entry.get("title"),
            "url": url,
            "view_count": entry.get("view_count") or 0,
            "duration": entry.get("duration"),
            "upload_date": entry.get("upload_date"),
        })
    log_step(f"get_channel_videos: found {len(videos)} videos from {channel_url}")
    return videos


def get_transcript(video_id: str, langs: Optional[List[str]] = None) -> Optional[Dict]:
    """Fetch the fullest possible transcript using youtube-transcript-api.

    Strategy (in order):
      1. Find a manual transcript in the preferred languages list.
      2. Find an auto-generated transcript in the preferred languages list.
         YouTube auto-captions use codes like "en" OR "a.en" depending on the
         video — both are tried automatically via find_transcript().
      3. Take the first available transcript in any language and translate it
         to English (covers videos with only non-English captions).
      4. Return None only if no transcript exists at all.

    Returns dict { video_id, transcript, segments, language, is_generated } or None.
    """
    if not video_id:
        log.warning("get_transcript called with empty video_id — skipping")
        return None

    preferred = list(langs or TRANSCRIPT_LANGS)

    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)

        transcript_obj = None

        # ── 1. Try preferred languages (manual first, then auto-generated) ───
        try:
            transcript_obj = transcript_list.find_transcript(preferred)
            log.info("Transcript found (preferred lang) for %s: %s", video_id, transcript_obj.language_code)
        except Exception:
            pass

        # ── 2. Try any available transcript and translate to English ─────────
        if transcript_obj is None:
            available = list(transcript_list)
            if available:
                try:
                    # pick the first one; translate if not already English
                    candidate = available[0]
                    if candidate.language_code not in preferred:
                        log.info(
                            "No preferred-lang transcript for %s — translating %s → en",
                            video_id, candidate.language_code,
                        )
                        transcript_obj = candidate.translate("en")
                    else:
                        transcript_obj = candidate
                except Exception as te:
                    log.warning("Translation failed for %s: %s", video_id, te)
                    transcript_obj = available[0]  # use as-is

        if transcript_obj is None:
            log.info("No transcript available for %s", video_id)
            return None

        # ── 3. Fetch all segments ─────────────────────────────────────────────
        fetched = transcript_obj.fetch()
        segments = [
            {"text": s.text, "start": s.start, "duration": s.duration}
            for s in fetched
        ]
        full_text = " ".join(s["text"] for s in segments)

        log.info(
            "Transcript fetched for %s: %d segments, %d chars, lang=%s, auto=%s",
            video_id, len(segments), len(full_text),
            transcript_obj.language_code, transcript_obj.is_generated,
        )
        return {
            "video_id": video_id,
            "transcript": full_text,
            "segments": segments,
            "language": transcript_obj.language_code,
            "is_generated": transcript_obj.is_generated,
        }

    except Exception as e:
        log.info("Transcript fetch failed for %s: %s", video_id, e)
        return None


def enrich_video(video: Dict, langs: Optional[List[str]] = None) -> Dict:
    """Full metadata + transcript enrichment for a single video dict (in-place).

    Expects `video` to contain at least `url`. video_id is resolved from URL
    if not already present, so yt-dlp failures don't block transcript fetching.
    """
    raw_url = video.get("url") or ""

    # ── Step 1: Resolve video_id robustly ─────────────────────────────────────
    # Always try URL parsing first (fast, no network, no bot-detection risk)
    video_id = video.get("video_id") or extract_video_id_from_url(raw_url)

    # Fall back to yt-dlp only if URL parsing didn't work
    if not video_id:
        try:
            info = safe_extract_info(raw_url, {"noplaylist": True})
            video_id = info.get("id") or info.get("video_id")
        except Exception as e:
            log.warning("yt-dlp video_id resolution failed: %s", e)

    if not video_id:
        log.error("Could not resolve video_id for URL: %s", raw_url)
        video["video_id"] = None
        video["transcript"] = None
        video["transcript_segments"] = None
        return video

    video["video_id"] = video_id
    canonical_url = f"https://www.youtube.com/watch?v={video_id}"
    video["url"] = canonical_url

    # ── Step 2: Enrich metadata via yt-dlp (best-effort) ─────────────────────
    try:
        info = safe_extract_info(canonical_url, {"noplaylist": True})
        if info:
            video["title"] = video.get("title") or info.get("title")
            video["description"] = info.get("description")
            video["like_count"] = info.get("like_count")
            video["comment_count"] = info.get("comment_count")
            video["view_count"] = video.get("view_count") or info.get("view_count") or 0
            video["tags"] = info.get("tags") or []
            video["categories"] = info.get("categories") or []
            video["thumbnail"] = info.get("thumbnail")
            video["uploader"] = info.get("uploader")
            video["channel_id"] = info.get("channel_id")
            video["upload_date"] = info.get("upload_date")
            video["duration"] = video.get("duration") or info.get("duration")
            views = video.get("view_count") or 1
            likes = video.get("like_count") or 0
            comments = video.get("comment_count") or 0
            video["engagement_score"] = round((likes + comments) / (views or 1), 5)
        else:
            log.warning("yt-dlp returned no metadata for %s — metadata fields will be null", video_id)
    except Exception as e:
        log.warning("enrich_video metadata error for %s: %s", video_id, e)

    # ── Step 3: Fetch transcript (video_id is guaranteed valid here) ──────────
    try:
        transcript_data = get_transcript(video_id, langs or TRANSCRIPT_LANGS)
        if transcript_data:
            video["transcript"] = transcript_data["transcript"]
            video["transcript_segments"] = transcript_data["segments"]
            video["transcript_language"] = transcript_data.get("language")
            video["transcript_is_generated"] = transcript_data.get("is_generated")
        else:
            video["transcript"] = None
            video["transcript_segments"] = None
            video["transcript_language"] = None
            video["transcript_is_generated"] = None
    except Exception as e:
        log.warning("transcript attach failed for %s: %s", video_id, e)
        video["transcript"] = None
        video["transcript_segments"] = None

    return video


def save_to_json(videos: List[Dict]) -> str:
    fn = f"videos_{format_timestamp()}.json"
    path = os.path.join(DOWNLOADS_DIR, fn)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(videos, f, ensure_ascii=False, indent=2)
    log_step(f"Saved JSON: {path}")
    return fn


def save_to_csv(videos: List[Dict]) -> str:
    fn = f"videos_{format_timestamp()}.csv"
    path = os.path.join(DOWNLOADS_DIR, fn)
    preferred = ["video_id", "title", "url", "uploader", "view_count", "like_count", "comment_count", "duration", "engagement_score", "thumbnail"]
    all_keys = list({k for v in videos for k in v.keys()})
    keys = [k for k in preferred if k in all_keys] + [k for k in all_keys if k not in preferred]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        for v in videos:
            row = {}
            for k in keys:
                val = v.get(k)
                if isinstance(val, (list, dict)):
                    row[k] = json.dumps(val, ensure_ascii=False)
                else:
                    row[k] = "" if val is None else val
            writer.writerow(row)
    log_step(f"Saved CSV: {path}")
    return fn


# -------------------------
# Flask routes
# -------------------------
@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "downloads": DOWNLOADS_DIR,
        "cookies_loaded": os.path.isfile(COOKIES_FILE),
    })


@app.route("/api/channel/analyze", methods=["POST"])
def channel_analyze():
    body = request.get_json(silent=True) or {}
    channel_url = (body.get("channel_url") or "").strip()
    max_videos = int(body.get("max_videos") or 50)
    top_n = int(body.get("top_n") or 3)

    if not channel_url:
        return jsonify({"error": "channel_url is required"}), 400

    log_step(f"Channel analyze: url={channel_url} max={max_videos} top={top_n}")
    try:
        videos = get_channel_videos(channel_url, max_videos)
    except Exception as e:
        log.error("get_channel_videos failed: %s", e)
        return jsonify({"error": str(e)}), 502

    if not videos:
        return jsonify({"error": "No videos found"}), 404

    videos = sorted(videos, key=lambda x: x.get("view_count") or 0, reverse=True)[:top_n]

    for i, v in enumerate(videos, 1):
        log_step(f"Enriching video {i}/{len(videos)}: {v.get('title')}")
        enrich_video(v)

    json_fn = save_to_json(videos)
    csv_fn = save_to_csv(videos)

    return jsonify({
        "videos": videos,
        "json_url": f"/downloads/{json_fn}",
        "csv_url": f"/downloads/{csv_fn}",
        "count": len(videos),
    })


@app.route("/api/video/process", methods=["POST"])
def video_process():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400

    # Resolve video_id immediately from URL — don't wait for yt-dlp
    video_id = extract_video_id_from_url(url)
    video = {"url": url, "video_id": video_id}

    log_step(f"Video process: enriching single video {video_id or url}")
    try:
        enriched = enrich_video(video)
    except Exception as e:
        log.error("enrich single video failed: %s", e)
        return jsonify({"error": str(e)}), 502

    return jsonify(enriched)


@app.route("/downloads/<path:filename>")
def serve_downloads(filename: str):
    if ".." in filename or filename.startswith("/"):
        abort(400)
    path = os.path.join(DOWNLOADS_DIR, filename)
    if not os.path.exists(path):
        abort(404)
    return send_from_directory(DOWNLOADS_DIR, filename, as_attachment=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8001))
    log_step(f"Starting TubeIntel (simple) on port {port}")
    app.run(host="0.0.0.0", port=port)