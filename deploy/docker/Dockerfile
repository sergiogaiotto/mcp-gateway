# syntax=docker/dockerfile:1.7
#
# Imagens do RaPA, construídas da raiz do repositório:
#
#   docker buildx build -f deploy/docker/Dockerfile --target controle -t rapa-controle .
#   docker buildx build -f deploy/docker/Dockerfile --target executor -t rapa-executor .
#
# - controle: o plano de controle (python -m rapa.server, a migração e a linha de comando de
#   administração), com todos os extras e o mesmo navegador do executor (o Chromium do Debian,
#   as fontes e o ffmpeg do vídeo da falha) para o lançador e o host de sessão locais
#   (RAPA_EXECUTOR=local e RAPA_SESSOES_EXECUTOR=local, fora do cluster, com
#   RAPA_CHROMIUM_PATH=/usr/bin/chromium; no cluster, as execuções e as sessões rodam em pods
#   com a imagem do executor).
# - executor: o pod de uso único de cada execução e de cada sessão de gravação (só o extra
#   runner), com o Chromium do Debian, as fontes, o fuso e o ffmpeg do vídeo da falha.
#
# As duas rodam como o usuário 10001 (sem shell de login), com HOME=/tmp, sem escrever
# bytecode e com o sistema de arquivos só de leitura: o que precisa escrever (/tmp e a pasta de
# trabalho) vem de volumes do pod. Nenhum segredo entra numa camada: a configuração e os
# segredos chegam pelo ambiente e por arquivos montados na hora de rodar.
#
# Fixe a imagem base pelo digest em produção (--build-arg PYTHON_IMAGE=python:3.14-slim-trixie@sha256:...).

ARG PYTHON_IMAGE=python:3.14-slim-trixie
ARG UV_VERSION=0.11.32

# ---------------------------------------------------------------------------------------------
# Construção: o ambiente virtual em /opt/rapa, só com as dependências travadas no uv.lock.
# ---------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS construcao
ARG UV_VERSION
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/rapa \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./

FROM construcao AS dependencias-controle
# Primeiro só as dependências (a camada fica no cache enquanto o uv.lock não muda).
RUN uv sync --frozen --no-dev --no-install-project --all-extras
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --all-extras

FROM construcao AS dependencias-executor
RUN uv sync --frozen --no-dev --no-install-project --extra runner
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --extra runner

# ---------------------------------------------------------------------------------------------
# Base de execução: tini como init, o usuário 10001 e o ambiente de sempre.
# ---------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS base
ARG PYTHON_IMAGE
ARG VERSAO=0.1.0
ARG REVISAO=desconhecida
ARG FONTE=
LABEL org.opencontainers.image.version="${VERSAO}" \
      org.opencontainers.image.revision="${REVISAO}" \
      org.opencontainers.image.source="${FONTE}" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="RaPA" \
      org.opencontainers.image.base.name="${PYTHON_IMAGE}"
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10001 rapa \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /tmp \
       --shell /usr/sbin/nologin rapa
# PYTHONSAFEPATH: nada da pasta de trabalho entra no sys.path de "python -m".
ENV PATH=/opt/rapa/bin:${PATH} \
    HOME=/tmp \
    TMPDIR=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONSAFEPATH=1 \
    LANG=C.UTF-8
WORKDIR /
ENTRYPOINT ["/usr/bin/tini", "--"]

# ---------------------------------------------------------------------------------------------
# O navegador das duas imagens: o Chromium do Debian 13, as fontes e o ffmpeg do vídeo da falha.
# ---------------------------------------------------------------------------------------------
FROM base AS navegador
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-liberation fonts-noto-core ffmpeg \
    && rm -rf /var/lib/apt/lists/*
# O vídeo da falha usa o ffmpeg que o Playwright procura em
# <PLAYWRIGHT_BROWSERS_PATH>/ffmpeg-<revisão>/ffmpeg-linux: aqui, o do Debian (o link é posto
# em cada imagem, com o Playwright do ambiente dela, e conferido na construção).
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/rapa-navegadores

# ---------------------------------------------------------------------------------------------
# Plano de controle.
# ---------------------------------------------------------------------------------------------
FROM navegador AS controle
LABEL org.opencontainers.image.title="rapa-controle" \
      org.opencontainers.image.description="RaPA: plano de controle (REST, MCP, interface e despachante)"
COPY --from=dependencias-controle /opt/rapa /opt/rapa
RUN set -eu; \
    revisao="$(python -c 'import json, pathlib, playwright; p = pathlib.Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"; print(next(b["revision"] for b in json.loads(p.read_text())["browsers"] if b["name"] == "ffmpeg"))')"; \
    mkdir -p "/opt/rapa-navegadores/ffmpeg-${revisao}"; \
    ln -s /usr/bin/ffmpeg "/opt/rapa-navegadores/ffmpeg-${revisao}/ffmpeg-linux"; \
    python -c 'import sys; from rapa.runner.video import ffmpeg_installed; sys.exit(0 if ffmpeg_installed() else 1)'
# O NiceGUI guarda o que for preciso em /tmp (o resto do disco é só de leitura).
ENV NICEGUI_STORAGE_PATH=/tmp/nicegui
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "rapa.server", "--host", "0.0.0.0", "--port", "8000"]

# ---------------------------------------------------------------------------------------------
# Executor: o pod de uso único de cada execução e de cada sessão de gravação.
# ---------------------------------------------------------------------------------------------
FROM navegador AS executor
LABEL org.opencontainers.image.title="rapa-executor" \
      org.opencontainers.image.description="RaPA: executor dos robôs e host das sessões de gravação"
COPY --from=dependencias-executor /opt/rapa /opt/rapa
RUN set -eu; \
    revisao="$(python -c 'import json, pathlib, playwright; p = pathlib.Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"; print(next(b["revision"] for b in json.loads(p.read_text())["browsers"] if b["name"] == "ffmpeg"))')"; \
    mkdir -p "/opt/rapa-navegadores/ffmpeg-${revisao}"; \
    ln -s /usr/bin/ffmpeg "/opt/rapa-navegadores/ffmpeg-${revisao}/ffmpeg-linux"; \
    python -c 'import sys; from rapa.runner.video import ffmpeg_installed; sys.exit(0 if ffmpeg_installed() else 1)'
# --no-sandbox: o sandbox do Chromium precisa criar namespaces de usuário, que o perfil
# seccomp RuntimeDefault e a falta de capacidades do pod recusam (e que o gVisor não
# oferece do mesmo jeito). O isolamento do navegador é o pod de uso único: o kernel do
# gVisor (RuntimeClass gvisor), sem capacidades, sem conta de serviço montada, com o disco
# só de leitura e a saída presa pela NetworkPolicy e pela guarda de saída. Só esta imagem
# leva o argumento; a de controle nunca.
ENV RAPA_CHROMIUM_PATH=/usr/bin/chromium \
    RAPA_ARGS_NAVEGADOR=--no-sandbox
USER 10001:10001
CMD ["python", "-m", "rapa.runner.pod"]
