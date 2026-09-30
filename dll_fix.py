"""
dll_fix.py — Windows CUDA DLL path fix.
MUST be imported FIRST in start.py before any other imports.

Discovers CUDA DLLs from:
  1. nvidia-* pip packages (nvidia-cublas-cu12 etc.)
  2. CUDA_PATH environment variable (set by CUDA Toolkit installer)
  3. Common CUDA Toolkit install locations
"""

import os
import sys
from pathlib import Path


def _find_cuda_dll_dirs() -> list[str]:
    dirs = []

    # ── 1. nvidia-* pip packages ─────────────────────────────────────
    import site
    site_packages = []
    if hasattr(site, "getsitepackages"):
        site_packages = site.getsitepackages()
    if hasattr(sys, "prefix"):
        venv_site = Path(sys.prefix) / "Lib" / "site-packages"
        if venv_site.exists():
            site_packages = [str(venv_site)] + list(site_packages)

    nvidia_subpackages = [
        "nvidia/cublas/bin",
        "nvidia/cudnn/bin",
        "nvidia/cuda_runtime/bin",
        "nvidia/cufft/bin",
        "nvidia/cuda_nvrtc/bin",
        "nvidia/cusolver/bin",
        "nvidia/cusparse/bin",
        "nvidia/nccl/bin",
    ]
    for sp in site_packages:
        for subpkg in nvidia_subpackages:
            candidate = Path(sp) / subpkg.replace("/", os.sep)
            if candidate.exists():
                dirs.append(str(candidate))

    # ── 2. CUDA_PATH env var (set by CUDA Toolkit installer) ─────────
    cuda_path = os.environ.get("CUDA_PATH", "")
    if cuda_path:
        for subdir in ["bin", "lib/x64"]:
            p = Path(cuda_path) / subdir
            if p.exists():
                dirs.append(str(p))

    # ── 3. Common CUDA Toolkit locations (CUDA 12.x) ─────────────────
    common = [
        r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.4\bin",
        r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.3\bin",
        r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.2\bin",
        r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.1\bin",
        r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.0\bin",
        r"C:\Program Files\NVIDIA\CUDNN\v9\bin",
        r"C:\Program Files\NVIDIA\CUDNN\v8\bin",
    ]
    for c in common:
        if Path(c).exists():
            dirs.append(c)

    return dirs


def apply():
    if sys.platform != "win32":
        return

    dirs = _find_cuda_dll_dirs()
    if not dirs:
        return

    existing_path = os.environ.get("PATH", "")
    new_dirs = []
    for d in dirs:
        if d not in existing_path:
            try:
                os.add_dll_directory(d)   # Python 3.8+ DLL search path
                new_dirs.append(d)
            except Exception:
                pass

    if new_dirs:
        os.environ["PATH"] = os.pathsep.join(new_dirs) + os.pathsep + existing_path


# Apply immediately on import
apply()
