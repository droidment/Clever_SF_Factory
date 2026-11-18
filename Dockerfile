FROM node:24-bookworm-slim AS console
WORKDIR /web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.13-slim AS runtime
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY factory/ ./factory/
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-deps .
COPY --from=console /web/dist ./web/dist/
RUN useradd --create-home --uid 10001 factory && chown -R factory:factory /app
USER factory
EXPOSE 8000
CMD ["python", "-m", "factory.server"]

FROM runtime AS test
COPY --chown=factory:factory tests/ ./tests/
COPY --chown=factory:factory scripts/gcp_proxy.py ./scripts/gcp_proxy.py
COPY --chown=factory:factory deploy/gcp/config.example.json ./deploy/gcp/config.example.json
CMD ["python", "-m", "pytest", "-q"]

FROM runtime AS production
