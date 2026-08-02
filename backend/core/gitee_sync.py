"""Git repository synchronization for GitHub and Gitee."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse


logger = logging.getLogger(__name__)
SUPPORTED_PROVIDERS = {"github": "github.com", "gitee": "gitee.com"}


def _provider_name(provider: str) -> str:
    return "GitHub" if provider == "github" else "Gitee" if provider == "gitee" else "Git"


class GiteeSync:
    """Backward-compatible name for the generic Git sync manager."""

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = Path(data_dir or os.getenv("METIS_DATA_DIR", "data"))
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _run_git(
        self,
        *args: str,
        cwd: Optional[Path] = None,
        auth_token: str = "",
    ) -> Tuple[bool, str]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        helper: Optional[Path] = None
        if auth_token:
            handle = tempfile.NamedTemporaryFile("w", suffix=".py", encoding="utf-8", delete=False)
            handle.write(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "prompt = sys.argv[1] if len(sys.argv) > 1 else ''\n"
                "print('oauth2' if 'username' in prompt.lower() else os.environ.get('METIS_GIT_TOKEN', ''))\n"
            )
            handle.close()
            helper = Path(handle.name)
            helper.chmod(0o700)
            env["GIT_ASKPASS"] = str(helper.resolve())
            env["METIS_GIT_TOKEN"] = auth_token
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(cwd or self.data_dir),
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            output = result.stdout.strip() if result.returncode == 0 else result.stderr.strip()
            return result.returncode == 0, output
        except FileNotFoundError:
            return False, "服务器未安装 Git"
        except subprocess.TimeoutExpired:
            return False, "Git 操作超时"
        except Exception as exc:
            logger.exception("Git operation failed")
            return False, str(exc)
        finally:
            if helper is not None:
                helper.unlink(missing_ok=True)

    @staticmethod
    def validate_remote(provider: str, repo_url: str) -> Tuple[str, str]:
        normalized_provider = str(provider or "").strip().lower()
        if normalized_provider not in SUPPORTED_PROVIDERS:
            raise ValueError("仅支持 GitHub 或 Gitee")
        normalized_url = str(repo_url or "").strip()
        parsed = urlparse(normalized_url)
        if parsed.scheme != "https" or parsed.hostname != SUPPORTED_PROVIDERS[normalized_provider]:
            raise ValueError(f"仓库地址必须是 https://{SUPPORTED_PROVIDERS[normalized_provider]}/... 格式")
        if parsed.username or parsed.password:
            raise ValueError("仓库地址不能包含用户名、密码或 Token")
        if not parsed.path.strip("/") or parsed.path.strip("/").count("/") < 1:
            raise ValueError("仓库地址缺少所有者或仓库名称")
        return normalized_provider, normalized_url

    def is_git_repo(self) -> bool:
        return (self.data_dir / ".git").exists()

    def init_repo(self) -> Dict[str, Any]:
        if self.is_git_repo():
            return {"success": True, "message": "数据目录已初始化"}
        ok, message = self._run_git("init")
        if not ok:
            return {"success": False, "error": message}
        (self.data_dir / ".gitignore").write_text(
            ".env\n.env.*\n!.env.example\n*.key\n*.pem\n*.p12\n*.tmp\n",
            encoding="utf-8",
        )
        self._run_git("config", "user.email", "metis@local")
        self._run_git("config", "user.name", "METIS")
        return {"success": True, "message": "数据目录已初始化"}

    def set_remote(self, repo_url: str, token: str, provider: str = "gitee") -> Dict[str, Any]:
        try:
            provider, repo_url = self.validate_remote(provider, repo_url)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        if not token.strip():
            return {"success": False, "error": "访问令牌不能为空"}
        initialized = self.init_repo()
        if not initialized["success"]:
            return initialized

        ok, remotes = self._run_git("remote")
        if ok and "origin" in remotes.splitlines():
            ok, message = self._run_git("remote", "set-url", "origin", repo_url)
        else:
            ok, message = self._run_git("remote", "add", "origin", repo_url)
        if not ok:
            return {"success": False, "error": message}

        ok, message = self._run_git("ls-remote", "--heads", "origin", auth_token=token)
        if not ok:
            return {"success": False, "error": f"仓库连接失败：{message}"}
        return {
            "success": True,
            "message": f"{_provider_name(provider)} 仓库验证并绑定成功",
            "provider": provider,
            "repo_url": repo_url,
        }

    def get_status(self, provider: str = "", repo_url: str = "") -> Dict[str, Any]:
        if not self.is_git_repo():
            return {"initialized": False, "has_remote": False, "pending_changes": 0, "provider": provider or None}
        ok, status = self._run_git("status", "--porcelain")
        pending = len([line for line in status.splitlines() if line.strip()]) if ok else 0
        ok_remote, remote = self._run_git("remote", "get-url", "origin")
        ok_log, log = self._run_git("log", "--oneline", "-5")
        if not provider and ok_remote:
            host = urlparse(remote).hostname
            provider = next((key for key, value in SUPPORTED_PROVIDERS.items() if value == host), "")
        return {
            "initialized": True,
            "has_remote": ok_remote and bool(repo_url or remote),
            "provider": provider or None,
            "remote_url": repo_url or (remote if ok_remote else None),
            "pending_changes": pending,
            "recent_commits": log.splitlines() if ok_log else [],
        }

    def push(self, token: str, provider: str, commit_message: str = "") -> Dict[str, Any]:
        if not self.is_git_repo():
            return {"success": False, "error": "请先绑定 Git 仓库"}
        if not token:
            return {"success": False, "error": "仓库令牌不可用，请重新绑定"}
        ok, output = self._run_git("add", ".")
        if not ok:
            return {"success": False, "error": f"暂存失败：{output}"}
        ok, status = self._run_git("status", "--porcelain")
        if ok and status.strip():
            commit_message = commit_message or f"METIS 数据备份 {time.strftime('%Y-%m-%d %H:%M:%S')}"
            ok, output = self._run_git("commit", "-m", commit_message)
            if not ok:
                return {"success": False, "error": f"提交失败：{output}"}
        else:
            commit_message = ""
        ok, output = self._run_git("push", "-u", "origin", "HEAD", auth_token=token)
        if not ok:
            return {"success": False, "error": f"推送失败：{output}"}
        return {
            "success": True,
            "message": f"已成功推送到 {_provider_name(provider)}",
            "commit_message": commit_message,
            "pushed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def pull(self, token: str, provider: str) -> Dict[str, Any]:
        if not self.is_git_repo():
            return {"success": False, "error": "请先绑定 Git 仓库"}
        if not token:
            return {"success": False, "error": "仓库令牌不可用，请重新绑定"}
        ok, output = self._run_git("pull", "origin", "HEAD", auth_token=token)
        if not ok:
            return {"success": False, "error": f"拉取失败：{output}"}
        return {"success": True, "message": f"已从 {_provider_name(provider)} 拉取最新数据", "output": output}

    # Compatibility methods for older callers.
    def push_to_gitee(self, commit_message: str = "") -> Dict[str, Any]:
        return {"success": False, "error": "请通过新的 Git 同步接口重新绑定仓库"}

    def pull_from_gitee(self) -> Dict[str, Any]:
        return {"success": False, "error": "请通过新的 Git 同步接口重新绑定仓库"}
