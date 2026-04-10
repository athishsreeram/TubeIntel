"""TubeIntel — simplified Flask API

Exposes two operations:
- /api/channel/analyze  (POST): runs Channel Intelligence pipeline (fetch list, deep-fetch top N, save CSV/JSON)
- /api/video/process    (POST): runs Video Intelligence (enrich single video: metadata + transcript)

This file is a server-adapted version of the provided Colab script.
"""

import os
import json
import csv
import logging
import hashlib
from datetime import datetime
from threading import Lock
from itertools import count
from typing import List, Dict, Optional

import yt_dlp
from flask import Flask, request, jsonify, send_from_directory, abort
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# Quiet noisy third-party loggers
# hide yt-dlp warnings (ffmpeg/js runtime messages) — user can enable by changing level
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


# -------------------------
# Utility helpers (from Colab)
# -------------------------
def format_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def safe_extract_info(url: str, ydl_opts: Optional[dict] = None) -> dict:
    opts = dict(ydl_opts or {})
    opts.update({"quiet": True, "skip_download": True, "ignoreerrors": True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False) or {}


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
    """Fetch transcript using youtube-transcript-api (Colab style).

    Returns dict { video_id, transcript, segments } or None on failure.
    """
    if langs is None:
        langs = TRANSCRIPT_LANGS
    try:
        api = YouTubeTranscriptApi()
        transcript = api.fetch(video_id, languages=langs)
        #  log.info("Transcript fetched for %s: %d segments  : %s   ", video_id, len(transcript), transcript)
        # transcript items commonly expose .text, .start, .duration
        full_text = " ".join([t.text for t in transcript])
        # log.info("Transcript full text for %s: %s ", video_id, len(full_text))
        segments = [{"text": t.text, "start": t.start, "duration": t.duration} for t in transcript]
        return {"video_id": video_id, "transcript": full_text, "segments": segments}
    except Exception as e:
        log.info("Transcript fetch failed for %s: %s", video_id, e)
        return None


def enrich_video(video: Dict, langs: Optional[List[str]] = None) -> Dict:
    """Full metadata + transcript enrichment for a single video dict (in-place).

    Expects `video` to contain at least `url` and `video_id`.
    """
    try:
        info = safe_extract_info(video.get("url") or f"https://www.youtube.com/watch?v={video.get('video_id')}")
        if info:
            video["title"] = video.get("title") or info.get("title")
            video["description"] = info.get("description")
            video["like_count"] = info.get("like_count")
            video["comment_count"] = info.get("comment_count")
            video["tags"] = info.get("tags") or []
            video["categories"] = info.get("categories") or []
            video["thumbnail"] = info.get("thumbnail")
            video["uploader"] = info.get("uploader")
            video["channel_id"] = info.get("channel_id")
            views = video.get("view_count") or info.get("view_count") or 1
            likes = video.get("like_count") or 0
            comments = video.get("comment_count") or 0
            video["engagement_score"] = round((likes + comments) / (views or 1), 5)
    except Exception as e:
        log.warning("enrich_video metadata error for %s: %s", video.get("video_id"), e)

    # transcript
    try:
        tid = video.get("video_id")
        transcript_data = get_transcript(tid, langs or TRANSCRIPT_LANGS)
        if transcript_data:
            video["transcript"] = transcript_data["transcript"]
            video["transcript_segments"] = transcript_data["segments"]
        else:
            video["transcript"] = None
            video["transcript_segments"] = None
    except Exception as e:
        log.debug("transcript attach failed for %s: %s", video.get("video_id"), e)
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
    # collect keys and prefer stable order
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
    return jsonify({"status": "ok", "downloads": DOWNLOADS_DIR})


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

    # try to extract video id via yt-dlp quick info
    try:
        info = safe_extract_info(url, {"noplaylist": True})
        vid = info.get("id") or info.get("video_id")
    except Exception:
        vid = None

    video = {"url": url, "video_id": vid}
    log_step(f"Video process: enriching single video {vid or url}")
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
