# Build from the repository root:  docker build -f services/chat/Dockerfile .
FROM python:3.12-slim AS build
WORKDIR /src
COPY packages/eap_core packages/eap_core
COPY services/chat services/chat
RUN pip wheel --no-cache-dir --wheel-dir /wheels ./packages/eap_core ./services/chat

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080
RUN useradd --uid 10001 --create-home app
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
USER app
WORKDIR /home/app
EXPOSE 8080
CMD ["eap-chat"]
