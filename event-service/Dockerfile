FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8100
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --system --no-create-home agrosense
COPY agrosense_events/ agrosense_events/
USER agrosense
EXPOSE 8100
# Render and docker compose both read the port from PORT.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8100\")}/health', timeout=4)"
# One process: the open streams live in its memory.
CMD ["python", "-m", "agrosense_events"]
