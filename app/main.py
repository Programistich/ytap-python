#!/usr/bin/env python3

import asyncio
import datetime
import glob
import json
import logging
import os
import random
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

# Optional cookies file to get past YouTube's "confirm you're not a bot" check
# when running from a datacenter IP. Passed to yt-dlp only if the file exists,
# so the bot still works without it.
COOKIES_FILE = os.getenv("COOKIES_FILE", "/app/cookies.txt")


def ytdlp_cmd(*args):
    """
    Build a yt-dlp argv list, adding --cookies when a cookies file is present.
    """
    cmd = ["yt-dlp"]
    if os.path.isfile(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]
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
    """
    video_filename = f"/tmp/video-{id}.mp4"
    try:
        download = subprocess.run(
            ytdlp_cmd("--newline", "-f", "bestaudio[ext=m4a]", url, "-o", video_filename)
        )
        download.check_returncode()
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
    id = id_generator()

    try:
        # yt-dlp / ffmpeg are blocking; run them off the event loop so the bot
        # stays responsive to other users.
        video_name = await asyncio.to_thread(download_video, id, link)
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
                await update.message.reply_audio(audio=f, title=video_name)
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
                await update.message.reply_audio(audio=f, title=video_name)
    except Exception as e:
        logging.exception(f"Failed to process {link}: {e}")
        await update.message.reply_text("Couldn't download this video, sorry")
    finally:
        cleanup(id)


def main():
    logging.info("YTAP-Bot has started")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
