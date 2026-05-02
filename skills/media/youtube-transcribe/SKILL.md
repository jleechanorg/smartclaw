---
name: youtube-transcribe
description: Download a YouTube video and transcribe it using Whisper AI. Extracts audio, runs transcription, optionally uploads the video to Google Drive.
triggers:
  - "download and transcribe a YouTube video"
  - "transcribe this video"
  - "YouTube to text"
  - "download video and transcript"
  - "transcribe youtube"
---

# YouTube Video Download + Transcription Skill

## Prerequisites

### Required tools (all must be installed)
```bash
# yt-dlp — video/audio downloader
brew install yt-dlp

# ffmpeg — audio extraction (usually comes with ffmpeg)
brew install ffmpeg

# whisper — OpenAI's transcription CLI
pip install -U openai-whisper

# gog — Google Drive upload (Hermes-specific)
# Already configured in TOOLS.md; credentials at:
#   - ~/.google_drive_credentials.json  (OAuth2 client/refresh token)
#   - ~/google_drive_token.json          (short-lived access token)
```

### Verify installation
```bash
yt-dlp --version        # → 2026.03.17 or later
ffmpeg -version 2>&1 | head -1  # → ffmpeg version 8.x
whisper --model turbo --help 2>&1 | grep model  # → shows model arg
gog --version           # → v0.10.0 or later
```

---

## Step 1 — Download Video + Audio

### Recommended: Video + audio (for upload) + separate audio extraction
```bash
VIDEO_URL="<YouTube URL>"
OUTPUT_DIR="$HOME/Downloads/yt-transcribe/$(date +%Y-%m-%d)"
mkdir -p "$OUTPUT_DIR"

# Download best-quality MP4 video + best audio (puts both in $OUTPUT_DIR)
yt-dlp \
  -f "bv[ext=mp4]+ba[ext=m4a]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best" \
  -o "%(title)s.%(ext)s" \
  --embed-metadata \
  --parse-metadata "%(title)s:%(meta_title)s" \
  "$VIDEO_URL" \
  -P "$OUTPUT_DIR"
```

**Output:** `$OUTPUT_DIR/<video-title>.mp4` + `$OUTPUT_DIR/<video-title>.m4a`

### Alternative: Audio-only (faster, smaller)
```bash
yt-dlp \
  -x --audio-format mp3 --audio-quality 0 \
  -o "%(title)s.%(ext)s" \
  "$VIDEO_URL" \
  -P "$OUTPUT_DIR"
```

**Output:** `$OUTPUT_DIR/<video-title>.mp3`

### Extract audio from downloaded video (if you downloaded video but want separate MP3)
```bash
ffmpeg -i "$OUTPUT_DIR/<video-title>.mp4" \
  -vn -acodec libmp3lame -q:a 2 \
  "$OUTPUT_DIR/<video-title>.mp3"
```

---

## Step 2 — Transcribe with Whisper

### Quick start (uses turbo model by default)
```bash
whisper \
  --model turbo \
  --language English \
  --output_dir "$OUTPUT_DIR" \
  --output_format all \
  "$OUTPUT_DIR/<video-title>.mp3"
```

### Recommended: Plain text + timestamped versions
```bash
whisper \
  --model turbo \
  --language English \
  --output_dir "$OUTPUT_DIR" \
  --output_format txt \
  "$OUTPUT_DIR/<video-title>.mp3"

whisper \
  --model turbo \
  --language English \
  --output_dir "$OUTPUT_DIR" \
  --output_format tsv \
  --word_timestamps True \
  "$OUTPUT_DIR/<video-title>.mp3"
```

**Outputs:**
- `$OUTPUT_DIR/<video-title>.txt` — plain transcript (1 line per segment)
- `$OUTPUT_DIR/<video-title>.tsv` — timestamped (start, end, text per row)
- `$OUTPUT_DIR/<video-title>.vtt` — WebVTT captions
- `$OUTPUT_DIR/<video-title>.srt` — SubRip subtitles
- `$OUTPUT_DIR/<video-title>.json` — full JSON with timing data

### Model guide
| Model   | Speed | Accuracy | RAM needed |
|---------|-------|----------|------------|
| tiny    | fastest | lowest | ~1 GB |
| base    | fast   | low     | ~1 GB |
| small   | medium | medium   | ~2 GB |
| turbo   | fast   | high     | ~4 GB (default for CLI) |
| medium  | slow   | very high | ~5 GB |
| large   | slowest | highest | ~10 GB |

Default is `turbo`. Download models with:
```bash
whisper --model <name> --model_dir ~/.cache/whisper <audio-file>
# Model files land in ~/.cache/whisper/
```

### Timing estimate
- ~1 GB MP3 ≈ 15–20 min transcription with turbo
- ~100 MB MP3 ≈ 2–5 min

---

## Step 3 — Upload Video to Google Drive (optional)

### Using gog (recommended — uses pre-existing credentials)
```bash
gog drive upload "$OUTPUT_DIR/<video-title>.mp4" \
  --account jleechan@gmail.com \
  2>&1
```

### Using curl/REST (if gog is unavailable or token expired)

#### 1. Refresh the access token
```bash
CRED_FILE="$HOME/.google_drive_credentials.json"
TOKEN_FILE="$HOME/.google_drive_token.json"

CLIENT_ID=$(python3 -c "import json; print(json.load(open('$CRED_FILE'))['installed']['client_id'])")
CLIENT_SECRET=$(python3 -c "import json; print(json.load(open('$CRED_FILE'))['installed']['client_secret'])")
REFRESH_TOKEN=$(python3 -c "import json; print(json.load(open('$CRED_FILE'))['installed']['refresh_token'])")

RESPONSE=$(curl -s -X POST https://oauth2.googleapis.com/token \
  -d "client_id=${CLIENT_ID}&client_secret=${CLIENT_SECRET}&refresh_token=${REFRESH_TOKEN}&grant_type=refresh_token")

ACCESS_TOKEN=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
echo "$ACCESS_TOKEN" > "$TOKEN_FILE"
```

#### 2. Upload (multipart — simple form upload)
```bash
FILENAME="<video-title>.mp4"
MIME_TYPE="video/mp4"
FILE_PATH="$OUTPUT_DIR/<video-title>.mp4"

curl -s -X POST \
  "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart" \
  -H "Authorization: Bearer $(cat $TOKEN_FILE)" \
  -F "metadata={\"name\":\"$FILENAME\",\"parents\":[\"root\"]};type=application/json" \
  -F "file=@$FILE_PATH;type=$MIME_TYPE"
```

**For large files (>5MB), use resumable upload:**
```bash
# Step A: Create upload session
SESSION_URL=$(curl -s -X POST \
  -H "Authorization: Bearer $(cat $TOKEN_FILE)" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"$FILENAME\",\"parents\":[\"root\"]}" \
  "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable" \
  -D - | grep -i "^Location:" | tr -d '\r' | awk '{print $2}')

# Step B: Upload the file contents
curl -s -X PUT "$SESSION_URL" \
  -H "Authorization: Bearer $(cat $TOKEN_FILE)" \
  -H "Content-Type: $MIME_TYPE" \
  --data-binary @"$FILE_PATH"
```

---

## Step 4 — Build a Summary (optional)

Read the plain transcript and synthesize a summary. Key things to extract:
1. **What is the content** (dev stream? tutorial? podcast?)
2. **Who are the participants** (names/roles if given)
3. **Main topics or sections** (with timestamps if available)
4. **Key capabilities, features, or claims demonstrated**
5. **Any roadmap items or future plans mentioned**
6. **Comparison to similar products** (if applicable)

Store significant findings as mem0 memories with `mem0_conclude`.

---

## Step 5 — Create GitHub PR with Transcript (optional)

If the transcript belongs in a repo `docs/` folder:

```bash
REPO_DIR="$HOME/work/<repo-name>"
cd "$REPO_DIR"

# Create branch
git checkout -b docs/transcript-$(date +%Y-%m-%d)

# Copy transcript
cp "$OUTPUT_DIR/<video-title>.txt" "$REPO_DIR/docs/transcript-$(date +%Y-%m-%d).md"

# Edit: prepend YAML frontmatter with source, date, URL
# (Use write_file tool, don't manually edit)

# Commit and push
git add docs/transcript-*.md
git commit -m "docs: add transcript for <video-title>"
git push -u origin HEAD

# Create PR via GitHub API
curl -s -X POST \
  -H "Authorization: token $(gh auth token)" \
  -H "Accept: application/vnd.github.v3+json" \
  https://api.github.com/repos/<org>/<repo>/pulls \
  -d '{
    "title": "docs: transcript — <video-title>",
    "head": "docs/transcript-'"$(date +%Y-%m-%d)"'",
    "base": "main",
    "body": "## Source\n\n- **YouTube:** https://youtube.com/watch?v=XXXXXXXXXXX\n- **Google Drive:** https://drive.google.com/file/d/XXXXXXXXX/view\n\n## Summary\n\n[TODO: fill in after reading transcript]\n\n## Test Plan\n\n- [x] Transcript reviewed for accuracy\n- [x] Links verified"
  }'
```

---

## Directory Layout Convention

```
~/Downloads/yt-transcribe/
└── YYYY-MM-DD/
    ├── <video-title>.mp4          # original video
    ├── <video-title>.m4a          # audio track
    ├── <video-title>.mp3         # extracted audio
    ├── <video-title>.txt         # plain transcript
    ├── <video-title>.tsv         # timestamped transcript
    └── transcription_log.txt     # whisper CLI output
```

---

## Pitfalls & Troubleshooting

### yt-dlp fails with "Sign in to confirm"
- **Cause:** Video is age-restricted or requires login
- **Fix:** `yt-dlp --cookies-from-browser chrome <url>` or `yt-dlp --cookies /path/to/cookies.txt <url>`

### whisper: error: the following arguments are required: audio
- **Cause:** Audio file path not passed as positional argument
- **Fix:** Always put the audio file path as the last positional argument

### whisper is very slow on long audio
- **Cause:** Default model may be too large; running on CPU
- **Fix:** Use `--model turbo` (fast + accurate) and ensure no other heavy processes running
- **Background:** `whisper ... <audio>.mp3 2>&1 | tee transcription_log.txt &`

### whisper CLI default model
- The CLI defaults to `turbo` if `--model` is not specified (not `base` as some guides suggest)
- Model files: `~/.cache/whisper/` — if downloads fail, `mkdir -p ~/.cache/whisper`

### Language assumption
- Default language is **English** — always use `--language English` unless user specifies otherwise.
- If language is unknown, omit the `--language` flag — Whisper auto-detects by default

### YouTube comments not accessible
- `googleapis.com/comments/thread` API requires YouTube Data API v3 key
- Not available in this environment — do not attempt, skip comments

### gog drive upload fails with token error
- Token expired — refresh manually or re-authenticate:
  ```bash
  gog auth credentials ~/Downloads/client_secret.json
  ```

### google-api-python-client incompatible with Python 3.14
- Do NOT pip install `google-api-python-client` on this machine
- Use curl/REST approach for Drive uploads (see Step 3 above)

### Transcript has Whisper artifacts
- Common fixes:
  - "cold old arc" → "Colt Oldark" (character name)
  - "Bloom of the deep" → correct title
  - "Dracos" / "Draikos" → "Dracos" (same character, spelling variation)
  - "Brexburg" → "Brecht" (dwarf cleric name)
- Always search-replace common name errors after transcription

### ffprobe/ffmpeg version mismatch
- If ffmpeg warns about missing codecs, install full version:
  ```bash
  brew install ffmpeg
  ```

---

## Verification Checklist

Before reporting completion, verify:

- [ ] Video downloaded (`ls -lh $OUTPUT_DIR/*.mp4`)
- [ ] Audio extracted or audio-only download confirmed
- [ ] Transcript generated (`cat $OUTPUT_DIR/*.txt | wc -l` shows segments)
- [ ] Transcript is readable (no binary/missing chars)
- [ ] Google Drive upload confirmed (file ID returned)
- [ ] For PR: branch pushed, PR created with correct title/description
