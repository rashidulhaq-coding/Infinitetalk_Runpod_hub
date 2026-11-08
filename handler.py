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
