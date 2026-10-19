# Crypto Desk image. The bot, the watchdog and the dashboard all run from it
# (see docker-compose.yml). Build + start everything with:
#     docker compose up -d --build
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Libraries first, so changing the code doesn't re-download them.
COPY requirements.txt .
RUN pip install -r requirements.txt

# An ordinary user to run the bot (never root). The entrypoint hands the
# data/logs/reports folders to this user, then switches to it.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin desk

COPY . .
RUN mkdir -p data logs reports && chown -R desk:desk data logs reports

ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
CMD ["python", "-m", "desk.main"]
