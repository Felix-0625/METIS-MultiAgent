"""
配置加载器
支持 YAML 和 JSON 格式的配置文件
"""

import json
import yaml
from typing import Dict, Any, Optional
from pathlib import Path


class ConfigLoader:
    """
    配置加载器
    
    支持：
    - YAML 格式
    - JSON 格式
    - 环境变量覆盖
    """

    def __init__(self, config_dir: str = "config"):
        self.config_dir = Path(config_dir)
        self._configs: Dict[str, Dict] = {}

    def load(self, filename: str) -> Dict[str, Any]:
        """
        加载配置文件
        
        Args:
            filename: 配置文件名（不含扩展名）
            
        Returns:
            配置字典
        """
        if filename in self._configs:
            return self._configs[filename]

        # 尝试加载 YAML
        yaml_path = self.config_dir / f"{filename}.yaml"
        if yaml_path.exists():
            with open(yaml_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
                self._configs[filename] = config
                return config

        # 尝试加载 JSON
        json_path = self.config_dir / f"{filename}.json"
        if json_path.exists():
            with open(json_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                self._configs[filename] = config
                return config

        raise FileNotFoundError(f"Config file not found: {filename}")

    def load_all(self) -> Dict[str, Dict]:
        """加载所有配置文件"""
        for path in self.config_dir.glob("*.yaml"):
            name = path.stem
            self.load(name)
        for path in self.config_dir.glob("*.json"):
            name = path.stem
            if name not in self._configs:
                self.load(name)
        return self._configs

    def get(self, filename: str, key: str, default: Any = None) -> Any:
        """
        获取配置值
        
        支持点号分隔的嵌套键，如 "pm_agent.llm.model"
        """
        config = self.load(filename)
        keys = key.split(".")
        value = config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
        return value if value is not None else default

    def reload(self, filename: str) -> Dict:
        """重新加载配置"""
        if filename in self._configs:
            del self._configs[filename]
        return self.load(filename)

    def set(self, filename: str, key: str, value: Any) -> None:
        """
        设置配置值（仅内存中）
        
        不会持久化到文件
        """
        if filename not in self._configs:
            self.load(filename)
        
        keys = key.split(".")
        config = self._configs[filename]
        
        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]
        
        config[keys[-1]] = value

    def save(self, filename: str) -> None:
        """
        保存配置到文件
        
        根据原文件扩展名保存
        """
        if filename not in self._configs:
            return

        config = self._configs[filename]
        
        yaml_path = self.config_dir / f"{filename}.yaml"
        if yaml_path.exists():
            with open(yaml_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
            return

        json_path = self.config_dir / f"{filename}.json"
        if json_path.exists():
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)