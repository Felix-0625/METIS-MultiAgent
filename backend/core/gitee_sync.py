"""
Gitee 同步模块
支持绑定 Gitee 仓库，手动推送数据（不自动提交）
用户需要主动触发推送操作
"""

import json
import subprocess
import time
from pathlib import Path
from typing import Dict, Any, Optional, Tuple


DATA_DIR = Path("data")


class GiteeSync:
    """
    Gitee 同步管理器
    
    工作流程：
    1. 用户配置 Gitee 仓库地址 + Access Token
    2. 系统在 data/ 目录初始化 git 仓库
    3. 用户手动触发"推送到 Gitee"操作
    4. 系统执行 git add + git commit + git push（不自动执行）
    """

    def __init__(self, data_dir: Path = DATA_DIR):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _run_git(self, *args, cwd: Optional[Path] = None) -> Tuple[bool, str]:
        """执行 git 命令"""
        try:
            result = subprocess.run(
                ["git"] + list(args),
                cwd=str(cwd or self.data_dir),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return True, result.stdout.strip()
            return False, result.stderr.strip()
        except FileNotFoundError:
            return False, "git 未安装，请先安装 git"
        except subprocess.TimeoutExpired:
            return False, "git 操作超时"
        except Exception as e:
            return False, str(e)

    def is_git_repo(self) -> bool:
        """检查 data/ 是否已是 git 仓库"""
        return (self.data_dir / ".git").exists()

    def init_repo(self) -> Dict[str, Any]:
        """初始化本地 git 仓库"""
        if self.is_git_repo():
            return {"success": True, "message": "已是 git 仓库"}

        ok, msg = self._run_git("init")
        if not ok:
            return {"success": False, "error": msg}

        # 创建 .gitignore
        gitignore = self.data_dir / ".gitignore"
        gitignore.write_text("snapshot_*.json\n*.tmp\n", encoding="utf-8")

        # 配置默认用户信息
        self._run_git("config", "user.email", "ai-agent@local")
        self._run_git("config", "user.name", "AI Agent System")

        return {"success": True, "message": "git 仓库初始化成功"}

    def set_remote(self, repo_url: str, token: str) -> Dict[str, Any]:
        """
        设置 Gitee 远程仓库（使用 git credential 机制，避免 token 写入 .git/config）

        repo_url 格式: https://gitee.com/username/repo.git
        token: Gitee 个人访问令牌
        """
        if not self.is_git_repo():
            init_result = self.init_repo()
            if not init_result["success"]:
                return init_result

        # 使用 git credential 机制注入 token，不写入 .git/config 明文
        # 环境变量方式：git 会从 GIT_ASKPASS 环境变量读取密码
        git_askpass_script = self.data_dir / ".git-askpass.sh"
        git_askpass_script.write_text(
            "#!/bin/sh\necho " + token + "\n",
            encoding="utf-8"
        )
        git_askpass_script.chmod(0o700)

        # 设置 remote URL（不含 token）
        ok, remotes = self._run_git("remote")
        if ok and "origin" in remotes:
            self._run_git("remote", "set-url", "origin", repo_url)
        else:
            ok, msg = self._run_git("remote", "add", "origin", repo_url)
            if not ok:
                # 清理临时脚本
                git_askpass_script.unlink(missing_ok=True)
                return {"success": False, "error": msg}

        # 配置 git 使用 askpass 脚本进行认证
        self._run_git("config", "core.askPass", str(git_askpass_script))
        # 测试连接预检（避免 token 无效后后续操作全部失败）
        self._check_auth()

        return {"success": True, "message": "Gitee 远程仓库配置成功", "repo_url": repo_url}

    def _check_auth(self):
        """预检 git 认证是否有效"""
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", "-q"],
            cwd=str(self.data_dir),
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        # 不阻断：即使 ls-remote 失败，也可能在 push 时使用不同策略
        if result.returncode != 0:
            logger.warning("Gitee 远程仓库认证预检失败: %s", result.stderr.strip() or "unknown error")

    def get_status(self) -> Dict[str, Any]:
        """获取 git 状态"""
        if not self.is_git_repo():
            return {"initialized": False, "has_remote": False, "pending_changes": 0}

        ok, status = self._run_git("status", "--porcelain")
        pending = len([l for l in status.splitlines() if l.strip()]) if ok else 0

        ok2, remote = self._run_git("remote", "get-url", "origin")
        # 隐藏 token
        if ok2 and "@" in remote:
            parts = remote.split("@")
            remote = parts[0].split("://")[0] + "://*****@" + parts[1]

        ok3, log = self._run_git("log", "--oneline", "-5")
        recent_commits = log.splitlines() if ok3 else []

        return {
            "initialized": True,
            "has_remote": ok2,
            "remote_url": remote if ok2 else None,
            "pending_changes": pending,
            "recent_commits": recent_commits,
        }

    def push_to_gitee(self, commit_message: str = "") -> Dict[str, Any]:
        """
        手动推送到 Gitee（用户主动触发，不自动执行）
        
        步骤：git add → git commit → git push
        """
        if not self.is_git_repo():
            return {"success": False, "error": "未初始化 git 仓库，请先在设置中配置 Gitee"}

        # 检查是否有 remote
        ok, _ = self._run_git("remote", "get-url", "origin")
        if not ok:
            return {"success": False, "error": "未配置 Gitee 远程仓库"}

        # git add
        ok, add_out = self._run_git("add", ".")
        if not ok:
            return {"success": False, "error": f"git add 失败: {add_out}"}

        # 检查是否有变更
        ok2, status = self._run_git("status", "--porcelain")
        if ok2 and not (status or "").strip():
            return {"success": True, "message": "没有新的变更需要推送"}

        # git commit
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        msg_text = commit_message or f"AI Agent System 数据备份 {ts}"
        ok, commit_out = self._run_git("commit", "-m", msg_text)
        if not ok and "nothing to commit" not in (commit_out or ""):
            return {"success": False, "error": f"git commit 失败: {commit_out}"}

        # git push
        ok, push_out = self._run_git("push", "-u", "origin", "HEAD")
        if not ok:
            # 尝试 master / main 分支
            ok, push_out = self._run_git("push", "--set-upstream", "origin", "master")
            if not ok:
                ok, push_out = self._run_git("push", "--set-upstream", "origin", "main")
            if not ok:
                return {"success": False, "error": f"推送失败: {push_out}"}

        return {
            "success": True,
            "message": f"已成功推送到 Gitee",
            "commit_message": msg_text,
            "pushed_at": ts,
        }

    def pull_from_gitee(self) -> Dict[str, Any]:
        """从 Gitee 拉取最新数据"""
        if not self.is_git_repo():
            return {"success": False, "error": "未初始化 git 仓库"}

        ok, msg = self._run_git("pull", "origin", "HEAD")
        if not ok:
            ok, msg = self._run_git("pull", "origin", "master")
            if not ok:
                ok, msg = self._run_git("pull", "origin", "main")
            if not ok:
                return {"success": False, "error": f"拉取失败: {msg}"}

        return {"success": True, "message": "已从 Gitee 拉取最新数据", "output": msg}
