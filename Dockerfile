# Decision Brain app image: the web app, the real-time workers and the bootstrap all run from it.
FROM python:3.10-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 FASTEMBED_CACHE_PATH=/models
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
# bake the embedding model into the image (no download at first query)
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"
COPY src ./src
COPY web ./web
COPY eval ./eval
COPY scripts ./scripts
COPY data/seed ./data/seed
COPY data/MANIFEST.json ./data/MANIFEST.json
EXPOSE 8000
CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000"]
