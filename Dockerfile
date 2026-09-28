FROM python:3.13-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && useradd --create-home --uid 10001 monitor \
    && mkdir /app/data \
    && chown monitor:monitor /app/data
COPY monitor.py .
USER monitor
CMD ["python", "monitor.py"]
