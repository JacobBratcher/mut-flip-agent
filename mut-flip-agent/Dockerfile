# Base image is fixed here on purpose: HA passes its own BUILD_FROM (Alpine, no Python).
FROM docker.io/library/python:3.12-slim
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /opt/agent
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*
COPY app ./app
CMD ["python", "-m", "app"]
