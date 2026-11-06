# syntax=docker/dockerfile:1

# Codex Lab: сайт, заявки, бот и студия — один образ для обеих машин. Чем он
# будет, решает переменная ROLE: web — машина сайта, admin — машина админки,
# all — всё в одном (свой компьютер). Файлы выкладки — deploy/uzcloud/.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Сначала зависимости: меняются редко, слой переиспользуется между релизами
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Код и вёрстка сайта из git. Владелец файлов — root: процесс сайта их только
# читает. Байт-код собран заранее — писать его при запуске некому
COPY app ./app
COPY site ./site
RUN python -m compileall -q app \
    && useradd --system --uid 10001 --no-create-home --home-dir /app codexlab \
    && mkdir data && chown codexlab:codexlab data

# Пишет процесс только в data/ — кэш курса доллара (rate.json). Заявки,
# тексты сайта и сайты клиентов — в PostgreSQL
USER codexlab
ENV SITE_DIR=/app/site DATA_DIR=/app/data

# Жив ли процесс: /api/health на адресе и порту из HOST и PORT
HEALTHCHECK --interval=15s --timeout=5s --start-period=150s --retries=4 \
    CMD ["python", "-c", "import os, urllib.request; host = os.environ.get('HOST', '127.0.0.1').strip() or '127.0.0.1'; host = '127.0.0.1' if host in ('0.0.0.0', '::') else host; host = f'[{host}]' if ':' in host else host; urllib.request.urlopen(f\"http://{host}:{os.environ.get('PORT', '8080')}/api/health\", timeout=4)"]

CMD ["python", "-m", "app.main"]
