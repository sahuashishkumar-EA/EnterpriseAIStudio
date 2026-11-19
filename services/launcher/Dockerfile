# Configuration-first platform services (C03, C05/C11, C07; C08 in Phase 2): one image,
# EAP_SERVICE selects the component. Build from the repository root:
#   docker build -f services/launcher/Dockerfile -t eap-platform .
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080 \
    EAP_SCHEMAS_DIR=/app/schemas/v1 \
    PYTHONPATH=/app/packages/eap_core:/app/packages/eap_metamodel:/app/packages/eap_store:/app/services/registry-api:/app/services/workflow:/app/services/eval-runner:/app/services/resolver:/app/services/compiler:/app/services/config-mcp:/app/services/launcher:/app/sdk/python
RUN useradd --uid 10001 --create-home app
WORKDIR /app
COPY services/launcher/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY schemas schemas
COPY db db
COPY packages/eap_core packages/eap_core
COPY packages/eap_metamodel packages/eap_metamodel
COPY packages/eap_store packages/eap_store
COPY services/registry-api services/registry-api
COPY services/workflow services/workflow
COPY services/eval-runner services/eval-runner
COPY services/resolver services/resolver
COPY services/compiler services/compiler
COPY services/config-mcp services/config-mcp
COPY services/launcher services/launcher
COPY sdk/python sdk/python
USER app
EXPOSE 8080
CMD ["python", "-m", "eap_launcher"]
