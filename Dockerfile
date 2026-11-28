FROM python:3.13-slim
# openssh-client is only needed for sftp:// destinations
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml bandcamp_downloader.py ./
# Bandcamp answers some curl-cffi releases with a "Client Challenge" page instead of
# the site. 0.14.0 is known to get through; change this if that stops being true.
ARG CURL_CFFI_VERSION=0.14.0
RUN pip install --no-cache-dir . "curl-cffi==${CURL_CFFI_VERSION}"
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["bandcamp-dl"]
