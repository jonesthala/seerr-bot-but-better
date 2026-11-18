FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
# /data belongs to nobody, so a named volume mounted there is writable for the bot
RUN pip install --no-cache-dir -r requirements.txt \
    && mkdir -p /data && chown nobody:nogroup /data
COPY bot ./bot

USER nobody
CMD ["python", "-m", "bot"]
