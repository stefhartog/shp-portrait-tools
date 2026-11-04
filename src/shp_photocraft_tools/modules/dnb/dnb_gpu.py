"""Import this before creating any onnxruntime session: makes the pip-installed CUDA 13 / cuDNN libraries loadable."""
import onnxruntime as ort

try:
    ort.preload_dlls()
except Exception as e:  # CPU fallback still works
    print(f"[dnb_gpu] CUDA preload failed, running on CPU: {e}")

PROVIDERS = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in ort.get_available_providers()]
