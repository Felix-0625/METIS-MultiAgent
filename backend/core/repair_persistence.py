"""
repair_persistence.py - RepairRegistry 持久化管理辅助模块

提供 RepairRegistry 的加载和保存功能，用于在应用启动时恢复修复状态，
以及在关键操作后自动保存状态。
"""

import logging
from pathlib import Path
from typing import Optional

from .repair_loop import repair_registry

logger = logging.getLogger(__name__)

# 默认持久化文件路径
DEFAULT_REPAIR_STATE_PATH = "data/repair_registry_state.json"


def load_repair_registry(filepath: Optional[str] = None) -> bool:
    """
    从文件加载 RepairRegistry 状态。
    
    Args:
        filepath: JSON 文件路径，默认使用 DEFAULT_REPAIR_STATE_PATH
    
    Returns:
        True 表示加载成功，False 表示文件不存在或加载失败
    """
    path = filepath or DEFAULT_REPAIR_STATE_PATH
    try:
        success = repair_registry.load_from_file(path)
        if success:
            logger.info(f"✅ RepairRegistry 状态已从 {path} 恢复")
            # 统计加载的数据
            total_controllers = sum(len(subs) for subs in repair_registry._controllers.values())
            total_arbiters = sum(len(arbs) for arbs in repair_registry._arbiters.values())
            logger.info(f"   - 已恢复 {total_controllers} 个修复控制器")
            logger.info(f"   - 已恢复 {total_arbiters} 个仲裁者")
        else:
            logger.info(f"ℹ️  RepairRegistry 状态文件不存在：{path}，将从空状态开始")
        return success
    except Exception as e:
        logger.error(f"❌ 加载 RepairRegistry 状态失败：{e}")
        return False


def save_repair_registry(filepath: Optional[str] = None) -> bool:
    """
    将 RepairRegistry 状态保存到文件，并设置安全文件权限。
    
    Args:
        filepath: JSON 文件路径，默认使用 DEFAULT_REPAIR_STATE_PATH
    
    Returns:
        True 表示保存成功，False 表示保存失败
    """
    path = filepath or DEFAULT_REPAIR_STATE_PATH
    try:
        repair_registry.save_to_file(path)
        # 设置安全文件权限（700 目录 / 600 文件），防止其他用户读取
        import os
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        # 确保父目录权限安全
        parent = Path(path).parent
        if parent.exists():
            try:
                parent.chmod(0o700)
            except OSError:
                pass
        logger.info(f"💾 RepairRegistry 状态已保存到 {path}")
        return True
    except Exception as e:
        logger.error(f"❌ 保存 RepairRegistry 状态失败：{e}")
        return False


def auto_save_repair_registry(filepath: Optional[str] = None):
    """
    自动保存装饰器/辅助函数，可用于包装需要自动保存的操作。
    
    使用示例：
        @auto_save_repair_registry()
        def some_operation():
            # ... 修改 repair_registry 状态的操作
            pass
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            save_repair_registry(filepath)
            return result
        return wrapper
    return decorator


# ============================================================================
# FastAPI 生命周期钩子（可选用）
# ============================================================================

async def startup_load_repair_registry(filepath: Optional[str] = None):
    """FastAPI 启动事件：加载 RepairRegistry 状态"""
    load_repair_registry(filepath)


async def shutdown_save_repair_registry(filepath: Optional[str] = None):
    """FastAPI 关闭事件：保存 RepairRegistry 状态"""
    save_repair_registry(filepath)
