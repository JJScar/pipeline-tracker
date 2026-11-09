# Pipeline Tracker, team mode. See DEPLOY.md for the settings to pass in.
FROM python:3.12-slim
WORKDIR /app
COPY server.py trellus_sync.py email_sync.py daily_report.py ./
COPY static ./static
ENV AUTH_MODE=header DATA_DIR=/data PORT=8765 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765
RUN useradd --create-home tracker && mkdir -p /data && chown tracker /data
USER tracker
CMD ["python3", "server.py", "--no-browser"]
