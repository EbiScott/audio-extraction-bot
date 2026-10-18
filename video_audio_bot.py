import os
import re
import uuid
import json
import subprocess
import logging
from pathlib import Path

import httpx
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Render injects these two automatically — no need to set them yourself
RENDER_HOST = os.environ["RENDER_EXTERNAL_HOSTNAME"]   # e.g. video-audio-bot.onrender.com
PORT = int(os.environ.get("PORT", 8443))               # Render assigns a port dynamically

WEBHOOK_URL = f"https://{RENDER_HOST}/webhook"         # full public URL Telegram will POST to
WEBHOOK_PATH = "/webhook"                              # local path our server listens on

DOWNLOAD_DIR = Path("/tmp/vab")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MAX_TG_FILE_SIZE_MB = 20
MAX_URL_FILE_SIZE_MB = 500
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)


# ── URL normalisation ─────────────────────────────────────────────────────────

def normalise_url(url: str) -> str:
    """Convert Google Drive / Dropbox share links to direct download URLs."""
    # Google Drive: /file/d/<ID>/view  →  /uc?export=download&id=<ID>
    gd = re.match(r"https://drive\.google\.com/file/d/([^/]+)", url)
    if gd:
        return f"https://drive.google.com/uc?export=download&id={gd.group(1)}"

    # Dropbox: ?dl=0  →  ?dl=1
    if "dropbox.com" in url:
        return re.sub(r"[?&]dl=0", "?dl=1", url)

    return url


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_timestamp(ts: str) -> str:
    """Accept HH:MM:SS or MM:SS — ffmpeg handles both natively."""
    if re.fullmatch(r"(\d{1,2}:)?\d{1,2}:\d{2}(\.\d+)?", ts):
        return ts
    raise ValueError(f"Invalid timestamp: {ts!r}")


def run_ffmpeg(*args: str) -> tuple[bool, str]:
    """Run an ffmpeg command. Returns (success, stderr)."""
    cmd = ["ffmpeg", "-y", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, result.stderr


def get_metadata(video_path: Path) -> str:
    """Return a human-readable metadata string via ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return "Could not read metadata."

    data = json.loads(result.stdout)
    fmt = data.get("format", {})
    streams = data.get("streams", [])

    duration = float(fmt.get("duration", 0))
    size_mb = int(fmt.get("size", 0)) / (1024 * 1024)
    fmt_name = fmt.get("format_long_name", "Unknown")

    minutes, seconds = divmod(int(duration), 60)
    hours, minutes = divmod(minutes, 60)
    dur_str = f"{hours:02}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"

    video_stream = next((s for s in streams if s["codec_type"] == "video"), None)
    audio_stream = next((s for s in streams if s["codec_type"] == "audio"), None)

    lines = [
        "📋 *Video Metadata*",
        f"• Format: {fmt_name}",
        f"• Duration: `{dur_str}`",
        f"• File size: `{size_mb:.2f} MB`",
    ]

    if video_stream:
        w = video_stream.get("width", "?")
        h = video_stream.get("height", "?")
        codec = video_stream.get("codec_name", "?").upper()
        fps_raw = video_stream.get("r_frame_rate", "0/1")
        try:
            num, den = fps_raw.split("/")
            fps = round(int(num) / int(den), 2)
        except Exception:
            fps = "?"
        lines.append(f"• Video: `{codec}` — {w}×{h} @ {fps} fps")

    if audio_stream:
        acodec = audio_stream.get("codec_name", "?").upper()
        sample_rate = audio_stream.get("sample_rate", "?")
        channels = audio_stream.get("channels", "?")
        lines.append(f"• Audio: `{acodec}` — {sample_rate} Hz, {channels}ch")

    return "\n".join(lines)


async def download_from_telegram(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Path | None:
    """Download a video attached to a Telegram message (≤20 MB)."""
    msg = update.effective_message
    video = msg.video or msg.document

    if not video:
        return None

    file_size_mb = (video.file_size or 0) / (1024 * 1024)
    if file_size_mb > MAX_TG_FILE_SIZE_MB:
        await msg.reply_text(
            f"⚠️ File is {file_size_mb:.1f} MB — Telegram caps bot downloads at {MAX_TG_FILE_SIZE_MB} MB.\n\n"
            f"Upload it to Google Drive or Dropbox and send me the link instead!"
        )
        return None

    status = await msg.reply_text("⏳ Downloading video…")
    tg_file = await context.bot.get_file(video.file_id)
    video_path = DOWNLOAD_DIR / f"{video.file_unique_id}.mp4"
    await tg_file.download_to_drive(video_path)
    await status.delete()
    return video_path


async def download_from_url(url: str, msg) -> Path | None:
    """Stream-download a video from a URL into /tmp. Returns path or None."""
    direct_url = normalise_url(url)
    video_path = DOWNLOAD_DIR / f"{uuid.uuid4().hex}.mp4"

    status = await msg.reply_text("⏳ Downloading from URL…")

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
            async with client.stream("GET", direct_url) as r:
                r.raise_for_status()

                content_length = r.headers.get("content-length")
                if content_length:
                    size_mb = int(content_length) / (1024 * 1024)
                    if size_mb > MAX_URL_FILE_SIZE_MB:
                        await status.delete()
                        await msg.reply_text(
                            f"⚠️ File is {size_mb:.0f} MB — limit is {MAX_URL_FILE_SIZE_MB} MB."
                        )
                        return None

                downloaded = 0
                with video_path.open("wb") as f:
                    async for chunk in r.aiter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if downloaded > MAX_URL_FILE_SIZE_MB * 1024 * 1024:
                            await status.delete()
                            await msg.reply_text(
                                f"⚠️ Download exceeded {MAX_URL_FILE_SIZE_MB} MB limit. Stopping."
                            )
                            video_path.unlink(missing_ok=True)
                            return None

    except httpx.HTTPStatusError as e:
        await status.delete()
        await msg.reply_text(
            f"❌ Couldn't fetch the file: HTTP {e.response.status_code}.\n"
            "Make sure the link is public and points to a video."
        )
        video_path.unlink(missing_ok=True)
        return None
    except Exception as e:
        logger.error("URL download error: %s", e)
        await status.delete()
        await msg.reply_text("❌ Failed to download from that URL. Is it a public direct link?")
        video_path.unlink(missing_ok=True)
        return None

    await status.delete()
    return video_path


async def extract_and_send(
    video_path: Path,
    msg,
    caption: str = "Here's your MP3 🎵",
    start_ts: str = None,
    end_ts: str = None,
) -> None:
    """Run ffmpeg extraction (with optional trim) and send the MP3."""
    mp3_path = video_path.with_suffix(".mp3")

    label = (
        f"✂️ Trimming `{start_ts}` → `{end_ts}` and extracting…"
        if start_ts else "🎵 Extracting audio…"
    )
    status = await msg.reply_text(label, parse_mode="Markdown")

    ffmpeg_args = ["-i", str(video_path)]
    if start_ts:
        ffmpeg_args += ["-ss", start_ts, "-to", end_ts]
    ffmpeg_args += ["-vn", "-acodec", "libmp3lame", "-q:a", "2", str(mp3_path)]

    ok, stderr = run_ffmpeg(*ffmpeg_args)
    await status.delete()
    video_path.unlink(missing_ok=True)

    if not ok or not mp3_path.exists():
        logger.error("ffmpeg error: %s", stderr)
        await msg.reply_text("❌ Failed to extract audio. Is this a valid video file?")
        return

    title = f"Trimmed Audio ({start_ts} – {end_ts})" if start_ts else "Extracted Audio"
    await msg.reply_audio(
        audio=mp3_path.open("rb"),
        title=title,
        caption=caption,
        parse_mode="Markdown",
    )
    mp3_path.unlink(missing_ok=True)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎬 *Video Audio Extractor*\n\n"
        "Send or forward any video and I'll extract the audio as an MP3.\n"
        "For videos over 20 MB, send a Google Drive or Dropbox link.\n\n"
        "*Commands:*\n"
        "• Send a video → full MP3\n"
        "• Send a URL → download & extract\n"
        "• Reply to a video with `/trim 00:30 01:45` → trimmed MP3\n"
        "• Reply to a video with `/metadata` → video info\n\n"
        "Telegram upload limit: *20 MB* | URL limit: *500 MB*"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a direct video upload."""
    video_path = await download_from_telegram(update, context)
    if not video_path:
        return
    await extract_and_send(video_path, update.effective_message)


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a plain text message containing a URL."""
    msg = update.effective_message
    text = msg.text or ""
    match = URL_PATTERN.search(text)
    if not match:
        return

    url = match.group(0)
    video_path = await download_from_url(url, msg)
    if not video_path:
        return
    await extract_and_send(video_path, msg)


async def trim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply to a video with /trim <start> <end>."""
    msg = update.effective_message
    replied = msg.reply_to_message

    if not replied or not (replied.video or replied.document):
        await msg.reply_text(
            "⚠️ Reply to a video message with `/trim <start> <end>`.\nExample: `/trim 00:30 01:45`",
            parse_mode="Markdown",
        )
        return

    if len(context.args) != 2:
        await msg.reply_text(
            "⚠️ Usage: `/trim <start> <end>`\nExample: `/trim 00:30 01:45`",
            parse_mode="Markdown",
        )
        return

    try:
        start_ts = parse_timestamp(context.args[0])
        end_ts = parse_timestamp(context.args[1])
    except ValueError as e:
        await msg.reply_text(f"⚠️ {e}\nUse format `MM:SS` or `HH:MM:SS`.", parse_mode="Markdown")
        return

    update._effective_message = replied
    video_path = await download_from_telegram(update, context)
    update._effective_message = msg

    if not video_path:
        return

    await extract_and_send(
        video_path, msg,
        caption=f"Trimmed `{start_ts}` → `{end_ts}` 🎵",
        start_ts=start_ts,
        end_ts=end_ts,
    )


async def metadata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply to a video with /metadata."""
    msg = update.effective_message
    replied = msg.reply_to_message

    if not replied or not (replied.video or replied.document):
        await msg.reply_text("⚠️ Reply to a video message with `/metadata`.", parse_mode="Markdown")
        return

    update._effective_message = replied
    video_path = await download_from_telegram(update, context)
    update._effective_message = msg

    if not video_path:
        return

    status = await msg.reply_text("🔍 Reading metadata…")
    info = get_metadata(video_path)
    await status.delete()
    video_path.unlink(missing_ok=True)

    await msg.reply_text(info, parse_mode="Markdown")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("trim", trim))
    app.add_handler(CommandHandler("metadata", metadata))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(URL_PATTERN), handle_url))

    logger.info("Starting webhook on port %s → %s", PORT, WEBHOOK_URL)

    # run_webhook() does three things automatically:
    # 1. Calls Telegram's setWebhook API with our WEBHOOK_URL
    # 2. Starts a Tornado HTTP server locally on PORT
    # 3. Listens on WEBHOOK_PATH for incoming POST requests from Telegram
    app.run_webhook(
        listen="0.0.0.0",       # accept connections from anywhere (Render's load balancer)
        port=PORT,               # the port Render assigned us via $PORT
        url_path=WEBHOOK_PATH,   # e.g. /webhook
        webhook_url=WEBHOOK_URL, # full public URL registered with Telegram
    )


if __name__ == "__main__":
    main()
