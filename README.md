# TubeIntel — AI Learning Companion for YouTube

A production-ready Flask app that turns YouTube channels and videos into structured knowledge.

## What It Does

| Feature | Input | Output |
|---------|-------|--------|
| **Channel Analysis** | Channel URL | Top N videos enriched with metadata, transcripts, engagement scores → JSON + CSV download |
| **Video AI** | Single video URL | AI-generated summary, key insights, timestamped sections |

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  index.html (browser)               │
│   Tab 1: Video AI      │   Tab 2: Channel Analysis  │
└───────────┬─────────────────────────┬───────────────┘
            │ POST /api/video/process  │ POST /api/channel/analyze
            ▼                          ▼
┌─────────────────────────────────────────────────────┐
│                   app.py (Flask)                    │
│                                                     │
│  DATA PIPELINE LAYER          AI LAYER              │
│  ─────────────────────        ─────────────────     │
│  get_channel_videos()         call_openrouter_ai()  │
│  get_transcript()             (only in /video/proc) │
│  enrich_video()                                     │
│  save_to_json()                                     │
│  save_to_csv()                                      │
└──────────┬──────────────────────────┬───────────────┘
           │                          │
           ▼                          ▼
      yt-dlp /                 OpenRouter API
   youtube-transcript-api    (google/gemma-3-27b-it)
```

---

## Deploy to Render

### 1. Push to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/youtube-ai-companion.git
git push -u origin main
```

### 2. Create Render Web Service

1. Go to [render.com](https://render.com) → **New** → **Web Service**
2. Connect your GitHub repository
3. Set the following:

| Setting | Value |
|---------|-------|
| **Environment** | Python 3 |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `gunicorn app:app` |
| **Instance Type** | Free (or Starter for longer timeouts) |

### 3. Set Environment Variables

In the Render dashboard → your service → **Environment**:

| Key | Value |
|-----|-------|
| `OPENROUTER_API_KEY` | Your key from [openrouter.ai](https://openrouter.ai) |

> Get your free API key at https://openrouter.ai → Keys

---

## Local Development

```bash
# Clone
git clone https://github.com/YOUR_USERNAME/youtube-ai-companion.git
cd youtube-ai-companion

# Install
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Run
export OPENROUTER_API_KEY=sk-or-your-key-here
python app.py

# Open
open http://localhost:5000
```

---

## API Reference

### `POST /api/channel/analyze`

Runs the full Colab pipeline: fetch → sort by views → enrich top N → export.

```bash
curl -X POST http://localhost:5000/api/channel/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "channel_url": "https://www.youtube.com/@rajshamani/videos",
    "max_videos": 50,
    "top_n": 3
  }'
```

Response:
```json
{
  "count": 3,
  "videos": [
    {
      "title": "...",
      "url": "...",
      "video_id": "...",
      "view_count": 7980694,
      "like_count": 373910,
      "comment_count": 26000,
      "engagement_score": 0.05015,
      "transcript": "France invented a lot...",
      "transcript_segments": [...],
      "thumbnail": "https://...",
      "tags": [...],
      "categories": [...]
    }
  ],
  "json_url": "/downloads/videos_20260410_120000.json",
  "csv_url": "/downloads/videos_20260410_120000.csv"
}
```

### `POST /api/video/process`

AI-powered analysis of a single video.

```bash
curl -X POST http://localhost:5000/api/video/process \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=9QXCkMTbrSk"}'
```

Response:
```json
{
  "video_id": "9QXCkMTbrSk",
  "summary": "...",
  "insights": ["Insight 1", "Insight 2", "..."],
  "sections": [
    {"title": "Introduction", "start_seconds": 0},
    {"title": "Main Topic", "start_seconds": 145}
  ],
  "transcript_word_count": 5823
}
```

### `GET /downloads/<filename>`

Download generated files. Files must end in `.json` or `.csv`.

### `GET /api/health`

```json
{
  "status": "ok",
  "openrouter_configured": true,
  "downloads_dir": "/opt/render/project/src/downloads",
  "cached_videos": 2
}
```

---

## Known Limitations & Gotchas

### ⚠ Ephemeral Disk (Important)
Files in `downloads/` are stored on Render's ephemeral disk. They **will be deleted** on every redeploy or dyno restart. Download your files immediately after the analysis completes. For persistence, integrate S3 or Cloudflare R2.

### ⏱ Channel Analysis Timeouts
Each video enrichment makes 2 network requests (yt-dlp metadata + transcript API). For `top_n = 3`, expect **2–4 minutes**. For `top_n > 5`, you may hit Render's free tier 30-second HTTP timeout. Solutions:
- Keep `top_n ≤ 5` on the free tier
- Upgrade to Render Starter plan (no timeout limit)
- Implement background jobs with polling (future enhancement)

### 🔇 yt-dlp JS Runtime Warning
On Render you will see in logs:
```
WARNING: No supported JavaScript runtime could be found.
```
This is **non-fatal** and expected. It affects some format lookups but not metadata extraction or transcript fetching. Safe to ignore.

### 🌐 Transcript Availability
Not all YouTube videos have transcripts. The app:
1. Tries English first
2. Falls back to any available language, translated to English
3. Returns HTTP 404 if no transcript exists at all

Videos with only auto-generated non-English transcripts (common for non-English channels) will still work via the translation fallback.

### 💾 In-Memory Cache
Video AI results are cached in memory (per-process). Cache is cleared on server restart. This means the same video won't be re-processed within a session, but will be on next deploy.

---

## Project Structure

```
youtube-ai-companion/
├── app.py              # Flask backend (data pipeline + AI layer + routes)
├── index.html          # Frontend (HTML/CSS/JS, single file)
├── requirements.txt    # Python dependencies (pinned versions)
├── runtime.txt         # Python version for Render
├── .gitignore
├── README.md
└── downloads/          # Generated at runtime (gitignored)
```

---

## Stack

- **Backend**: Flask 2.3, Python 3.11
- **YouTube data**: yt-dlp 2024.12, youtube-transcript-api 1.2.1
- **AI**: OpenRouter → `google/gemma-3-27b-it:free`
- **Deploy**: Render (gunicorn)
- **Frontend**: Vanilla HTML/CSS/JS (zero dependencies)
