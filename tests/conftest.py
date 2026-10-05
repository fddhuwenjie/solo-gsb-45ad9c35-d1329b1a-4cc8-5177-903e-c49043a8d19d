"""pytest 公共配置：把 SQLite 指到临时目录，避免污染仓库/本地数据。"""

import os
import tempfile

_DB_DIR = tempfile.mkdtemp(prefix="tempo-map-test-")
os.environ.setdefault("TEMPO_MAP_DB_PATH", os.path.join(_DB_DIR, "scores.db"))
