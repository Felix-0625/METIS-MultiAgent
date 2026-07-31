"""
PG Agent 实现
项目文件管理 Agent，负责目录构建、文件归档和版本控制
"""

import os
import hashlib
import time
from functools import wraps
from pathlib import Path
from typing import Dict, List, Optional, Any
from .base.hermes_agent import AgentBase, AgentType, Task, AgentState, Certificate
from core.project_write_fence import project_write_guard


def _project_write_fenced(method):
    @wraps(method)
    def guarded(self, project_id: str, *args, **kwargs):
        with project_write_guard(project_id, self._project_path(project_id)):
            return method(self, project_id, *args, **kwargs)

    return guarded


PG_SKILL_FRAMEWORK = """【项目文件管理技能框架 — Project File Manager】

文件管理原则：
- 目录结构遵循项目规范，不自创目录层级
- 文件命名语义化：[阶段]-[类型]-[版本]，如 phase1-design-v1.md
- 版本控制：每次重大变更前备份，有错误用新版本替换旧版本（不打补丁）
- 产出归档：按阶段/子项目分类，附元数据（创建时间/版本/负责人）

目录结构规范：
```
project/
  ├── docs/          # 需求/设计文档
  ├── phases/        # 各阶段产出
  │   ├── phase-1/
  │   └── phase-2/
  ├── deliverables/  # 最终交付物
  └── archive/       # 历史版本归档
```

版本管理规范：
- 文档版本：v1.0.0（主版本.次版本.修订号）
- 变更记录：每次修改必须记录变更原因和变更内容
- 归档策略：阶段完成后将产出归档，保留最新版本在工作目录

进度追踪规范：
- 每个子项目有独立的进度文件（progress.json）
- 进度更新：任务状态变更时立即更新
- 里程碑记录：关键节点完成时生成里程碑报告

输出规范：
- 目录树：清晰展示项目文件结构
- 归档报告：列出归档文件、版本、时间
- 进度报告：完成率、待完成任务、阻塞项"""


class PGAgent(AgentBase):
    """
    PG Agent
    
    职责：
    - 目录构建
    - 文件归档
    - 进度追踪
    - 版本控制
    - 产出管理
    """

    # 基本能力极值
    ESSENTIAL_CAPABILITIES = [
        "目录构建",
        "文件归档",
        "进度追踪",
        "版本控制",
        "产出管理"
    ]

    # 默认目录结构模板
    DEFAULT_TEMPLATE = [
        ".project",
        "docs",
        "src",
        "tests",
        "deploy",
        "assets"
    ]

    def __init__(
        self,
        *args,
        workspace: str = "projects",
        workspace_is_project_root: bool = False,
        **kwargs,
    ):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.workspace_is_project_root = bool(workspace_is_project_root)
        self.projects: Dict[str, Dict] = {}
        self._capabilities = list(self.ESSENTIAL_CAPABILITIES)
        super().__init__(*args, agent_type=AgentType.PG, **kwargs)

    def _project_path(self, project_id: str) -> Path:
        return self.workspace if self.workspace_is_project_root else self.workspace / project_id

    @_project_write_fenced
    def build_directory_structure(self, project_id: str, template: List[str] = None) -> Dict[str, Any]:
        """
        构建项目目录结构
        
        Args:
            project_id: 项目 ID
            template: 目录模板
            
        Returns:
            目录结构信息
        """
        project_path = self._project_path(project_id)
        template = template or self.DEFAULT_TEMPLATE

        directories = []
        for dir_name in template:
            dir_path = project_path / dir_name
            dir_path.mkdir(parents=True, exist_ok=True)
            directories.append(str(dir_path.relative_to(self.workspace)))

        # 创建 .project 目录的配置文件
        project_config = project_path / ".project"
        project_config.mkdir(parents=True, exist_ok=True)

        config_data = {
            "project_id": project_id,
            "created_at": time.time(),
            "template": template,
            "directories": directories
        }

        # 写入配置
        import json
        config_file = project_config / "config.yaml"
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2)

        self.projects[project_id] = {
            "path": str(project_path),
            "directories": directories,
            "progress": 0,
            "status": "initialized"
        }

        return {
            "success": True,
            "project_id": project_id,
            "directories": directories
        }

    def verify_certificate(self, certificate: Certificate) -> Dict[str, Any]:
        """
        验证验收证书
        
        检查：
        - 签名有效
        - 哈希匹配
        - 时间戳有效
        - 全闸门通过
        """
        # 检查时间戳（24小时有效）
        if time.time() - certificate.issued_at > 86400:
            return {
                "verified": False,
                "reason": "证书已过期"
            }

        # 检查必需的全闸门通过项
        required_checks = [
            "self_check_passed",
            "supervisor_accepted",
            "qa_approved",
            "perf_approved",
            "sec_approved",
            "uxo_approved"
        ]

        missing_checks = [c for c in required_checks if c not in certificate.checks_passed]
        if missing_checks:
            return {
                "verified": False,
                "reason": f"缺少检查项: {missing_checks}"
            }

        # 签名验证：用确定性签名（不含 time.time()）重新计算并比对
        expected_sig = self._generate_signature(certificate.content_hash, certificate.signer_id)
        if certificate.signature != expected_sig:
            return {
                "verified": False,
                "reason": "签名验证失败"
            }

        return {
            "verified": True,
            "certificate_id": certificate.id,
            "task_id": certificate.task_id
        }

    def _generate_signature(self, content_hash: str, signer_id: str) -> str:
        """生成确定性签名（不含时间戳，保证可重复验证）"""
        return hashlib.sha256(
            f"{signer_id}:{content_hash}".encode()
        ).hexdigest()[:32]

    @_project_write_fenced
    def archive_deliverable(
        self,
        project_id: str,
        certificate: Certificate,
        files: List[str]
    ) -> Dict[str, Any]:
        """
        归档产出
        
        Args:
            project_id: 项目 ID
            certificate: 验收证书
            files: 产出文件列表
            
        Returns:
            归档结果
        """
        # 验证证书
        verify_result = self.verify_certificate(certificate)
        if not verify_result.get("verified"):
            return {
                "success": False,
                "error": verify_result.get("reason", "证书验证失败")
            }

        # 归档到项目目录
        project_path = self._project_path(project_id)
        if not project_path.exists():
            return {
                "success": False,
                "error": "项目目录不存在"
            }

        archived_files = []
        version = 0  # 初始化 version，避免 files 为空时 NameError
        for file_path in files:
            src = Path(file_path)
            if src.exists():
                # 生成版本号
                version = self._get_next_version(project_id, certificate.task_id)
                dst = project_path / "src" / f"{certificate.task_id}_v{version}" / src.name

                dst.parent.mkdir(parents=True, exist_ok=True)
                # 简单复制
                import shutil
                shutil.copy2(src, dst)
                archived_files.append(str(dst.relative_to(self.workspace)))

        # 更新进度
        self._update_progress(project_id, certificate.task_id)

        return {
            "success": True,
            "project_id": project_id,
            "task_id": certificate.task_id,
            "archived_files": archived_files,
            "version": version
        }

    def _get_next_version(self, project_id: str, task_id: str) -> int:
        """获取下一个版本号"""
        version_file = self._project_path(project_id) / ".project" / f"{task_id}_version.txt"
        if version_file.exists():
            with open(version_file, "r") as f:
                version = int(f.read().strip()) + 1
        else:
            version = 1

        with open(version_file, "w") as f:
            f.write(str(version))

        return version

    def _update_progress(self, project_id: str, task_id: str) -> None:
        """更新项目进度"""
        if project_id in self.projects:
            self.projects[project_id]["completed_tasks"] = (
                self.projects[project_id].get("completed_tasks", 0) + 1
            )

    @_project_write_fenced
    def rollback_version(self, project_id: str, task_id: str, version: int) -> Dict[str, Any]:
        """
        回滚到指定版本
        
        Args:
            project_id: 项目 ID
            task_id: 任务 ID
            version: 版本号
            
        Returns:
            回滚结果
        """
        version_dir = self._project_path(project_id) / "src" / f"{task_id}_v{version}"
        if not version_dir.exists():
            return {
                "success": False,
                "error": f"版本 {version} 不存在"
            }

        import shutil as _shutil
        # 将版本快照中的所有文件覆盖回工作区
        rolled_back = []
        for src in version_dir.rglob("*"):
            if not src.is_file():
                continue
            # 相对于 version_dir 的路径
            rel = src.relative_to(version_dir)
            dst = self._project_path(project_id) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copy2(src, dst)
            rolled_back.append(str(rel))

        return {
            "success": True,
            "message": f"已回滚到版本 {version}",
            "version_dir": str(version_dir),
            "rolled_back_files": rolled_back,
        }

    def get_project_files(self, project_id: str) -> List[Dict]:
        """获取项目文件列表"""
        project_path = self._project_path(project_id)
        if not project_path.exists():
            return []

        files = []
        for path in project_path.rglob("*"):
            if path.is_file():
                files.append({
                    "path": str(path.relative_to(self.workspace)),
                    "name": path.name,
                    "size": path.stat().st_size,
                    "modified": path.stat().st_mtime
                })

        return files

    def read_file(self, project_id: str, file_path: str) -> Dict[str, Any]:
        """读取文件内容"""
        full_path = self._project_path(project_id) / file_path
        # 兼容 get_project_files 返回的路径：project_id/path/to/file
        if not full_path.exists():
            prefixed = f"{project_id}/"
            if file_path.startswith(prefixed):
                full_path = self.workspace / file_path

        if not full_path.exists():
            return {"error": "File not found"}

        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()

        return {
            "content": content,
            "path": str(full_path.relative_to(self.workspace))
        }

    # ─── 暂存区 / 提交工作流 ──────────────────────────────────────────────────────

    @_project_write_fenced
    def stage_file(self, project_id: str, file_path: str, content: str) -> Dict[str, Any]:
        """
        将文件写入暂存区（不覆盖已归档版本）
        
        工作区 → 暂存区 → 提交（覆盖工作区）
        暂存区路径：.project/staging/<file_path>
        """
        staging_path = self._project_path(project_id) / ".project" / "staging" / file_path
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_text(content, encoding="utf-8")

        # 记录暂存索引
        index_file = self._project_path(project_id) / ".project" / "staging_index.json"
        import json as _json
        index: Dict = {}
        if index_file.exists():
            try:
                index = _json.loads(index_file.read_text(encoding="utf-8"))
            except Exception:
                index = {}
        index[file_path] = {
            "staged_at": time.time(),
            "size": len(content),
        }
        index_file.write_text(_json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

        return {
            "success": True,
            "staged": file_path,
            "staging_path": str(staging_path.relative_to(self.workspace)),
        }

    @_project_write_fenced
    def commit_staged(self, project_id: str, message: str = "") -> Dict[str, Any]:
        """
        提交暂存区：将暂存区所有文件覆盖到工作区，并生成版本快照
        
        Returns:
            commit_id, committed_files, version
        """
        import json as _json, shutil as _shutil
        staging_dir = self._project_path(project_id) / ".project" / "staging"
        index_file = self._project_path(project_id) / ".project" / "staging_index.json"

        if not staging_dir.exists() or not index_file.exists():
            return {"success": False, "error": "暂存区为空，没有可提交的内容"}

        index: Dict = {}
        try:
            index = _json.loads(index_file.read_text(encoding="utf-8"))
        except Exception:
            return {"success": False, "error": "暂存区索引损坏"}

        if not index:
            return {"success": False, "error": "暂存区为空"}

        # 生成 commit_id 和版本号
        commit_id = hashlib.sha256(f"{project_id}:{time.time()}".encode()).hexdigest()[:12]
        version = self._get_next_commit_version(project_id)

        # 快照目录：.project/versions/<version>/
        snapshot_dir = self._project_path(project_id) / ".project" / "versions" / str(version)
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        committed = []
        for rel_path in index:
            src = staging_dir / rel_path
            if not src.exists():
                continue
            # 覆盖工作区
            dst = self._project_path(project_id) / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copy2(src, dst)
            # 同时存入版本快照
            snap = snapshot_dir / rel_path
            snap.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copy2(src, snap)
            committed.append(rel_path)

        # 写 commit 元数据
        meta = {
            "commit_id": commit_id,
            "version": version,
            "message": message,
            "committed_at": time.time(),
            "files": committed,
        }
        (snapshot_dir / "commit.json").write_text(
            _json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 清空暂存区
        _shutil.rmtree(staging_dir, ignore_errors=True)
        index_file.unlink(missing_ok=True)

        return {
            "success": True,
            "commit_id": commit_id,
            "version": version,
            "committed_files": committed,
            "message": message,
        }

    def _get_next_commit_version(self, project_id: str) -> int:
        """获取下一个提交版本号"""
        versions_dir = self._project_path(project_id) / ".project" / "versions"
        if not versions_dir.exists():
            return 1
        existing = [int(d.name) for d in versions_dir.iterdir() if d.is_dir() and d.name.isdigit()]
        return max(existing, default=0) + 1

    def list_versions(self, project_id: str) -> List[Dict]:
        """列出所有提交版本"""
        import json as _json
        versions_dir = self._project_path(project_id) / ".project" / "versions"
        if not versions_dir.exists():
            return []
        result = []
        for vdir in sorted(versions_dir.iterdir(), key=lambda d: d.name):
            if not vdir.is_dir():
                continue
            meta_file = vdir / "commit.json"
            if meta_file.exists():
                try:
                    meta = _json.loads(meta_file.read_text(encoding="utf-8"))
                    result.append(meta)
                except Exception:
                    pass
        return result

    @_project_write_fenced
    def write_file(self, project_id: str, file_path: str, content: str) -> Dict[str, Any]:
        """
        写入文件到项目工作区。
        
        默认直接写入以保持历史测试和 PG 基础文件管理语义；前端用户修改如需版本保护，
        可显式调用 stage_file/commit_staged 工作流。
        """
        full_path = self._project_path(project_id) / file_path
        try:
            full_path.resolve().relative_to(self._project_path(project_id).resolve())
        except ValueError:
            return {"success": False, "error": "路径越界"}
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        return {
            "success": True,
            "path": str(full_path.relative_to(self.workspace)),
            "size": len(content),
        }

    def _do_execute(self, task: Task) -> Any:
        """执行任务"""
        if task.title == "build_directory":
            return self.build_directory_structure(
                task.metadata.get("project_id", ""),
                task.metadata.get("template")
            )
        elif task.title == "archive":
            cert_data = task.metadata.get("certificate", {})
            cert = Certificate.from_dict(cert_data)
            return self.archive_deliverable(
                task.metadata.get("project_id", ""),
                cert,
                task.metadata.get("files", [])
            )
        elif task.title == "list_files":
            return {"files": self.get_project_files(task.metadata.get("project_id", ""))}
        elif task.title == "read_file":
            return self.read_file(
                task.metadata.get("project_id", ""),
                task.metadata.get("file_path", "")
            )
        elif task.title == "write_file":
            return self.write_file(
                task.metadata.get("project_id", ""),
                task.metadata.get("file_path", ""),
                task.metadata.get("content", "")
            )
        elif task.title == "rollback":
            return self.rollback_version(
                task.metadata.get("project_id", ""),
                task.metadata.get("task_id", ""),
                task.metadata.get("version", 1)
            )
        return {"error": f"Unknown task: {task.title}"}

    def get_status(self) -> Dict[str, Any]:
        """获取 PG Agent 状态"""
        return {
            "agent_id": self.agent_id,
            "type": "pg",
            "state": self.state.value,
            "projects_count": len(self.projects),
            "workspace": str(self.workspace)
        }
