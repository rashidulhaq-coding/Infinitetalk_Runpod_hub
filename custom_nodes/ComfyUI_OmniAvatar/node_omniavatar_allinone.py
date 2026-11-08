# coding: utf-8
"""
ComfyUI 自定义节点：OmniAvatar All-in-One (14B)

功能概述：
1. 接收静态人像（IMAGE）、音频流（AUDIO）与文本提示（STRING）。
2. 自动将各种形式的音频（内存 buffer、MP3/WAV 文件等）转换为推理所需的 WAV。
3. 调用 GitHub 原项目的 OmniAvatar 推理脚本，生成同步口型与表情的视频。
4. 从生成的视频中提取帧，输出为 IMAGE 张量序列。

custom_nodes/
├─ ComfyUI_OmniAvatar/
│   ├─ __init__.py
│   └─ node_omniavatar_allinone.py
└─ OmniAvatar/
    ├─ configs/
    │   ├─ inference.yaml
    │   └─ inference_1.3B.yaml
    ├─ OmniAvatar/      # 原项目源码
    └─ scripts/
        └─ inference.py
放置路径：
F:\Comfyui\custom_nodes\ComfyUI_OmniAvatar\node_omniavatar_allinone.py
"""

import os
import io
import sys
import shutil
import tempfile
import random

from tqdm import tqdm
from PIL import Image
import numpy as np
import torch


# 用于读写 WAV，以及音频重采样
import soundfile as sf
import librosa


def _resolve_omni_root():
    this_dir = os.path.dirname(os.path.abspath(__file__))
    custom_nodes = os.path.abspath(os.path.join(this_dir, os.pardir))
    comfy_root = os.path.abspath(os.path.join(custom_nodes, os.pardir))
    env_root = os.environ.get("OMNIAVATAR_ROOT")
    if env_root and os.path.isdir(env_root):
        return os.path.abspath(env_root), comfy_root
    candidate = os.path.join(comfy_root, "OmniAvatar")
    if os.path.isdir(candidate):
        return candidate, comfy_root
    alt = os.path.join(custom_nodes, "OmniAvatar")
    if os.path.isdir(alt):
        return alt, comfy_root
    return candidate, comfy_root


def _ensure_pretrained_symlinks(comfy_root):
    model_names = ["OmniAvatar-1.3B", "Wan2.1-T2V-1.3B", "wav2vec2-base-960h"]
    pretrained_dir = os.path.join(comfy_root, "pretrained_models")
    if all(os.path.exists(os.path.join(pretrained_dir, m)) for m in model_names):
        return

    candidates = []
    for env_key in ("OMNIAVATAR_MODEL_DIR", "MODEL_DIR"):
        val = os.environ.get(env_key)
        if val:
            candidates.append(val)
            candidates.append(os.path.join(val, "omniavatar"))
    candidates.extend([
        os.path.join(comfy_root, "models", "omniavatar"),
        "/workspace/runpod-slim/ComfyUI/models/omniavatar",
        "/runpod-volume/runpod-slim/ComfyUI/models/omniavatar",
        "/runpod-volume/ComfyUI/models/omniavatar",
        "/runpod-volume/models/omniavatar",
        "/runpod-volume/omniavatar",
    ])

    source_dir = None
    for cand in candidates:
        if cand and all(os.path.exists(os.path.join(cand, m)) for m in model_names):
            source_dir = os.path.abspath(cand)
            break

    if not source_dir:
        return

    os.makedirs(pretrained_dir, exist_ok=True)
    for m in model_names:
        dst = os.path.join(pretrained_dir, m)
        if not os.path.exists(dst):
            if os.path.lexists(dst):
                os.unlink(dst)
            os.symlink(os.path.join(source_dir, m), dst)


#导入原项目推理管线
def _import_pipeline():
    omni_root, comfy_root = _resolve_omni_root()
    _ensure_pretrained_symlinks(comfy_root)
    # 添加脚本与源码路径
    for p in (os.path.join(omni_root, "OmniAvatar"), omni_root, os.path.join(omni_root, "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        from inference import WanInferencePipeline, save_video_as_grid_and_mp4
        from OmniAvatar.utils.args_config import parse_args
    except ImportError as e:
        raise RuntimeError(
            f"OmniAvatar import failed: {e}"
        ) from e
    return WanInferencePipeline, save_video_as_grid_and_mp4, parse_args

class OmniAvatarAllInOneNode:
    """
    ComfyUI 节点：OmniAvatar All-in-One (14B)
    -----------------------------
    输入：
      portrait : IMAGE — 静态人脸图 (H×W×3 uint8)
      audio    : AUDIO — ComfyUI 音频 buffer (np.ndarray, sr) / torch.Tensor / bytes
      prompt   : STRING — 文本提示
    可调参数：
      sample_steps            — 推理采样步数
      sample_text_guide_scale — 文本引导强度
      sample_audio_guide_scale— 音频引导强度
      teacache_thresh         — TeaCache 一级缓存阈值
      seed                    — 随机种子
      control_after_generate  — 控制模式：fixed/dynamic
    输出：
      frames : IMAGE — 张量列表 (F, C, H, W)，像素归一化到 [0,1]
    """

    CATEGORY = "OmniAvatar"
    FUNCTION = "run"
    RETURN_TYPES = ("IMAGE", )
    RETURN_NAMES = ("frames",)

    @classmethod
    def INPUT_TYPES(cls):
        """
        定义 UI 面板上的输入接口和调节参数：
        - portrait: ComfyUI IMAGE
        - audio   : ComfyUI AUDIO
        - prompt  : STRING
        - 其余为滑块或下拉菜单，可选
        """
        return {
            "required": {
                "portrait": ("IMAGE", {}),
                "audio":   ("AUDIO", {}),
                "prompt":  ("STRING", {
                    "default": "你好，欢迎体验 OmniAvatar",
                    "multiline": True
                }),
            },
            "optional": {
                "sample_steps": ("INT", {
                    "default": 20, "min": 1, "max": 100, "step": 1
                }),
                "sample_text_guide_scale": ("FLOAT", {
                    "default": 3.0, "min": 0.0, "max": 20.0, "step": 0.1
                }),
                "sample_audio_guide_scale": ("FLOAT", {
                    "default": 3.0, "min": 0.0, "max": 20.0, "step": 0.1
                }),
                "teacache_thresh": ("FLOAT", {
                    "default": 0.20, "min": 0.0, "max": 1.0, "step": 0.01
                }),
                "seed": ("INT", {
                    "default": 91123456789, "min": 0, "max": 2**64 - 1, "step": 1
                }),
                "control_after_generate": ("OPTION", {
                    "default": "fixed",
                    "options": ["fixed", "dynamic"]
                }),
            }
        }

    _shared_pipeline = None
    _shared_save_tool = None
    _shared_args = None

    def __init__(self):
        # 延迟加载管线
        """
        延迟初始化模型实例：
        防止 ComfyUI 启动时因模型加载过慢而卡顿
        """
        self._pipeline = None
        self._save_tool = None
        self._parse_args = None
        self._args = None

    def _ensure_pipeline(self):
        """
        延迟导入并实例化 OmniAvatar 推理管线
        """
        if OmniAvatarAllInOneNode._shared_pipeline is not None:
            self._pipeline = OmniAvatarAllInOneNode._shared_pipeline
            self._save_tool = OmniAvatarAllInOneNode._shared_save_tool
            self._args = OmniAvatarAllInOneNode._shared_args
            return self._pipeline, self._save_tool, self._args

        if self._pipeline is None:
            omni_root, comfy_root = _resolve_omni_root()
            _ensure_pretrained_symlinks(comfy_root)
            config_path = os.environ.get(
                "OMNIAVATAR_CONFIG",
                os.path.join(omni_root, "configs", "inference_1.3B.yaml"),
            )
            old_argv = sys.argv
            old_cwd = os.getcwd()
            try:
                os.chdir(comfy_root)
                sys.argv = ["comfyui_omniavatar", "--config", config_path]
                WanInferencePipeline, save_tool, parse_args = _import_pipeline()
                self._args = parse_args()
                os.environ.setdefault("RANK", "0")
                os.environ.setdefault("WORLD_SIZE", "1")
                os.environ.setdefault("LOCAL_RANK", "0")
                os.environ.setdefault("MASTER_ADDR", "127.0.0.1")
                os.environ.setdefault("MASTER_PORT", "29500")
                self._pipeline = WanInferencePipeline(self._args)
                self._save_tool = save_tool
                OmniAvatarAllInOneNode._shared_pipeline = self._pipeline
                OmniAvatarAllInOneNode._shared_save_tool = self._save_tool
                OmniAvatarAllInOneNode._shared_args = self._args
            finally:
                sys.argv = old_argv
                os.chdir(old_cwd)
        return self._pipeline, self._save_tool, self._args
    
    def run(
        self,
        portrait: np.ndarray,
        audio,
        prompt: str,
        sample_steps: int = 20,
        sample_text_guide_scale: float = 3.0,
        sample_audio_guide_scale: float = 3.0,
        teacache_thresh: float = 0.20,
        seed: int = 42,
        control_after_generate: str = "fixed",
    ):
        """
        主流程：
        1. 随机种子设定
        2. 保存 portrait & audio 为临时文件
        3. 调用 WanInferencePipeline.log_video 生成视频数组
        4. 写出临时 MP4，并封装帧与音频输出
        """

        # ─── 0. 随机种子 ─────────────────────────────────────
        random.seed(seed)
        torch.manual_seed(seed)
        np.random.seed(seed)

        # ─── 1. 创建临时目录 ─────────────────────────────────
        tmp = tempfile.mkdtemp(prefix="omniavatar_")
        try:
            img_path = os.path.join(tmp, "input.png")
            audio_path = os.path.join(tmp, "input.wav")

            # ─── 2. 保存人像 ─────────────────────────────────────
            if isinstance(portrait, torch.Tensor):
                portrait = portrait.detach().cpu().numpy()
                if portrait.ndim == 4:
                    portrait = portrait[0]
                portrait = np.clip(portrait * 255.0, 0, 255).astype(np.uint8)
            img = Image.fromarray(portrait)
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(img_path)
            # ─── 3. 处理音频并保存 WAV ────────────────────────────
            # 支持多种形式：tuple/torch.Tensor/bytes/文件路径
            # 保存音频为 WAV
            if isinstance(audio, dict):
                waveform = audio["waveform"]
                sr = audio.get("sample_rate", 16000)
                waveform = waveform.detach().cpu().numpy()
                waveform = np.squeeze(waveform)
                if waveform.ndim == 2 and waveform.shape[0] <= 8 and waveform.shape[0] < waveform.shape[1]:
                    waveform = waveform.T
                sf.write(audio_path, waveform, sr)
            elif isinstance(audio, tuple) and len(audio) == 2:
                sf.write(audio_path, audio[0], audio[1])
            elif isinstance(audio, torch.Tensor):
                sf.write(audio_path, audio.cpu().numpy(), 16000)
            elif isinstance(audio, (bytes, bytearray)):
                arr, sr = sf.read(io.BytesIO(audio))
                sf.write(audio_path, arr, sr)
            else:
                arr, sr = librosa.load(audio, sr=16000)
                sf.write(audio_path, arr, sr)

            # ─── 4. 获取推理管线并覆盖参数 ─────────────────────────
            pipeline, save_tool, args = self._ensure_pipeline()
            args.num_steps = sample_steps
            args.guidance_scale = sample_text_guide_scale
            args.audio_scale = sample_audio_guide_scale
            args.tea_cache_l1_thresh = teacache_thresh
            args.control_after_generate = control_after_generate
            if "inference" in sys.modules and hasattr(sys.modules["inference"], "args"):
                sys.modules["inference"].args.tea_cache_l1_thresh = teacache_thresh

            # ─── 5. 执行模型推理 ─────────────────────────────────
            # 调用原项目中的 log_video 生成 numpy 视频数组 (T, H, W, 3)
            with tqdm(total=1, desc="OmniAvatar 推理") as pbar:
                video_np = pipeline.forward(
                    prompt=prompt,
                    image_path=img_path,
                    audio_path=audio_path,
                    num_steps=sample_steps,
                    guidance_scale=sample_text_guide_scale,
                    audio_scale=sample_audio_guide_scale
                )
                pbar.update(1)

            # ─── 6. 将视频数组保存为 MP4 ───────────────────────────
            # 原项目提供 save_video_as_grid_and_mp4 工具也可直接使用
            save_tool(
                video_np,
                save_path=tmp,
                fps=args.fps,
                audio_path=audio_path,
                prefix="output"
            )
            video_path = os.path.join(tmp, "output_000_wav.mp4")

            # ─── 7. 提取帧并构建张量 ───────────────────────────────
            import imageio
            reader = imageio.get_reader(video_path, "ffmpeg")
            frames = []
            for frame in reader:  # frame: H,W,3 uint8
                t = torch.from_numpy(frame.astype(np.float32) / 255.0)  # 归一化
                frames.append(t)
            reader.close()

            if not frames:
                raise RuntimeError("未从生成视频中提取到任何帧，请检查推理是否成功。")
            frames_tensor = torch.stack(frames, dim=0)  # (F, C, H, W)

            # ─── 9. 返回 IMAGE 张量序列 与 VIDEO 对象 ─────────────
            return (frames_tensor,)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

