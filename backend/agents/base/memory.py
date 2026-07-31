"""
记忆系统实现
SQLite + 向量存储双层架构
"""

import json
import time
import sqlite3
import hashlib
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field
from pathlib import Path
import os

from core.workspace import metis_data_path


def _memory_path(base_path: str | Path) -> Path:
    """Place relative Agent memory below METIS_DATA_DIR when configured."""
    path = Path(base_path)
    if path.is_absolute():
        return path
    parts = path.parts
    if parts and parts[0].casefold() == "memory":
        path = Path(*parts[1:]) if len(parts) > 1 else Path()
    return metis_data_path(Path("memory") / path, legacy=Path(base_path))


class MemoryStore:
    """
    记忆存储基类
    
    提供统一的记忆存取接口
    """

    def __init__(self, base_path: str = "memory"):
        self.base_path = _memory_path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def save(self, agent_id: str, key: str, value: Any) -> None:
        """保存记忆"""
        raise NotImplementedError

    def load(self, agent_id: str, key: str) -> Optional[Any]:
        """加载记忆"""
        raise NotImplementedError

    def search(self, agent_id: str, query: str, limit: int = 10) -> List[Any]:
        """搜索记忆"""
        raise NotImplementedError

    def delete(self, agent_id: str, key: str) -> None:
        """删除记忆"""
        raise NotImplementedError

    def list_keys(self, agent_id: str) -> List[str]:
        """列出记忆键"""
        raise NotImplementedError


class SQLiteStore(MemoryStore):
    """
    SQLite 存储
    
    用于结构化数据存储：
    - Agent 配置
    - 任务状态
    - 项目信息
    - 验收证书
    """

    def __init__(self, base_path: str = "memory", db_name: str = "memory.db"):
        super().__init__(base_path)
        self.db_path = self.base_path / db_name
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        # 记忆表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(agent_id, key)
            )
        """)

        # 向量索引表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS vector_index (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                memory_key TEXT NOT NULL,
                content TEXT NOT NULL,
                embedding BLOB,
                created_at REAL NOT NULL,
                UNIQUE(agent_id, memory_key)
            )
        """)

        # 索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_key ON memories(agent_id, key)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vector_agent ON vector_index(agent_id)")

        conn.commit()
        conn.close()

    def save(self, agent_id: str, key: str, value: Any) -> None:
        """保存记忆"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        value_str = json.dumps(value, ensure_ascii=False)
        now = time.time()

        cursor.execute("""
            INSERT INTO memories (agent_id, key, value, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(agent_id, key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
        """, (agent_id, key, value_str, now, now))

        conn.commit()
        conn.close()

    def load(self, agent_id: str, key: str) -> Optional[Any]:
        """加载记忆"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        cursor.execute(
            "SELECT value FROM memories WHERE agent_id = ? AND key = ?",
            (agent_id, key)
        )
        row = cursor.fetchone()

        conn.close()

        if row:
            return json.loads(row[0])
        return None

    def search(self, agent_id: str, query: str, limit: int = 10) -> List[Dict]:
        """
        搜索记忆（模糊匹配）
        
        实际应用中应使用向量检索，这里简化实现
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        cursor.execute("""
            SELECT key, value FROM memories
            WHERE agent_id = ? AND (key LIKE ? OR value LIKE ?)
            ORDER BY updated_at DESC
            LIMIT ?
        """, (agent_id, f"%{query}%", f"%{query}%", limit))

        rows = cursor.fetchall()
        conn.close()

        return [{"key": r[0], "value": json.loads(r[1])} for r in rows]

    def delete(self, agent_id: str, key: str) -> None:
        """删除记忆"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        cursor.execute(
            "DELETE FROM memories WHERE agent_id = ? AND key = ?",
            (agent_id, key)
        )

        conn.commit()
        conn.close()

    def list_keys(self, agent_id: str) -> List[str]:
        """列出记忆键"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        cursor.execute(
            "SELECT key FROM memories WHERE agent_id = ? ORDER BY updated_at DESC",
            (agent_id,)
        )

        rows = cursor.fetchall()
        conn.close()

        return [r[0] for r in rows]

    def get_all_by_agent(self, agent_id: str) -> Dict[str, Any]:
        """获取 Agent 的所有记忆"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        cursor.execute(
            "SELECT key, value FROM memories WHERE agent_id = ?",
            (agent_id,)
        )

        rows = cursor.fetchall()
        conn.close()

        return {r[0]: json.loads(r[1]) for r in rows}


class VectorStore(MemoryStore):
    """
    向量存储
    
    用于语义检索：
    - 上下文记忆
    - 知识库检索
    - 相似度匹配
    """

    def __init__(self, base_path: str = "memory", dim: int = 384):
        super().__init__(base_path)
        self.dim = dim
        self.vectors_path = self.base_path / "vectors.db"
        self._init_db()

    def _init_db(self) -> None:
        """初始化向量数据库"""
        conn = sqlite3.connect(str(self.vectors_path))
        cursor = conn.cursor()

        # 向量存储表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS vectors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                key TEXT NOT NULL,
                content TEXT NOT NULL,
                embedding BLOB,
                created_at REAL NOT NULL,
                UNIQUE(agent_id, key)
            )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vec_agent ON vectors(agent_id)")

        conn.commit()
        conn.close()

    def save(self, agent_id: str, key: str, value: Any) -> None:
        """保存向量记忆"""
        # 生成简化的嵌入向量（实际应用中应使用专门的嵌入模型）
        content = json.dumps(value, ensure_ascii=False)
        embedding = self._simple_embedding(content)

        conn = sqlite3.connect(str(self.vectors_path))
        cursor = conn.cursor()

        now = time.time()

        cursor.execute("""
            INSERT INTO vectors (agent_id, key, content, embedding, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(agent_id, key) DO UPDATE SET
                content = excluded.content,
                embedding = excluded.embedding
        """, (agent_id, key, content, embedding, now))

        conn.commit()
        conn.close()

    def load(self, agent_id: str, key: str) -> Optional[Any]:
        """加载向量记忆"""
        conn = sqlite3.connect(str(self.vectors_path))
        cursor = conn.cursor()

        cursor.execute(
            "SELECT content FROM vectors WHERE agent_id = ? AND key = ?",
            (agent_id, key)
        )
        row = cursor.fetchone()

        conn.close()

        if row:
            return json.loads(row[0])
        return None

    def search(
        self,
        agent_id: str,
        query: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        向量检索
        
        使用余弦相似度计算最相似的记忆
        """
        query_embedding = self._simple_embedding(query)

        conn = sqlite3.connect(str(self.vectors_path))
        cursor = conn.cursor()

        cursor.execute("""
            SELECT key, content, embedding FROM vectors
            WHERE agent_id = ?
            ORDER BY created_at DESC
        """, (agent_id,))

        rows = cursor.fetchall()
        conn.close()

        # 计算相似度并排序
        results = []
        for key, content, embedding in rows:
            if embedding:
                similarity = self._cosine_similarity(
                    query_embedding,
                    self._bytes_to_list(embedding)
                )
                results.append({
                    "key": key,
                    "content": json.loads(content),
                    "similarity": similarity
                })

        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:limit]

    def delete(self, agent_id: str, key: str) -> None:
        """删除向量记忆"""
        conn = sqlite3.connect(str(self.vectors_path))
        cursor = conn.cursor()

        cursor.execute(
            "DELETE FROM vectors WHERE agent_id = ? AND key = ?",
            (agent_id, key)
        )

        conn.commit()
        conn.close()

    def list_keys(self, agent_id: str) -> List[str]:
        """列出向量键"""
        conn = sqlite3.connect(str(self.vectors_path))
        cursor = conn.cursor()

        cursor.execute(
            "SELECT key FROM vectors WHERE agent_id = ?",
            (agent_id,)
        )

        rows = cursor.fetchall()
        conn.close()

        return [r[0] for r in rows]

    def _simple_embedding(self, text: str) -> bytes:
        """
        简化的嵌入生成
        
        实际应用中应使用专门的嵌入模型（如 OpenAI embedding, BERT 等）
        这里使用基于哈希的简化实现
        """
        import struct

        # 将文本转换为固定长度的向量
        text_hash = hashlib.sha256(text.encode()).digest()
        vector = []

        for i in range(self.dim):
            byte_index = i % len(text_hash)
            value = (text_hash[byte_index] + i * 17) % 256
            vector.append(value / 255.0 * 2 - 1)  # 归一化到 [-1, 1]

        # 转换为字节
        return struct.pack(f"{self.dim}f", *vector)

    def _bytes_to_list(self, data: bytes) -> List[float]:
        """将字节转换为浮点列表"""
        import struct
        return list(struct.unpack(f"{self.dim}f", data))

    def _cosine_similarity(self, vec1: bytes, vec2: List[float]) -> float:
        """计算余弦相似度"""
        v1 = self._bytes_to_list(vec1)
        
        dot_product = sum(a * b for a, b in zip(v1, vec2))
        norm1 = sum(a * a for a in v1) ** 0.5
        norm2 = sum(a * a for a in vec2) ** 0.5
        
        if norm1 == 0 or norm2 == 0:
            return 0.0
        
        return dot_product / (norm1 * norm2)


class HybridMemory:
    """
    混合记忆系统
    
    结合 SQLite 和 VectorStore：
    - 结构化数据用 SQLite
    - 语义检索用 VectorStore
    """

    def __init__(self, base_path: str = "memory"):
        self.sqlite = SQLiteStore(base_path)
        self.vector = VectorStore(base_path)

    def save(self, agent_id: str, key: str, value: Any, vectorize: bool = True) -> None:
        """保存记忆到两个存储"""
        # SQLite 存储（必有）
        self.sqlite.save(agent_id, key, value)

        # Vector 存储（可选，用于语义检索）
        if vectorize:
            self.vector.save(agent_id, key, value)

    def load(self, agent_id: str, key: str) -> Optional[Any]:
        """从 SQLite 加载"""
        return self.sqlite.load(agent_id, key)

    def search(self, agent_id: str, query: str, limit: int = 10) -> List[Dict]:
        """从 Vector 搜索"""
        return self.vector.search(agent_id, query, limit)

    def delete(self, agent_id: str, key: str) -> None:
        """从两个存储删除"""
        self.sqlite.delete(agent_id, key)
        self.vector.delete(agent_id, key)

    def list_keys(self, agent_id: str) -> List[str]:
        """从 SQLite 列出键"""
        return self.sqlite.list_keys(agent_id)


@dataclass
class ContextEntry:
    """上下文记忆条目"""
    id: str
    agent_id: str
    content: str
    timestamp: float = field(default_factory=time.time)
    importance: float = 1.0  # 重要性评分
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "content": self.content,
            "timestamp": self.timestamp,
            "importance": self.importance,
            "tags": self.tags,
            "metadata": self.metadata
        }


class ContextMemory:
    """
    上下文记忆管理器
    
    专门用于管理 Agent 的上下文历史
    支持重要性加权衰减
    """

    def __init__(self, hybrid_memory: HybridMemory):
        self.memory = hybrid_memory

    def add_entry(
        self,
        agent_id: str,
        content: str,
        importance: float = 1.0,
        tags: Optional[List[str]] = None
    ) -> str:
        """添加上下文条目"""
        import uuid

        entry = ContextEntry(
            id=str(uuid.uuid4())[:8],
            agent_id=agent_id,
            content=content,
            importance=importance,
            tags=tags or []
        )

        self.memory.save(agent_id, f"context:{entry.id}", entry.to_dict())

        return entry.id

    def get_recent(self, agent_id: str, limit: int = 20) -> List[ContextEntry]:
        """获取最近的上下文"""
        keys = self.memory.list_keys(agent_id)
        context_keys = [k for k in keys if k.startswith("context:")]

        entries = []
        for key in context_keys[-limit:]:
            data = self.memory.load(agent_id, key)
            if data:
                entries.append(ContextEntry(**data))

        return entries

    def get_important(self, agent_id: str, threshold: float = 0.7) -> List[ContextEntry]:
        """获取重要的上下文"""
        keys = self.memory.list_keys(agent_id)
        context_keys = [k for k in keys if k.startswith("context:")]

        entries = []
        for key in context_keys:
            data = self.memory.load(agent_id, key)
            if data and data.get("importance", 0) >= threshold:
                entries.append(ContextEntry(**data))

        return entries

    def decay_importance(self, agent_id: str, decay_rate: float = 0.95) -> None:
        """衰减重要性评分"""
        keys = self.memory.list_keys(agent_id)
        context_keys = [k for k in keys if k.startswith("context:")]

        for key in context_keys:
            data = self.memory.load(agent_id, key)
            if data:
                data["importance"] *= decay_rate
                self.memory.save(agent_id, key, data)
