"""TubeIntel — Flask API (Render-compatible)

Two problems on Render that this version solves:

PROBLEM 1 — yt-dlp "Sign in to confirm you're not a bot" / no JS runtime
  Root cause: Render has no deno/node, so yt-dlp's web client fails.
  Fix A: Switch to android innertube client (no JS runtime needed at all).
  Fix B: oEmbed fallback for title + thumbnail when yt-dlp still fails.

PROBLEM 2 — youtube-transcript-api "Cloud provider IP blocked"
  Root cause: YouTube blocks transcript requests from cloud IPs.
  Fix: Route transcript requests through an HTTP proxy.

ENV VARS (set in Render dashboard → Environment):
  PROXY_URL   HTTP proxy URL. Format: http://user:pass@host:port
              Get 10 free proxies at https://www.webshare.io (no credit card)
              → After signup: Dashboard → Proxy → Static → Download list
              Use format: http://USERNAME:PASSWORD@IP:PORT
"""

import os
import re
import json
import csv
import logging
import requests as _requests
from datetime import datetime
from itertools import count
from typing import List, Dict, Optional
from urllib.parse import urlparse, parse_qs

import yt_dlp
from flask import Flask, request, jsonify, send_from_directory, abort
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

logging.getLogger("yt_dlp").setLevel(logging.ERROR)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

_step_counter = count(1)
def log_step(msg: str) -> None:
    n = next(_step_counter)
    log.info("STEP %02d: %s", n, msg)

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

app = Flask(__name__, static_folder=None)
CORS(app)

TRANSCRIPT_LANGS = ["en"]

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ─── Proxy setup ──────────────────────────────────────────────────────────────
# Set PROXY_URL in Render env vars: http://user:pass@host:port
PROXY_URL = os.environ.get("PROXY_URL", "").strip()

def _make_proxy_config() -> Optional[GenericProxyConfig]:
    if not PROXY_URL:
        return None
    try:
        return GenericProxyConfig(http_url=PROXY_URL, https_url=PROXY_URL)
    except Exception as e:
        log.warning("Invalid PROXY_URL, transcripts will fail on cloud: %s", e)
        return None

_PROXY_CONFIG = _make_proxy_config()

if _PROXY_CONFIG:
    log.info("Proxy configured — transcript requests will use proxy")
else:
    log.warning(
        "PROXY_URL not set — transcripts will fail on Render. "
        "Add PROXY_URL=http://user:pass@host:port in Render env vars."
    )


# ─── yt-dlp options ───────────────────────────────────────────────────────────
def base_ydl_opts() -> dict:
    """
    Uses the Android innertube client.
    Key benefit: does NOT require deno/node/JS runtime.
    Works on Render without any system dependencies.
    """
    return {
        "quiet":         True,
        "skip_download": True,
        "ignoreerrors":  True,
        "user_agent":    CHROME_UA,
        "extractor_args": {
            "youtube": {
                "player_client": ["android"],   # ← no JS runtime needed
            }
        },
        "retries":        3,
        "socket_timeout": 20,
    }


def safe_extract_info(url: str, extra_opts: Optional[dict] = None) -> dict:
    opts = base_ydl_opts()
    if extra_opts:
        opts.update(extra_opts)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except Exception as e:
        log.warning("yt_dlp extract_info failed for %s: %s", url, e)
        return {}


# ─── oEmbed fallback ──────────────────────────────────────────────────────────
def fetch_oembed(video_id: str) -> dict:
    """
    YouTube oEmbed — works from any IP including cloud hosts.
    Returns: title, author_name, thumbnail_url.
    No auth, no JS, no bot detection.
    """
    try:
        r = _requests.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
            headers={"User-Agent": CHROME_UA},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            log.info("oEmbed OK for %s: %s", video_id, data.get("title"))
            return data
        log.warning("oEmbed %s for %s", r.status_code, video_id)
    except Exception as e:
        log.warning("oEmbed failed for %s: %s", video_id, e)
    return {}


# ─── Helpers ──────────────────────────────────────────────────────────────────
def format_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def extract_video_id_from_url(url: str) -> Optional[str]:
    """Parse video ID from any YouTube URL format without network calls."""
    if not url:
        return None
    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "v" in qs:
        vid = qs["v"][0]
        if re.match(r"^[A-Za-z0-9_-]{11}$", vid):
            return vid
    m = re.search(r"/(embed|shorts|v)/([A-Za-z0-9_-]{11})", parsed.path)
    if m:
        return m.group(2)
    return None


# ─── Core pipeline ────────────────────────────────────────────────────────────
def get_channel_videos(channel_url: str, max_videos: Optional[int] = None) -> List[Dict]:
    info    = safe_extract_info(channel_url, {"extract_flat": True, "playlistend": max_videos})
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
            "video_id":    vid,
            "title":       entry.get("title"),
            "url":         url,
            "view_count":  entry.get("view_count") or 0,
            "duration":    entry.get("duration"),
            "upload_date": entry.get("upload_date"),
        })
    log_step(f"get_channel_videos: found {len(videos)} from {channel_url}")
    return videos


def get_transcript(video_id: str, langs: Optional[List[str]] = None) -> Optional[Dict]:
    """
    Fetch transcript via youtube-transcript-api.
    Routes through proxy if PROXY_URL is set (required on Render).
    """
    if not video_id:
        return None

    preferred = list(langs or TRANSCRIPT_LANGS)

    try:
        # proxy_config is the key arg that routes requests through PROXY_URL
        api = (
            YouTubeTranscriptApi(proxy_config=_PROXY_CONFIG)
            if _PROXY_CONFIG
            else YouTubeTranscriptApi()
        )
        transcript_list = api.list(video_id)
        transcript_obj  = None

        # 1. Try preferred languages (handles "en" and "a.en" both)
        try:
            transcript_obj = transcript_list.find_transcript(preferred)
            log.info("Transcript (preferred) for %s: %s", video_id, transcript_obj.language_code)
        except Exception:
            pass

        # 2. Fall back to any language, translate to English
        if transcript_obj is None:
            available = list(transcript_list)
            if available:
                candidate = available[0]
                try:
                    if candidate.language_code not in preferred:
                        log.info("Translating %s→en for %s", candidate.language_code, video_id)
                        transcript_obj = candidate.translate("en")
                    else:
                        transcript_obj = candidate
                except Exception as te:
                    log.warning("Translation failed for %s: %s", video_id, te)
                    transcript_obj = candidate

        if transcript_obj is None:
            log.info("No transcript available for %s", video_id)
            return None

        fetched   = transcript_obj.fetch()
        segments  = [{"text": s.text, "start": s.start, "duration": s.duration} for s in fetched]
        full_text = " ".join(s["text"] for s in segments)

        log.info(
            "Transcript OK for %s: %d segs, %d chars, lang=%s, auto=%s",
            video_id, len(segments), len(full_text),
            transcript_obj.language_code, transcript_obj.is_generated,
        )
        return {
            "video_id":     video_id,
            "transcript":   full_text,
            "segments":     segments,
            "language":     transcript_obj.language_code,
            "is_generated": transcript_obj.is_generated,
        }

    except Exception as e:
        log.info("Transcript fetch failed for %s: %s", video_id, e)
        return None


def enrich_video(video: Dict, langs: Optional[List[str]] = None) -> Dict:
    """
    Enrich a video dict with metadata + transcript.

    Metadata layers (most-reliable-first):
      1. URL parsing for video_id (no network, always works)
      2. yt-dlp android client (no JS needed, works on Render for public videos)
      3. oEmbed (title + thumbnail guaranteed from any IP as last resort)
    """
    raw_url = video.get("url") or ""

    # Step 1: video_id
    video_id = video.get("video_id") or extract_video_id_from_url(raw_url)
    if not video_id:
        try:
            info = safe_extract_info(raw_url, {"noplaylist": True})
            video_id = info.get("id") or info.get("video_id")
        except Exception as e:
            log.warning("yt-dlp video_id resolution failed: %s", e)

    if not video_id:
        log.error("Could not resolve video_id for: %s", raw_url)
        video.update({
            "video_id": None, "transcript": None, "transcript_segments": None,
            "transcript_language": None, "transcript_is_generated": None,
        })
        return video

    video["video_id"] = video_id
    canonical_url     = f"https://www.youtube.com/watch?v={video_id}"
    video["url"]      = canonical_url

    # Step 2: yt-dlp metadata (android client, no JS)
    ydlp_info = safe_extract_info(canonical_url, {"noplaylist": True})
    if ydlp_info:
        log.info("yt-dlp metadata OK for %s", video_id)
        video["title"]         = video.get("title")      or ydlp_info.get("title")
        video["description"]   = ydlp_info.get("description")
        video["like_count"]    = ydlp_info.get("like_count")
        video["comment_count"] = ydlp_info.get("comment_count")
        video["view_count"]    = video.get("view_count") or ydlp_info.get("view_count") or 0
        video["tags"]          = ydlp_info.get("tags")       or []
        video["categories"]    = ydlp_info.get("categories") or []
        video["thumbnail"]     = ydlp_info.get("thumbnail")
        video["uploader"]      = ydlp_info.get("uploader")
        video["channel_id"]    = ydlp_info.get("channel_id")
        video["upload_date"]   = ydlp_info.get("upload_date")
        video["duration"]      = video.get("duration") or ydlp_info.get("duration")
    else:
        log.warning("yt-dlp returned nothing for %s — falling back to oEmbed", video_id)

    # Step 3: oEmbed fills gaps (always works on Render)
    if not video.get("title") or not video.get("thumbnail"):
        oembed = fetch_oembed(video_id)
        if oembed:
            video["title"]     = video.get("title")     or oembed.get("title")
            video["thumbnail"] = video.get("thumbnail") or oembed.get("thumbnail_url")
            video["uploader"]  = video.get("uploader")  or oembed.get("author_name")

    # Step 4: Engagement score
    views    = video.get("view_count")    or 1
    likes    = video.get("like_count")    or 0
    comments = video.get("comment_count") or 0
    video["engagement_score"] = round((likes + comments) / (views or 1), 5)

    # Step 5: Transcript (proxied on Render)
    try:
        td = get_transcript(video_id, langs or TRANSCRIPT_LANGS)
        if td:
            video["transcript"]              = td["transcript"]
            video["transcript_segments"]     = td["segments"]
            video["transcript_language"]     = td.get("language")
            video["transcript_is_generated"] = td.get("is_generated")
        else:
            video["transcript"]              = None
            video["transcript_segments"]     = None
            video["transcript_language"]     = None
            video["transcript_is_generated"] = None
    except Exception as e:
        log.warning("transcript attach failed for %s: %s", video_id, e)
        video["transcript"]              = None
        video["transcript_segments"]     = None
        video["transcript_language"]     = None
        video["transcript_is_generated"] = None

    return video


def save_to_json(videos: List[Dict]) -> str:
    fn   = f"videos_{format_timestamp()}.json"
    path = os.path.join(DOWNLOADS_DIR, fn)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(videos, f, ensure_ascii=False, indent=2)
    log_step(f"Saved JSON: {path}")
    return fn


def save_to_csv(videos: List[Dict]) -> str:
    fn   = f"videos_{format_timestamp()}.csv"
    path = os.path.join(DOWNLOADS_DIR, fn)
    preferred = [
        "video_id", "title", "url", "uploader", "view_count",
        "like_count", "comment_count", "duration", "engagement_score", "thumbnail",
    ]
    all_keys = list({k for v in videos for k in v.keys()})
    keys     = [k for k in preferred if k in all_keys] + [k for k in all_keys if k not in preferred]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        for v in videos:
            row = {
                k: (json.dumps(v.get(k), ensure_ascii=False) if isinstance(v.get(k), (list, dict))
                    else ("" if v.get(k) is None else v.get(k)))
                for k in keys
            }
            writer.writerow(row)
    log_step(f"Saved CSV: {path}")
    return fn


# ─── Flask routes ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/health")
def health():
    return jsonify({
        "status":           "ok",
        "downloads":        DOWNLOADS_DIR,
        "proxy_configured": bool(_PROXY_CONFIG),
        "cookies_loaded":   bool(_PROXY_CONFIG),   # backwards compat
    })


@app.route("/api/channel/analyze", methods=["POST"])
def channel_analyze():
    body        = request.get_json(silent=True) or {}
    channel_url = (body.get("channel_url") or "").strip()
    max_videos  = int(body.get("max_videos") or 50)
    top_n       = int(body.get("top_n") or 3)

    if not channel_url:
        return jsonify({"error": "channel_url is required"}), 400

    log_step(f"Channel analyze: {channel_url} max={max_videos} top={top_n}")
    try:
        videos = get_channel_videos(channel_url, max_videos)
    except Exception as e:
        log.error("get_channel_videos failed: %s", e)
        return jsonify({"error": str(e)}), 502

    if not videos:
        return jsonify({"error": "No videos found"}), 404

    videos = sorted(videos, key=lambda x: x.get("view_count") or 0, reverse=True)[:top_n]
    for i, v in enumerate(videos, 1):
        log_step(f"Enriching {i}/{len(videos)}: {v.get('title')}")
        enrich_video(v)

    json_fn = save_to_json(videos)
    csv_fn  = save_to_csv(videos)
    return jsonify({"videos": videos, "json_url": f"/downloads/{json_fn}",
                    "csv_url": f"/downloads/{csv_fn}", "count": len(videos)})


@app.route("/api/video/process", methods=["POST"])
def video_process():
    body = request.get_json(silent=True) or {}
    url  = (body.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400

    video_id = extract_video_id_from_url(url)
    video    = {"url": url, "video_id": video_id}

    log_step(f"Video process: {video_id or url}")
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
    if not os.path.exists(os.path.join(DOWNLOADS_DIR, filename)):
        abort(404)
    return send_from_directory(DOWNLOADS_DIR, filename, as_attachment=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8001))
    log_step(f"Starting TubeIntel on port {port}")
    app.run(host="0.0.0.0", port=port)