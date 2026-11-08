# Comf — OmniAvatar 1.3B RunPod Serverless Worker

RunPod Serverless worker for **Comf** video generation powered by:

- **ComfyUI** (`v0.30.0`)
- **OmniAvatar 1.3B** (`pretrained_models/OmniAvatar-1.3B`)
- **Wan 2.1 T2V 1.3B** (`pretrained_models/Wan2.1-T2V-1.3B`)
- **Wav2Vec2** (`pretrained_models/wav2vec2-base-960h`)
- **Custom Nodes**: `ComfyUI_OmniAvatar` (`OmniAvatar All-in-One (14B)`) and `ComfyUI-VideoHelperSuite` (`VHS_VideoCombine`)

---

## Architecture

```text
Client Application (Comf)
        │
        ▼
RunPod Serverless Endpoint
        │
        ▼
handler.py (RunPod SDK)
        │
        ▼
ComfyUI API (127.0.0.1:8188)
        │
        ▼
OmniAvatar All-in-One (14B) + VHS_VideoCombine
        │
        ▼
Persistent Models on Attached Network Volume
        │
        ▼
MP4 Video Output (Base64 and/or Network Volume Path)
```

---

## Repository Structure

```text
Infinitetalk_Runpod_hub/
├── handler.py
├── Dockerfile
├── entrypoint.sh
├── requirements.txt
├── .dockerignore
├── .gitignore
├── README.md
├── workflows/
│   └── workflow_api.json
├── custom_nodes/
│   └── ComfyUI_OmniAvatar/
│       ├── __init__.py
│       ├── node_omniavatar_allinone.py
│       ├── pyproject.toml
│       └── requirements.txt
├── OmniAvatar/
│   ├── configs/
│   │   ├── inference.yaml
│   │   └── inference_1.3B.yaml
│   ├── scripts/
│   │   └── inference.py
│   └── OmniAvatar/
└── config/
    └── model_paths.yaml
```

---

## Persistent Storage & Model Paths

Model weights (~18 GB total) are stored on the RunPod Network Volume (`cz7gbm325z`, `EU-RO-1`) under:

- **Pod mount (`/workspace`)**: `/workspace/runpod-slim/ComfyUI/models/omniavatar/`
- **Serverless mount (`/runpod-volume`)**: `/runpod-volume/runpod-slim/ComfyUI/models/omniavatar/`

Contains:
- `OmniAvatar-1.3B/pytorch_model.pt` (~355 MB) and `config.json`
- `Wan2.1-T2V-1.3B/models_t5_umt5-xxl-enc-bf16.pth` (~11.36 GB), `diffusion_pytorch_model.safetensors` (~5.68 GB), `Wan2.1_VAE.pth` (~508 MB), and `google/umt5-xxl`
- `wav2vec2-base-960h/` (~378 MB)

At startup, `entrypoint.sh` and `node_omniavatar_allinone.py` automatically detect the attached volume and configure `pretrained_models/` symbolic links without re-downloading any model weights.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `COMFYUI_DIR` | `/ComfyUI` (or `/workspace/runpod-slim/ComfyUI`) | Path to ComfyUI installation |
| `COMFYUI_HOST` | `127.0.0.1` | ComfyUI listen host |
| `COMFYUI_PORT` | `8188` | ComfyUI listen port |
| `OMNIAVATAR_MODEL_DIR` / `MODEL_DIR` | Auto-detected from `/runpod-volume` or `/workspace` | Directory containing `OmniAvatar-1.3B`, `Wan2.1-T2V-1.3B`, `wav2vec2-base-960h` |
| `WORKFLOW_PATH` | `/app/workflows/workflow_api.json` | Default ComfyUI API workflow JSON |
| `OUTPUT_DIR` | `/runpod-volume/outputs` | Directory for persisted MP4 outputs |
| `JOB_TIMEOUT` | `1200` | Maximum execution timeout per job in seconds |

---

## Serverless API Request Format

```json
{
  "input": {
    "prompt": "Young adult woman speaking naturally to the camera...",
    "image_url": "https://example.com/portrait.png",
    "audio_url": "https://example.com/speech.wav",
    "sample_steps": 20,
    "sample_text_guide_scale": 3.0,
    "sample_audio_guide_scale": 3.0,
    "teacache_thresh": 0.2,
    "seed": 123456789,
    "frame_rate": 25.0,
    "crf": 19,
    "return_base64": true,
    "network_volume": false
  }
}
```

### Supported Input Fields
- **Portrait Image**: `image_url`, `image_base64`, `image_path`, `image`, `portrait_url`, `portrait_base64`, `portrait_path`, or `portrait`
- **Speech Audio**: `audio_url`, `audio_base64`, `audio_path`, `audio`, `wav_url`, `wav_base64`, or `wav_path`
- **Generation Parameters**: `prompt`, `sample_steps` (alias `steps`), `sample_text_guide_scale` (alias `guidance_scale`), `sample_audio_guide_scale` (alias `audio_scale`), `teacache_thresh`, `seed`, `control_after_generate`, `frame_rate` (alias `fps`), `crf`
