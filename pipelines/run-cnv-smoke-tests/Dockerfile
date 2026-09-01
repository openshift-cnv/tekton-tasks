FROM quay.io/centos/centos:stream10

# Add EPEL repository
RUN dnf install -y --nodocs --setopt=install_weak_deps=False \
    epel-release jq git openssl \
    && dnf clean all \
    && rm -rfv \
    /var/cache/dnf \
    /var/lib/dnf \
    /var/log/dnf.* \
    /var/log/hawkey.log \
    /var/cache/ldconfig

# Add uv package manager
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Add yq
COPY --from=docker.io/mikefarah/yq:latest-githubaction /usr/bin/yq /usr/bin/yq

WORKDIR /app

COPY . .

RUN groupadd -g 1000 app && \
    useradd -m -u 1000 -g 1000 -s /bin/bash app && \
    chown -R app:app /app

USER app

RUN uv sync --frozen

CMD ["uv", "run", "gangway.py", "--help"]
