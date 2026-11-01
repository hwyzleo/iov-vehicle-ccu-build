# ci/Dockerfile - CCU Build container for cross-compilation
#
# CR: CCU-BUILD-DSN-CR-001
#
# Provides a reproducible build environment. Base OS, compiler version and
# architecture are TBD per platform manifest (build_environment section);
# they must be confirmed and locked before the first releasable build.
#
# Build:
#   docker build -t ccu-build:0.1-draft -f ci/Dockerfile .
#
# Run:
#   docker run --rm -v "$PWD":/workspace -e CCU_ROOT=/workspace \
#     ccu-build:0.1-draft python3 -m ccu_build build --set ccu-dev-minimal

# Base image: TBD - must be confirmed per platform manifest.
# Placeholder: Ubuntu 20.04 (linux/arm64). Replace with confirmed image+digest.
FROM docker.m.daocloud.io/library/ubuntu:20.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# Install build tools (versions TBD - will be locked per platform manifest)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    cmake \
    ninja-build \
    python3 \
    python3-pip \
    python3-yaml \
    python3-jsonschema \
    python3-setuptools \
    pkg-config \
    binutils \
    make \
    file \
    openssh-client \
    rsync \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/*

# Record installed package versions for manifest
RUN dpkg -l > /tmp/installed-packages.txt && \
    gcc --version | head -1 > /tmp/toolchain-version.txt && \
    cmake --version | head -1 >> /tmp/toolchain-version.txt && \
    ninja --version >> /tmp/toolchain-version.txt

# Set up workspace
ENV CCU_ROOT=/workspace
WORKDIR /workspace

CMD ["bash"]
