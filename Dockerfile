FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# config.yaml and jobfinder.db live on the /data volume; secrets come from
# the environment (docker-compose env_file), never from files in the image.
ENV JOBFINDER_CONFIG=/data/config.yaml \
    JOBFINDER_DB=/data/jobfinder.db

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

CMD ["uvicorn", "jobfinder.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
