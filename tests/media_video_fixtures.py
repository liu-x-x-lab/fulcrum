"""只从纯色合成短片，不读取业务视频或访问外部资源。"""

from pathlib import Path
import shutil
import subprocess

import pytest


def video_bytes(directory: Path, *, sar: int = 1) -> bytes:
    binary = shutil.which("ffmpeg")
    if not binary:
        pytest.skip("Synthetic video checks require FFmpeg")
    target = directory / "synthetic.mp4"
    subprocess.run([binary, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        "color=c=red:size=96x64:rate=5:duration=0.4", "-vf", f"setsar={sar}", "-c:v", "mpeg4", "-threads", "1", str(target)],
        check=True, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return target.read_bytes()
