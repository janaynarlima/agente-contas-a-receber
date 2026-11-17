FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# OCR local (Tesseract com idioma portugues): nenhum boleto sai da maquina.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-por tzdata \
    && rm -rf /var/lib/apt/lists/*

# Fuso de Sao Paulo: "hoje" define o dia de cada aviso. Em UTC, depois das 21h o
# agente trataria o dia seguinte como hoje.
ENV TZ=America/Sao_Paulo

RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY recebiveis ./recebiveis
COPY config.yaml Prompt ./

# Pastas de trabalho vem de bind mounts (docker-compose.yml); /data guarda o
# SQLite em volume proprio (fora do OneDrive, que pode corromper o banco durante
# a sincronizacao). Tela web e terminal (docker compose exec) usam este MESMO banco.
RUN mkdir -p /app/entrada /app/saida /app/fila_aprovacao /app/logs /app/templates /app/docs /data \
    && chown -R app:app /app /data

USER app

ENV RECEBIVEIS_CONFIG=/app/config.yaml \
    RECEBIVEIS_ENTRADA=/app/entrada \
    RECEBIVEIS_BANCO=/data/estado.db \
    RECEBIVEIS_SAIDA=/app/saida \
    RECEBIVEIS_FILA=/app/fila_aprovacao \
    RECEBIVEIS_LOGS=/app/logs \
    RECEBIVEIS_RELATORIO=/app/logs/relatorio.md \
    RECEBIVEIS_TEMPLATES=/app/templates \
    RECEBIVEIS_CLIENTES=/app/docs/clientes.csv

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/saude', timeout=3)"

# 1 worker: o processamento (SQLite + OCR) e serializado por trava em memoria.
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--timeout", "180", \
     "--control-socket", "/tmp/gunicorn.ctl", \
     "--bind", "0.0.0.0:8000", "recebiveis.web:criar_app_do_ambiente()"]
