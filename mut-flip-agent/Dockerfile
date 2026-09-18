ARG BUILD_FROM=python:3.12-slim
FROM ${BUILD_FROM}
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /opt/agent
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
CMD ["python", "-m", "app"]
