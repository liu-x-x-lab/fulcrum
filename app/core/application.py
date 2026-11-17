"""进程级应用元数据，供启动日志和页面展示共同读取。"""

from pathlib import Path

from app.core.time import now_shanghai


APP_STARTED_AT = now_shanghai()
APP_ROOT = Path(__file__).resolve().parents[2]
