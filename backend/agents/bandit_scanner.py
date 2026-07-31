"""
Bandit SAST 安全扫描器
集成 Bandit 进行静态应用安全测试（SAST）
"""

import subprocess
import json
import tempfile
from pathlib import Path
from typing import List, Dict, Any, Tuple


class BanditScanner:
    """Bandit SAST 扫描器"""
    
    def __init__(self):
        self.severity_map = {
            "HIGH": "critical",
            "MEDIUM": "high",
            "LOW": "medium",
        }
    
    def scan_files(self, file_sources: List[Tuple[str, str]]) -> Dict[str, Any]:
        """
        扫描文件列表
        
        Args:
            file_sources: [(file_path, content), ...]
            
        Returns:
            {
                "passed": bool,
                "issues": List[Dict],
                "summary": {
                    "total": int,
                    "by_severity": {...}
                }
            }
        """
        if not file_sources:
            return {"passed": True, "issues": [], "summary": {"total": 0, "by_severity": {}}}
        
        # 创建临时目录存放文件
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            
            # 写入所有文件
            for file_path, content in file_sources:
                # 只扫描 Python 文件
                if not file_path.endswith('.py'):
                    continue
                
                file_full_path = tmp_path / file_path.lstrip('/')
                file_full_path.parent.mkdir(parents=True, exist_ok=True)
                file_full_path.write_text(content, encoding='utf-8')
            
            # 运行 Bandit
            try:
                result = subprocess.run(
                    ['bandit', '-r', str(tmp_path), '-f', 'json'],
                    capture_output=True,
                    text=True,
                    timeout=60
                )
                
                # Bandit 返回码：0=无问题, 1=有问题
                if result.returncode not in [0, 1]:
                    return {
                        "passed": False,
                        "issues": [{
                            "file": "N/A",
                            "severity": "critical",
                            "message": f"Bandit 执行失败: {result.stderr}",
                            "line": 0
                        }],
                        "summary": {"total": 1, "by_severity": {"critical": 1}}
                    }
                
                # 解析结果
                try:
                    bandit_output = json.loads(result.stdout)
                except json.JSONDecodeError:
                    return {
                        "passed": False,
                        "issues": [{
                            "file": "N/A",
                            "severity": "critical",
                            "message": "Bandit 输出解析失败",
                            "line": 0
                        }],
                        "summary": {"total": 1, "by_severity": {"critical": 1}}
                    }
                
                issues = []
                severity_count = {"critical": 0, "high": 0, "medium": 0, "low": 0}
                
                for result_item in bandit_output.get('results', []):
                    severity = self.severity_map.get(result_item.get('issue_severity', 'LOW'), 'medium')
                    
                    # 将临时路径转换回原始路径
                    file_path = result_item.get('filename', '').replace(str(tmp_path), '')
                    
                    issue = {
                        "file": file_path,
                        "line": result_item.get('line_number', 0),
                        "severity": severity,
                        "message": result_item.get('issue_text', ''),
                        "cwe": result_item.get('issue_cwe', {}).get('id', 'N/A'),
                        "confidence": result_item.get('issue_confidence', 'MEDIUM'),
                        "code": result_item.get('code', ''),
                        "test_id": result_item.get('test_id', ''),
                    }
                    
                    issues.append(issue)
                    severity_count[severity] += 1
                
                # 判断是否通过：critical 或 high 问题视为不通过
                passed = severity_count['critical'] == 0 and severity_count['high'] == 0
                
                return {
                    "passed": passed,
                    "issues": issues,
                    "summary": {
                        "total": len(issues),
                        "by_severity": severity_count,
                        "metrics": bandit_output.get('metrics', {})
                    }
                }
                
            except subprocess.TimeoutExpired:
                return {
                    "passed": False,
                    "issues": [{
                        "file": "N/A",
                        "severity": "critical",
                        "message": "Bandit 扫描超时（>60s）",
                        "line": 0
                    }],
                    "summary": {"total": 1, "by_severity": {"critical": 1}}
                }
            except FileNotFoundError:
                # Bandit 未安装
                return {
                    "passed": True,  # 不阻断流程
                    "issues": [{
                        "file": "N/A",
                        "severity": "info",
                        "message": "Bandit 未安装，跳过 SAST 扫描",
                        "line": 0
                    }],
                    "summary": {"total": 0, "by_severity": {}}
                }
            except Exception as e:
                return {
                    "passed": False,
                    "issues": [{
                        "file": "N/A",
                        "severity": "critical",
                        "message": f"Bandit 扫描异常: {str(e)}",
                        "line": 0
                    }],
                    "summary": {"total": 1, "by_severity": {"critical": 1}}
                }
    
    def format_issues_for_report(self, issues: List[Dict]) -> str:
        """格式化问题列表为可读报告"""
        if not issues:
            return "✅ 未发现安全问题"
        
        lines = ["## 🔒 Bandit SAST 扫描结果\n"]
        
        # 按严重程度分组
        by_severity = {"critical": [], "high": [], "medium": [], "low": []}
        for issue in issues:
            severity = issue.get('severity', 'low')
            by_severity.get(severity, by_severity['low']).append(issue)
        
        # 输出各级别问题
        for severity in ['critical', 'high', 'medium', 'low']:
            items = by_severity[severity]
            if not items:
                continue
            
            severity_icon = {
                'critical': '🔴',
                'high': '🟠',
                'medium': '🟡',
                'low': '🔵'
            }.get(severity, '⚪')
            
            lines.append(f"\n### {severity_icon} {severity.upper()} ({len(items)})")
            
            for item in items:
                lines.append(f"\n**{item['file']}:{item['line']}**")
                lines.append(f"- 问题：{item['message']}")
                lines.append(f"- CWE：{item.get('cwe', 'N/A')}")
                lines.append(f"- 置信度：{item.get('confidence', 'MEDIUM')}")
                if item.get('code'):
                    lines.append(f"- 代码：`{item['code'].strip()}`")
        
        return "\n".join(lines)
