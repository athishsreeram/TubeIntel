"""
AI Learning Companion for YouTube
==================================
Flask backend — production-grade, Render-deployable.

Layers:
  - Data Pipeline Layer  : get_channel_videos, get_transcript, enrich_video
                           (ported directly from Colab notebook)
  - AI Intelligence Layer: /api/video/process via OpenRouter
                           (new — never used in channel pipeline)
  - API Orchestration    : Flask routes, caching, file serving
"""

import os
import re
import json
import csv
import logging
import hashlib
from datetime import datetime
from threading import Lock

import requests
from flask import Flask, request, jsonify, send_from_directory, abort
from flask_cors import CORS

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi

# ─────────────────────────────────────────────────────────────────────────────
# App Setup
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder=None)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# In-memory cache for /api/video/process results (video_id → result dict)
_video_cache: dict = {}
_cache_lock = Lock()

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "google/gemma-3-27b-it:free"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")


# ─────────────────────────────────────────────────────────────────────────────
# ████████████████████████████████████████████████████████████████████████████
#  DATA PIPELINE LAYER — ported from Colab notebook
#  Source: athishsreeram/tubenotebook (top3videos_metadata_transcript_preview...)
# ████████████████████████████████████████████████████████████████████████████
# ─────────────────────────────────────────────────────────────────────────────


def get_channel_videos(channel_url: str, max_videos: int = 50) -> list[dict]:
    """
    [COLAB PORT] Fetch a flat list of videos from a YouTube channel URL.
    Returns list of dicts: title, url, video_id, view_count, duration, upload_date.
    """
    ydl_opts = {
        "quiet": True,
        "extract_flat": True,
        "skip_download": True,
        "ignoreerrors": True,
        "playlistend": max_videos,
    }

    videos = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
            if not info:
                log.warning("yt-dlp returned no info for channel: %s", channel_url)
                return []
            for entry in info.get("entries", []):
                if not entry:
                    continue
                video_id = entry.get("id")
                if not video_id:
                    continue
                videos.append({
                    "title": entry.get("title"),
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "video_id": video_id,
                    "view_count": entry.get("view_count") or 0,
                    "duration": entry.get("duration"),
                    "upload_date": entry.get("upload_date"),
                })
    except Exception as e:
        log.error("get_channel_videos failed: %s", e)
        raise

    log.info("get_channel_videos: found %d videos from %s", len(videos), channel_url)
    return videos


def get_transcript(video_id: str, langs: list[str] | None = None) -> dict | None:
    """
    [COLAB PORT + EXTENDED] Fetch transcript for a video using youtube-transcript-api v1.x.
    
    Two-stage fallback:
      1. Try requested langs directly.
      2. Find any available transcript and translate to English.
    
    Returns dict: { video_id, transcript (str), segments (list) }
    Returns None if no transcript is available at all.
    """
    if langs is None:
        langs = ["en"]

    api = YouTubeTranscriptApi()

    # Stage 1: Primary language request
    try:
        fetched = api.fetch(video_id, languages=langs)
        full_text = " ".join(seg.text for seg in fetched)
        segments = [
            {"text": seg.text, "start": seg.start, "duration": seg.duration}
            for seg in fetched
        ]
        log.info("Transcript OK (primary): %s", video_id)
        return {"video_id": video_id, "transcript": full_text, "segments": segments}
    except Exception as e:
        log.info("Primary transcript failed for %s: %s — trying fallback", video_id, e)

    # Stage 2: Any available language, translate to English
    try:
        transcript_list = api.list(video_id)
        for t in transcript_list:
            try:
                if t.language_code == "en":
                    fetched = t.fetch()
                else:
                    fetched = t.translate("en").fetch()
                full_text = " ".join(seg.text for seg in fetched)
                segments = [
                    {"text": seg.text, "start": seg.start, "duration": seg.duration}
                    for seg in fetched
                ]
                log.info("Transcript OK (fallback lang=%s): %s", t.language_code, video_id)
                return {"video_id": video_id, "transcript": full_text, "segments": segments}
            except Exception as inner:
                log.debug("Fallback transcript attempt failed for %s: %s", video_id, inner)
                continue
    except Exception as e:
        log.warning("No transcripts available for %s: %s", video_id, e)

    return None


def enrich_video(video: dict, langs: list[str] | None = None) -> dict:
    """
    [COLAB PORT] Enrich a video dict with full metadata and transcript in-place.
    
    - langs: transcript language preference (default ["en"])
    - Does NOT use any global variable — all config passed explicitly.
    - Metadata fetch and transcript fetch are in separate try blocks so a
      transcript failure never loses already-fetched metadata.
    """
    if langs is None:
        langs = ["en"]

    # — Metadata enrichment —
    try:
        ydl_opts = {"quiet": True, "skip_download": True, "ignoreerrors": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video["url"], download=False)
            if info:
                video["description"] = info.get("description")
                video["like_count"] = info.get("like_count")
                video["comment_count"] = info.get("comment_count")
                video["tags"] = info.get("tags") or []
                video["categories"] = info.get("categories") or []
                video["thumbnail"] = info.get("thumbnail")
                video["uploader"] = info.get("uploader")
                video["channel_id"] = info.get("channel_id")
                # Engagement score: (likes + comments) / views
                views = video.get("view_count") or 1
                likes = video.get("like_count") or 0
                comments = video.get("comment_count") or 0
                video["engagement_score"] = round((likes + comments) / views, 5)
    except Exception as e:
        log.error("enrich_video metadata failed for %s: %s", video.get("video_id"), e)
        video.setdefault("engagement_score", 0)

    # — Transcript enrichment —
    transcript_data = get_transcript(video["video_id"], langs)
    if transcript_data:
        video["transcript"] = transcript_data["transcript"]
        video["transcript_segments"] = transcript_data["segments"]
    else:
        video["transcript"] = None
        video["transcript_segments"] = None

    return video


def save_to_json(videos: list[dict], filepath: str) -> str:
    """
    [COLAB PORT] Save enriched video list to a JSON file.
    filepath must be an absolute path. Returns filepath.
    """
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(videos, f, indent=2, ensure_ascii=False, default=str)
    log.info("Saved JSON: %s", filepath)
    return filepath


def save_to_csv(videos: list[dict], filepath: str) -> str:
    """
    [COLAB PORT] Save enriched video list to a CSV file.
    Complex nested fields (tags, segments, etc.) are JSON-stringified.
    filepath must be an absolute path. Returns filepath.
    """
    # Collect all keys across all video dicts
    keys = list({k for v in videos for k in v.keys()})

    def flatten(val):
        if isinstance(val, (list, dict)):
            return json.dumps(val, ensure_ascii=False)
        return val

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        for v in videos:
            writer.writerow({k: flatten(v.get(k)) for k in keys})

    log.info("Saved CSV: %s", filepath)
    return filepath


# ─────────────────────────────────────────────────────────────────────────────
# ████████████████████████████████████████████████████████████████████████████
#  AI INTELLIGENCE LAYER — new, OpenRouter only
#  ONLY used by /api/video/process. NEVER touches channel pipeline.
# ████████████████████████████████████████████████████████████████████████████
# ─────────────────────────────────────────────────────────────────────────────


def _extract_video_id(url: str) -> str | None:
    """
    [AI LAYER HELPER] Extract YouTube video ID from various URL formats:
      - https://www.youtube.com/watch?v=VIDEO_ID
      - https://youtu.be/VIDEO_ID
      - https://www.youtube.com/shorts/VIDEO_ID
    """
    patterns = [
        r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def _truncate_transcript(transcript: str, max_words: int = 6000) -> str:
    """[AI LAYER HELPER] Truncate transcript to max_words words."""
    words = transcript.split()
    if len(words) <= max_words:
        return transcript
    truncated = " ".join(words[:max_words])
    log.info("Transcript truncated from %d to %d words", len(words), max_words)
    return truncated + "\n\n[Transcript truncated for processing]"


def _strip_json_fences(text: str) -> str:
    """[AI LAYER HELPER] Strip markdown code fences from AI response."""
    # Remove ```json ... ``` or ``` ... ``` wrappers
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text.strip())
    return text.strip()


def call_openrouter_ai(transcript: str) -> dict:
    """
    [AI LAYER] Send transcript to OpenRouter and return parsed JSON with
    keys: summary (str), insights (list[str]), sections (list[{title, start_seconds}]).
    
    Raises ValueError on bad JSON from AI.
    Raises RuntimeError on API-level failures.
    """
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY environment variable is not set")

    truncated = _truncate_transcript(transcript, max_words=6000)

    system_prompt = (
        "You are an expert at extracting structured knowledge from video transcripts. "
        "Always respond with valid JSON only. No markdown fences, no explanation, no preamble. "
        "Your entire response must be parseable by json.loads()."
    )

    user_prompt = f"""Analyze this YouTube transcript and return a JSON object with exactly these fields:

{{
  "summary": "2-3 paragraph summary of the video content",
  "insights": ["insight 1", "insight 2", "insight 3", "insight 4", "insight 5"],
  "sections": [
    {{"title": "Section title", "start_seconds": 0}},
    {{"title": "Next section", "start_seconds": 120}}
  ]
}}

Rules:
- summary: 2-3 paragraphs of flowing prose
- insights: 5-8 key takeaways, each a complete sentence
- sections: major topic shifts, start_seconds as integer (best estimate from transcript flow)
- Return ONLY the JSON object, nothing else

TRANSCRIPT:
{truncated}"""

    payload = {
        "model": OPENROUTER_MODEL,
        "max_tokens": 2048,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://youtube-ai-companion.onrender.com",
        "X-Title": "AI Learning Companion for YouTube",
    }

    try:
        resp = requests.post(OPENROUTER_API_URL, json=payload, headers=headers, timeout=90)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        raise RuntimeError("OpenRouter API timed out after 90 seconds")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"OpenRouter API HTTP error: {e.response.status_code} — {e.response.text[:300]}")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"OpenRouter API request failed: {e}")

    data = resp.json()
    raw_text = ""
    try:
        raw_text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected OpenRouter response shape: {data}")

    cleaned = _strip_json_fences(raw_text)

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"AI returned invalid JSON: {e}. Raw: {raw_text[:500]}")

    # Validate expected fields are present
    for field in ("summary", "insights", "sections"):
        if field not in result:
            raise ValueError(f"AI response missing field '{field}'. Got keys: {list(result.keys())}")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# ████████████████████████████████████████████████████████████████████████████
#  API ROUTES — orchestration only, no business logic here
# ████████████████████████████████████████████████████████████████████████████
# ─────────────────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    """Serve the frontend."""
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/channel/analyze", methods=["POST"])
def channel_analyze():
    """
    [DATA PIPELINE] Fetch, sort, enrich top N videos from a YouTube channel.
    Exact Colab pipeline — no AI involved.

    Body: { "channel_url": str, "max_videos": int, "top_n": int }
    Returns: { "videos": [...], "json_url": str, "csv_url": str }
    """
    body = request.get_json(silent=True) or {}
    channel_url = (body.get("channel_url") or "").strip()
    max_videos = int(body.get("max_videos") or 50)
    top_n = int(body.get("top_n") or 3)

    if not channel_url:
        return jsonify({"error": "channel_url is required"}), 400
    if top_n < 1 or top_n > 20:
        return jsonify({"error": "top_n must be between 1 and 20"}), 400
    if max_videos < 1 or max_videos > 200:
        return jsonify({"error": "max_videos must be between 1 and 200"}), 400

    log.info("Channel analyze: url=%s max=%d top=%d", channel_url, max_videos, top_n)

    try:
        videos = get_channel_videos(channel_url, max_videos)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch channel videos: {str(e)}"}), 502

    if not videos:
        return jsonify({"error": "No videos found for this channel URL"}), 404

    # Sort by view count, take top N
    videos = sorted(videos, key=lambda x: x.get("view_count") or 0, reverse=True)[:top_n]

    # Enrich each video (metadata + transcript)
    for i, v in enumerate(videos, 1):
        log.info("Enriching video %d/%d: %s", i, len(videos), v.get("title", "")[:50])
        enrich_video(v, langs=["en"])

    # Generate shared timestamp for matched file pair
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_filename = f"videos_{ts}.json"
    csv_filename = f"videos_{ts}.csv"
    json_path = os.path.join(DOWNLOADS_DIR, json_filename)
    csv_path = os.path.join(DOWNLOADS_DIR, csv_filename)

    try:
        save_to_json(videos, json_path)
        save_to_csv(videos, csv_path)
    except Exception as e:
        log.error("File save failed: %s", e)
        return jsonify({"error": f"Failed to save output files: {str(e)}"}), 500

    return jsonify({
        "videos": videos,
        "json_url": f"/downloads/{json_filename}",
        "csv_url": f"/downloads/{csv_filename}",
        "count": len(videos),
    })


@app.route("/api/video/process", methods=["POST"])
def video_process():
    """
    [AI LAYER] Extract AI-powered summary, insights, and sections from a single video.
    Uses OpenRouter — never used in channel pipeline.

    Body: { "url": str }
    Returns: { "video_id": str, "summary": str, "insights": [...], "sections": [...] }
    """
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()

    if not url:
        return jsonify({"error": "url is required"}), 400

    video_id = _extract_video_id(url)
    if not video_id:
        return jsonify({"error": "Could not extract video ID from URL. Supported: youtube.com/watch?v=, youtu.be/, /shorts/"}), 400

    # Check cache
    with _cache_lock:
        if video_id in _video_cache:
            log.info("Cache hit for video_id: %s", video_id)
            return jsonify(_video_cache[video_id])

    # Fetch transcript
    log.info("Fetching transcript for video_id: %s", video_id)
    transcript_data = get_transcript(video_id, langs=["en"])
    if not transcript_data:
        return jsonify({"error": "No transcript available for this video. The video may be private, have no captions, or captions may be disabled."}), 404

    # Call AI layer
    log.info("Calling OpenRouter AI for video_id: %s", video_id)
    try:
        ai_result = call_openrouter_ai(transcript_data["transcript"])
    except ValueError as e:
        return jsonify({"error": "AI returned invalid JSON", "detail": str(e)}), 502
    except RuntimeError as e:
        return jsonify({"error": "AI processing failed", "detail": str(e)}), 502

    result = {
        "video_id": video_id,
        "url": url,
        "summary": ai_result.get("summary", ""),
        "insights": ai_result.get("insights", []),
        "sections": ai_result.get("sections", []),
        "transcript_word_count": len(transcript_data["transcript"].split()),
    }

    # Store in cache
    with _cache_lock:
        _video_cache[video_id] = result

    return jsonify(result)


@app.route("/downloads/<path:filename>")
def serve_download(filename):
    """
    Serve generated JSON/CSV files from the downloads directory.
    Guards against path traversal.
    """
    # Security: reject any path traversal attempts
    if ".." in filename or "/" in filename or "\\" in filename:
        abort(400, "Invalid filename")

    # Only allow .json and .csv
    if not (filename.endswith(".json") or filename.endswith(".csv")):
        abort(400, "Only .json and .csv files are served")

    filepath = os.path.join(DOWNLOADS_DIR, filename)
    if not os.path.isfile(filepath):
        abort(404, "File not found — it may have been cleared by a server restart")

    return send_from_directory(DOWNLOADS_DIR, filename, as_attachment=True)


@app.route("/api/health")
def health():
    """Health check endpoint."""
    return jsonify({
        "status": "ok",
        "openrouter_configured": bool(OPENROUTER_API_KEY),
        "downloads_dir": DOWNLOADS_DIR,
        "cached_videos": len(_video_cache),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Error handlers
# ─────────────────────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": str(e)}), 404

@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": str(e)}), 400

@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": "Internal server error"}), 500


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
