"""
ExpertLock 文件级租约锁管理器
参考设计文档 4.3.1：带有效期的文件级租约锁

使用数据库 expert_locks 表存储，支持：
- 分配时创建租约锁记录
- 执行过程中自动续期
- 自然过期自动失效
- 任务完成后主动释放
- 动态认领防撞
"""

import json
import time
import logging
from typing import Any, Dict, List, Optional

from core.database import get_conn, _use_postgres

logger = logging.getLogger(__name__)

LOCK_TTL_SECONDS = 30 * 60  # 默认 30 分钟 TTL


def _serialize_file_scope(file_scope: List[str]) -> str:
    return json.dumps(file_scope, ensure_ascii=False)


def _deserialize_file_scope(raw: str) -> List[str]:
    try:
        return json.loads(raw)
    except Exception:
        return [raw]


def _scope_covers(scope: str, file_path: str) -> bool:
    """Match exact paths and the prefix formats used by phase ownership."""
    scope = str(scope or "").replace("\\", "/").strip().casefold()
    file_path = str(file_path or "").replace("\\", "/").strip().casefold()
    if not scope or not file_path:
        return False
    if scope in {"*", "/*"}:
        return True
    if scope.endswith("/*"):
        return file_path.startswith(scope[:-1])
    if scope.endswith("/"):
        return file_path.startswith(scope)
    return scope == file_path


def _scopes_overlap(left: List[str], right: List[str]) -> bool:
    return any(
        _scope_covers(a, b) or _scope_covers(b, a)
        for a in left
        for b in right
    )


def create_lock(
    expert_id: str,
    project_id: str,
    task_id: str,
    file_scope: List[str],
    ttl_seconds: int = LOCK_TTL_SECONDS,
) -> Dict[str, Any]:
    """
    创建文件级租约锁
    
    如果锁已存在，返回 success=False
    """
    lock_id = f"lock:{expert_id}:{project_id}:{task_id}"
    now = time.time()
    leased_until = now + ttl_seconds
    scope_json = _serialize_file_scope(file_scope)

    with get_conn() as conn:
        cur = conn.cursor()
        # Lock acquisition and file-scope conflict detection must happen in
        # one write transaction.  Otherwise two agents can both pass a read
        # check and overwrite the same files concurrently.
        if _use_postgres():
            cur.execute(
                """SELECT lock_id, file_scope FROM expert_locks
                   WHERE project_id = %s AND released_at IS NULL AND leased_until > %s
                   FOR UPDATE""",
                (project_id, now),
            )
        else:
            conn.execute("BEGIN IMMEDIATE")
            cur.execute(
                """SELECT lock_id, file_scope FROM expert_locks
                   WHERE project_id = ? AND released_at IS NULL AND leased_until > ?""",
                (project_id, now),
            )
        requested_scope = _deserialize_file_scope(scope_json)
        for existing_lock_id, existing_scope_raw in cur.fetchall():
            if existing_lock_id == lock_id:
                return {"success": False, "lock_id": lock_id, "error": "锁已存在"}
            if _scopes_overlap(requested_scope, _deserialize_file_scope(existing_scope_raw)):
                return {
                    "success": False,
                    "lock_id": lock_id,
                    "error": "文件范围与现有活跃锁冲突",
                }
        if _use_postgres():
            cur.execute(
                """INSERT INTO expert_locks (lock_id, expert_id, project_id, task_id, file_scope, leased_until, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (lock_id) DO NOTHING""",
                (lock_id, expert_id, project_id, task_id, scope_json, leased_until, now),
            )
        else:
            cur.execute(
                """INSERT OR IGNORE INTO expert_locks (lock_id, expert_id, project_id, task_id, file_scope, leased_until, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (lock_id, expert_id, project_id, task_id, scope_json, leased_until, now),
            )
        if cur.rowcount == 0:
            return {"success": False, "lock_id": lock_id, "error": "锁已存在"}

    logger.info("ExpertLock 创建: %s, scope=%s, TTL=%ds", lock_id, file_scope, ttl_seconds)
    return {
        "success": True,
        "lock_id": lock_id,
        "expert_id": expert_id,
        "project_id": project_id,
        "task_id": task_id,
        "file_scope": file_scope,
        "leased_until": leased_until,
        "ttl_seconds": ttl_seconds,
    }


def atomic_claim_lock(
    expert_id: str,
    project_id: str,
    task_id: str,
    file_scope: List[str],
    ttl_seconds: int = LOCK_TTL_SECONDS,
) -> Dict[str, Any]:
    """
    原子级锁声明 - 防止竞态条件
    
    使用 UPDATE ... RETURNING 或 CAS（Compare-And-Swap）实现原子操作
    即使多个专家同时尝试获取同一个锁，也只有一个能成功
    
    Args:
        expert_id: 专家ID
        project_id: 项目ID
        task_id: 任务ID
        file_scope: 文件范围
        ttl_seconds: TTL秒数
        
    Returns:
        {"success": True/False, "lock_id": str, ...}
    """
    lock_id = f"lock:{expert_id}:{project_id}:{task_id}"
    now = time.time()
    leased_until = now + ttl_seconds
    scope_json = _serialize_file_scope(file_scope)

    with get_conn() as conn:
        cur = conn.cursor()

        # Serialize claims per project, not merely per lock_id. Different
        # experts/tasks can generate different IDs while still owning the same
        # path, so lock-id upsert alone does not prevent concurrent writes.
        if _use_postgres():
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (project_id,),
            )
            cur.execute(
                """SELECT lock_id, file_scope FROM expert_locks
                   WHERE project_id = %s AND released_at IS NULL AND leased_until > %s
                   FOR UPDATE""",
                (project_id, now),
            )
        else:
            conn.execute("BEGIN IMMEDIATE")
            cur.execute(
                """SELECT lock_id, file_scope FROM expert_locks
                   WHERE project_id = ? AND released_at IS NULL AND leased_until > ?""",
                (project_id, now),
            )
        for existing_lock_id, existing_scope_raw in cur.fetchall():
            if existing_lock_id == lock_id:
                return {
                    "success": False,
                    "lock_id": lock_id,
                    "error": "锁已被占用且未过期",
                }
            if _scopes_overlap(file_scope, _deserialize_file_scope(existing_scope_raw)):
                return {
                    "success": False,
                    "lock_id": lock_id,
                    "error": "文件范围与现有活跃锁冲突",
                }

        if _use_postgres():
            # PostgreSQL: 使用 INSERT ... ON CONFLICT DO UPDATE ... RETURNING 实现原子操作
            # 如果锁不存在或已过期，则获取锁；否则失败
            cur.execute(
                """
                INSERT INTO expert_locks (lock_id, expert_id, project_id, task_id, file_scope, leased_until, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (lock_id) DO UPDATE
                  SET expert_id = EXCLUDED.expert_id,
                      leased_until = EXCLUDED.leased_until,
                      file_scope = EXCLUDED.file_scope,
                      released_at = NULL
                  WHERE expert_locks.released_at IS NOT NULL 
                     OR expert_locks.leased_until < %s
                RETURNING lock_id, expert_id, project_id, task_id, file_scope, leased_until, created_at
                """,
                (lock_id, expert_id, project_id, task_id, scope_json, leased_until, now, now),
            )
            row = cur.fetchone()
            
            if row is None:
                # 锁已被其他专家持有且未过期
                return {
                    "success": False,
                    "lock_id": lock_id,
                    "error": "锁已被占用且未过期",
                }
            
            logger.info("ExpertLock 原子获取成功: %s by %s", lock_id, expert_id)
            return {
                "success": True,
                "lock_id": row[0],
                "expert_id": row[1],
                "project_id": row[2],
                "task_id": row[3],
                "file_scope": _deserialize_file_scope(row[4]),
                "leased_until": row[5],
                "created_at": row[6],
            }
        else:
            # SQLite: 使用两步操作（先查后更新）+ 事务隔离
            # 注意：SQLite 的事务隔离可能不如 PostgreSQL 强，但在单进程场景下足够
            
            # 步骤1：尝试插入新锁
            cur.execute(
                """INSERT OR IGNORE INTO expert_locks 
                   (lock_id, expert_id, project_id, task_id, file_scope, leased_until, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (lock_id, expert_id, project_id, task_id, scope_json, leased_until, now),
            )
            
            if cur.rowcount > 0:
                # 插入成功，锁获取成功
                logger.info("ExpertLock 原子获取成功（新建）: %s by %s", lock_id, expert_id)
                return {
                    "success": True,
                    "lock_id": lock_id,
                    "expert_id": expert_id,
                    "project_id": project_id,
                    "task_id": task_id,
                    "file_scope": file_scope,
                    "leased_until": leased_until,
                    "created_at": now,
                }
            
            # 步骤2：锁已存在，尝试更新过期锁
            cur.execute(
                """UPDATE expert_locks 
                   SET expert_id = ?, leased_until = ?, file_scope = ?, released_at = NULL
                   WHERE lock_id = ? 
                     AND (released_at IS NOT NULL OR leased_until < ?)""",
                (expert_id, leased_until, scope_json, lock_id, now),
            )
            
            if cur.rowcount > 0:
                # 更新成功，获取了已过期的锁
                logger.info("ExpertLock 原子获取成功（更新过期锁）: %s by %s", lock_id, expert_id)
                return {
                    "success": True,
                    "lock_id": lock_id,
                    "expert_id": expert_id,
                    "project_id": project_id,
                    "task_id": task_id,
                    "file_scope": file_scope,
                    "leased_until": leased_until,
                    "created_at": now,
                }
            
            # 锁已被占用且未过期
            return {
                "success": False,
                "lock_id": lock_id,
                "error": "锁已被占用且未过期",
            }


def renew_lock(lock_id: str, extend_seconds: int = LOCK_TTL_SECONDS) -> Dict[str, Any]:
    """续期租约锁"""
    new_leased_until = time.time() + extend_seconds
    with get_conn() as conn:
        cur = conn.cursor()
        if _use_postgres():
            cur.execute(
                "UPDATE expert_locks SET leased_until = %s WHERE lock_id = %s AND released_at IS NULL",
                (new_leased_until, lock_id),
            )
        else:
            cur.execute(
                "UPDATE expert_locks SET leased_until = ? WHERE lock_id = ? AND released_at IS NULL",
                (new_leased_until, lock_id),
            )
        affected = cur.rowcount
    return {"success": affected > 0, "lock_id": lock_id, "leased_until": new_leased_until if affected > 0 else None}


def release_lock(lock_id: str) -> Dict[str, Any]:
    """主动释放锁（任务完成后调用）"""
    now = time.time()
    with get_conn() as conn:
        cur = conn.cursor()
        if _use_postgres():
            cur.execute(
                "UPDATE expert_locks SET released_at = %s WHERE lock_id = %s AND released_at IS NULL",
                (now, lock_id),
            )
        else:
            cur.execute(
                "UPDATE expert_locks SET released_at = ? WHERE lock_id = ? AND released_at IS NULL",
                (now, lock_id),
            )
    logger.info("ExpertLock 释放: %s", lock_id)
    return {"success": True, "lock_id": lock_id, "released_at": now}


def cleanup_expired_locks() -> int:
    """清理所有已过期的 ExpertLock 记录"""
    now = time.time()
    with get_conn() as conn:
        cur = conn.cursor()
        if _use_postgres():
            cur.execute("DELETE FROM expert_locks WHERE released_at IS NULL AND leased_until < %s", (now,))
        else:
            cur.execute("DELETE FROM expert_locks WHERE released_at IS NULL AND leased_until < ?", (now,))
        deleted = cur.rowcount
        if deleted > 0:
            logger.info("清理了 %d 条过期 ExpertLock", deleted)
        return deleted


def get_active_locks(
    expert_id: Optional[str] = None,
    project_id: Optional[str] = None,
    include_expired: bool = False,
) -> List[Dict[str, Any]]:
    """查询活跃锁"""
    now = time.time()
    conditions = []
    params: List[Any] = []
    if _use_postgres():
        if not include_expired:
            conditions.append("released_at IS NULL AND leased_until > %s")
            params.append(now)
        else:
            conditions.append("released_at IS NULL")
        if expert_id:
            conditions.append("expert_id = %s")
            params.append(expert_id)
        if project_id:
            conditions.append("project_id = %s")
            params.append(project_id)
        _LOCK_COLS = "lock_id, expert_id, project_id, task_id, file_scope, leased_until, released_at, created_at"
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        safe_sql = f"SELECT {_LOCK_COLS} FROM expert_locks {where} ORDER BY created_at DESC"
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(safe_sql, params)
            rows = cur.fetchall()
    else:
        if not include_expired:
            conditions.append("released_at IS NULL AND leased_until > ?")
            params.append(now)
        else:
            conditions.append("released_at IS NULL")
        if expert_id:
            conditions.append("expert_id = ?")
            params.append(expert_id)
        if project_id:
            conditions.append("project_id = ?")
            params.append(project_id)
        _LOCK_COLS = "lock_id, expert_id, project_id, task_id, file_scope, leased_until, released_at, created_at"
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        safe_sql = f"SELECT {_LOCK_COLS} FROM expert_locks {where} ORDER BY created_at DESC"
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(safe_sql, params)
            rows = cur.fetchall()

    return [
        {
            "lock_id": r[0],
            "expert_id": r[1],
            "project_id": r[2],
            "task_id": r[3],
            "file_scope": _deserialize_file_scope(r[4]),
            "leased_until": r[5],
            "released_at": r[6],
            "created_at": r[7],
            "is_expired": not include_expired and (r[6] is None and r[5] <= now),
        }
        for r in rows
    ]


def is_expert_available(expert_id: str) -> bool:
    """检查专家是否可用（无未释放且未过期的活跃锁）"""
    locks = get_active_locks(expert_id=expert_id, include_expired=False)
    return len(locks) == 0


def detect_lock_contention(project_id: str, time_window_seconds: int = 300) -> Dict[str, Any]:
    """
    检测锁争用热点
    
    统计指定时间窗口内的锁争用情况，识别高并发竞争的文件或任务
    
    Args:
        project_id: 项目ID
        time_window_seconds: 时间窗口（秒），默认5分钟
        
    Returns:
        {
            "total_locks": int,
            "active_locks": int,
            "expired_locks": int,
            "contention_rate": float,  # 争用率
            "hotspot_files": List[str],  # 高争用文件
            "experts_stats": Dict[str, int],  # 专家锁数量统计
        }
    """
    now = time.time()
    cutoff_time = now - time_window_seconds
    
    with get_conn() as conn:
        cur = conn.cursor()
        
        # 统计总锁数量
        if _use_postgres():
            cur.execute(
                "SELECT COUNT(*) FROM expert_locks WHERE project_id = %s AND created_at > %s",
                (project_id, cutoff_time)
            )
        else:
            cur.execute(
                "SELECT COUNT(*) FROM expert_locks WHERE project_id = ? AND created_at > ?",
                (project_id, cutoff_time)
            )
        total_locks = cur.fetchone()[0]
        
        # 统计活跃锁
        if _use_postgres():
            cur.execute(
                "SELECT COUNT(*) FROM expert_locks WHERE project_id = %s AND released_at IS NULL AND leased_until > %s",
                (project_id, now)
            )
        else:
            cur.execute(
                "SELECT COUNT(*) FROM expert_locks WHERE project_id = ? AND released_at IS NULL AND leased_until > ?",
                (project_id, now)
            )
        active_locks = cur.fetchone()[0]
        
        # 统计过期未释放的锁（潜在问题）
        if _use_postgres():
            cur.execute(
                "SELECT COUNT(*) FROM expert_locks WHERE project_id = %s AND released_at IS NULL AND leased_until < %s",
                (project_id, now)
            )
        else:
            cur.execute(
                "SELECT COUNT(*) FROM expert_locks WHERE project_id = ? AND released_at IS NULL AND leased_until < ?",
                (project_id, now)
            )
        expired_locks = cur.fetchone()[0]
        
        # 专家锁数量统计
        if _use_postgres():
            cur.execute(
                "SELECT expert_id, COUNT(*) FROM expert_locks WHERE project_id = %s AND created_at > %s GROUP BY expert_id",
                (project_id, cutoff_time)
            )
        else:
            cur.execute(
                "SELECT expert_id, COUNT(*) FROM expert_locks WHERE project_id = ? AND created_at > ? GROUP BY expert_id",
                (project_id, cutoff_time)
            )
        experts_stats = {row[0]: row[1] for row in cur.fetchall()}
    
    # 获取所有活跃锁的文件范围，分析热点文件
    active_locks_list = get_active_locks(project_id=project_id, include_expired=False)
    file_access_count: Dict[str, int] = {}
    
    for lock in active_locks_list:
        for file_path in lock["file_scope"]:
            file_access_count[file_path] = file_access_count.get(file_path, 0) + 1
    
    # 找出访问次数 > 1 的文件（有争用）
    hotspot_files = [f for f, count in file_access_count.items() if count > 1]
    hotspot_files.sort(key=lambda f: file_access_count[f], reverse=True)
    
    # 争用率：有争用的锁 / 总锁数
    contention_rate = len(hotspot_files) / total_locks if total_locks > 0 else 0.0
    
    return {
        "total_locks": total_locks,
        "active_locks": active_locks,
        "expired_locks": expired_locks,
        "contention_rate": round(contention_rate, 3),
        "hotspot_files": hotspot_files[:10],  # 只返回前10个热点文件
        "experts_stats": experts_stats,
        "time_window_seconds": time_window_seconds,
        "analysis_time": now,
    }


def try_claim_file(
    expert_id: str,
    lock_id: str,
    new_file: str,
) -> Dict[str, Any]:
    """
    动态认领防撞：原子操作将新文件追加到 file_scope。
    返回 affected_rows 判断是否成功（防止双重占用）。
    """
    with get_conn() as conn:
        cur = conn.cursor()
        # 检查是否有其他专家锁定了此文件
        if _use_postgres():
            cur.execute(
                """SELECT lock_id, expert_id, file_scope FROM expert_locks
                   WHERE released_at IS NULL AND leased_until > %s
                   FOR UPDATE""",
                (time.time(),),
            )
            for lock_row in cur.fetchall():
                if lock_row[1] == expert_id or lock_row[0] == lock_id:
                    continue
                for scope in _deserialize_file_scope(lock_row[2]):
                    if _scope_covers(scope, new_file):
                        return {"success": False, "lock_id": lock_id, "conflict_with": lock_row[0]}
        else:
            # SQLite 不支持 SIMILAR TO，简化检查
            # Acquire a write reservation before the read+update sequence so
            # concurrent claims cannot both pass the conflict check.
            conn.execute("BEGIN IMMEDIATE")
            cur.execute(
                "SELECT lock_id, expert_id, file_scope FROM expert_locks "
                "WHERE released_at IS NULL AND leased_until > ?",
                (time.time(),),
            )
            for lock_row in cur.fetchall():
                if lock_row[1] == expert_id or lock_row[0] == lock_id:
                    continue
                for scope in _deserialize_file_scope(lock_row[2]):
                    if _scope_covers(scope, new_file):
                        return {"success": False, "lock_id": lock_id, "conflict_with": lock_row[0]}

        # 原子追加
        if _use_postgres():
            cur.execute(
                "SELECT file_scope FROM expert_locks WHERE lock_id = %s AND released_at IS NULL FOR UPDATE",
                (lock_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"success": False, "lock_id": lock_id, "error": "lock not found or released"}
            current_scope = _deserialize_file_scope(row[0])
            if new_file not in current_scope:
                current_scope.append(new_file)
            cur.execute(
                "UPDATE expert_locks SET file_scope = %s WHERE lock_id = %s AND released_at IS NULL",
                (_serialize_file_scope(current_scope), lock_id),
            )
        else:
            # SQLite: 先读取再写入
            cur.execute(
                "SELECT file_scope FROM expert_locks WHERE lock_id = ? AND released_at IS NULL",
                (lock_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"success": False, "lock_id": lock_id, "error": "锁不存在或已释放"}
            current_scope = _deserialize_file_scope(row[0])
            if new_file not in current_scope:
                current_scope.append(new_file)
            cur.execute(
                "UPDATE expert_locks SET file_scope = ? WHERE lock_id = ? AND released_at IS NULL",
                (_serialize_file_scope(current_scope), lock_id),
            )
        return {"success": cur.rowcount > 0, "lock_id": lock_id, "new_file": new_file}
