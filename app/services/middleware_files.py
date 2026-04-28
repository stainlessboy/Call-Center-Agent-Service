"""
Файловый I/O для интеграции с chat-middleware.

Порт `call_center_bot/helpers/sessions.py` (upload в MinIO) и
`call_center_bot/helpers/messages.py` (скачать URL → отправить в Telegram
адекватным методом по расширению).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import aiofiles
import aiohttp
from aiogram import Bot
from aiogram.types import FSInputFile

logger = logging.getLogger(__name__)


async def upload_file_to_minio(
    file_path: str,
    base_url: str,
    username: str,
    password: str,
    *,
    verify_ssl: bool = True,
) -> Optional[str]:
    """
    POST {base_url}/api/chat/upload-file/ с multipart `file`.
    Возвращает `file_path` из JSON-ответа (то самое значение, которое
    middleware ожидает как `message` в `send-message`).
    """
    auth = aiohttp.BasicAuth(username, password)
    connector = aiohttp.TCPConnector(verify_ssl=verify_ssl)
    async with aiohttp.ClientSession(auth=auth, connector=connector) as session:
        form = aiohttp.FormData()
        async with aiofiles.open(file_path, "rb") as fh:
            file_data = await fh.read()
            form.add_field("file", file_data, filename=os.path.basename(file_path))

        url = f"{base_url.rstrip('/')}/api/chat/upload-file/"
        async with session.post(url, data=form) as response:
            if response.status >= 400:
                logger.error("MinIO upload failed: HTTP %s", response.status)
                return None
            data = await response.json()
            return data.get("file_path")


async def download_and_send_to_user(
    bot: Bot,
    telegram_user_id: int,
    file_url: str,
) -> None:
    """
    Скачать `file_url` во временный файл и отправить пользователю
    адекватным методом aiogram (фото / видео / аудио / документ).
    Чеки asakabank.uz отправляем как document.
    """
    is_cheque_url = "get.asakabank.uz/" in file_url
    temp_file_path: Optional[str] = None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(file_url) as response:
                if response.status != 200:
                    logger.error("Failed to download file from %s: HTTP %s", file_url, response.status)
                    return

                content_type = response.headers.get("Content-Type", "")
                parsed_url = urlparse(file_url)
                file_extension = Path(parsed_url.path).suffix

                if not file_extension:
                    if "image" in content_type:
                        file_extension = ".jpg" if "jpeg" in content_type else ".png"
                    elif "video" in content_type:
                        file_extension = ".mp4"
                    elif "audio" in content_type:
                        file_extension = ".mp3"
                    elif "pdf" in content_type:
                        file_extension = ".pdf"
                    else:
                        file_extension = ".bin"

                if is_cheque_url and not file_extension and "image" in content_type:
                    file_extension = ".jpg"

                temp_file_path = f"/tmp/operator_file_{os.urandom(8).hex()}{file_extension}"

                async with aiofiles.open(temp_file_path, "wb") as f:
                    async for chunk in response.content.iter_chunked(8192):
                        await f.write(chunk)

        ext = file_extension.lower()
        file_input = FSInputFile(temp_file_path)

        if is_cheque_url:
            await bot.send_document(telegram_user_id, file_input)
        elif ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            await bot.send_photo(telegram_user_id, file_input)
        elif ext in (".mp4", ".avi", ".mov", ".mkv", ".webm"):
            await bot.send_video(telegram_user_id, file_input)
        elif ext in (".mp3", ".ogg", ".wav", ".m4a"):
            await bot.send_audio(telegram_user_id, file_input)
        else:
            await bot.send_document(telegram_user_id, file_input)

        logger.info("Sent operator file %s to user %s", file_url, telegram_user_id)

    except Exception as exc:
        logger.exception("Error sending file from URL %s: %s", file_url, exc)
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except OSError:
                pass
