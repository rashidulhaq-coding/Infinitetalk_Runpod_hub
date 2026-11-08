FROM wlsdml1114/engui_genai-base_blackwell:1.1 AS runtime

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV COMFYUI_DIR=/ComfyUI
ENV COMFYUI_HOST=127.0.0.1
ENV COMFYUI_PORT=8188
ENV WORKFLOW_PATH=/app/workflows/workflow_api.json
ENV OUTPUT_DIR=/runpod-volume/outputs

RUN apt-get update && \
    apt-get install -y --no-install-recommends git wget curl ffmpeg libgl1 libsndfile1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /

# Clone ComfyUI and core custom nodes
RUN git clone https://github.com/comfyanonymous/ComfyUI.git /ComfyUI && \
    cd /ComfyUI && \
    pip install --no-cache-dir -r requirements.txt

RUN cd /ComfyUI/custom_nodes && \
    git clone https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git && \
    cd ComfyUI-VideoHelperSuite && \
    pip install --no-cache-dir -r requirements.txt && \
    cd /ComfyUI/custom_nodes && \
    git clone https://github.com/kijai/ComfyUI-KJNodes.git && \
    cd ComfyUI-KJNodes && \
    pip install --no-cache-dir -r requirements.txt

# Install OmniAvatar & RunPod Serverless dependencies without overwriting CUDA PyTorch
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir \
    runpod websocket-client librosa soundfile tqdm peft accelerate \
    ftfy einops omegaconf imageio imageio-ffmpeg opencv-python-headless \
    Pillow PyYAML safetensors scipy regex boto3 requests && \
    pip install --no-cache-dir --no-deps xfuser==0.4.1 yunchang==0.6.4 diffusers distvae

# Copy modified OmniAvatar custom node and standalone inference package
COPY custom_nodes/ComfyUI_OmniAvatar /ComfyUI/custom_nodes/ComfyUI_OmniAvatar
COPY OmniAvatar /ComfyUI/OmniAvatar

# Copy workflows, config, handler, and entrypoint
COPY workflows /app/workflows
COPY workflows /workflows
COPY config /app/config
COPY examples /app/examples
COPY examples /examples
COPY workflow_api.json /app/workflow_api.json
COPY handler.py /app/handler.py
COPY handler.py /handler.py
COPY entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh

WORKDIR /app
CMD ["/entrypoint.sh"]
