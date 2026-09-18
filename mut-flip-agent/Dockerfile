# Base image is fixed here on purpose: HA passes its own BUILD_FROM (Alpine, no Python).
FROM docker.io/library/python:3.12-slim
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /opt/agent
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
CMD ["python", "-m", "app"]
