# TubeIntel

A lightweight Flask API for YouTube channel and video intelligence — fetches metadata, transcripts, and engagement scores without the YouTube Data API.

---

## What It Does

| Endpoint | Method | Description |
|---|---|---|
| `GET /api/health` | GET | Health check |
| `/api/video/process` | POST | Enrich a single video (metadata + transcript) |
| `/api/channel/analyze` | POST | Analyze top N videos from a channel |
| `/downloads/<file>` | GET | Download saved JSON/CSV exports |

---

## Why It Was Broken (and What Was Fixed)

The original code relied entirely on `yt-dlp` to resolve the `video_id` from a URL. On Render (and many cloud hosts), YouTube aggressively bot-detects `yt-dlp` requests, causing `extract_info` to return `{}` silently — leaving `video_id` as `null` and the transcript fetch never running.

**Fix:** The `video_id` is now extracted directly from the URL using regex + `urllib.parse` (no network call required). This handles all standard YouTube URL formats:

- `https://www.youtube.com/watch?v=VIDEO_ID`
- `https://youtu.be/VIDEO_ID`
- `https://www.youtube.com/shorts/VIDEO_ID`
- `https://www.youtube.com/embed/VIDEO_ID`

`yt-dlp` is still used for rich metadata (title, likes, description), but it's now **best-effort** — if it fails, the transcript is still fetched successfully using the URL-parsed ID.

---

## Local Development

### Prerequisites

- Python 3.10+
- pip

### Setup

```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/tubeintel.git
cd tubeintel

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run
python app.py
```

Server starts on `http://localhost:8001`

### Test locally

```bash
curl -X POST http://localhost:8001/api/video/process \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=xAt1xcC6qfM"}'
```

---

## Deploy to Render

### Step 1 — Prepare your repo

Make sure these files exist in the root of your GitHub repo:

```
app.py
requirements.txt
render.yaml          # optional but recommended
index.html           # served at GET /
```

Your `requirements.txt` should contain:

```
flask
flask-cors
yt-dlp
youtube-transcript-api
gunicorn
```

### Step 2 — Create a Web Service on Render

1. Go to [https://dashboard.render.com](https://dashboard.render.com) and click **New → Web Service**
2. Connect your GitHub repo
3. Fill in the settings:

| Setting | Value |
|---|---|
| **Name** | `tubeintel` (or your choice) |
| **Region** | Oregon (US West) or nearest |
| **Branch** | `main` |
| **Runtime** | `Python 3` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120 --workers 2` |
| **Instance Type** | Free (or Starter for better performance) |

> **Why `--timeout 120`?** Transcript + metadata fetches can take 10–30 seconds per video. The default gunicorn timeout of 30s will kill those requests on the free tier. Set it to at least 120s.

### Step 3 — Add Environment Variables (optional)

In **Render → Environment**, you can add:

| Variable | Default | Description |
|---|---|---|
| `PORT` | Set by Render automatically | Do not override |

### Step 4 — Deploy

Click **Deploy Web Service**. Render will install dependencies and start the server. First deploy takes ~2 minutes.

### Step 5 — Verify

```bash
curl https://YOUR-APP.onrender.com/api/health
# Expected: {"status":"ok","downloads":"/opt/render/project/src/downloads"}

curl -X POST https://YOUR-APP.onrender.com/api/video/process \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=xAt1xcC6qfM"}'
# Expected: JSON with video_id, transcript, metadata fields
```

---

## Optional: render.yaml (Infrastructure as Code)

Add this file to your repo root to configure Render automatically:

```yaml
services:
  - type: web
    name: tubeintel
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120 --workers 2
    envVars:
      - key: PYTHON_VERSION
        value: "3.11.0"
```

---

## API Reference

### `POST /api/video/process`

Enrich a single YouTube video.

**Request:**
```json
{ "url": "https://www.youtube.com/watch?v=VIDEO_ID" }
```

**Response:**
```json
{
  "video_id": "xAt1xcC6qfM",
  "url": "https://www.youtube.com/watch?v=xAt1xcC6qfM",
  "title": "Video Title",
  "description": "...",
  "view_count": 123456,
  "like_count": 4500,
  "comment_count": 320,
  "engagement_score": 0.03916,
  "tags": ["tag1", "tag2"],
  "categories": ["Education"],
  "thumbnail": "https://...",
  "uploader": "Channel Name",
  "channel_id": "UCxxxxxxx",
  "upload_date": "20240101",
  "transcript": "Full transcript text joined into one string...",
  "transcript_segments": [
    { "text": "Hello", "start": 0.0, "duration": 1.5 }
  ]
}
```

> `transcript` and `transcript_segments` will be `null` if the video has no captions or captions are disabled.

---

### `POST /api/channel/analyze`

Fetch and enrich the top N videos from a channel.

**Request:**
```json
{
  "channel_url": "https://www.youtube.com/@ChannelHandle",
  "max_videos": 50,
  "top_n": 3
}
```

**Response:**
```json
{
  "count": 3,
  "videos": [ /* array of enriched video objects */ ],
  "json_url": "/downloads/videos_20240410_120000.json",
  "csv_url": "/downloads/videos_20240410_120000.csv"
}
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `video_id: null`, `transcript: null` | yt-dlp blocked by YouTube | Already fixed — video_id now parsed from URL directly |
| `transcript: null` only | Video has no English captions | Expected — some videos disable captions |
| 502 on channel analyze | yt-dlp bot-detected on Render free tier | Try adding cookies or use Render Starter plan |
| Request timeout (gunicorn) | Default 30s too short | Use `--timeout 120` in start command (see above) |
| `downloads/` files missing after redeploy | Render ephemeral disk | Free tier has no persistent disk; use Render Disk add-on or export to S3 |

---

## License

MIT