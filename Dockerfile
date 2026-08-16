# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CCREPORT_ENVIRONMENT=production \
    WEBSITES_PORT=8000 \
    PORT=8000

WORKDIR /app

# WeasyPrint needs Cairo, Pango, gdk-pixbuf, libffi, fonts, and MIME data at
# runtime. Keeping this list in the base stage prevents a build that succeeds
# and then fails on the first PDF render in App Service.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fonts-dejavu-core \
        fonts-liberation \
        libffi8 \
        libcairo2 \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libgdk-pixbuf-2.0-0 \
        shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

FROM base AS builder

ARG INSTALL_PLAYWRIGHT=true

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE.md ./
COPY src/ ./src/
RUN python -m pip install --upgrade pip wheel \
    && if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
         python -m pip install --prefix=/install '.[app,playwright]'; \
       else \
         python -m pip install --prefix=/install '.[app]'; \
       fi \
    && if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
         /install/bin/playwright install chromium; \
       else \
         mkdir -p /root/.cache/ms-playwright; \
       fi

FROM base AS runtime

ARG INSTALL_PLAYWRIGHT=true
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY --from=builder /install /usr/local
COPY --from=builder /root/.cache/ms-playwright /ms-playwright
COPY src/ ./src/
COPY pyproject.toml README.md LICENSE.md ./
RUN python -m pip install --no-deps -e . \
    && addgroup --system ccreport \
    && adduser --system --ingroup ccreport --home /home/ccreport ccreport \
    && chown -R ccreport:ccreport /app /home/ccreport /ms-playwright

USER ccreport
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz >/dev/null || exit 1

# ccreport.web.app exposes a module-level `app` created by create_app().
CMD ["uvicorn", "ccreport.web.app:app", "--host", "0.0.0.0", "--port", "8000"]

# The image the test suite runs in: the runtime image plus test dependencies and
# nothing else. Tests therefore exercise the same WeasyPrint, the same system
# libraries and the same Python as production, which is the only way a rendering
# regression shows up before a faculty member finds it.
FROM runtime AS test

USER root
COPY pyproject.toml README.md LICENSE.md ./
RUN python -m pip install --no-cache-dir '.[test]'
USER ccreport

ENV CCREPORT_ENVIRONMENT=test
CMD ["pytest", "-p", "no:cacheprovider", "tests/unit", "tests/render"]
