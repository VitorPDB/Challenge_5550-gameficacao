# ---- stage 1: build do frontend ---------------------------------------------
# Sem isto, app/static/dist/ precisava estar COMMITADO no repositório pra
# imagem ter o que servir (era o caso: 593 KB de bundle versionado).
FROM node:20-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build          # vite.config.ts emite em ../app/static/dist

# ---- stage 2: dependências Python -------------------------------------------
FROM python:3.11-slim AS deps
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# gcc/libpq-dev só são necessários pra COMPILAR psycopg2; ficavam na imagem
# final à toa. Aqui morrem junto com este stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- stage 3: runtime --------------------------------------------------------
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# Só as libs de RUNTIME: libgl1/libglib2.0-0 para o OpenCV, libpq5 para o
# psycopg2 já compilado. Sem gcc, sem headers.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deps /install /usr/local
COPY app/ ./app/
COPY wsgi.py run.py pyproject.toml ./
COPY migrations/ ./migrations/
COPY --from=frontend /app/static/dist ./app/static/dist

EXPOSE 5000

# gunicorn sobre wsgi:app — NUNCA `python run.py`, que é o servidor de
# desenvolvimento do Werkzeug.
#
# Por que gthread e não eventlet, mesmo com Flask-SocketIO:
#   - o loop de captura chama cv2/torch, que bloqueiam em C e não devolvem
#     controle ao hub do eventlet — um monkey-patch aqui congelaria o servidor
#     inteiro a cada inferência;
#   - o frontend já fala só long-polling de propósito (ver socket/client.ts),
#     que é exatamente o que async_mode=threading suporta.
# -w 1 é obrigatório, não preferência: o estado das câmeras (workers, modelos
# YOLO carregados, alertas ativos em memória) vive DENTRO do processo. Dois
# workers = duas cópias divergentes disputando a mesma webcam.
CMD ["sh", "-c", "gunicorn -k gthread -w 1 --threads ${GUNICORN_THREADS:-32} --timeout 120 -b 0.0.0.0:${PORT:-5000} wsgi:app"]
