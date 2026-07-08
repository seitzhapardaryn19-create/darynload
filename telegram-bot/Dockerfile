# Dockerfile для деплоя на Render / Koyeb / любую платформу с Docker
FROM python:3.12-slim

# ffmpeg нужен yt-dlp для конвертации в MP3 и склейки видео+аудио
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Deno — JavaScript-движок, необходимый новым версиям yt-dlp
# для расшифровки форматов YouTube. Без него YouTube не отдаёт
# форматы и возникает ошибка "Requested format is not available".
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Обновляем yt-dlp при каждом старте контейнера:
# YouTube часто меняет защиту, и только свежая версия yt-dlp её обходит
CMD ["sh", "-c", "pip install --no-cache-dir -U yt-dlp && python bot.py"]
