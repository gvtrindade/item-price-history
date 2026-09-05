FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install runtime deps first for better layer caching.
COPY requirements.txt ./

# openlibrary-client==0.0.30 pins six/requests/urllib3 versions that are broken
# on modern Python and conflict with requirements.txt in a single resolve step.
# Install it first, then let requirements.txt override the stale pins
# (pip warnings about the conflict are expected). See README.
RUN pip install --no-cache-dir "openlibrary-client==0.0.30" \
    && pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Run as non-root.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=4).getcode() == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
