"""
OWASP 安全扫描器 v1
基于正则匹配的静态代码安全检查，覆盖 5 类常见漏洞：
  - SQL 注入 (SQLi)
  - 跨站脚本 (XSS)
  - 硬编码密钥 (Hardcoded Secrets)
  - 路径遍历 (Path Traversal)
  - 命令注入 (Command Injection)
"""

import re
from typing import Dict, List, Any, Tuple


class OWASPScanner:
    """
    OWASP 安全扫描器 v1
    基于正则匹配的静态代码安全检查，覆盖 5 类常见漏洞。

    用法:
        scanner = OWASPScanner()
        findings = scanner.scan(content, "app/routes.py")
        report = scanner.scan_files([("routes.py", content1)])
    """

    VULN_PATTERNS = {
        "sql_injection": {
            "label": "SQL 注入",
            "severity": "critical",
            "description": "检测到潜在的 SQL 注入风险：未使用参数化查询，而是通过字符串拼接/f-string 构建 SQL",
            "rules": [
                (r'''(?:cursor|connection|db|db_session|pool)\.(?:execute|exec|query)\(\s*[fF]["'](?=.*(?:SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|EXEC|CALL)).*?\{''', 1.0),
                (r'''(?:cursor|connection|db)\.(?:execute|exec|query)\(\s*(["'])(?=.*(?:SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|EXEC|CALL))(?:(?!\1).)*\1\s*\+''', 1.0),
                (r'''(?:cursor|connection|db)\.(?:execute|exec|query)\(\s*["'](?=.*(?:SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|EXEC|CALL)).*?%\s*[srd]''', 1.0),
                (r'''(?:raw_sql|RawSQL|raw_query)\(\s*[fF]["']''', 1.0),
            ],
        },
        "xss": {
            "label": "跨站脚本 (XSS)",
            "severity": "high",
            "description": "检测到潜在的 XSS 风险：未对用户输入进行无害化处理直接输出到 DOM",
            "rules": [
                (r'''\.innerHTML\s*=(?!=)''', 1.0),
                (r'''dangerouslySetInnerHTML''', 1.0),
                (r'''document\.write\([^)]*[\w+$\-]''', 0.9),
                (r'''v-html\s*=['"]''', 0.9),
                (r'''\.innerHTML\s*\+=''', 1.0),
                (r'''\$\(.*\)\.html\([^)]*[\w+\-]''', 0.7),
            ],
        },
        "hardcoded_secrets": {
            "label": "硬编码密钥",
            "severity": "critical",
            "description": "检测到硬编码密钥/凭据：密钥、密码、Token 不应直接写在源码中",
            "rules": [
                (r'''(?i)(?:api[_-]?key|secret[_-]?key|access[_-]?key|auth[_-]?token|private[_-]?key|encryption[_-]?key)\s*[:=]\s*["\'][A-Za-z0-9_\-!@#$%^&*()+=\[\]{}|;:,.<>?/~`]{16,}["\']''', 1.0),
                (r'''(?i)(?:password|passwd|pwd)\s*[:=]\s*["\'][A-Za-z0-9_\-!@#$%^&*()+=\[\]{}|;:,.<>?/~`]{8,}["\']''', 1.0),
                (r'''(?i)jwt[_-]?(?:secret|key|token)\s*[:=]\s*["\'][^"']{10,}["\']''', 1.0),
                (r'''-----BEGIN\s+(?:RSA|OPENSSH|EC|DSA|PRIVATE)\s+KEY-----''', 1.0),
                (r'''(?i)(?:token|secret|credential|api_key)\s*=\s*["\'][^"'\s]{20,}["\']''', 0.8),
            ],
        },
        "path_traversal": {
            "label": "路径遍历",
            "severity": "high",
            "description": "检测到潜在的路径遍历风险：用户输入拼接文件路径时未经校验",
            "rules": [
                (r'''(?:open|read_file|read_text|send_file|File|load_file)\([^)]*(?:request|req\.|params|query|body|form|args|kwargs|user_input|filename|filepath)''', 0.9),
                (r'''os\.path\.join\([^)]*(?:request|req\.|params|query|body|form|user_input|filename|filepath)''', 0.9),
                (r'''Path\([^)]*(?:request|req\.|params|query|body|form|user_input|filename|filepath)''', 0.8),
                (r'''(?:\.\.\/|\.\.\\)''', 0.7),
            ],
        },
        "command_injection": {
            "label": "命令注入",
            "severity": "critical",
            "description": "检测到潜在的 OS 命令注入风险：用户输入拼接执行系统命令",
            "rules": [
                (r'''os\.system\(\s*[fF]["']''', 1.0),
                (r'''subprocess\.(?:run|call|Popen|check_output|check_call)\([^)]*shell\s*=\s*True[^)]*[fF]["']''', 1.0),
                (r'''subprocess\.(?:run|call|Popen|check_output|check_call)\([^)]*shell\s*=\s*True[^)]*["']\s*\+''', 1.0),
                (r'''(?:eval|exec)\(\s*(?:request|req\.|params|query|body|form|user_input|data\s*=|input|raw_input)''', 1.0),
                (r'''subprocess\.(?:run|call|Popen|check_output)\([^)]*["'][^"']*?(?:\+|%|\.format|f[{"\']).*?["']''', 0.8),
                (r'''os\.popen\(\s*[fF]["']''', 1.0),
            ],
        },
    }

    def __init__(self):
        self._compiled: Dict[str, List[Tuple[re.Pattern, float, str]]] = {}
        for vuln_type, config in self.VULN_PATTERNS.items():
            self._compiled[vuln_type] = []
            for pattern_str, weight in config["rules"]:
                try:
                    compiled = re.compile(pattern_str, re.IGNORECASE | re.DOTALL)
                    self._compiled[vuln_type].append((compiled, weight, pattern_str))
                except re.error:
                    pass

    def scan(self, content: str, file_path: str = "") -> List[Dict]:
        """扫描单文件内容，返回命中详情列表"""
        results: List[Dict] = []
        seen: set = set()

        for vuln_type, compiled_rules in self._compiled.items():
            config = self.VULN_PATTERNS[vuln_type]
            for compiled_re, weight, pattern_str in compiled_rules:
                for match in compiled_re.finditer(content):
                    line_start = content.rfind('\n', 0, match.start()) + 1
                    line_no = content[:match.start()].count('\n') + 1
                    col_no = match.start() - line_start + 1
                    line_end = content.find('\n', match.end())
                    if line_end == -1:
                        line_end = len(content)
                    snippet = content[line_start:line_end].strip()
                    if len(snippet) > 120:
                        snippet = snippet[:60] + "..." + snippet[-60:]
                    sig = (vuln_type, line_no, snippet[:50])
                    if sig in seen:
                        continue
                    seen.add(sig)
                    results.append({
                        "file": file_path,
                        "line": line_no,
                        "column": col_no,
                        "type": vuln_type,
                        "label": config["label"],
                        "severity": config["severity"],
                        "snippet": snippet,
                        "description": config["description"],
                        "pattern": pattern_str,
                        "weight": weight,
                    })

        results.sort(key=lambda r: (r["file"], r["line"], r["column"]))
        return results

    def scan_files(self, file_sources: List[Tuple[str, str]]) -> Dict[str, Any]:
        """批量扫描文件，返回汇总报告"""
        all_findings: List[Dict] = []
        for rel_path, content in file_sources:
            all_findings.extend(self.scan(content, rel_path))

        by_severity: Dict[str, int] = {}
        for f in all_findings:
            sev = f["severity"]
            by_severity[sev] = by_severity.get(sev, 0) + 1

        has_critical = by_severity.get("critical", 0) > 0
        has_high = by_severity.get("high", 0) > 0
        total = len(all_findings)

        if has_critical:
            security_status = "critical"
        elif has_high:
            security_status = "high_risk"
        elif total > 0:
            security_status = "warning"
        else:
            security_status = "clean"

        return {
            "security_status": security_status,
            "findings": all_findings,
            "summary": {
                "total_findings": total,
                "by_severity": by_severity,
                "by_category": {
                    t: len([f for f in all_findings if f["type"] == t])
                    for t in {f["type"] for f in all_findings}
                },
                "status": security_status,
                "passed": not has_critical,
                "score": max(0, 100 - total * 15),
            },
        }
