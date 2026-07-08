"""
Telegram-бот для скачивания видео и музыки из YouTube, TikTok и Instagram.
Стек: aiogram 3 + yt-dlp. Работает через long polling.
Для бесплатного хостинга (Render/Koyeb) поднимается мини веб-сервер
со health-check эндпоинтом, чтобы сервис не считался "мёртвым".
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import uuid

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiohttp import web
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.environ.get("PORT", 8080))

# Telegram Bot API не позволяет ботам отправлять файлы больше 50 МБ
MAX_FILE_SIZE = 50 * 1024 * 1024

URL_PATTERN = re.compile(
    r"https?://(?:[\w-]+\.)*"
    r"(?:youtube\.com|youtu\.be|tiktok\.com|instagram\.com)"
    r"/\S+",
    re.IGNORECASE,
)

# Храним ссылки в памяти, чтобы не передавать длинный URL в callback_data
# (лимит callback_data — 64 байта)
pending_urls: dict[str, str] = {}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ---------- Хендлеры ----------

@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я скачиваю видео и музыку из YouTube, TikTok и Instagram.\n\n"
        "Просто отправь мне ссылку, и я предложу выбрать формат:\n"
        "— Видео (MP4)\n"
        "— Аудио (MP3)\n\n"
        "Ограничение Telegram: файл не больше 50 МБ."
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Поддерживаемые сайты: YouTube, TikTok, Instagram.\n"
        "Отправь ссылку — выбери формат — получи файл.\n"
        "Если файл больше 50 МБ, Telegram не позволит его отправить, "
        "попробуй более короткое видео."
    )


@dp.message(F.text)
async def handle_message(message: Message) -> None:
    match = URL_PATTERN.search(message.text or "")
    if not match:
        await message.answer(
            "Не вижу ссылку на YouTube, TikTok или Instagram. "
            "Отправь корректную ссылку."
        )
        return

    url = match.group(0)
    token = uuid.uuid4().hex[:16]
    pending_urls[token] = url

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Видео (MP4)", callback_data=f"v:{token}"),
                InlineKeyboardButton(text="Аудио (MP3)", callback_data=f"a:{token}"),
            ]
        ]
    )
    await message.answer("Выбери формат:", reply_markup=keyboard)


@dp.callback_query(F.data.regexp(r"^[va]:"))
async def handle_format_choice(callback: CallbackQuery) -> None:
    mode, token = callback.data.split(":", 1)
    url = pending_urls.pop(token, None)

    if url is None:
        await callback.answer("Ссылка устарела, отправь её ещё раз.", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text("Скачиваю, подожди немного...")

    try:
        file_path, title = await asyncio.to_thread(
            download_media, url, audio_only=(mode == "a")
        )
    except Exception as exc:
        logger.exception("Download failed")
        await callback.message.edit_text(
            "Не удалось скачать. Возможно, видео приватное, удалено "
            "или сайт временно блокирует запросы.\n"
            f"Ошибка: {type(exc).__name__}"
        )
        return

    try:
        if os.path.getsize(file_path) > MAX_FILE_SIZE:
            await callback.message.edit_text(
                "Файл больше 50 МБ — Telegram не позволяет ботам отправлять "
                "такие файлы. Попробуй более короткое видео или аудио-формат."
            )
            return

        media = FSInputFile(file_path)
        if mode == "a":
            await callback.message.answer_audio(media, title=title)
        else:
            await callback.message.answer_video(media, caption=title)
        await callback.message.delete()
    finally:
        shutil.rmtree(os.path.dirname(file_path), ignore_errors=True)


# ---------- Скачивание через yt-dlp ----------

def download_media(url: str, audio_only: bool) -> tuple[str, str]:
    """Скачивает медиа во временную папку. Возвращает (путь, название)."""
    tmp_dir = tempfile.mkdtemp(prefix="tgdl_")
    ydl_opts = {
        "outtmpl": os.path.join(tmp_dir, "%(title).80s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
              # Обход блокировки YouTube для датацентровых IP:
        # представляемся мобильными клиентами YouTube вместо браузера
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "web"],
            }
        },
        # Ограничиваем качество, чтобы уложиться в лимит 50 МБ
        "format": (
            "bestaudio/best"
            if audio_only
            else "best[filesize<48M]/bv*[height<=720]+ba/b[height<=720]/best"
        ),
        "merge_output_format": None if audio_only else "mp4",
    }

    if audio_only:
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title", "media")

    files = os.listdir(tmp_dir)
    if not files:
        raise RuntimeError("Файл не был скачан")

    return os.path.join(tmp_dir, files[0]), title


# ---------- Health-check сервер для бесплатного хостинга ----------

async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def start_web_server() -> None:
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Health-check server started on port %s", PORT)


# ---------- Запуск ----------

async def main() -> None:
    await start_web_server()
    logger.info("Bot polling started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
