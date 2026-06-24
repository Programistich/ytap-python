#!/usr/bin/env python3

import asyncio
import datetime
import glob
import json
import logging
import os
import random
import shutil
import string
import subprocess
from urllib.parse import urlparse

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

TOKEN = os.environ["BOT_TOKEN"]  # fail fast with a clear KeyError if unset

# Telegram doesn't support media files > 50 MB for bots to send.
MAX_TELEGRAM_AUDIO_MB = 50
# Target size per chunk when splitting a large file (leaves headroom below 50 MB).
TARGET_CHUNK_MB = 45
# Telegram caps the audio "title" field at 64 characters.
MAX_TITLE_LEN = 64
# How many leading characters of the video title to use as the audio file name.
FILENAME_LEN = 30

# Format selection. YouTube serves direct (DASH/https) audio URLs that a
# datacenter IP gets HTTP 403 on, while HLS (m3u8) streams still download.
# So prefer HLS: an audio-only HLS stream if offered, otherwise the smallest
# HLS variant that carries audio (ffmpeg extracts the audio anyway). Fall back
# to plain bestaudio for environments where direct URLs aren't blocked.
YTDLP_FORMAT = (
    "bestaudio[protocol*=m3u8]/"
    "worst[protocol*=m3u8][acodec!=none]/"
    "bestaudio[ext=m4a]/bestaudio"
)

# Optional bootstrap cookies file to get past YouTube's "confirm you're not a
# bot" check when running from a datacenter IP. Mounted read-only; used only to
# seed the writable copy the first time (see prepare_cookies). Bot still works
# without it.
COOKIES_FILE = os.getenv("COOKIES_FILE", "/app/cookies.txt")
# Working cookies live on a persistent volume so that yt-dlp's refreshed cookies
# AND cookies uploaded via chat survive a container restart. yt-dlp also rewrites
# this file on exit, so it must be writable (the mounted COOKIES_FILE is not).
WORK_COOKIES_FILE = os.getenv("WORK_COOKIES_FILE", "/data/cookies.txt")
# bgutil PO Token provider (separate container). yt-dlp fetches GVS PO tokens
# from here; without them YouTube media downloads from a datacenter IP get a
# HTTP 403. Empty string disables it.
POT_PROVIDER_URL = os.getenv("POT_PROVIDER_URL", "http://bgutil-provider:4416")

# Optional comma-separated chat ids allowed to upload fresh cookies. Empty means
# anyone may (fine for a private/single-user bot). On a public bot set this so a
# stranger can't swap the shared cookies for everyone else.
COOKIE_ADMINS = {
    s.strip() for s in os.getenv("COOKIE_ADMINS", "").split(",") if s.strip()
}

# yt-dlp stderr fragments that mean "the cookies are missing/expired" (as opposed
# to some other download failure). We only ask the user for new cookies on these.
COOKIE_ERROR_MARKERS = (
    "Sign in to confirm you're not a bot",
    "Sign in to confirm your age",
    "cookies are no longer valid",
    "The provided YouTube account cookies are no longer valid",
    "Use --cookies",
    "Please sign in",
)

# Chat id -> the youtube link that failed on expired cookies and is waiting for
# a fresh cookies.txt upload to be retried. In-memory: cleared on restart.
COOKIE_WAITERS = {}

COOKIES_EXPIRED_MESSAGE = (
    "🍪 YouTube cookies протермінувалися або більше не дійсні.\n"
    "Надішліть, будь ласка, новий cookies.txt (Netscape-формат) файлом у цей "
    "чат — щойно отримаю, автоматично продовжу обробку вашого посилання."
)


class CookiesExpired(Exception):
    """Raised when yt-dlp fails in a way that points at missing/expired cookies."""


def prepare_cookies():
    """
    Make sure a writable cookies file exists on the persistent volume.

    If persisted cookies are already there (refreshed by yt-dlp or uploaded via
    chat on a previous run) we keep them. Only the very first time do we seed the
    volume from the read-only mounted bootstrap file.
    """
    work_dir = os.path.dirname(WORK_COOKIES_FILE)
    if work_dir:
        try:
            os.makedirs(work_dir, exist_ok=True)
        except OSError as e:
            logging.error(f"Couldn't create cookies dir {work_dir}: {e}")

    if os.path.isfile(WORK_COOKIES_FILE):
        logging.info(f"Using persisted cookies at {WORK_COOKIES_FILE}")
        return

    if os.path.isfile(COOKIES_FILE):
        try:
            shutil.copyfile(COOKIES_FILE, WORK_COOKIES_FILE)
            logging.info(f"Seeded cookies at {WORK_COOKIES_FILE}")
        except OSError as e:
            logging.error(f"Couldn't prepare cookies file: {e}")


def may_update_cookies(chat_id):
    """True if this chat is allowed to replace the shared cookies file."""
    return not COOKIE_ADMINS or str(chat_id) in COOKIE_ADMINS


def is_netscape_cookies(path):
    """
    Light sanity check that an uploaded file is a Netscape cookies.txt: either it
    carries the standard header or has at least one 7-field tab-delimited line.
    Stops us from overwriting working cookies with an unrelated text file.
    """
    try:
        with open(path, "r", errors="ignore") as f:
            head = f.read(4096)
    except OSError:
        return False
    if "# Netscape HTTP Cookie File" in head or "# HTTP Cookie File" in head:
        return True
    return any(
        line and not line.startswith("#") and len(line.split("\t")) >= 7
        for line in head.splitlines()
    )


def ytdlp_cmd(*args):
    """
    Build a yt-dlp argv list, adding --cookies and the PO token provider URL
    when they are available.
    """
    cmd = ["yt-dlp"]
    if os.path.isfile(WORK_COOKIES_FILE):
        cmd += ["--cookies", WORK_COOKIES_FILE]
    if POT_PROVIDER_URL:
        cmd += ["--extractor-args",
                f"youtubepot-bgutilhttp:base_url={POT_PROVIDER_URL}"]
    cmd += list(args)
    return cmd

# Only hosts in this set are accepted. Using urlparse + an exact host match
# prevents both command injection (no shell, validated input) and spoofing
# such as "https://youtu.be.evil.com" that a substring check would allow.
ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
}

START_MESSAGE = """
Hi! Just send me a youtube link and I'll send you an audio from it.
⚠️ Pay attention: if I send several files start playing them from the last one ⚠️
The reason: telegram plays audio files from bottom upwards.
"""


def id_generator(size=6, chars=string.ascii_uppercase + string.digits):
    """
    Generates random sequence
    """
    return "".join(random.choice(chars) for _ in range(size))


def is_youtube_url(text):
    """
    Checks that text is a URL pointing at an allowed YouTube host.
    :param text: any text
    """
    try:
        parsed = urlparse(text.strip())
    except (ValueError, AttributeError):
        return False
    return parsed.scheme in ("http", "https") and parsed.netloc in ALLOWED_HOSTS


def trim_link(link):
    """
    Trims link to get direct link to the video
    Reason: If the link contains list id of playlist yt-dlp will download parts of it
    :param link: youtube link
    """
    return link.split("&")[0]


def download_video(id, url):
    """
    Download best audio stream and fetch the video title.
    Arguments are passed as an argv list (no shell) so user-supplied URLs
    cannot inject commands.
    :param url: youtube video url
    :param id: generated id for a file
    :return: title of the video, or None on failure
    :raises CookiesExpired: when the failure looks like missing/expired cookies
    """
    video_filename = f"/tmp/video-{id}.mp4"
    try:
        download = subprocess.run(
            ytdlp_cmd("--newline", "-f", YTDLP_FORMAT, url, "-o", video_filename),
            capture_output=True,
            text=True,
        )
        if download.returncode != 0:
            # A bot-check / "sign in" error means our cookies are stale: surface
            # it so the handler can ask the user for a fresh cookies.txt instead
            # of giving the generic "couldn't download" reply.
            if any(m in download.stderr for m in COOKIE_ERROR_MARKERS):
                logging.warning("yt-dlp failed on a cookie/bot-check error")
                raise CookiesExpired()
            logging.error(
                f"yt-dlp download failed: {download.stderr.strip()[-500:]}"
            )
            return None
        title_proc = subprocess.run(
            ytdlp_cmd("--skip-download", "--get-title", "--no-warnings", url),
            capture_output=True,
            text=True,
        )
        title_proc.check_returncode()
    except subprocess.CalledProcessError as e:
        logging.error(f"Something went wrong with downloading video: {e}")
        return None
    title = title_proc.stdout.strip()[:MAX_TITLE_LEN]
    logging.info(f"video title: {title}")
    return title


def get_audio_from_video(id):
    """
    Extract audio with ffmpeg.
    :return: path to the audio file, or None on failure
    """
    audio_filename = f"/tmp/audio-{id}.mp3"
    video_filename = f"/tmp/video-{id}.mp4"
    try:
        code = subprocess.run(
            ["ffmpeg", "-i", video_filename, "-q:a", "0", "-af", "dynaudnorm",
             "-map", "a", audio_filename]
        )
        code.check_returncode()
    except subprocess.CalledProcessError as e:
        logging.error(f"Something went wrong with getting audio from video: {e}")
        return None
    return audio_filename


def audio_filename(title, suffix=""):
    """
    Build the file name Telegram shows for the audio: the first FILENAME_LEN
    characters of the video title (plus an optional suffix like "_part0") and an
    .mp3 extension. Path separators are stripped so the title can't break the
    name; we fall back to "audio" if nothing usable is left.
    """
    base = title[:FILENAME_LEN].replace("/", "_").replace("\\", "_").strip()
    return f"{base or 'audio'}{suffix}.mp3"


def calculate_file_size(filepath: str) -> float:
    """
    Returns filesize in MB
    :param filepath: path to file
    """
    return os.stat(filepath).st_size / (1024 * 1024)


def get_audio_duration(audiofile):
    """
    Returns duration of audiofile in seconds, or None on failure.
    :param audiofile: path to audiofile
    """
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", audiofile],
            capture_output=True,
            text=True,
        )
        proc.check_returncode()
        full_audio_info = json.loads(proc.stdout)
        duration = full_audio_info["format"]["duration"]
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as e:
        logging.error(f"Something went wrong with reading audio duration: {e}")
        return None
    return int(float(duration))


def divide_audio_into_parts(number_of_parts, duration, id):
    """
    Divides audio into several parts depending on duration.
    :param number_of_parts: number of parts the file should be divided into
    :param duration: duration of audio in seconds
    :param id: id of audio file
    :return: True on success, False otherwise
    """
    duration_of_one_part = duration // number_of_parts
    for i in range(0, number_of_parts):
        t1 = str(datetime.timedelta(seconds=i * int(duration_of_one_part)))
        t2 = str(datetime.timedelta(seconds=(i + 1) * int(duration_of_one_part)))
        try:
            code = subprocess.run(
                ["ffmpeg", "-i", f"/tmp/audio-{id}.mp3", "-ss", t1, "-to", t2,
                 "-c", "copy", f"/tmp/audio-{id}_part{i}.mp3"]
            )
            code.check_returncode()
        except subprocess.CalledProcessError as e:
            logging.error(f"Something went wrong with splitting audio: {e}")
            return False
    return True


def cleanup(id):
    """
    Remove every temp file belonging to this id (video, audio and part files).
    Safe to call even if some files are missing.
    """
    for path in glob.glob(f"/tmp/video-{id}*") + glob.glob(f"/tmp/audio-{id}*"):
        try:
            os.remove(path)
        except OSError as e:
            logging.error(f"Couldn't remove {path}: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_MESSAGE)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text
    user_id = update.effective_chat.id
    logging.info(f"User id: {user_id}, message: {message}")

    if not is_youtube_url(message):
        await update.message.reply_text(
            f"Couldn't find a youtube link in your message: {message}"
        )
        return

    logging.info(f"Got a youtube link {message}")
    await update.message.reply_text("Preparing video")
    link = trim_link(message)
    logging.info(f"Trimmed link to {link}")
    await process_link(update, link)


async def process_link(update: Update, link):
    """
    Run the full download → extract → split → send pipeline for one link.
    Shared by the text handler and the cookies-upload handler (which retries the
    link that was waiting on fresh cookies). Always cleans up temp files.
    """
    id = id_generator()
    try:
        # yt-dlp / ffmpeg are blocking; run them off the event loop so the bot
        # stays responsive to other users.
        try:
            video_name = await asyncio.to_thread(download_video, id, link)
        except CookiesExpired:
            # Park the link and ask for fresh cookies; handle_document resumes it.
            COOKIE_WAITERS[update.effective_chat.id] = link
            await update.message.reply_text(COOKIES_EXPIRED_MESSAGE)
            return
        if video_name is None:
            await update.message.reply_text("Couldn't download this video, sorry")
            return

        audiofile = await asyncio.to_thread(get_audio_from_video, id)
        if audiofile is None:
            await update.message.reply_text("Couldn't download this video, sorry")
            return

        filesize = calculate_file_size(audiofile)
        if filesize < MAX_TELEGRAM_AUDIO_MB:
            with open(audiofile, "rb") as f:
                await update.message.reply_audio(
                    audio=f,
                    title=video_name,
                    filename=audio_filename(video_name),
                    do_quote=True,
                )
            return

        number_of_parts = int(filesize // TARGET_CHUNK_MB + 1)
        audio_duration = await asyncio.to_thread(get_audio_duration, audiofile)
        if audio_duration is None:
            await update.message.reply_text("Couldn't download this video, sorry")
            return

        ok = await asyncio.to_thread(
            divide_audio_into_parts, number_of_parts, audio_duration, id
        )
        if not ok:
            await update.message.reply_text("Couldn't download this video, sorry")
            return

        await update.message.reply_text(
            "Sending several files. Start playing them from the last one"
        )
        # Send in reverse order: telegram plays audio from the bottom up.
        for i in range((number_of_parts - 1), -1, -1):
            with open(f"/tmp/audio-{id}_part{i}.mp3", "rb") as f:
                await update.message.reply_audio(
                    audio=f,
                    title=video_name,
                    filename=audio_filename(video_name, suffix=f"_part{i}"),
                    do_quote=True,
                )
    except Exception as e:
        logging.exception(f"Failed to process {link}: {e}")
        await update.message.reply_text("Couldn't download this video, sorry")
    finally:
        cleanup(id)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Accept a fresh cookies.txt sent as a document, validate and install it, then
    resume any link that was waiting on expired cookies for this chat.
    """
    chat_id = update.effective_chat.id
    doc = update.message.document
    if doc is None:
        return

    if not may_update_cookies(chat_id):
        await update.message.reply_text(
            "Оновлення cookies дозволено лише адміністратору бота."
        )
        return

    if not doc.file_name or not doc.file_name.lower().endswith(".txt"):
        await update.message.reply_text("Очікую cookies.txt (текстовий файл).")
        return

    # cookies.txt is a few KB; reject anything large so we don't pull a big file.
    if doc.file_size and doc.file_size > 1_000_000:
        await update.message.reply_text("Файл завеликий для cookies.txt.")
        return

    # Stage the upload in the same dir as WORK_COOKIES_FILE so the os.replace
    # below is an atomic same-filesystem rename (cross-device rename fails).
    upload_path = os.path.join(
        os.path.dirname(WORK_COOKIES_FILE) or ".",
        f"cookies-upload-{id_generator()}.txt",
    )
    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(upload_path)
    except Exception as e:
        logging.exception(f"Couldn't download uploaded cookies: {e}")
        await update.message.reply_text(
            "Не вдалося завантажити файл, спробуйте ще раз."
        )
        return

    if not is_netscape_cookies(upload_path):
        os.remove(upload_path)
        await update.message.reply_text(
            "Це не схоже на Netscape cookies.txt. Експортуйте cookies у "
            "цьому форматі та надішліть знову."
        )
        return

    try:
        # os.replace is atomic within /tmp, so yt-dlp never reads a half-written
        # cookies file even if a download starts mid-update.
        os.replace(upload_path, WORK_COOKIES_FILE)
    except OSError as e:
        logging.error(f"Couldn't install new cookies: {e}")
        await update.message.reply_text("Не вдалося зберегти cookies, спробуйте ще.")
        return

    logging.info("Cookies updated from a chat upload")
    pending = COOKIE_WAITERS.pop(chat_id, None)
    if pending:
        await update.message.reply_text("🍪 Cookies оновлено, дякую! Продовжую…")
        await process_link(update, pending)
    else:
        await update.message.reply_text("🍪 Cookies оновлено, дякую!")


def main():
    logging.info("YTAP-Bot has started")
    prepare_cookies()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.run_polling()


if __name__ == "__main__":
    main()
