"""
wave_db.py — VCD 波形 SQLite 索引管理器
==========================================
将 VCD 解析结果存储为 SQLite 索引文件 (.wdb)，实现：
- 首次解析生成索引，后续查询直接加载
- 基于 mtime 的缓存有效性检查
- SQL 查询优化（value_at、transitions_in 等）

依赖：Python ≥ 3.8，仅使用标准库 sqlite3
"""

import sqlite3
import os
from typing import Optional, List, Tuple, Dict


class WaveDB:
    """SQLite 波形索引数据库管理器"""

    # 数据库版本号（用于兼容性检查）
    DB_VERSION = 1

    def __init__(self, vcd_path: str):
        self.vcd_path = vcd_path
        self.db_path = vcd_path + ".wdb"
        self._conn: Optional[sqlite3.Connection] = None

    def needs_rebuild(self) -> bool:
        """
        检查是否需要重建索引。
        返回 True 的条件：
          1. 索引文件不存在
          2. VCD 文件修改时间比索引文件新
          3. 索引版本不匹配
        """
        if not os.path.exists(self.db_path):
            return True

        vcd_mtime = os.path.getmtime(self.vcd_path)
        db_mtime = os.path.getmtime(self.db_path)

        if vcd_mtime > db_mtime:
            return True

        # 检查版本号
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute(
                "SELECT value FROM meta WHERE key = 'version'"
            )
            row = cursor.fetchone()
            conn.close()
            if row is None or int(row[0]) != self.DB_VERSION:
                return True
        except sqlite3.Error:
            return True

        return False

    def build_from_vcd(self, vcd_parser) -> None:
        """
        从 VCDParser 对象构建 SQLite 索引。
        表结构：
          - signals: 信号元信息（id_code, name, scope, width, var_type）
          - transitions: 跳变事件（id_code, time, value）
          - meta: 元数据（timescale, end_time, version）
        """
        # 删除旧索引文件
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

        self._conn = sqlite3.connect(self.db_path)
        cursor = self._conn.cursor()

        # ── 创建表结构 ───────────────────────────
        cursor.execute("""
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE signals (
                id_code TEXT PRIMARY KEY,
                name TEXT,
                scope TEXT,
                width INTEGER,
                var_type TEXT
            )
        """)
        cursor.execute("CREATE INDEX idx_signals_name ON signals(name)")
        cursor.execute("CREATE INDEX idx_signals_scope ON signals(scope)")

        cursor.execute("""
            CREATE TABLE transitions (
                id_code TEXT,
                time INTEGER,
                value TEXT,
                PRIMARY KEY (id_code, time)
            )
        """)
        cursor.execute("CREATE INDEX idx_transitions_time ON transitions(time)")
        cursor.execute("CREATE INDEX idx_transitions_code ON transitions(id_code)")

        # ── 写入元数据 ───────────────────────────
        cursor.execute("INSERT INTO meta VALUES ('version', ?)", (self.DB_VERSION,))
        cursor.execute("INSERT INTO meta VALUES ('timescale', ?)",
                       (vcd_parser.timescale,))
        cursor.execute("INSERT INTO meta VALUES ('end_time', ?)",
                       (vcd_parser.end_time,))
        cursor.execute("INSERT INTO meta VALUES ('vcd_path', ?)", (self.vcd_path,))

        # ── 写入信号信息（批量插入） ──────────────────
        cursor.executemany(
            "INSERT INTO signals VALUES (?, ?, ?, ?, ?)",
            [
                (sig.id_code, sig.name, sig.scope, sig.width, sig.var_type)
                for sig in vcd_parser.signals.values()
            ]
        )

        # ── 写入跳变事件 ───────────────────────────
        # 批量插入以提高性能
        # 使用 INSERT OR REPLACE 处理同一时刻同一信号的多次变化
        batch = []
        for sig in vcd_parser.signals.values():
            # 去重：同一时刻只保留最后一个值
            seen = {}
            for time, value in sig.changes:
                seen[time] = value  # 后出现的覆盖前面的
            for time, value in sorted(seen.items()):
                batch.append((sig.id_code, time, value))

        cursor.executemany(
            "INSERT OR REPLACE INTO transitions VALUES (?, ?, ?)",
            batch
        )

        self._conn.commit()
        self._conn.close()
        self._conn = None

    def load(self) -> bool:
        """
        加载现有索引文件。
        返回 True 表示加载成功。
        """
        if not os.path.exists(self.db_path):
            return False

        try:
            self._conn = sqlite3.connect(self.db_path)
            # 验证版本
            cursor = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'version'"
            )
            row = cursor.fetchone()
            if row is None or int(row[0]) != self.DB_VERSION:
                self._conn.close()
                self._conn = None
                return False
            return True
        except sqlite3.Error:
            self._conn = None
            return False

    def close(self) -> None:
        """关闭数据库连接"""
        if self._conn:
            self._conn.close()
            self._conn = None

    def get_meta(self) -> dict:
        """获取元数据（timescale, end_time）"""
        if not self._conn:
            raise RuntimeError("数据库未加载")

        cursor = self._conn.execute("SELECT key, value FROM meta")
        return {row[0]: row[1] for row in cursor.fetchall()}

    def get_signals(self) -> Dict[str, Dict]:
        """
        获取所有信号信息。
        返回 dict: id_code -> {name, scope, width, var_type, full_name}
        """
        if not self._conn:
            raise RuntimeError("数据库未加载")

        cursor = self._conn.execute(
            "SELECT id_code, name, scope, width, var_type FROM signals"
        )
        signals = {}
        for row in cursor.fetchall():
            id_code, name, scope, width, var_type = row
            full_name = f"{scope}.{name}" if scope else name
            signals[id_code] = {
                "id_code": id_code,
                "name": name,
                "scope": scope,
                "width": width,
                "var_type": var_type,
                "full_name": full_name,
            }
        return signals

    def query_value(self, id_code: str, time: int) -> Optional[str]:
        """
        查询某时刻信号值。
        使用 SQL 实现 value_at() 的二分搜索逻辑：
          SELECT value FROM transitions
          WHERE id_code=? AND time<=?
          ORDER BY time DESC LIMIT 1
        """
        if not self._conn:
            raise RuntimeError("数据库未加载")

        cursor = self._conn.execute(
            """
            SELECT value FROM transitions
            WHERE id_code = ? AND time <= ?
            ORDER BY time DESC LIMIT 1
            """,
            (id_code, time)
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def query_transitions(self, id_code: str, start: int, end: int) -> List[Tuple[int, str]]:
        """
        查询时间窗口内的跳变。
        返回 [(time, value), ...]，按时间升序排列。
        """
        if not self._conn:
            raise RuntimeError("数据库未加载")

        cursor = self._conn.execute(
            """
            SELECT time, value FROM transitions
            WHERE id_code = ? AND time >= ? AND time <= ?
            ORDER BY time ASC
            """,
            (id_code, start, end)
        )
        return cursor.fetchall()

    def query_all_transitions(self, id_code: str) -> List[Tuple[int, str]]:
        """查询信号的所有跳变事件"""
        if not self._conn:
            raise RuntimeError("数据库未加载")

        cursor = self._conn.execute(
            """
            SELECT time, value FROM transitions
            WHERE id_code = ?
            ORDER BY time ASC
            """,
            (id_code,)
        )
        return cursor.fetchall()

    def query_toggle_count(self, id_code: str, start: int = 0, end: Optional[int] = None) -> int:
        """
        计算跳变次数。
        SQL 实现：SELECT COUNT(*) FROM transitions WHERE id_code=? AND time>=? AND time<=?
        """
        if not self._conn:
            raise RuntimeError("数据库未加载")

        meta = self.get_meta()
        end = end if end is not None else int(meta.get("end_time", 0))

        cursor = self._conn.execute(
            """
            SELECT COUNT(*) FROM transitions
            WHERE id_code = ? AND time >= ? AND time <= ?
            """,
            (id_code, start, end)
        )
        count = cursor.fetchone()[0]
        return max(0, count - 1)  # 首次赋值不算跳变

    def query_times_in_range(self, start: int, end: int) -> List[int]:
        """查询时间范围内的所有时间戳（去重）"""
        if not self._conn:
            raise RuntimeError("数据库未加载")

        cursor = self._conn.execute(
            """
            SELECT DISTINCT time FROM transitions
            WHERE time >= ? AND time <= ?
            ORDER BY time ASC
            """,
            (start, end)
        )
        return [row[0] for row in cursor.fetchall()]