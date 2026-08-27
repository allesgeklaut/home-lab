# llama.cpp build for the experimental "qwen4exp" architecture
# Fork: unslothai/llama.cpp, branch qwen4exp/qwen3.8-flash-next (PR #27742)
# Backend: Vulkan (for AMD RX 9060 XT / RDNA4)
FROM ubuntu:24.04 AS build

ARG BRANCH=qwen4exp/qwen3.8-flash-next
ARG REPO=https://github.com/unslothai/llama.cpp

ENV DEBIAN_FRONTEND=noninteractive

# Vulkan toolchain per llama.cpp docs/build.md (Linux system packages):
# libvulkan-dev glslc spirv-headers, plus mesa-vulkan-drivers & vulkan-tools
# for runtime ICD/loader and validation. Build tools + git for the compile.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake ninja-build git ccache pkg-config ca-certificates \
        libcurl4-openssl-dev \
        libvulkan-dev glslc spirv-headers \
        mesa-vulkan-drivers vulkan-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --depth 1 --branch "${BRANCH}" "${REPO}" llama.cpp

RUN cmake -S llama.cpp -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF \
        -DGGML_VULKAN=ON \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_EXAMPLES=OFF \
        -DLLAMA_BUILD_SERVER=ON \
    && cmake --build build --config Release --target llama-server -j"$(nproc)"

# Runtime image: only the server binary + vulkan loader/ICDs
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        libvulkan1 mesa-vulkan-drivers vulkan-tools \
        libcurl4 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/build/bin/llama-server /usr/local/bin/llama-server

# ubuntu:24.04 ships a built-in 'ubuntu' user (UID 1000)
USER ubuntu
WORKDIR /models
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/llama-server"]