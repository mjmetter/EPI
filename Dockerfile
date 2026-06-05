# Stage 1: generate static HTML reports
FROM python:3.11-slim AS builder

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir -e .

RUN generate-productivity-report --output-dir /output && \
    cp /output/productivity.html /output/index.html


# Stage 2: serve the static files with nginx
FROM nginx:alpine

COPY --from=builder /output /usr/share/nginx/html

# Runtime env vars — values are injected into every HTML file at container start.
# LITELLM_API_KEY  : API key for the LiteLLM proxy (used by the in-page AI chat)
# LITELLM_BASE_URL : Base URL of the LiteLLM proxy (e.g. https://litellm.example.com)
# CURSOR_API_KEY   : Cursor API key (available to any future tooling; not used in the HTML)
ENV LITELLM_API_KEY="" \
    LITELLM_BASE_URL="" \
    CURSOR_API_KEY=""

# Entrypoint: replace placeholders in every HTML file with the runtime env var values,
# then hand off to the default nginx entrypoint.
RUN printf '%s\n' \
    '#!/bin/sh' \
    'set -e' \
    'HTML_DIR=/usr/share/nginx/html' \
    'find "$HTML_DIR" -name "*.html" | while read -r f; do' \
    '  sed -i "s|__LITELLM_API_KEY__|${LITELLM_API_KEY}|g; s|__LITELLM_BASE_URL__|${LITELLM_BASE_URL}|g" "$f"' \
    'done' \
    'exec /docker-entrypoint.sh "$@"' \
    > /docker-entrypoint-epi.sh && chmod +x /docker-entrypoint-epi.sh

ENTRYPOINT ["/docker-entrypoint-epi.sh"]
CMD ["nginx", "-g", "daemon off;"]

EXPOSE 80
