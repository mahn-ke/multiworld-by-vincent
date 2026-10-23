# Build context is app/ (the deploy workflow runs `docker build .` inside app/).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SKIP_REQUIREMENTS_UPDATE=1 \
    MULTIWORLD_SYSTEM_PYTHON=1 \
    MULTIWORLD_RUNTIME=/opt/archipelago \
    MULTIWORLD_WORLDS=/opt/worlds

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# MultiworldGG (latest release unless pinned) and every custom game released in the GitHub organization.
# Both are fetched whenever the image is built.
ARG MULTIWORLDGG_VERSION=latest
ARG WORLDS_ORG=mahnkepelago
COPY sources.py ./
RUN python sources.py runtime /opt/archipelago --version "$MULTIWORLDGG_VERSION" \
    && python sources.py worlds /opt/worlds --org "$WORLDS_ORG"

COPY . ./

FROM base AS test
COPY --from=node:24-slim /usr/local/bin/node /usr/local/bin/node
RUN python -m unittest test_launch test_room test_oidc test_cross_player test_hosting \
    && node --test test_site.js
# Smoke test: built-in and custom games generate together and appear in the editor.
RUN mkdir -p /tmp/smoke && cp smoke-test.yaml /tmp/smoke/Smoke.yaml \
    && MULTIWORLD_ROOT=/tmp/smoke python launch.py --generate-only \
    && cd /opt/archipelago && python /app/options_schema.py /tmp/smoke/options.json \
    && python -c "import json; games = json.load(open('/tmp/smoke/options.json'))['games']; required = {'Balatro', 'ChecksFinder', 'Pathogenic', 'Peglin', 'Super Mario Galaxy'}; assert required <= games.keys(), required - games.keys(); print(len(games), 'games, including', sorted(required))"

FROM base AS runtime
RUN useradd --system --uid 10001 --home-dir /data multiworld \
    && mkdir -p /data \
    && chown -R multiworld /data /opt/archipelago
ENV MULTIWORLD_ROOT=/data \
    MULTIWORLD_CACHE=/data/.cache \
    MULTIWORLD_TRUST_LOCALHOST=0
USER multiworld
VOLUME /data
EXPOSE 8085
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=10s --start-period=10m --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8085/api/status', timeout=8)"
CMD ["python", "launch.py", "--resume", "--allow-empty", "--web-port", "8085", "--ap-port", "38281"]
