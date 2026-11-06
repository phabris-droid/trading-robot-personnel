# Image autonome : Python, dépendances et config/ sont inclus.
# Aucune installation locale requise : `docker compose up -d --build` suffit.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DASHBOARD_HOST=0.0.0.0 \
    TZ=UTC

RUN useradd --uid 10001 --create-home trader
WORKDIR /app

# Dépendances d'abord (couche mise en cache tant que pyproject.toml ne change pas)
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

COPY config ./config
COPY scripts ./scripts
# data/ appartient à l'utilisateur du conteneur : un volume nommé hérite de ces droits
RUN mkdir -p data/audits data/backups && chown -R 10001:10001 data

USER 10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"
CMD ["python", "-m", "tradebot"]
