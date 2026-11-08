import base64
import binascii
import copy
import json
import logging
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import runpod
import websocket

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("omniavatar-handler")

COMFYUI_HOST = os.getenv("COMFYUI_HOST", os.getenv("SERVER_ADDRESS", "127.0.0.1"))
COMFYUI_PORT = int(os.getenv("COMFYUI_PORT", "8188"))
JOB_TIMEOUT = int(os.getenv("JOB_TIMEOUT", "1200"))

DEFAULT_PROMPT = (
    "Young adult woman with long dark hair and a premium casual blouse, speaking naturally "
    "to the camera. Maintain the exact identity and appearance of the reference image. "
    "Natural lip synchronization, subtle facial expressions, gentle blinking, slight head "
    "movements, minimal shoulder movement, relaxed expression, static camera, soft studio "
    "lighting, realistic skin texture, clean background, realistic commercial video."
)


def resolve_comfyui_dir():
    env_dir = os.getenv("COMFYUI_DIR")
    if env_dir and os.path.isdir(env_dir):
        return os.path.abspath(env_dir)
    for candidate in (
        "/workspace/runpod-slim/ComfyUI",
        "/runpod-volume/runpod-slim/ComfyUI",
        "/ComfyUI",
    ):
        if os.path.isdir(candidate):
            return candidate
    return "/workspace/runpod-slim/ComfyUI"


def resolve_workflow_path():
    env_path = os.getenv("WORKFLOW_PATH")
    if env_path and os.path.isfile(env_path):
        return os.path.abspath(env_path)
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        os.path.join(here, "workflows", "workflow_api.json"),
        os.path.join(here, "workflow_api.json"),
        "/app/workflows/workflow_api.json",
        "/workflows/workflow_api.json",
        "/workspace/Infinitetalk_Runpod_hub/workflows/workflow_api.json",
        "/workspace/runpod-slim/workflow_api.json",
    ):
        if os.path.isfile(candidate):
            return candidate
    raise FileNotFoundError("Could not locate workflow_api.json")


def resolve_output_dir(comfyui_dir):
    env_out = os.getenv("OUTPUT_DIR")
    if env_out:
        return env_out
    if os.path.isdir("/runpod-volume"):
        return "/runpod-volume/outputs"
    return os.path.join(comfyui_dir, "output")


def truncate_for_log(val, max_len=60):
    if not isinstance(val, str):
        return val
    if len(val) <= max_len:
        return val
    return f"{val[:max_len]}... ({len(val)} chars)"


def download_from_url(url, dest_path, timeout=60):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Comf-OmniAvatar-Worker/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest_path, "wb") as f:
        shutil.copyfileobj(resp, f)
    if not os.path.isfile(dest_path) or os.path.getsize(dest_path) == 0:
        raise ValueError(f"Downloaded file is empty: {url}")
    return dest_path


def decode_base64_to_file(b64_str, dest_path):
    if "," in b64_str and b64_str.strip().startswith("data:"):
        b64_str = b64_str.split(",", 1)[1]
    try:
        raw = base64.b64decode(b64_str, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Invalid base64 input: {exc}") from exc
    if not raw:
        raise ValueError("Decoded base64 data is empty")
    with open(dest_path, "wb") as f:
        f.write(raw)
    return dest_path


def stage_media_input(job_input, keys_prefix, dest_dir, dest_filename, fallback_candidates):
    """
    Resolve an input media file from URL, base64, local path, or fallback example,
    and copy/write it into ComfyUI's input directory under a job-unique filename.
    """
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, dest_filename)

    for prefix in keys_prefix:
        url_key = f"{prefix}_url"
        b64_key = f"{prefix}_base64"
        path_key = f"{prefix}_path"

        if job_input.get(url_key):
            val = job_input[url_key]
            logger.info("Downloading %s from URL: %s", prefix, val)
            return download_from_url(val, dest_path)

        if job_input.get(b64_key):
            logger.info("Decoding %s from base64", prefix)
            return decode_base64_to_file(job_input[b64_key], dest_path)

        if job_input.get(path_key):
            val = job_input[path_key]
            if not os.path.isfile(val):
                # Check inside ComfyUI input dir as well
                alt = os.path.join(dest_dir, val)
                if os.path.isfile(alt):
                    val = alt
                else:
                    raise FileNotFoundError(f"Input file not found for {path_key}: {val}")
            if os.path.abspath(val) != os.path.abspath(dest_path):
                shutil.copy2(val, dest_path)
            return dest_path

        if job_input.get(prefix) and isinstance(job_input[prefix], str):
            val = job_input[prefix].strip()
            if val.startswith(("http://", "https://")):
                logger.info("Downloading %s from URL: %s", prefix, val)
                return download_from_url(val, dest_path)
            if os.path.isfile(val):
                if os.path.abspath(val) != os.path.abspath(dest_path):
                    shutil.copy2(val, dest_path)
                return dest_path
            alt = os.path.join(dest_dir, val)
            if os.path.isfile(alt):
                if os.path.abspath(alt) != os.path.abspath(dest_path):
                    shutil.copy2(alt, dest_path)
                return dest_path
            # Otherwise treat as base64
            logger.info("Decoding %s as base64", prefix)
            return decode_base64_to_file(val, dest_path)

    for candidate in fallback_candidates:
        if candidate and os.path.isfile(candidate):
            logger.info("Using default %s file: %s", keys_prefix[0], candidate)
            if os.path.abspath(candidate) != os.path.abspath(dest_path):
                shutil.copy2(candidate, dest_path)
            return dest_path

    raise ValueError(
        f"Missing required {keys_prefix[0]} input. Provide one of: "
        + ", ".join(
            [f"{p}_url, {p}_base64, {p}_path, {p}" for p in keys_prefix]
        )
    )


def wait_for_comfyui(timeout=180):
    url = f"http://{COMFYUI_HOST}:{COMFYUI_PORT}/system_stats"
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return True
        except Exception as exc:
            last_err = exc
        time.sleep(2)
    raise RuntimeError(f"ComfyUI server at {url} not reachable within {timeout}s: {last_err}")


def queue_prompt(prompt_graph, client_id):
    url = f"http://{COMFYUI_HOST}:{COMFYUI_PORT}/prompt"
    payload = json.dumps({"prompt": prompt_graph, "client_id": client_id}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ComfyUI /prompt HTTP {exc.code}: {body}") from exc


def get_history(prompt_id):
    url = f"http://{COMFYUI_HOST}:{COMFYUI_PORT}/history/{prompt_id}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def execute_workflow(job, prompt_graph, client_id, timeout=JOB_TIMEOUT):
    ws_url = f"ws://{COMFYUI_HOST}:{COMFYUI_PORT}/ws?clientId={client_id}"
    ws = websocket.WebSocket()
    ws.settimeout(10)
    ws.connect(ws_url)

    try:
        queued = queue_prompt(prompt_graph, client_id)
        prompt_id = queued["prompt_id"]
        logger.info("Queued ComfyUI prompt_id=%s", prompt_id)

        start_time = time.time()
        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                raise TimeoutError(f"ComfyUI execution timed out after {timeout}s (prompt_id={prompt_id})")

            try:
                raw_msg = ws.recv()
                if isinstance(raw_msg, str):
                    msg = json.loads(raw_msg)
                    msg_type = msg.get("type")
                    data = msg.get("data", {})

                    if msg_type == "executing" and data.get("prompt_id") == prompt_id:
                        node = data.get("node")
                        if node is None:
                            logger.info("ComfyUI execution finished for prompt_id=%s", prompt_id)
                            break
                        logger.info("Executing node %s (prompt_id=%s)", node, prompt_id)
                        if os.getenv("RUNPOD_WEBHOOK_POST_OUTPUT") or os.getenv("RUNPOD_ENDPOINT_ID"):
                            try:
                                runpod.serverless.progress_update(job, f"Executing node {node}")
                            except Exception:
                                pass

                    elif msg_type == "execution_error" and data.get("prompt_id") == prompt_id:
                        err_msg = data.get("exception_message", "Unknown ComfyUI execution error")
                        node_type = data.get("node_type", data.get("node_id", "unknown"))
                        raise RuntimeError(f"ComfyUI error in {node_type}: {err_msg}")
            except websocket.WebSocketTimeoutException:
                pass

            hist = get_history(prompt_id)
            if prompt_id in hist:
                status = hist[prompt_id].get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI execution failed: {json.dumps(status.get('messages', []))}")
                if status.get("completed", False):
                    break
    finally:
        try:
            ws.close()
        except Exception:
            pass

    hist = get_history(prompt_id)
    if prompt_id not in hist:
        raise RuntimeError(f"Prompt {prompt_id} missing from ComfyUI history after completion")
    return prompt_id, hist[prompt_id]


def extract_output_video(history_entry, comfyui_dir, filename_prefix):
    outputs = history_entry.get("outputs", {})
    for node_id, node_out in outputs.items():
        if "gifs" in node_out:
            for item in node_out["gifs"]:
                fullpath = item.get("fullpath")
                if fullpath and os.path.isfile(fullpath):
                    return fullpath
                filename = item.get("filename")
                subfolder = item.get("subfolder", "")
                if filename:
                    candidate = os.path.join(comfyui_dir, "output", subfolder, filename)
                    if os.path.isfile(candidate):
                        return candidate

    output_dir = os.path.join(comfyui_dir, "output")
    if os.path.isdir(output_dir):
        matches = sorted(
            [
                os.path.join(output_dir, f)
                for f in os.listdir(output_dir)
                if f.startswith(filename_prefix) and f.endswith(".mp4")
            ],
            key=os.path.getmtime,
            reverse=True,
        )
        if matches:
            return matches[0]

    raise RuntimeError(f"No output MP4 found in ComfyUI outputs: {json.dumps(outputs)}")


def extract_output_images(history_entry, comfyui_dir):
    """Return list of absolute paths of generated images from a completed history entry."""
    outputs = history_entry.get("outputs", {})
    found = []
    for node_id, node_out in outputs.items():
        for item in node_out.get("images", []):
            fullpath = item.get("fullpath")
            if fullpath and os.path.isfile(fullpath):
                found.append(fullpath)
                continue
            filename = item.get("filename")
            if not filename:
                continue
            subfolder = item.get("subfolder", "")
            base = os.path.join(comfyui_dir, "output")
            if item.get("type") == "temp":
                base = os.path.join(comfyui_dir, "temp")
            candidate = os.path.join(base, subfolder, filename)
            if os.path.isfile(candidate):
                found.append(candidate)
    return found


MODEL_NAME_ALIASES = {
    "flux": ("flux_api.json", "flux1-dev-fp8.safetensors"),
    "flux1": ("flux_api.json", "flux1-dev-fp8.safetensors"),
    "flux-dev": ("flux_api.json", "flux1-dev-fp8.safetensors"),
    "sdxl": ("sdxl_api.json", "sdxl_base_1.0.safetensors"),
}


def resolve_image_workflow_path(job_input):
    """
    Pick an image workflow file based on the optional `model` request field.
    Returns (workflow_path, default_checkpoint).
    """
    model_key = str(job_input.get("model", "flux")).lower().strip()
    model_key = {"": "flux", "flux1-dev": "flux"}.get(model_key, model_key)
    if model_key not in MODEL_NAME_ALIASES:
        raise ValueError(
            f"Unknown image model '{model_key}'. Supported: {sorted(set(MODEL_NAME_ALIASES))}"
        )
    workflow_filename, default_checkpoint = MODEL_NAME_ALIASES[model_key]

    env_path = os.getenv("IMAGE_WORKFLOW_PATH")
    candidates = []
    if env_path:
        candidates.append(os.path.join(env_path, workflow_filename))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates += [
        os.path.join(here, "workflows", workflow_filename),
        os.path.join(here, workflow_filename),
        f"/app/workflows/{workflow_filename}",
        f"/workflows/{workflow_filename}",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate, default_checkpoint
    raise FileNotFoundError(f"Could not locate image workflow: {workflow_filename}")


def _node_ref_id(ref):
    """Extract node id string from a ComfyUI link reference like ["1", 0]."""
    if isinstance(ref, (list, tuple)) and ref:
        return str(ref[0])
    return None


def build_image_prompt_graph(base_workflow, job_input, filename_prefix, default_checkpoint):
    """
    Inject request parameters into an API-format image graph.

    Parameters are applied by following the graph's actual links (not fixed node ids),
    so it also works with a user-supplied `workflow` graph.
    """
    prompt = copy.deepcopy(base_workflow)

    sampler_id = None
    for nid, node in prompt.items():
        if node.get("class_type") == "KSampler":
            sampler_id = nid
            break

    positive_id = negative_id = latent_id = None

    if sampler_id is not None:
        s_in = prompt[sampler_id].setdefault("inputs", {})
        positive_id = _node_ref_id(s_in.get("positive"))
        negative_id = _node_ref_id(s_in.get("negative"))
        latent_id = _node_ref_id(s_in.get("latent_image"))

        if job_input.get("sampler_name"):
            s_in["sampler_name"] = str(job_input["sampler_name"])
        if job_input.get("scheduler"):
            s_in["scheduler"] = str(job_input["scheduler"])
        if job_input.get("steps") is not None:
            s_in["steps"] = int(job_input["steps"])
        if job_input.get("cfg") is not None:
            s_in["cfg"] = float(job_input["cfg"])
        if job_input.get("denoise") is not None:
            s_in["denoise"] = float(job_input["denoise"])

    seed = job_input.get("seed")
    if seed is not None and sampler_id is not None:
        prompt[sampler_id].setdefault("inputs", {})["seed"] = int(seed)

    if job_input.get("prompt") and positive_id and positive_id in prompt:
        prompt[positive_id].setdefault("inputs", {})["text"] = str(job_input["prompt"])
    negative_prompt = job_input.get("negative_prompt")
    if negative_prompt and negative_id and negative_id in prompt:
        prompt[negative_id].setdefault("inputs", {})["text"] = str(negative_prompt)

    if latent_id and latent_id in prompt:
        l_in = prompt[latent_id].setdefault("inputs", {})
        if job_input.get("width") is not None:
            l_in["width"] = int(job_input["width"])
        if job_input.get("height") is not None:
            l_in["height"] = int(job_input["height"])
        if job_input.get("batch_size") is not None:
            l_in["batch_size"] = int(job_input["batch_size"])

    # SaveImage filename prefix
    for node in prompt.values():
        if node.get("class_type") == "SaveImage":
            node.setdefault("inputs", {})["filename_prefix"] = filename_prefix

    # Checkpoint override (None => leave the workflow's own value untouched)
    checkpoint = job_input.get("checkpoint")
    if isinstance(checkpoint, str) and checkpoint.strip():
        checkpoint_value = checkpoint.strip()
    else:
        checkpoint_value = default_checkpoint
    if checkpoint_value:
        for node in prompt.values():
            if node.get("class_type") in ("CheckpointLoaderSimple", "CheckpointLoader"):
                node.setdefault("inputs", {})["ckpt_name"] = checkpoint_value

    # LoRA overrides: name / strengths, or explicit use:false to bypass the LoRA loader
    use_lora = job_input.get("use_lora")
    if use_lora is None:
        use_lora = job_input.get("lora") not in ("", None, "none", "false")
    lora_name = job_input.get("lora") or job_input.get("lora_name")

    for node in prompt.values():
        if node.get("class_type") == "LoraLoader":
            l_in = node.setdefault("inputs", {})
            if use_lora is False or use_lora in ("false", "0") or (
                isinstance(lora_name, str) and lora_name.strip().lower() in ("none", "")
            ):
                l_in["_bypass"] = True
            elif isinstance(lora_name, str) and lora_name.strip():
                l_in["lora_name"] = lora_name.strip()
            if job_input.get("lora_strength") is not None:
                strength = float(job_input["lora_strength"])
                l_in["strength_model"] = strength
                l_in["strength_clip"] = strength
            if job_input.get("lora_strength_model") is not None:
                l_in["strength_model"] = float(job_input["lora_strength_model"])
            if job_input.get("lora_strength_clip") is not None:
                l_in["strength_clip"] = float(job_input["lora_strength_clip"])

    _rewire_bypassed_loras(prompt)
    return prompt


def _rewire_bypassed_loras(prompt):
    """Remove LoRA loader nodes flagged `_bypass` and reconnect consumers to their upstream source."""
    bypassed = {}
    for nid, node in list(prompt.items()):
        if isinstance(node, dict) and node.get("inputs", {}).pop("_bypass", False):
            upstream = _node_ref_id(node.get("inputs", {}).get("model"))
            bypassed[nid] = upstream
    if not bypassed:
        return

    def _upstream_model(node_id):
        if node_id in bypassed:
            return _upstream_model(bypassed[node_id])
        return node_id

    for nid in bypassed:
        del prompt[nid]

    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs", {})
        for key, val in list(inputs.items()):
            if isinstance(val, (list, tuple)):
                source_node = _upstream_model(str(val[0]))
                if source_node != str(val[0]):
                    inputs[key] = [source_node, val[1]]


def build_prompt_graph(
    base_workflow,
    image_filename,
    audio_filename,
    filename_prefix,
    job_input,
):
    prompt = copy.deepcopy(base_workflow)

    # Node 2: LoadImage
    if "2" in prompt:
        prompt["2"].setdefault("inputs", {})["image"] = image_filename

    # Node 3: LoadAudio
    if "3" in prompt:
        prompt["3"].setdefault("inputs", {})["audio"] = audio_filename

    # Node 1: OmniAvatar All-in-One (14B)
    if "1" in prompt:
        n1 = prompt["1"].setdefault("inputs", {})
        n1["prompt"] = str(job_input.get("prompt", n1.get("prompt", DEFAULT_PROMPT)))
        n1["sample_steps"] = int(
            job_input.get(
                "sample_steps",
                job_input.get("steps", job_input.get("num_steps", n1.get("sample_steps", 20))),
            )
        )
        n1["sample_text_guide_scale"] = float(
            job_input.get(
                "sample_text_guide_scale",
                job_input.get("guidance_scale", job_input.get("cfg", n1.get("sample_text_guide_scale", 3.0))),
            )
        )
        n1["sample_audio_guide_scale"] = float(
            job_input.get(
                "sample_audio_guide_scale",
                job_input.get("audio_scale", n1.get("sample_audio_guide_scale", 3.0)),
            )
        )
        n1["teacache_thresh"] = float(
            job_input.get("teacache_thresh", n1.get("teacache_thresh", 0.2))
        )
        n1["seed"] = int(job_input.get("seed", n1.get("seed", 123456789)))
        n1["control_after_generate"] = str(
            job_input.get("control_after_generate", n1.get("control_after_generate", "fixed"))
        )

    # Node 5: VHS_VideoCombine
    if "5" in prompt:
        n5 = prompt["5"].setdefault("inputs", {})
        n5["filename_prefix"] = filename_prefix
        n5["frame_rate"] = float(
            job_input.get("frame_rate", job_input.get("fps", n5.get("frame_rate", 25.0)))
        )
        n5["crf"] = int(job_input.get("crf", n5.get("crf", 19)))
        n5["save_output"] = True
        if "audio" not in n5 or not n5["audio"]:
            n5["audio"] = ["3", 0]

    return prompt


def run_image_job(job, job_id, task_id, job_input, comfyui_dir, staged_files):
    """Run a text-to-image (or arbitrary) workflow and return image(s) as base64."""
    try:
        wait_for_comfyui(timeout=180)

        raw_workflow = job_input.get("workflow")
        if isinstance(raw_workflow, dict):
            default_checkpoint = None
            base_workflow = copy.deepcopy(raw_workflow)
            logger.info("Using user-supplied raw API workflow (nodes: %s)", list(base_workflow.keys()))
        else:
            workflow_path, default_checkpoint = resolve_image_workflow_path(job_input)
            logger.info("Using image workflow: %s", workflow_path)
            with open(workflow_path, "r", encoding="utf-8") as f:
                base_workflow = json.load(f)

        filename_prefix = str(job_input.get("filename_prefix") or f"image_{task_id}").strip()
        prompt_graph = build_image_prompt_graph(
            base_workflow=base_workflow,
            job_input=job_input,
            filename_prefix=filename_prefix,
            default_checkpoint=default_checkpoint,
        )

        client_id = str(uuid.uuid4())
        timeout = int(job_input.get("timeout") or JOB_TIMEOUT)
        prompt_id, history_entry = execute_workflow(
            job=job,
            prompt_graph=prompt_graph,
            client_id=client_id,
            timeout=timeout,
        )

        use_network_volume = bool(job_input.get("network_volume", False))
        return_base64 = bool(job_input.get("return_base64", not use_network_volume))
        output_dir = resolve_output_dir(comfyui_dir)
        result = {"status": "COMPLETED", "prompt_id": prompt_id}

        image_paths = extract_output_images(history_entry, comfyui_dir)
        if image_paths:
            saved = []
            if use_network_volume or os.getenv("OUTPUT_DIR"):
                os.makedirs(output_dir, exist_ok=True)
            for src_path in image_paths:
                dest_path = os.path.join(output_dir, os.path.basename(src_path))
                if os.path.abspath(src_path) != os.path.abspath(dest_path):
                    shutil.copy2(src_path, dest_path)
                saved.append(dest_path)
            logger.info("Generated %d image(s): %s", len(saved), [os.path.basename(p) for p in saved])
            result["file_size"] = sum(os.path.getsize(p) for p in saved)
            result["filenames"] = [os.path.basename(p) for p in saved]
            result["output_files"] = saved
            if return_base64:
                encoded = []
                for p in saved:
                    with open(p, "rb") as f:
                        encoded.append(base64.b64encode(f.read()).decode("utf-8"))
                result["images"] = encoded
            return result

        # No image outputs -> fall back to any video outputs (arbitrary user workflows)
        video_path = extract_output_video(
            history_entry=history_entry,
            comfyui_dir=comfyui_dir,
            filename_prefix=filename_prefix,
        )
        file_size = os.path.getsize(video_path)
        if file_size == 0:
            raise RuntimeError(f"Generated file is empty: {video_path}")
        logger.info("Generated file: %s (%d bytes)", video_path, file_size)
        result["filename"] = os.path.basename(video_path)
        result["video_path"] = video_path
        result["file_size"] = file_size
        if return_base64:
            with open(video_path, "rb") as vf:
                result["video"] = base64.b64encode(vf.read()).decode("utf-8")
        return result

    except Exception as exc:
        logger.exception("Image job %s failed: %s", job_id, exc)
        return {
            "status": "FAILED",
            "error": str(exc),
        }
    finally:
        for path in staged_files:
            try:
                if path and os.path.isfile(path):
                    os.remove(path)
            except Exception:
                pass


def handler(job):
    job_id = str(job.get("id", uuid.uuid4().hex))
    task_id = f"{job_id}_{uuid.uuid4().hex[:8]}"
    job_input = job.get("input") or {}

    if not isinstance(job_input, dict):
        return {"error": "Invalid job input: expected a JSON object"}

    safe_log = {k: truncate_for_log(v) for k, v in job_input.items()}
    logger.info("Starting job %s with input: %s", job_id, safe_log)

    comfyui_dir = resolve_comfyui_dir()
    comfy_input_dir = os.path.join(comfyui_dir, "input")
    os.makedirs(comfy_input_dir, exist_ok=True)

    staged_files = []

    task = str(job_input.get("task", "video")).lower().strip()
    if task in ("image", "img", "t2i", "txt2img") or isinstance(job_input.get("workflow"), dict):
        return run_image_job(job, job_id, task_id, job_input, comfyui_dir, staged_files)

    try:
        wait_for_comfyui(timeout=180)

        workflow_path = job_input.get("workflow_path") or resolve_workflow_path()
        with open(workflow_path, "r", encoding="utf-8") as f:
            base_workflow = json.load(f)

        here = os.path.dirname(os.path.abspath(__file__))
        image_ext = ".png"
        for k in ("image_url", "image_path", "image", "portrait_url", "portrait_path", "portrait"):
            v = job_input.get(k)
            if isinstance(v, str) and "." in os.path.basename(v.split("?")[0]):
                ext = os.path.splitext(v.split("?")[0])[1].lower()
                if ext in (".png", ".jpg", ".jpeg", ".webp"):
                    image_ext = ext
                    break

        audio_ext = ".wav"
        for k in ("audio_url", "audio_path", "audio", "wav_url", "wav_path"):
            v = job_input.get(k)
            if isinstance(v, str) and "." in os.path.basename(v.split("?")[0]):
                ext = os.path.splitext(v.split("?")[0])[1].lower()
                if ext in (".wav", ".mp3", ".flac", ".m4a", ".ogg"):
                    audio_ext = ext
                    break

        image_filename = f"omniavatar_in_{task_id}{image_ext}"
        audio_filename = f"omniavatar_in_{task_id}{audio_ext}"

        image_fallbacks = [
            os.path.join(comfy_input_dir, "ComfyUI_temp_hnddi_00002_.png"),
            os.path.join(here, "examples", "image.jpg"),
            "/examples/image.jpg",
        ]
        audio_fallbacks = [
            os.path.join(comfy_input_dir, "brightside_welcome.wav"),
            os.path.join(here, "examples", "audio.mp3"),
            "/examples/audio.mp3",
        ]

        staged_image = stage_media_input(
            job_input,
            keys_prefix=("image", "portrait"),
            dest_dir=comfy_input_dir,
            dest_filename=image_filename,
            fallback_candidates=image_fallbacks,
        )
        staged_files.append(staged_image)

        staged_audio = stage_media_input(
            job_input,
            keys_prefix=("audio", "wav"),
            dest_dir=comfy_input_dir,
            dest_filename=audio_filename,
            fallback_candidates=audio_fallbacks,
        )
        staged_files.append(staged_audio)

        filename_prefix = f"omniavatar_{task_id}"
        prompt_graph = build_prompt_graph(
            base_workflow=base_workflow,
            image_filename=image_filename,
            audio_filename=audio_filename,
            filename_prefix=filename_prefix,
            job_input=job_input,
        )

        client_id = str(uuid.uuid4())
        timeout = int(job_input.get("timeout", JOB_TIMEOUT))
        prompt_id, history_entry = execute_workflow(
            job=job,
            prompt_graph=prompt_graph,
            client_id=client_id,
            timeout=timeout,
        )

        output_video_path = extract_output_video(
            history_entry=history_entry,
            comfyui_dir=comfyui_dir,
            filename_prefix=filename_prefix,
        )
        file_size = os.path.getsize(output_video_path)
        if file_size == 0:
            raise RuntimeError(f"Generated video file is empty: {output_video_path}")

        logger.info("Generated MP4: %s (%d bytes)", output_video_path, file_size)

        use_network_volume = bool(job_input.get("network_volume", False))
        return_base64 = bool(job_input.get("return_base64", not use_network_volume))

        final_video_path = output_video_path
        output_dir = resolve_output_dir(comfyui_dir)
        if use_network_volume or os.getenv("OUTPUT_DIR"):
            os.makedirs(output_dir, exist_ok=True)
            dest_out = os.path.join(output_dir, f"{filename_prefix}.mp4")
            if os.path.abspath(output_video_path) != os.path.abspath(dest_out):
                shutil.copy2(output_video_path, dest_out)
                final_video_path = dest_out

        result = {
            "status": "COMPLETED",
            "prompt_id": prompt_id,
            "filename": os.path.basename(final_video_path),
            "video_path": final_video_path,
            "file_size": file_size,
        }

        if return_base64:
            with open(final_video_path, "rb") as vf:
                video_b64 = base64.b64encode(vf.read()).decode("utf-8")
            result["video"] = video_b64

        result["output"] = {
            "filename": result["filename"],
            "video_path": final_video_path,
            "file_size": file_size,
        }
        return result

    except Exception as exc:
        logger.exception("Job %s failed: %s", job_id, exc)
        return {
            "status": "FAILED",
            "error": str(exc),
        }
    finally:
        for path in staged_files:
            try:
                if path and os.path.isfile(path):
                    os.remove(path)
            except Exception:
                pass


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
