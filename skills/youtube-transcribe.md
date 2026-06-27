---
description: "Transcribe YouTube videos \u2014 tries VPS first, falls back to laptop\
  \ if cloud IP is blocked"
name: youtube-transcribe
triggers:
- transcribe
- youtube transcript
- transcript video
- youtube transcribe
- caption youtube
- get transcript
- transcription
- youtube caption
- subtitles youtube
---
# YouTube Transcription Workflow

## Steps

### 1. Extract Video ID
Parse the YouTube URL for the video ID (11-character string after `v=` or `youtu.be/`).

### 2. Try VPS First
Call `youtube_transcribe(url=url)` — this is the native tool that fetches captions for free.

**If successful:**
- Transcript saved to `memory vault/youtube/<video_id>_<title>.md`
- Notify user with path + brief summary + line count
- ✅ Done

**If it fails** (cloud IP blocked or other error):
- Proceed to Step 3 (Laptop fallback)

### 3. Fallback — SSH to Laptop
SSH to laptop via Tailscale (configure `CROWD_LAPTOP_SSH` env var):
```
host: $CROWD_LAPTOP_SSH
```

On laptop, extract the transcript using `youtube_transcript_api`:

```bash
pip install youtube_transcript_api -q 2>/dev/null
python3 -c "
import json, re
from youtube_transcript_api import YouTubeTranscriptApi

video_id = '<VIDEO_ID>'
transcript = YouTubeTranscriptApi.get_transcript(video_id)

# Build markdown
lines = []
for entry in transcript:
    start = entry['start']
    mins = int(start // 60)
    secs = int(start % 60)
    text = entry['text']
    lines.append(f'[{mins:02d}:{secs:02d}] {text}')

full_text = '\\n'.join(lines)

# Get title
try:
    from youtube_transcript_api._html import unescape_html
    import requests
    resp = requests.get(f'https://www.youtube.com/watch?v={video_id}', timeout=10)
    import re as re2
    title_match = re2.search(r'<title>(.*?)<\\/title>', resp.text)
    title = title_match.group(1).replace(' - YouTube', '').strip() if title_match else video_id
except:
    title = video_id

safe_title = re.sub(r'[^a-zA-Z0-9_\\- ]', '', title).strip().replace(' ', '_')
filename = f'{video_id}_{safe_title}.md'
filepath = f'$CROWD_VAULT/youtube/{filename}'

with open(filepath, 'w') as f:
    f.write(f'# {title}\\n\\n')
    f.write(f'**Source:** https://youtu.be/{video_id}\\n')
    f.write(f'**Duration:** {len(transcript)} entries\\n\\n')
    f.write(full_text)
    f.write('\\n')

print(f'SAVED:{filepath}')
print(f'LINES:{len(full_text.splitlines())}')
"
```

### 4. Copy Transcript Back to VPS
After laptop has the file, SCP it back to VPS:

```bash
scp "$CROWD_LAPTOP_SSH:<laptop_filepath>" "$CROWD_VAULT/youtube/"
```

### 5. Verify
- Check file exists on VPS: `list_dir(path="memory vault/youtube")`
- Check file exists on laptop: `ssh_exec` with `ls -la "<laptop_filepath>"`
- Confirm line counts match

### 6. Notify User
Report:
- ✅ Video title + duration
- 📁 VPS path
- 📁 Laptop path
- Line count

### File Naming Convention
`<video_id>_<Title_Case_With_Underscores>.md`

### Paths
- **VPS:** `$CROWD_VAULT/youtube/`
- **Laptop:** `$CROWD_VAULT/youtube/` (via SSH)
- **Laptop host:** `$CROWD_LAPTOP_SSH` (Tailscale IP)

### Notes
- VPS YouTube API is sometimes blocked (cloud IP detected) — laptop fallback works reliably
- Laptop must be awake and on Tailscale for SSH to work
- Always verify both locations have the file
