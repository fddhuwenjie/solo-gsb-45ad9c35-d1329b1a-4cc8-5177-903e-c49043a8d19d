"""谱例（速度图 + 录音锚点）的 SQLite 持久化层。

- 谱例：PPQ、拍号表、速度点表（JSON）、修订号；
- 锚点：录入位置（及展开的绝对 tick）、锁定标志、保存的秒数、
  录入时该位置一 tick 的时长（秒，作为锁定锚点的漂移容忍度）。

连接使用自动提交模式；写操作经 :meth:`ScoreStore.write_tx` 显式开启
``BEGIN IMMEDIATE`` 事务：读取当前修订、校验、更新在同一事务内完成，
保证“一次更新速度图、未锁定锚点秒数和修订号”的原子性——事务内任何
异常都会整体回滚，已存数据不变。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

_DEFAULT_DB_PATH = os.path.join("data", "scores.db")


def db_path_from_env() -> str:
    """数据库文件路径：环境变量 ``TEMPO_MAP_DB_PATH``，默认 ``./data/scores.db``。"""
    return os.environ.get("TEMPO_MAP_DB_PATH", _DEFAULT_DB_PATH)


class ScoreStore:
    """谱例/锚点的 SQLite 存取。单连接 + 可重入锁，写事务串行化。"""

    def __init__(self, path: str | None = None):
        self.path = path or db_path_from_env()
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        # 自动提交；写事务由 write_tx 显式管理（BEGIN IMMEDIATE ... COMMIT）。
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS scores (
                    id TEXT PRIMARY KEY,
                    ppq INTEGER NOT NULL,
                    time_signatures TEXT NOT NULL,
                    tempo_points TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS anchors (
                    score_id TEXT NOT NULL REFERENCES scores(id) ON DELETE CASCADE,
                    idx INTEGER NOT NULL,
                    position TEXT NOT NULL,
                    tick INTEGER NOT NULL,
                    locked INTEGER NOT NULL,
                    seconds REAL NOT NULL,
                    tick_seconds REAL NOT NULL,
                    PRIMARY KEY (score_id, idx)
                );
                """
            )

    @contextmanager
    def write_tx(self) -> Iterator[None]:
        """写事务：BEGIN IMMEDIATE 进入，正常结束 COMMIT，异常整体 ROLLBACK。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def create_score(
        self,
        *,
        ppq: int,
        time_signatures: list[dict],
        tempo_points: list[dict],
        anchors: list[dict],
    ) -> dict:
        """新建谱例（修订号 0）及其锚点，返回完整谱例。"""
        score_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        with self.write_tx():
            self._conn.execute(
                "INSERT INTO scores"
                " (id, ppq, time_signatures, tempo_points, revision, created_at)"
                " VALUES (?, ?, ?, ?, 0, ?)",
                (
                    score_id,
                    ppq,
                    json.dumps(time_signatures),
                    json.dumps(tempo_points),
                    created_at,
                ),
            )
            self._conn.executemany(
                "INSERT INTO anchors"
                " (score_id, idx, position, tick, locked, seconds, tick_seconds)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        score_id,
                        a["index"],
                        a["position"],
                        a["tick"],
                        int(a["locked"]),
                        a["seconds"],
                        a["tick_seconds"],
                    )
                    for a in anchors
                ],
            )
        score = self.get_score(score_id)
        assert score is not None
        return score

    def apply_publish(
        self,
        score_id: str,
        *,
        tempo_points: list[dict],
        anchor_seconds: dict[int, float],
    ) -> None:
        """发布落库（须在 write_tx 内调用）：一次更新速度点表、修订号 +1
        以及未锁定锚点的秒数；锁定锚点的秒数不在更新之列。"""
        cur = self._conn.execute(
            "UPDATE scores SET tempo_points = ?, revision = revision + 1"
            " WHERE id = ?",
            (json.dumps(tempo_points), score_id),
        )
        assert cur.rowcount == 1
        self._conn.executemany(
            "UPDATE anchors SET seconds = ? WHERE score_id = ? AND idx = ?",
            [(sec, score_id, idx) for idx, sec in anchor_seconds.items()],
        )

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def get_score(self, score_id: str) -> dict | None:
        """按 ID 读取谱例（含锚点，按录入顺序），不存在返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM scores WHERE id = ?", (score_id,)
            ).fetchone()
            if row is None:
                return None
            anchor_rows = self._conn.execute(
                "SELECT idx, position, tick, locked, seconds, tick_seconds"
                " FROM anchors WHERE score_id = ? ORDER BY idx",
                (score_id,),
            ).fetchall()
        return self._row_to_score(row, anchor_rows)

    @staticmethod
    def _row_to_score(row: sqlite3.Row, anchor_rows: list[sqlite3.Row]) -> dict:
        return {
            "score_id": row["id"],
            "ppq": row["ppq"],
            "time_signatures": json.loads(row["time_signatures"]),
            "tempo_points": json.loads(row["tempo_points"]),
            "revision": row["revision"],
            "created_at": row["created_at"],
            "anchors": [
                {
                    "index": a["idx"],
                    "position": a["position"],
                    "tick": a["tick"],
                    "locked": bool(a["locked"]),
                    "seconds": a["seconds"],
                    "tick_seconds": a["tick_seconds"],
                }
                for a in anchor_rows
            ],
        }


_store: ScoreStore | None = None
_store_lock = threading.Lock()


def get_store() -> ScoreStore:
    """进程级单例（惰性创建，路径取自 TEMPO_MAP_DB_PATH）。"""
    global _store
    with _store_lock:
        if _store is None:
            _store = ScoreStore()
        return _store
