FROM rocm/pytorch:rocm7.2.1_ubuntu24.04_py3.12_pytorch_release_2.9.1

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

ARG COMFYUI_REF=master

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone https://github.com/Comfy-Org/ComfyUI.git ComfyUI \
    && cd ComfyUI \
    && git checkout "${COMFYUI_REF}" \
    && pip install -r requirements.txt \
    && pip check

WORKDIR /opt/ComfyUI
RUN mkdir -p temp logs \
    && chown -R 1000:1000 /opt/ComfyUI
EXPOSE 8188
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188"]
