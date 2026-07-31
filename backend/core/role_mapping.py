"""Canonical role-to-expert mapping used by phase execution."""

from __future__ import annotations

import re
from typing import FrozenSet, Optional


_CANONICAL_EXPERT_TYPES = frozenset({
    "frontend",
    "backend",
    "database",
    "qa",
    "architecture",
    "devops",
    "security",
    "data",
    "fullstack_engineer",
})

_EXACT_ALIASES = {
    "developer": "fullstack_engineer",
    "engineer": "fullstack_engineer",
    "software developer": "fullstack_engineer",
    "software engineer": "fullstack_engineer",
    "web developer": "fullstack_engineer",
    "web engineer": "fullstack_engineer",
    "qa\u5de5\u7a0b\u5e08": "qa",
    "\u5168\u6808\u5de5\u7a0b\u5e08": "fullstack_engineer",
    "开发者": "fullstack_engineer",
    "开发工程师": "fullstack_engineer",
    "工程师": "fullstack_engineer",
    "软件工程师": "fullstack_engineer",
    "程序员": "fullstack_engineer",
}

_ENGLISH_ROLE_PATTERNS = (
    (
        "fullstack_engineer",
        re.compile(r"\b(?:full\s*stack|fullstack)(?:\s+(?:developer|engineer))?\b"),
    ),
    (
        "frontend",
        re.compile(
            r"\b(?:front\s*end|frontend)\b"
            r"|\b(?:react|vue)(?:\.js)?(?:\s+(?:developer|engineer|expert))?\b"
            r"|\b(?:ui\s*ux|ui|ux|user\s+interface)\s+designer\b"
        ),
    ),
    (
        "backend",
        re.compile(
            r"\b(?:back\s*end|backend)\b"
            r"|\bapi\s+(?:developer|engineer|designer|expert)\b"
            r"|\bnode(?:\.?js)?(?:\s+(?:developer|engineer|expert))?\b"
            r"|\b(?:fastapi|express)(?:\s+(?:developer|engineer|expert))?\b"
        ),
    ),
    (
        "database",
        re.compile(
            r"\b(?:database|data\s+base|sql|nosql|postgres|postgresql)"
            r"(?:\s+(?:developer|engineer|administrator|expert|dba))?\b"
        ),
    ),
    (
        "qa",
        re.compile(
            r"\bquality\s+assurance\b|\bqa\b"
            r"|\btest(?:\s+automation)?\s+engineer\b|\btester\b|\bsdet\b"
        ),
    ),
    (
        "architecture",
        re.compile(r"\barchitecture\b|\barchitect\b"),
    ),
    (
        "devops",
        re.compile(
            r"\bdevops\b|\bdeployment\s+engineer\b|\bdocker\s+engineer\b"
            r"|\bsre\b|\bplatform\s+engineer\b"
            r"|\bsite\s+reliability\s+engineer\b"
        ),
    ),
    (
        "security",
        re.compile(r"\bsecurity\b|\bappsec\b|\bcyber\s*security\b"),
    ),
    (
        "data",
        re.compile(r"\bdata\s+(?:engineer|analyst|scientist)\b"),
    ),
)

_LOCALIZED_ROLE_MARKERS = (
    ("fullstack_engineer", ("全栈", "全能工程师", "web开发工程师", "web工程师")),
    ("frontend", ("前端", "用户界面设计师", "交互设计师")),
    ("backend", ("后端", "api开发", "api设计", "api工程师")),
    ("database", ("数据库",)),
    ("qa", ("测试", "质量保证", "质检")),
    ("architecture", ("架构",)),
    ("devops", ("运维", "部署", "平台工程师")),
    ("security", ("安全",)),
    ("data", ("数据工程", "数据分析", "数据科学")),
)


def _normalized_role_label(role_label: str) -> str:
    normalized = re.sub(r"[-_/]+", " ", (role_label or "").casefold())
    return " ".join(normalized.split())


def expert_type_candidates(role_label: str) -> FrozenSet[str]:
    """Return every supported executor type explicitly named by a role."""
    normalized = _normalized_role_label(role_label)
    candidates = set()
    canonical_label = normalized.replace(" ", "_")
    if canonical_label in _CANONICAL_EXPERT_TYPES:
        candidates.add(canonical_label)
    exact = _EXACT_ALIASES.get(normalized)
    if exact:
        candidates.add(exact)
    for expert_type, pattern in _ENGLISH_ROLE_PATTERNS:
        if pattern.search(normalized):
            candidates.add(expert_type)
    for expert_type, markers in _LOCALIZED_ROLE_MARKERS:
        if any(marker in normalized for marker in markers):
            candidates.add(expert_type)
    return frozenset(candidates)


def infer_english_expert_type(role_label: str) -> Optional[str]:
    """Compatibility entrypoint for resolving one unambiguous executor."""
    return canonical_expert_type(role_label)


def canonical_expert_type(role_label: str) -> Optional[str]:
    """Resolve one executor; unknown or genuinely composite roles fail closed."""
    candidates = expert_type_candidates(role_label)
    if len(candidates) == 1:
        return next(iter(candidates))
    if (
        "fullstack_engineer" in candidates
        and candidates <= {
            "fullstack_engineer", "frontend", "backend", "database",
        }
    ):
        return "fullstack_engineer"
    return None
