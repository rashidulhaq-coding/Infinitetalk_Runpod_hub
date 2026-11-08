#!/bin/bash
set -e

COMFYUI_HOST="${COMFYUI_HOST:-127.0.0.1}"
COMFYUI_PORT="${COMFYUI_PORT:-8188}"
export COMFYUI_HOST COMFYUI_PORT

# If RunPod Serverless mounts the Network Volume at /runpod-volume and /workspace does not exist,
# symlink /workspace -> /runpod-volume so any absolute symlinks on the volume remain valid.
if [ -d "/runpod-volume" ] && [ ! -e "/workspace" ]; then
    ln -s /runpod-volume /workspace
    echo "Linked /workspace -> /runpod-volume"
fi

# Determine ComfyUI directory
if [ -z "${COMFYUI_DIR:-}" ]; then
    if [ -d "/ComfyUI" ]; then
        COMFYUI_DIR="/ComfyUI"
    elif [ -d "/workspace/runpod-slim/ComfyUI" ]; then
        COMFYUI_DIR="/workspace/runpod-slim/ComfyUI"
    elif [ -d "/runpod-volume/runpod-slim/ComfyUI" ]; then
        COMFYUI_DIR="/runpod-volume/runpod-slim/ComfyUI"
    else
        COMFYUI_DIR="/ComfyUI"
    fi
fi
export COMFYUI_DIR
echo "Using COMFYUI_DIR=${COMFYUI_DIR}"

# Locate existing OmniAvatar model weights on persistent storage
MODEL_Candidates=(
    "${OMNIAVATAR_MODEL_DIR:-}"
    "${MODEL_DIR:-}"
    "${MODEL_DIR:-}/omniavatar"
    "/runpod-volume/runpod-slim/ComfyUI/models/omniavatar"
    "/workspace/runpod-slim/ComfyUI/models/omniavatar"
    "/runpod-volume/ComfyUI/models/omniavatar"
    "/runpod-volume/models/omniavatar"
    "${COMFYUI_DIR}/models/omniavatar"
)

RESOLVED_OMNIAVATAR_MODELS=""
for cand in "${MODEL_Candidates[@]}"; do
    if [ -n "$cand" ] && [ -d "$cand/OmniAvatar-1.3B" ] && [ -d "$cand/Wan2.1-T2V-1.3B" ] && [ -d "$cand/wav2vec2-base-960h" ]; then
        RESOLVED_OMNIAVATAR_MODELS="$cand"
        break
    fi
done

if [ -n "$RESOLVED_OMNIAVATAR_MODELS" ]; then
    export OMNIAVATAR_MODEL_DIR="$RESOLVED_OMNIAVATAR_MODELS"
    echo "Found OmniAvatar models at: $OMNIAVATAR_MODEL_DIR"
    mkdir -p "${COMFYUI_DIR}/pretrained_models"
    for model_name in "OmniAvatar-1.3B" "Wan2.1-T2V-1.3B" "wav2vec2-base-960h"; do
        target_link="${COMFYUI_DIR}/pretrained_models/${model_name}"
        if [ ! -e "$target_link" ]; then
            rm -f "$target_link"
            ln -s "${OMNIAVATAR_MODEL_DIR}/${model_name}" "$target_link"
            echo "Linked ${target_link} -> ${OMNIAVATAR_MODEL_DIR}/${model_name}"
        fi
    done
else
    echo "WARNING: OmniAvatar model directories not found in standard volume paths."
fi

# Expose standard ComfyUI model folders (Flux/SDXL checkpoints, loras, controlnets, ...)
# from the Network Volume to the container's ComfyUI models directory.
VOLUME_MODELS="/runpod-volume/runpod-slim/ComfyUI/models"
if [ -d "$VOLUME_MODELS" ] && [ "$(cd "$VOLUME_MODELS" && pwd -P)" != "$(cd "${COMFYUI_DIR}/models" && pwd -P 2>/dev/null)" ]; then
    echo "Linking standard model folders from volume: $VOLUME_MODELS -> ${COMFYUI_DIR}/models"
    for sub in checkpoints loras controlnet clip clip_vision text_encoders vae unet diffusion_models style_models pulid insightface upscale_models embeddings; do
        if [ -d "$VOLUME_MODELS/$sub" ]; then
            target="${COMFYUI_DIR}/models/${sub}"
            # Skip if the destination already contains real model files
            if [ -d "$target" ] && \
               find "$target" -maxdepth 2 \( -name '*.safetensors' -o -name '*.ckpt' -o -name '*.pt' -o -name '*.pth' -o -name '*.bin' -o -name '*.onnx' \) 2>/dev/null | head -1 | grep -q .; then
                echo "  (skip $sub: destination already has model files)"
                continue
            fi
            ln -sfn "$VOLUME_MODELS/$sub" "$target"
            echo "  Linked $target -> $VOLUME_MODELS/$sub"
        fi
    done
fi

# Activate virtual environment if running from the Pod's persistent ComfyUI directory
if [ -f "${COMFYUI_DIR}/.venv-cu128/bin/activate" ]; then
    source "${COMFYUI_DIR}/.venv-cu128/bin/activate"
    echo "Activated virtualenv: ${COMFYUI_DIR}/.venv-cu128"
fi

# Start ComfyUI if not already listening on COMFYUI_PORT
if curl -s "http://${COMFYUI_HOST}:${COMFYUI_PORT}/system_stats" > /dev/null 2>&1; then
    echo "ComfyUI is already running at http://${COMFYUI_HOST}:${COMFYUI_PORT}"
else
    echo "Starting ComfyUI in ${COMFYUI_DIR}..."
    cd "${COMFYUI_DIR}"
    python main.py --listen "${COMFYUI_HOST}" --port "${COMFYUI_PORT}" --enable-cors-header &
    COMFY_PID=$!
fi

# Wait for ComfyUI readiness and custom node registration
echo "Waiting for ComfyUI readiness at http://${COMFYUI_HOST}:${COMFYUI_PORT}..."
MAX_WAIT="${COMFYUI_STARTUP_TIMEOUT:-180}"
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
    if curl -s "http://${COMFYUI_HOST}:${COMFYUI_PORT}/system_stats" > /dev/null 2>&1; then
        echo "ComfyUI /system_stats is responding (after ${WAITED}s)."
        break
    fi
    sleep 2
    WAITED=$((WAITED + 2))
done

if [ $WAITED -ge $MAX_WAIT ]; then
    echo "ERROR: ComfyUI failed to become ready within ${MAX_WAIT}s"
    exit 1
fi

# Verify OmniAvatar node registration
NODE_INFO=$(curl -s "http://${COMFYUI_HOST}:${COMFYUI_PORT}/object_info/OmniAvatar%20All-in-One%20%2814B%29" || true)
if echo "$NODE_INFO" | grep -q "OmniAvatar All-in-One (14B)"; then
    echo "Verified custom node registered: OmniAvatar All-in-One (14B)"
else
    echo "WARNING: OmniAvatar All-in-One (14B) not found in /object_info response: ${NODE_INFO}"
fi

HANDLER_SCRIPT="/app/handler.py"
if [ ! -f "$HANDLER_SCRIPT" ]; then
    HANDLER_SCRIPT="$(cd "$(dirname "$0")" && pwd)/handler.py"
fi

echo "Starting RunPod Serverless handler: ${HANDLER_SCRIPT}"
exec python -u "$HANDLER_SCRIPT"
