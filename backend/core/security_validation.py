"""Deterministic security checks for generated project files."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple


SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"\s]{16,}['\"]"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{20,}"),
]


def validate_generated_files(files: List[Tuple[str, str]]) -> Dict[str, Any]:
    """Run non-LLM security checks over generated files."""
    issues: List[Dict[str, Any]] = []

    for rel_path, content in files:
        for line_no, line in enumerate(content.splitlines(), 1):
            for pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    issues.append({
                        "file": rel_path,
                        "line": line_no,
                        "layer": "security",
                        "severity": "error",
                        "message": "Potential hard-coded secret or credential",
                        "fix_hint": "Move secrets to server-side configuration or environment variables.",
                    })
                    break

        if rel_path.endswith((".html", ".js", ".jsx", ".ts", ".tsx")):
            if "innerHTML" in content and not re.search(r"sanitize|DOMPurify|textContent", content):
                issues.append({
                    "file": rel_path,
                    "line": 0,
                    "layer": "security",
                    "severity": "warning",
                    "message": "innerHTML is used without an obvious sanitizer",
                    "fix_hint": "Use textContent or sanitize untrusted HTML before insertion.",
                })
            if re.search(r"eval\s*\(|new\s+Function\s*\(", content):
                issues.append({
                    "file": rel_path,
                    "line": 0,
                    "layer": "security",
                    "severity": "error",
                    "message": "Dynamic code execution detected",
                    "fix_hint": "Remove eval/new Function and use explicit control flow.",
                })

    error_count = sum(1 for issue in issues if issue.get("severity") == "error")
    return {
        "layer": "security",
        "passed": error_count == 0,
        "score": max(0, 100 - error_count * 30 - (len(issues) - error_count) * 10),
        "issues": issues,
        "issue_count": len(issues),
    }
