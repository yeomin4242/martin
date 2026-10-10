# Reproducible Linux environment for the PGO/BOLT experiment. This is a build
# environment, not the production image; production still copies CI artifacts.
FROM rust:1.99-bookworm@sha256:114c7a4425406451c2866b6aafe69fe29b1b298832db1277d411ac73c82d04d6 AS rust
RUN rustup component add llvm-tools-preview \
    && cargo install cargo-pgo --version 0.3.0 --locked

FROM node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS node

FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential ca-certificates curl git pkg-config python3 sqlite3 \
       libfreetype-dev libfontconfig1-dev \
    && rm -rf /var/lib/apt/lists/*
COPY --from=rust /usr/local/rustup /usr/local/rustup
COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=node /usr/local/ /usr/local/
COPY .github/files/install-bolt.sh /tmp/install-bolt.sh
RUN bash /tmp/install-bolt.sh 23 \
    && rm /tmp/install-bolt.sh \
    && rm -rf /var/lib/apt/lists/*
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo
ENV PATH=/usr/local/cargo/bin:$PATH
ARG TARGETARCH
RUN case "$TARGETARCH" in \
      amd64) oha_arch=amd64 ;; \
      arm64) oha_arch=arm64 ;; \
      *) echo "Unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl --fail --location --retry 3 \
       "https://github.com/hatoo/oha/releases/download/v1.16.0/oha-linux-${oha_arch}" \
       --output /usr/local/bin/oha \
    && chmod +x /usr/local/bin/oha \
    && git config --global --add safe.directory /work
WORKDIR /work
ENTRYPOINT ["python3", "tests/bench/pgo.py"]
