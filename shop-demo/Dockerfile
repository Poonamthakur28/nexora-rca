# syntax=docker/dockerfile:1
FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
# The optional `pipcert` secret supplies a CA bundle when building behind a TLS-inspecting proxy.
RUN --mount=type=secret,id=pipcert,required=false \
    if [ -f /run/secrets/pipcert ]; then export PIP_CERT=/run/secrets/pipcert; fi; \
    pip install --no-cache-dir -r requirements.txt
COPY app ./app
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
