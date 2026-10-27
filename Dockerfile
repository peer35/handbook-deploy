FROM python:3.12-slim-bookworm AS app

# Quarto renders the site inside this container, so it ships git, rsync and Quarto too
ARG QUARTO_VERSION=1.8.26
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git rsync \
 && arch=$(dpkg --print-architecture) \
 && curl -fsSL -o /tmp/quarto.deb \
      "https://github.com/quarto-dev/quarto-cli/releases/download/v${QUARTO_VERSION}/quarto-${QUARTO_VERSION}-linux-${arch}.deb" \
 && apt-get install -y --no-install-recommends /tmp/quarto.deb \
 && rm -rf /tmp/quarto.deb /var/lib/apt/lists/* \
 && quarto --version

# Fixed uid/gid: the host directories mounted at /var/www/previews and /srv/pr-preview
# must be owned by it (setup.sh does that)
RUN groupadd --gid 10001 preview \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /srv/pr-preview/home preview

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bin/pr-preview /usr/local/bin/pr-preview
COPY pr_preview ./pr_preview

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/srv/pr-preview/home \
    LISTEN=0.0.0.0:9000
USER 10001:10001
EXPOSE 9000
CMD ["python", "-m", "pr_preview"]


# `docker build --target test .` runs the test suite
FROM app AS test
USER root
RUN pip install --no-cache-dir pytest
COPY tests ./tests
USER 10001:10001
ENV HOME=/tmp
RUN python -m pytest -q -p no:cacheprovider tests
