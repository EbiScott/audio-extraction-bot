import asyncio
import os
import re
import uuid
import json
import subprocess
import logging
from pathlib import Path

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
RENDER_HOST = os.environ["RENDER_EXTERNAL_HOSTNAME"]
PORT = int(os.environ.get("PORT", 8443))
WEBHOOK_URL = f"https://{RENDER_HOST}/webhook"
WEBHOOK_PATH = "/webhook"

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

# ── Conversation states ───────────────────────────────────────────────────────
# These are just integers used as labels for each step in the conversation.
# We define them as constants so the code is readable instead of using
# magic numbers like 0, 1, 2 everywhere.
AWAITING_QUALITY = 0

# ── Quality options ───────────────────────────────────────────────────────────
# Maps callback data string → (display label, ffmpeg -q:a value)
# ffmpeg's -q:a controls VBR quality: lower = better (0=best, 9=worst)
# 128kbps ≈ q:a 7 | 192kbps ≈ q:a 4 | 320kbps ≈ q:a 0
QUALITY_OPTIONS = {
    "q_128": ("128 kbps", "7"),
    "q_192": ("192 kbps", "4"),
    "q_320": ("320 kbps", "0"),
}

def quality_keyboard() -> InlineKeyboardMarkup:
    """
    Build the inline keyboard for quality selection.

    InlineKeyboardMarkup takes a list of rows.
    Each row is a list of InlineKeyboardButton objects.
    We put all three buttons in one row here.

    Each button has:
      text  → what the user sees on the button
      callback_data → the string sent back to us when they tap it
    """
    buttons = [
        InlineKeyboardButton(label, callback_data=key)
        for key, (label, _) in QUALITY_OPTIONS.items()
    ]
    return InlineKeyboardMarkup([buttons])  # one row of three buttons


# ── URL normalisation ─────────────────────────────────────────────────────────

def normalise_url(url: str) -> str:
    gd = re.match(r"https://drive\.google\.com/file/d/([^/]+)", url)
    if gd:
        return f"https://drive.google.com/uc?export=download&id={gd.group(1)}"
    if "dropbox.com" in url:
        return re.sub(r"[?&]dl=0", "?dl=1", url)
    return url


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_timestamp(ts: str) -> str:
    if re.fullmatch(r"(\d{1,2}:)?\d{1,2}:\d{2}(\.\d+)?", ts):
        return ts
    raise ValueError(f"Invalid timestamp: {ts!r}")


def run_ffmpeg(*args: str) -> tuple[bool, str]:
    cmd = ["ffmpeg", "-y", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, result.stderr


def get_metadata(video_path: Path) -> str:
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
                        await msg.reply_text(f"⚠️ File is {size_mb:.0f} MB — limit is {MAX_URL_FILE_SIZE_MB} MB.")
                        return None

                downloaded = 0
                with video_path.open("wb") as f:
                    async for chunk in r.aiter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if downloaded > MAX_URL_FILE_SIZE_MB * 1024 * 1024:
                            await status.delete()
                            await msg.reply_text(f"⚠️ Download exceeded {MAX_URL_FILE_SIZE_MB} MB limit. Stopping.")
                            video_path.unlink(missing_ok=True)
                            return None

    except httpx.HTTPStatusError as e:
        await status.delete()
        await msg.reply_text(f"❌ Couldn't fetch the file: HTTP {e.response.status_code}.\nMake sure the link is public.")
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
    quality_key: str = "q_192",
    caption_suffix: str = "",
    start_ts: str = None,
    end_ts: str = None,
) -> None:
    """
    Run ffmpeg and send the MP3.

    quality_key maps to QUALITY_OPTIONS, e.g. "q_192" → q:a value "4"
    caption_suffix lets callers append extra info (e.g. trim range)
    """
    mp3_path = video_path.with_suffix(".mp3")
    label, qa_value = QUALITY_OPTIONS.get(quality_key, ("192 kbps", "4"))

    trim_note = f" — trimmed `{start_ts}` → `{end_ts}`" if start_ts else ""
    status_text = f"🎵 Extracting at *{label}*{trim_note}…"
    status = await msg.reply_text(status_text, parse_mode="Markdown")

    ffmpeg_args = ["-i", str(video_path)]
    if start_ts:
        ffmpeg_args += ["-ss", start_ts, "-to", end_ts]
    # -vn         → strip video stream
    # -acodec     → encode with LAME MP3
    # -q:a value  → VBR quality level (0=best/320kbps, 7=~128kbps)
    ffmpeg_args += ["-vn", "-acodec", "libmp3lame", "-q:a", qa_value, str(mp3_path)]

    ok, stderr = run_ffmpeg(*ffmpeg_args)
    await status.delete()
    video_path.unlink(missing_ok=True)

    if not ok or not mp3_path.exists():
        logger.error("ffmpeg error: %s", stderr)
        await msg.reply_text("❌ Failed to extract audio. Is this a valid video file?")
        return

    caption = f"🎵 *{label}* MP3{caption_suffix}"
    await msg.reply_audio(
        audio=mp3_path.open("rb"),
        title="Extracted Audio",
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
        "• Send a video → choose quality → get MP3\n"
        "• Send a URL → choose quality → get MP3\n"
        "• Reply to a video with `/trim 00:30 01:45` → trimmed MP3\n"
        "• Reply to a video with `/metadata` → video info\n\n"
        "Telegram upload limit: *20 MB* | URL limit: *500 MB*"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ── ConversationHandler entry points ─────────────────────────────────────────

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Step 1 of the conversation (triggered by receiving a video).

    We download the video, save its path to user_data, then ask
    the user to pick a quality. Returning AWAITING_QUALITY tells
    the ConversationHandler to move this user into that state —
    their next interaction will be handled by handle_quality().
    """
    video_path = await download_from_telegram(update, context)
    if not video_path:
        return ConversationHandler.END  # abort if download failed

    # Store the path as a string — user_data must be serialisable
    context.user_data["pending_video"] = str(video_path)
    context.user_data["pending_source"] = "telegram"

    await update.effective_message.reply_text(
        "🎵 What quality do you want?",
        reply_markup=quality_keyboard(),
    )
    return AWAITING_QUALITY  # move to next state


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Step 1 of the conversation (triggered by receiving a URL).
    Same pattern as handle_video — download first, ask quality second.
    """
    msg = update.effective_message
    text = msg.text or ""
    match = URL_PATTERN.search(text)
    if not match:
        return ConversationHandler.END

    url = match.group(0)
    video_path = await download_from_url(url, msg)
    if not video_path:
        return ConversationHandler.END

    context.user_data["pending_video"] = str(video_path)
    context.user_data["pending_source"] = "url"

    await msg.reply_text(
        "🎵 What quality do you want?",
        reply_markup=quality_keyboard(),
    )
    return AWAITING_QUALITY


async def handle_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Step 2 of the conversation (triggered by tapping a quality button).

    update.callback_query is the button tap event.
    query.data is the string we attached to the button ("q_128" etc.)
    query.answer() MUST be called to dismiss the loading spinner on the button.
    query.edit_message_text() replaces the "What quality?" message in-place.
    """
    query = update.callback_query
    await query.answer()  # always required — dismisses Telegram's loading state

    quality_key = query.data  # e.g. "q_128", "q_192", "q_320"
    label, _ = QUALITY_OPTIONS.get(quality_key, ("192 kbps", "4"))

    # Edit the quality prompt message to remove the buttons and show status
    await query.edit_message_text(f"Got it — extracting at *{label}*…", parse_mode="Markdown")

    # Retrieve the video path we stored in step 1
    video_path_str = context.user_data.pop("pending_video", None)
    context.user_data.pop("pending_source", None)

    if not video_path_str:
        await query.message.reply_text("⚠️ Something went wrong — please send the video again.")
        return ConversationHandler.END

    video_path = Path(video_path_str)
    if not video_path.exists():
        await query.message.reply_text("⚠️ The video file expired — please send it again.")
        return ConversationHandler.END

    await extract_and_send(
        video_path,
        query.message,
        quality_key=quality_key,
    )

    return ConversationHandler.END  # conversation is over


async def handle_quality_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Called if the user never taps a quality button.
    We clean up the downloaded file so it doesn't sit in /tmp forever.
    """
    video_path_str = context.user_data.pop("pending_video", None)
    if video_path_str:
        Path(video_path_str).unlink(missing_ok=True)
    if update.effective_message:
        await update.effective_message.reply_text(
            "⏰ Quality selection timed out. Send the video again when you're ready."
        )
    return ConversationHandler.END


# ── Non-conversation handlers (trim, metadata) ────────────────────────────────

async def trim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    replied = msg.reply_to_message

    if not replied or not (replied.video or replied.document):
        await msg.reply_text(
            "⚠️ Reply to a video message with `/trim <start> <end>`.\nExample: `/trim 00:30 01:45`",
            parse_mode="Markdown",
        )
        return

    if len(context.args) != 2:
        await msg.reply_text("⚠️ Usage: `/trim <start> <end>`\nExample: `/trim 00:30 01:45`", parse_mode="Markdown")
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
        quality_key="q_192",  # trim always uses 192kbps default
        caption_suffix=f" — trimmed `{start_ts}` → `{end_ts}`",
        start_ts=start_ts,
        end_ts=end_ts,
    )


async def metadata(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # ── ConversationHandler ───────────────────────────────────────────────────
    #
    # entry_points  → what triggers the start of a conversation
    # states        → a dict mapping each state to its handler(s)
    # fallbacks     → what handles messages that don't match the current state
    # conversation_timeout → seconds before handle_quality_timeout is called
    #
    # IMPORTANT: the ConversationHandler must be added BEFORE the plain
    # MessageHandlers, because PTB checks handlers in registration order.
    # If we registered the plain VIDEO handler first, it would intercept
    # the video before the ConversationHandler ever sees it.
    #
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video),
            MessageHandler(filters.TEXT & filters.Regex(URL_PATTERN), handle_url),
        ],
        states={
            AWAITING_QUALITY: [
                # Only fires when the user is in AWAITING_QUALITY state
                # and taps a button whose data starts with "q_"
                CallbackQueryHandler(handle_quality, pattern=r"^q_"),
            ],
        },
        fallbacks=[
            # If the user sends anything else while we're waiting for a
            # quality tap, gently remind them what we need
            MessageHandler(
                filters.ALL,
                lambda u, c: u.effective_message.reply_text(
                    "👆 Please tap one of the quality buttons above first."
                ),
            )
        ],
        conversation_timeout=120,  # 2 minutes to tap a button, then cleanup
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("trim", trim))
    app.add_handler(CommandHandler("metadata", metadata))

    logger.info("Starting webhook on port %s → %s", PORT, WEBHOOK_URL)

    async with app:
        await app.start()
        await app.updater.start_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=WEBHOOK_URL,
            allowed_updates=Update.ALL_TYPES,
        )
        logger.info("Bot is live ✅")
        await asyncio.Event().wait()
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
