FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin grokbot
COPY bot.py entrypoint.sh ./

# Healthy while connected to Discord (bot.py touches the file every 30s).
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os,sys,time; sys.exit(time.time() - os.path.getmtime('/tmp/grokbot-heartbeat') > 120)"

ENTRYPOINT ["sh", "/app/entrypoint.sh"]
CMD ["python", "/app/bot.py"]
