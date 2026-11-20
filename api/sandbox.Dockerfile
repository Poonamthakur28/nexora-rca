# Optional Step 6 sandbox image (FIX_SANDBOX=docker). Python + pytest only; Nexora runs it with
# --network none, --read-only, memory/CPU/process limits, and the source copy mounted at /work.
#   docker build -t nexora-fix-sandbox:local -f api/sandbox.Dockerfile api
FROM python:3.12-slim
RUN --mount=type=secret,id=pipcert,required=false \
    if [ -f /run/secrets/pipcert ]; then export PIP_CERT=/run/secrets/pipcert; fi; \
    pip install --no-cache-dir pytest==8.4.1
RUN useradd --create-home --uid 10001 sandbox
USER sandbox
WORKDIR /work
