"""数据库操作模块"""
import sqlite3
from datetime import datetime
from typing import Optional, List
import threading


class Database:
    """管理扫描历史数据的数据库"""

    def __init__(self, db_path: str = "folder_history.db"):
        self.db_path = db_path
        self._conn = None
        self._local = threading.local()  # 线程本地存储，每个线程有独立的连接
        self._lock = threading.Lock()  # 保护初始化操作
        self._init_db()

    def _get_connection(self):
        """获取数据库连接（支持多线程，每个线程使用独立连接）"""
        # 如果当前线程已有连接，直接返回
        if hasattr(self._local, 'conn'):
            return self._local.conn
        
        # 为当前线程创建新连接
        with self._lock:
            conn = sqlite3.connect(
                self.db_path, 
                check_same_thread=False,
                isolation_level=None  # 使用 autocommit 模式，避免手动事务管理
            )
            # 启用 WAL 模式以提高并发性能
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
            return conn

    def _init_db(self):
        """初始化数据库表（使用主连接，仅初始化时调用）"""
        with self._lock:
            if self._conn is None:
                self._conn = sqlite3.connect(
                    self.db_path,
                    check_same_thread=False,
                    isolation_level=None
                )
                self._conn.execute("PRAGMA journal_mode=WAL")

        # 扫描会话表
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP,
                duration REAL,
                root_path TEXT NOT NULL,
                max_depth INTEGER NOT NULL,
                total_folders INTEGER DEFAULT 0,
                large_folders_count INTEGER DEFAULT 0,
                total_size_bytes BIGINT DEFAULT 0
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_session_start_time ON scan_sessions(start_time DESC)
        """)

        # 扫描记录表（添加session_id关联）
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                scan_time TIMESTAMP NOT NULL,
                path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                depth INTEGER DEFAULT 0,
                parent_path TEXT,
                FOREIGN KEY (session_id) REFERENCES scan_sessions(id)
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scan_session_id ON scans(session_id)
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scan_time ON scans(scan_time DESC)
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_path ON scans(path)
        """)

        # AI配置表
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                api_key TEXT NOT NULL,
                base_url TEXT,
                model TEXT NOT NULL,
                language TEXT DEFAULT 'zh',
                is_default INTEGER DEFAULT 0,
                created_at TIMESTAMP NOT NULL,
                last_used_at TIMESTAMP
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_config_default ON ai_configs(is_default DESC)
        """)

    def save_scan(self, session_id: int, path: str, size_bytes: int, scan_time: datetime = None, depth: int = 0, parent_path: str = None):
        """保存一次扫描结果（线程安全）"""
        if scan_time is None:
            scan_time = datetime.now()
        conn = self._get_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO scans (session_id, scan_time, path, size_bytes, depth, parent_path) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, scan_time.isoformat(), path, size_bytes, depth, parent_path)
            )
        except sqlite3.Error as e:
            if hasattr(self._local, 'conn'):
                try:
                    self._local.conn.close()
                except:
                    pass
                delattr(self._local, 'conn')
            raise

    # ========== 扫描会话管理 ==========

    def create_scan_session(self, root_path: str, max_depth: int) -> int:
        """创建新的扫描会话，返回session_id"""
        conn = self._get_connection()
        cursor = conn.execute(
            "INSERT INTO scan_sessions (start_time, root_path, max_depth) VALUES (?, ?, ?)",
            (datetime.now().isoformat(), root_path, max_depth)
        )
        return cursor.lastrowid

    def update_scan_session(self, session_id: int, total_folders: int = None, large_folders_count: int = None, total_size_bytes: int = None):
        """更新扫描会话统计信息"""
        conn = self._get_connection()
        updates = []
        params = []

        if total_folders is not None:
            updates.append("total_folders = ?")
            params.append(total_folders)
        if large_folders_count is not None:
            updates.append("large_folders_count = ?")
            params.append(large_folders_count)
        if total_size_bytes is not None:
            updates.append("total_size_bytes = ?")
            params.append(total_size_bytes)

        if updates:
            params.append(session_id)
            conn.execute(
                f"UPDATE scan_sessions SET {', '.join(updates)} WHERE id = ?",
                params
            )

    def finish_scan_session(self, session_id: int):
        """结束扫描会话，计算持续时间"""
        conn = self._get_connection()
        # 获取开始时间
        cursor = conn.execute("SELECT start_time FROM scan_sessions WHERE id = ?", (session_id,))
        row = cursor.fetchone()
        if row:
            start_time = datetime.fromisoformat(row[0])
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            conn.execute(
                "UPDATE scan_sessions SET end_time = ?, duration = ? WHERE id = ?",
                (end_time.isoformat(), duration, session_id)
            )

    def get_scan_session(self, session_id: int) -> Optional[dict]:
        """获取单个扫描会话信息"""
        conn = self._get_connection()
        cursor = conn.execute("SELECT * FROM scan_sessions WHERE id = ?", (session_id,))
        row = cursor.fetchone()
        if row:
            return {
                "id": row[0],
                "start_time": datetime.fromisoformat(row[1]),
                "end_time": datetime.fromisoformat(row[2]) if row[2] else None,
                "duration": row[3],
                "root_path": row[4],
                "max_depth": row[5],
                "total_folders": row[6],
                "large_folders_count": row[7],
                "total_size_bytes": row[8]
            }
        return None

    def get_all_scan_sessions(self, limit: int = 50) -> List[dict]:
        """获取所有扫描会话（按最新时间排序）"""
        conn = self._get_connection()
        cursor = conn.execute(
            f"SELECT * FROM scan_sessions ORDER BY start_time DESC LIMIT {limit}"
        )
        results = []
        for row in cursor.fetchall():
            results.append({
                "id": row[0],
                "start_time": datetime.fromisoformat(row[1]),
                "end_time": datetime.fromisoformat(row[2]) if row[2] else None,
                "duration": row[3],
                "root_path": row[4],
                "max_depth": row[5],
                "total_folders": row[6],
                "large_folders_count": row[7],
                "total_size_bytes": row[8]
            })
        return results

    def get_scans_by_session(self, session_id: int) -> List[dict]:
        """获取指定会话的所有扫描记录"""
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT scan_time, path, size_bytes, depth, parent_path FROM scans WHERE session_id = ? ORDER BY scan_time DESC",
            (session_id,)
        )
        results = []
        for row in cursor.fetchall():
            results.append({
                "scan_time": datetime.fromisoformat(row[0]),
                "path": row[1],
                "size_bytes": row[2],
                "depth": row[3],
                "parent_path": row[4]
            })
        return results

    def rebuild_scan_result_from_session(self, session_id: int):
        """从数据库重建ScanResult树形结构"""
        from models import ScanResult
        
        # 获取会话信息
        session = self.get_scan_session(session_id)
        if not session:
            return None
        
        # 获取该会话的所有扫描记录
        scans = self.get_scans_by_session(session_id)
        if not scans:
            return None
        
        # 按深度和路径排序，确保父节点在子节点之前
        scans.sort(key=lambda x: (x['depth'], x['path']))
        
        # 创建路径到ScanResult的映射
        path_to_result = {}
        root_result = None
        
        # 第一遍：创建所有节点
        for scan in scans:
            scan_time = scan['scan_time']
            result = ScanResult(
                path=scan['path'],
                size_bytes=scan['size_bytes'],
                scan_time=scan_time,
                depth=scan['depth']
            )
            path_to_result[scan['path']] = result
            
            # 找到根节点（depth=0或parent_path为None）
            if scan['depth'] == 0 or not scan['parent_path']:
                root_result = result
        
        # 如果没有找到根节点，使用root_path创建
        if not root_result:
            root_result = ScanResult(
                path=session['root_path'],
                size_bytes=session['total_size_bytes'],
                scan_time=datetime.fromisoformat(session['start_time']) if isinstance(session['start_time'], str) else session['start_time'],
                depth=0
            )
            path_to_result[session['root_path']] = root_result
        
        # 第二遍：建立父子关系
        for scan in scans:
            result = path_to_result.get(scan['path'])
            if not result:
                continue
            
            parent_path = scan['parent_path']
            if parent_path and parent_path in path_to_result:
                parent_result = path_to_result[parent_path]
                if result not in parent_result.children:
                    parent_result.children.append(result)
            elif scan['depth'] > 0:
                # 如果找不到父节点，但depth>0，尝试添加到根节点
                if root_result and result not in root_result.children:
                    root_result.children.append(result)
        
        return root_result

    # ========== AI配置管理 ==========

    def save_ai_config(self, name: str, api_key: str, base_url: str = None, model: str = "gpt-4o-mini", language: str = "zh", is_default: bool = False) -> int:
        """保存AI配置"""
        conn = self._get_connection()
        # 如果设置为默认，先取消其他默认配置
        if is_default:
            conn.execute("UPDATE ai_configs SET is_default = 0")
        cursor = conn.execute(
            "INSERT OR REPLACE INTO ai_configs (name, api_key, base_url, model, language, is_default, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, api_key, base_url, model, language, 1 if is_default else 0, datetime.now().isoformat())
        )
        return cursor.lastrowid

    def get_ai_config(self, name: str = None, config_id: int = None) -> Optional[dict]:
        """获取AI配置（通过名称或ID）"""
        conn = self._get_connection()
        if config_id:
            cursor = conn.execute("SELECT * FROM ai_configs WHERE id = ?", (config_id,))
        elif name:
            cursor = conn.execute("SELECT * FROM ai_configs WHERE name = ?", (name,))
        else:
            return None
        row = cursor.fetchone()
        if row:
            return {
                "id": row[0],
                "name": row[1],
                "api_key": row[2],
                "base_url": row[3],
                "model": row[4],
                "language": row[5],
                "is_default": bool(row[6]),
                "created_at": datetime.fromisoformat(row[7]),
                "last_used_at": datetime.fromisoformat(row[8]) if row[8] else None
            }
        return None

    def get_default_ai_config(self) -> Optional[dict]:
        """获取默认AI配置"""
        conn = self._get_connection()
        cursor = conn.execute("SELECT * FROM ai_configs WHERE is_default = 1 LIMIT 1")
        row = cursor.fetchone()
        if row:
            return {
                "id": row[0],
                "name": row[1],
                "api_key": row[2],
                "base_url": row[3],
                "model": row[4],
                "language": row[5],
                "is_default": bool(row[6]),
                "created_at": datetime.fromisoformat(row[7]),
                "last_used_at": datetime.fromisoformat(row[8]) if row[8] else None
            }
        return None

    def get_all_ai_configs(self) -> List[dict]:
        """获取所有AI配置"""
        conn = self._get_connection()
        cursor = conn.execute("SELECT * FROM ai_configs ORDER BY is_default DESC, created_at DESC")
        results = []
        for row in cursor.fetchall():
            results.append({
                "id": row[0],
                "name": row[1],
                "api_key": row[2],
                "base_url": row[3],
                "model": row[4],
                "language": row[5],
                "is_default": bool(row[6]),
                "created_at": datetime.fromisoformat(row[7]),
                "last_used_at": datetime.fromisoformat(row[8]) if row[8] else None
            })
        return results

    def update_ai_config_last_used(self, config_id: int):
        """更新AI配置最后使用时间"""
        conn = self._get_connection()
        conn.execute(
            "UPDATE ai_configs SET last_used_at = ? WHERE id = ?",
            (datetime.now().isoformat(), config_id)
        )

    def delete_ai_config(self, config_id: int):
        """删除AI配置"""
        conn = self._get_connection()
        conn.execute("DELETE FROM ai_configs WHERE id = ?", (config_id,))

    def set_default_ai_config(self, config_id: int):
        """设置默认AI配置"""
        conn = self._get_connection()
        conn.execute("UPDATE ai_configs SET is_default = 0")
        conn.execute("UPDATE ai_configs SET is_default = 1 WHERE id = ?", (config_id,))

    def get_latest_scan(self, path: str, before: datetime = None) -> Optional[dict]:
        """获取指定路径的最新扫描记录"""
        conn = self._get_connection()
        if before:
            cursor = conn.execute(
                "SELECT scan_time, path, size_bytes, depth FROM scans WHERE path = ? AND scan_time < ? ORDER BY scan_time DESC LIMIT 1",
                (path, before.isoformat())
            )
        else:
            cursor = conn.execute(
                "SELECT scan_time, path, size_bytes, depth FROM scans WHERE path = ? ORDER BY scan_time DESC LIMIT 1",
                (path,)
            )
        row = cursor.fetchone()
        if row:
            return {
                "path": row[1],
                "size_bytes": row[2],
                "scan_time": datetime.fromisoformat(row[0]),
                "depth": row[3]
            }
        return None

    def get_all_paths(self) -> List[str]:
        """获取所有已扫描的路径"""
        conn = self._get_connection()
        cursor = conn.execute("SELECT DISTINCT path FROM scans ORDER BY path")
        return [row[0] for row in cursor.fetchall()]

    def get_scan_history(self, path: str, limit: int = 10) -> List[dict]:
        """获取指定路径的扫描历史"""
        conn = self._get_connection()
        cursor = conn.execute(
            f"SELECT scan_time, path, size_bytes, depth FROM scans WHERE path = ? ORDER BY scan_time DESC LIMIT {limit}",
            (path,)
        )
        results = []
        for row in cursor.fetchall():
            results.append({
                "path": row[1],
                "size_bytes": row[2],
                "scan_time": datetime.fromisoformat(row[0]),
                "depth": row[3]
            })
        return results

    def get_all_scans(self, limit: int = 1000) -> List[dict]:
        """获取所有扫描记录（按最新时间排序）"""
        conn = self._get_connection()
        cursor = conn.execute(
            f"SELECT scan_time, path, size_bytes, depth, parent_path FROM scans ORDER BY scan_time DESC LIMIT {limit}"
        )
        results = []
        for row in cursor.fetchall():
            results.append({
                "scan_time": datetime.fromisoformat(row[0]),
                "path": row[1],
                "size_bytes": row[2],
                "depth": row[3],
                "parent_path": row[4]
            })
        return results

    def close(self):
        """关闭数据库连接"""
        # 关闭主连接
        if self._conn:
            self._conn.close()
            self._conn = None
        # 关闭线程本地连接
        if hasattr(self._local, 'conn'):
            try:
                self._local.conn.close()
            except:
                pass
            delattr(self._local, 'conn')

    def __del__(self):
        """析构函数，确保连接关闭"""
        self.close()
