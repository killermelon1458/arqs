FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     ARQS_CONFIG=/app/config.toml

WORKDIR /app

RUN apt-get update     && apt-get install -y --no-install-recommends tini     && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config.example.toml ./config.toml
COPY README.md ./README.md

RUN mkdir -p /data

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
