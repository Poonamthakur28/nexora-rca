# syntax=docker/dockerfile:1
# Nexora RCA engine: builds the UI, then serves API + UI from one container.

FROM node:22-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN --mount=type=secret,id=pipcert,required=false \
    if [ -f /run/secrets/pipcert ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/pipcert; fi; \
    npm ci --silent
COPY ui/ ./
RUN npx vite build

FROM python:3.12-slim
WORKDIR /srv/api
COPY api/requirements.txt .
RUN --mount=type=secret,id=pipcert,required=false \
    if [ -f /run/secrets/pipcert ]; then export PIP_CERT=/run/secrets/pipcert; fi; \
    pip install --no-cache-dir -r requirements.txt
COPY api/app ./app
COPY --from=ui /ui/dist /srv/ui/dist
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
