"""Deterministic PM contract extraction and plan validation.

The requirement document is the authority.  Model output may enrich prose, but
it cannot change identifiers, ordering, names, roles, technology or declared
dependencies captured by :class:`ProjectContract`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from core.delivery_contract import collect_required_file_paths, is_delivery_file_path
from core.role_mapping import canonical_expert_type


CONTRACT_VERSION = 3
PLAN_VERSION = 1
PHASE_PLAN_VERSION = 1

_TECH_ALIASES = (
    ("node.js", ("node.js", "nodejs")),
    ("express", ("express",)),
    ("typescript", ("typescript",)),
    ("javascript", ("javascript",)),
    ("sqlite", ("sqlite",)),
    ("prisma", ("prisma",)),
    ("better-sqlite3", ("better-sqlite3",)),
    ("sequelize", ("sequelize",)),
    ("react", ("react",)),
    ("vite", ("vite",)),
    ("python", ("python",)),
    ("fastapi", ("fastapi",)),
    ("django", ("django",)),
    ("flask", ("flask",)),
    ("spring boot", ("spring boot",)),
    ("postgresql", ("postgresql", "postgres")),
    ("mysql", ("mysql",)),
    ("mongodb", ("mongodb",)),
    ("vue", ("vue", "vue.js", "vuejs")),
    ("angular", ("angular",)),
    ("docker", ("docker",)),
    ("kubernetes", ("kubernetes",)),
    ("redis", ("redis",)),
    ("jwt", ("jwt",)),
)
_CONFLICT_GROUPS = (
    frozenset(("react", "vue", "angular")),
    frozenset(("express", "fastapi", "django", "flask", "spring boot")),
    frozenset(("sqlite", "postgresql", "mysql", "mongodb")),
    frozenset(("prisma", "sequelize", "better-sqlite3")),
)
_TECH_CHOICE_PAIRS = (
    ("vue", "react"), ("better-sqlite3", "sequelize"),
    ("prisma", "sequelize"), ("express", "fastapi"),
)
_LOCK_CUES = ("技术栈", "固定", "必须使用", "严格遵循", "only use", "must use", "fixed stack")
_KNOWN_FORBIDDEN = (
    "roles table", "permissions table", "role table", "permission table",
    "public registration", "公开注册", "开放注册", "默认管理员密码",
    "rbac admin", "rbac 管理后台", "fine-grained rbac",
)
_CN_NUMBERS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


_EN_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _strings(item)


def _flatten_text(value: Any) -> str:
    return " ".join(_strings(value)).lower()


def _split_values(value: str) -> Tuple[str, ...]:
    # A spaced slash may delimit roles, while embedded slashes belong to
    # repository paths and API endpoints such as backend/package.json.
    return tuple(part.strip(" \t-*") for part in re.split(
        r"[,，、;；|]+|\s+/\s+", value,
    ) if part.strip(" \t-*"))


_BINDING_CUES = re.compile(
    r"(?:必须|不得|禁止|固定|严格|只(?:能|允许)|需要|应当|先|再|然后|最后|阶段|验收|"
    r"\bmust\b|\bshall\b|\brequired\b|\bonly\b|\bneed(?:s|ed)?\b)",
    re.IGNORECASE,
)
_ACCEPTANCE_CUES = re.compile(r"(?:验收|通过|成功|完成|acceptance|pass(?:es|ed)?)", re.IGNORECASE)
_OUTLINE_CUES = re.compile(r"(?:阶段|步骤|任务|功能|先|再|然后|最后|phase|step|task|feature)", re.IGNORECASE)


def extract_requirement_units(text: str) -> Tuple[RequirementUnit, ...]:
    """Split free-form Chinese/English requirements without imposing a template."""
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    # Preserve wrapped prose as one requirement. A lowercase continuation line
    # is not a new outline item (for example "must work\nin all package roots").
    raw = re.sub(r"\n(?=[ \t]*[a-z])", " ", raw)
    chunks: List[Tuple[str, bool]] = []

    def append_items(value: str, *, outline_hint: bool = False) -> None:
        numbered = list(re.finditer(
            r"(?:^|[\s；;])(?:\d+|[一二三四五六七八九十]+)"
            r"(?:[)、]\s*|\.(?=\s|[^\d])\s*)",
            value,
        ))
        if numbered:
            prefix = value[:numbered[0].start()].strip()
            if prefix:
                chunks.append((prefix, outline_hint))
            for index, marker in enumerate(numbered):
                start = marker.end()
                end = numbered[index + 1].start() if index + 1 < len(numbered) else len(value)
                item = value[start:end].strip()
                if item:
                    chunks.append((item, True))
            return
        cleaned = re.sub(
            r"^\s*(?:[-*+]|\d+[.)、]|[一二三四五六七八九十]+[、.)])\s*",
            "",
            value,
        ).strip()
        if not cleaned:
            return
        for item in re.split(r"(?<=[。！？!?；;])\s*", cleaned):
            if item.strip():
                chunks.append((item.strip(), outline_hint))

    for paragraph in re.split(r"\n+", raw):
        stripped = paragraph.strip()
        if not stripped:
            continue
        if "|" in stripped:
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if cells and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells):
                continue
            normalized_cells = {cell.casefold() for cell in cells}
            if normalized_cells and normalized_cells <= {
                "id", "no", "编号", "序号", "requirement", "requirements",
                "需求", "需求项", "功能",
            }:
                continue
            if cells and re.fullmatch(r"(?:\d+|[一二三四五六七八九十]+)", cells[0]):
                cells = cells[1:]
            append_items(" | ".join(cell for cell in cells if cell), outline_hint=True)
            continue
        append_items(
            stripped,
            outline_hint=bool(re.match(r"^\s*[-*+]\s+", paragraph)),
        )
    units: List[RequirementUnit] = []
    for order, (exact_text, outline_hint) in enumerate(chunks, 1):
        normalized = " ".join(exact_text.split())
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        kind = (
            "acceptance" if _ACCEPTANCE_CUES.search(normalized)
            else "outline" if outline_hint or _OUTLINE_CUES.search(normalized)
            else "constraint" if _BINDING_CUES.search(normalized)
            else "context"
        )
        units.append(RequirementUnit(
            unit_id=f"req-{order:03d}-{digest[:10]}",
            order=order,
            exact_text=exact_text,
            digest=f"sha256:{digest}",
            kind=kind,
            binding=bool(_BINDING_CUES.search(normalized)),
        ))
    return tuple(units)


def _number(value: str) -> int:
    if value.isdigit():
        return int(value)
    return _CN_NUMBERS.get(value, _EN_NUMBERS.get(value.lower(), 0))


def _dedupe(values: Iterable[str]) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if str(value).strip()))


def _detect_technologies(text: str) -> Tuple[str, ...]:
    lower = text.lower()
    found: List[str] = []
    for canonical, aliases in _TECH_ALIASES:
        if any(re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", lower) for alias in aliases):
            found.append(canonical)
    return tuple(found)


_NON_BINDING_TECH_CUES = re.compile(
    r"(?:"
    r"不要|不得|禁止|避免|不(?:使用|采用|选择)|"
    r"可(?:以)?考虑|可选|例如|比如|示例|仅作示例|"
    r"由(?:\s*pm|产品经理|专家|用户)?\s*(?:自行)?选择|"
    r"\bdo\s+not\b|\bdon't\b|\bmust\s+not\b|\bavoid\b|"
    r"\bmay\b|\bmight\b|\bcould\b|\boptional\b|"
    r"\bfor\s+example\b|\bas\s+an?\s+example\b|\be\.g\."
    r")",
    re.IGNORECASE,
)
_BINDING_TECH_CUES = re.compile(
    r"(?:"
    r"技术栈\s*(?:固定(?:为|是)?|改为|必须(?:使用|采用)?|采用|使用)|"
    r"(?:必须|务必|固定|严格(?:遵循)?)[^。！？；;\n]{0,16}(?:使用|采用)|"
    r"\b(?:use|required|must(?:\s+use)?|built\s+with|powered\s+by)\b"
    r")",
    re.IGNORECASE,
)


def _explicit_required_technology_source(text: str) -> str:
    """Return only technology clauses that impose a binding choice."""
    clauses = re.split(
        r"[。！？!?；;\n]+|\.(?=\s+[A-Z])|"
        r"[,，](?=\s*(?:不要|不得|禁止|避免|不(?:使用|采用)|可考虑|"
        r"可选|例如|比如|示例|但|但是))|"
        r"\bbut\b",
        str(text or ""),
        flags=re.IGNORECASE,
    )
    selected: List[str] = []
    for clause in clauses:
        forbidden_cue = _FORBIDDEN_TECH_CUE.search(clause)
        binding_clause = (
            clause[:forbidden_cue.start()]
            if forbidden_cue
            else clause
        )
        technologies = _detect_technologies(binding_clause)
        if (
            technologies
            and not _NON_BINDING_TECH_CUES.search(binding_clause)
            and (
                _BINDING_TECH_CUES.search(binding_clause)
                or len(technologies) == 1
            )
        ):
            selected.append(binding_clause)
    return " ".join(selected)


_FORBIDDEN_TECH_CUE = re.compile(
    r"(?:"
    r"不要|不得|禁止|避免|不(?:使用|采用|添加|引入|需要)|"
    r"\bdo\s+not\b|\bdon't\b|\bmust\s+not\b|\bavoid\b|"
    r"\bwithout\b|\bno\b"
    r")",
    re.IGNORECASE,
)


def _explicit_forbidden_technologies(text: str) -> Tuple[str, ...]:
    """Extract technologies inside explicit negative requirement clauses."""
    clauses = re.split(
        r"[。！？!?；;\n]+|\.(?=\s+[A-Z])",
        str(text or ""),
    )
    forbidden: List[str] = []
    for clause in clauses:
        cue = _FORBIDDEN_TECH_CUE.search(clause)
        if not cue:
            continue
        negative_tail = re.split(
            r"\bbut\b|但(?:是)?",
            clause[cue.start():],
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        negative_tail = negative_tail.split("|", 1)[0]
        forbidden.extend(_detect_technologies(negative_tail))
    return _dedupe(forbidden)


class FrozenContractDict(dict):
    """JSON-serialisable immutable mapping used by legacy dict callers."""

    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("ProjectContract is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _immutable

    def __copy__(self) -> "FrozenContractDict":
        return self

    def __deepcopy__(self, _memo: Dict[int, Any]) -> "FrozenContractDict":
        return self


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenContractDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class LockedTask:
    task_id: str
    order: int
    name: str
    roles: Tuple[str, ...] = ()
    forbidden_scope: Tuple[str, ...] = ()
    acceptance_criteria: Tuple[str, ...] = ()
    dependencies: Tuple[str, ...] = ()
    source_constraints: Tuple[str, ...] = ()
    source_requirement_ids: Tuple[str, ...] = ()
    planning_placeholder: bool = False

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "task_id": self.task_id, "order": self.order, "name": self.name,
            "roles": list(self.roles), "forbidden_scope": list(self.forbidden_scope),
            "acceptance_criteria": list(self.acceptance_criteria),
            "dependencies": list(self.dependencies), "source_constraints": list(self.source_constraints),
            "source_requirement_ids": list(self.source_requirement_ids),
        }
        if self.planning_placeholder:
            data["planning_placeholder"] = True
        return data


@dataclass(frozen=True)
class PhaseContract:
    phase_id: str
    order: int
    name: str
    roles: Tuple[str, ...] = ()
    tasks: Tuple[LockedTask, ...] = ()
    forbidden_scope: Tuple[str, ...] = ()
    acceptance_criteria: Tuple[str, ...] = ()
    dependencies: Tuple[str, ...] = ()
    source_constraints: Tuple[str, ...] = ()
    planning_placeholder: bool = False

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "phase_id": self.phase_id, "order": self.order, "name": self.name,
            "roles": list(self.roles), "locked_task_count": len(self.tasks),
            "tasks": [task.to_dict() for task in self.tasks],
            "forbidden_scope": list(self.forbidden_scope),
            "acceptance_criteria": list(self.acceptance_criteria),
            "dependencies": list(self.dependencies), "source_constraints": list(self.source_constraints),
        }
        if self.planning_placeholder:
            data["planning_placeholder"] = True
        return data


@dataclass(frozen=True)
class RequiredFileContract:
    """Immutable ownership record for one project deliverable."""

    path: str
    owner_type: str
    phase_id: str
    required: bool = True
    task_id: str = ""
    criterion: str = ""
    evidence_spec: str = "registry_byte_digest"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "owner_type": self.owner_type,
            "phase_id": self.phase_id,
            "required": self.required,
            "task_id": self.task_id,
            "criterion": self.criterion,
            "evidence_spec": self.evidence_spec,
        }


@dataclass(frozen=True)
class RequirementUnit:
    """One deterministic, verbatim-addressable unit from the user requirement."""

    unit_id: str
    order: int
    exact_text: str
    digest: str
    kind: str
    binding: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "order": self.order,
            "exact_text": self.exact_text,
            "digest": self.digest,
            "kind": self.kind,
            "binding": self.binding,
        }


@dataclass(frozen=True)
class ProjectContract:
    contract_version: int
    technology_stack: Tuple[str, ...]
    phase_count: int | None
    minimum_phase_count: int | None
    phases: Tuple[PhaseContract, ...]
    roles: Tuple[str, ...]
    forbidden_scope: Tuple[str, ...]
    acceptance_criteria: Tuple[str, ...]
    dependencies: Tuple[str, ...]
    source_constraints: Tuple[str, ...]
    source_requirements: str = field(repr=False)
    locked: bool = True
    required_files: Tuple[RequiredFileContract, ...] = ()
    requirements_summary: str = ""
    requirements_digest: str = ""
    requirement_units: Tuple[RequirementUnit, ...] = ()
    requirements_revision: int = 0
    requirement_event_ids: Tuple[str, ...] = ()
    requirement_lineage_digest: str = ""

    def to_dict(self) -> Dict[str, Any]:
        phase_counts = {phase.phase_id: len(phase.tasks) for phase in self.phases if phase.tasks}
        return {
            "contract_version": self.contract_version, "version": self.contract_version,
            "locked": self.locked, "technology_stack": list(self.technology_stack),
            "required_tech": list(self.technology_stack), "phase_count": self.phase_count,
            "required_phase_count": self.phase_count,
            "minimum_phase_count": self.minimum_phase_count,
            "phases": [phase.to_dict() for phase in self.phases],
            "roles": list(self.roles), "forbidden_scope": list(self.forbidden_scope),
            "acceptance_criteria": list(self.acceptance_criteria), "dependencies": list(self.dependencies),
            "source_constraints": list(self.source_constraints), "phase_task_counts": phase_counts,
            "source_requirements": self.source_requirements,
            "required_files": [item.to_dict() for item in self.required_files],
            "requirements_summary": self.requirements_summary,
            "requirements_digest": self.requirements_digest,
            "requirement_units": [item.to_dict() for item in self.requirement_units],
            "requirements_revision": self.requirements_revision,
            "requirement_event_ids": list(self.requirement_event_ids),
            "requirement_lineage_digest": self.requirement_lineage_digest,
        }

    def as_mapping(self) -> FrozenContractDict:
        return _freeze(self.to_dict())


@dataclass(frozen=True)
class ValidationIssue:
    layer: str
    code: str
    path: str
    message: str
    expected: Any = None
    actual: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {key: value for key, value in vars(self).items() if value is not None}


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    artifact_type: str
    issues: Tuple[ValidationIssue, ...]
    contract_version: int
    schema_version: int

    @property
    def violations(self) -> List[str]:
        return [issue.message for issue in self.issues]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid, "artifact_type": self.artifact_type,
            "contract_version": self.contract_version, "schema_version": self.schema_version,
            "issues": [issue.to_dict() for issue in self.issues],
        }


PLAN_SCHEMA: Dict[str, Any] = {
    "$id": "metis://schemas/project-plan/v1", "type": "object", "required": ["phases"],
    "properties": {
        "plan_version": {"type": "integer"}, "tech_stack": {"type": ["object", "array", "string"]},
        "phases": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["phase_id"]}},
        "subprojects": {"type": "array", "items": {"type": "object"}},
    },
}
PHASE_PLAN_SCHEMA: Dict[str, Any] = {
    "$id": "metis://schemas/phase-plan/v1", "type": "array", "minItems": 1,
    "items": {"type": "object", "required": ["task_id", "task_name", "task_description", "required_role"]},
}


def _extract_labeled(text: str, labels: Sequence[str]) -> Tuple[str, ...]:
    label = "|".join(re.escape(item) for item in labels)
    matches = re.findall(rf"(?:^|\n)\s*(?:[-*]\s*)?(?:{label})\s*[：:]\s*([^\n]+)", text, re.IGNORECASE)
    return _dedupe(part for match in matches for part in _split_values(match))


_MINIMUM_PHASE_PATTERNS = (
    re.compile(
        r"(?:至少|不少于|不得少于|最少)\s*"
        r"([一二三四五六七八九十]|\d+)\s*(?:个)?\s*"
        r"[^。！？!?；;\n0-9一二三四五六七八九十]{0,40}?"
        r"阶段",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:at\s+least|(?:a\s+)?minimum(?:\s+of)?)\s+"
        r"(one|two|three|four|five|six|seven|eight|nine|ten|\d+)"
        r"\s+[^.!?;\n0-9]{0,64}?phases?\b",
        re.IGNORECASE,
    ),
)


def _extract_minimum_phase_count(text: str) -> int | None:
    counts = [
        _number(match.group(1))
        for pattern in _MINIMUM_PHASE_PATTERNS
        for match in pattern.finditer(text)
    ]
    return max((count for count in counts if count > 0), default=None)


def _extract_phase_count(text: str) -> int | None:
    exact_text = text
    for pattern in _MINIMUM_PHASE_PATTERNS:
        exact_text = pattern.sub(" ", exact_text)
    match = re.search(
        r"(?:严格(?:规划|划分)?(?:为)?|固定(?:为)?|共|"
        r"规划(?:恰好|正好)?(?:为)?|划分为|分为|恰好|正好)\s*"
        r"([一二三四五六七八九十]|\d+)\s*(?:个)?\s*阶段",
        exact_text,
        re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"(?:strictly|exactly|plans?(?:\s+for)?|planned(?:\s+for)?|"
            r"divided?\s+into)\s+"
            r"(one|two|three|four|five|six|seven|eight|nine|ten|\d+)"
            r"\s+(?:implementation\s+)?phases?",
            exact_text,
            re.IGNORECASE,
        )
    return _number(match.group(1)) if match else None


def _extract_locked_phase_task_counts(
    text: str,
    phase_count: int | None = None,
) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    per_phase = re.search(
        r"(?:每|各)(?:个)?\s*阶段(?:均|各自)?\s*"
        r"(?:严格|固定|只(?:包含|有)?)?\s*"
        r"([一二三四五六七八九十]|\d+)\s*(?:项|个)?\s*任务",
        text,
        re.IGNORECASE,
    )
    if per_phase and phase_count:
        count = _number(per_phase.group(1))
        if count > 0:
            counts.update(
                (f"phase-{index}", count)
                for index in range(1, phase_count + 1)
            )
    phase_token = r"(?:[一二三四五六七八九十]|\d+)"
    bounded = rf"(?:(?!阶段\s*{phase_token})[\s\S])"
    patterns = (
        re.compile(
            rf"阶段\s*({phase_token}){bounded}{{0,100}}?"
            rf"任务(?:清单|边界)?{bounded}{{0,40}}?"
            r"(?:共|严格|固定|锁定)\s*"
            rf"({phase_token})\s*(?:项|个)?",
            re.IGNORECASE,
        ),
        re.compile(
            rf"阶段\s*({phase_token}){bounded}{{0,100}}?"
            rf"任务(?:清单|边界)?{bounded}{{0,40}}?"
            rf"({phase_token})\s*(?:项|个)",
            re.IGNORECASE,
        ),
        re.compile(
            rf"阶段\s*({phase_token}){bounded}{{0,100}}?"
            r"(?:共|严格|固定|锁定)\s*"
            rf"({phase_token})\s*(?:(?:项|个)\s*)?任务",
            re.IGNORECASE,
        ),
        re.compile(
            rf"阶段\s*({phase_token}){bounded}{{0,100}}?"
            rf"({phase_token})\s*(?:项|个)\s*任务",
            re.IGNORECASE,
        ),
    )
    for pattern in patterns:
        for phase_no, count in pattern.findall(text):
            if _number(phase_no) and _number(count):
                counts[f"phase-{_number(phase_no)}"] = _number(count)
    if not counts and all(marker in text for marker in ("任务一", "任务二", "任务三", "任务四")):
        counts["phase-1"] = 4
    return counts


def _task_from_mapping(item: Mapping[str, Any], phase_id: str, order: int, phase_roles: Tuple[str, ...]) -> LockedTask:
    task_id = str(item.get("task_id") or f"{phase_id}-task-{order}").strip()
    name = str(item.get("name") or item.get("task_name") or item.get("deliverable") or f"任务 {order}").strip()
    roles = _dedupe(_as_strings(item.get("roles") or item.get("roles_needed") or item.get("required_role"))) or phase_roles
    return LockedTask(
        task_id=task_id, order=order, name=name, roles=roles,
        forbidden_scope=_dedupe(_as_strings(item.get("forbidden_scope"))),
        acceptance_criteria=_dedupe(_as_strings(item.get("acceptance_criteria"))),
        dependencies=_dedupe(_as_strings(item.get("dependencies"))),
        source_constraints=_dedupe(_as_strings(item.get("source_constraints") or item.get("source_refs"))),
        source_requirement_ids=_dedupe(_as_strings(item.get("source_requirement_ids"))),
        planning_placeholder=bool(item.get("planning_placeholder", False)),
    )


def _as_strings(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return _split_values(value)
    if isinstance(value, (list, tuple, set)):
        return _dedupe(str(item) for item in value)
    return (str(value),)


_PLACEHOLDER_VALUE = re.compile(
    r"^(?:"
    r"tbd|todo|n/?a|none|null|unknown|pending|later|"
    r"to\s+be\s+(?:determined|assigned|confirmed)|"
    r"待定|待补充|待完善|待确认|待分配|待阶段pm分配|"
    r"暂无|未知|后续确定|由阶段pm分配"
    r")$",
    re.IGNORECASE,
)


def _is_placeholder_value(value: Any) -> bool:
    normalized = " ".join(str(value or "").strip().split()).lower()
    return not normalized or bool(_PLACEHOLDER_VALUE.fullmatch(normalized))


def _real_strings(value: Any) -> Tuple[str, ...]:
    if isinstance(value, str):
        candidates = (value.strip(),)
    elif isinstance(value, Mapping):
        candidates = tuple(_strings(value))
    else:
        candidates = _as_strings(value)
    return _dedupe(
        item
        for item in candidates
        if not _is_placeholder_value(item)
    )


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _phases_from_plan(plan: Mapping[str, Any]) -> Tuple[PhaseContract, ...]:
    phases: List[PhaseContract] = []
    for order, raw in enumerate(plan.get("phases") or [], 1):
        if not isinstance(raw, Mapping):
            continue
        phase_id = str(raw.get("phase_id") or f"phase-{order}").strip()
        roles = _dedupe(_as_strings(raw.get("roles") or raw.get("roles_needed")))
        raw_tasks = raw.get("task_contract") or raw.get("tasks") or []
        tasks = tuple(_task_from_mapping(item, phase_id, index, roles) for index, item in enumerate(raw_tasks, 1) if isinstance(item, Mapping))
        phases.append(PhaseContract(
            phase_id=phase_id, order=order,
            name=str(raw.get("name") or raw.get("phase_name") or f"阶段 {order}").strip(),
            roles=roles, tasks=tasks,
            forbidden_scope=_dedupe(_as_strings(raw.get("forbidden_scope"))),
            acceptance_criteria=_dedupe(_as_strings(raw.get("acceptance_criteria"))),
            dependencies=_dedupe(_as_strings(raw.get("dependencies"))),
            source_constraints=_dedupe(_as_strings(raw.get("source_constraints") or raw.get("source_refs"))),
        ))
    return tuple(phases)


def _phases_from_text(text: str, phase_count: int | None, global_roles: Tuple[str, ...]) -> Tuple[PhaseContract, ...]:
    heading = re.compile(r"^\s*阶段\s*([一二三四五六七八九十]|\d+)\s*[：:]?\s*([^\n]*)$", re.MULTILINE)
    matches = list(heading.finditer(text))
    english_headings = False
    if not matches:
        heading = re.compile(
            r"^\s*phase\s*(\d+)\s*[:.-]?\s*([^\n]*)$",
            re.MULTILINE | re.IGNORECASE,
        )
        matches = list(heading.finditer(text))
        english_headings = bool(matches)
    phase_rows: List[Tuple[int, str, str]] = []
    for index, match in enumerate(matches):
        number = _number(match.group(1))
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        phase_rows.append((number, match.group(2).strip(" ：:（("), text[match.end():body_end]))
    generated_phase_skeleton = not phase_rows and bool(phase_count)
    if generated_phase_skeleton:
        phase_rows = [(index, f"阶段{next((key for key, value in _CN_NUMBERS.items() if value == index), index)}", "") for index in range(1, phase_count + 1)]
    phases: List[PhaseContract] = []
    for order, (number, name, body) in enumerate(phase_rows, 1):
        phase_id = f"phase-{number or order}"
        phase_roles = _extract_labeled(body, ("角色集合", "允许角色", "角色")) or global_roles
        if english_headings:
            phase_roles = (
                _extract_labeled(body, ("owner", "owners", "role", "roles"))
                or phase_roles
            )
        tasks: List[LockedTask] = []
        task_line = re.compile(
            r"^\s*(?:[-*]\s*)?(?:(phase-\d+-task-\d+|t\d+)\s*[|｜:：-]\s*|任务\s*([一二三四五六七八九十]|\d+)\s*[|｜:：.-]\s*)([^\n]+)$",
            re.MULTILINE | re.IGNORECASE,
        )
        for task_order, match in enumerate(task_line.finditer(body), 1):
            explicit_id, ordinal, remainder = match.groups()
            parts = [part.strip() for part in re.split(r"[|｜]", remainder)]
            task_id = explicit_id or f"{phase_id}-task-{_number(ordinal) or task_order}"
            name_part = re.sub(r"^(?:名称|任务名)\s*[：:]\s*", "", parts[0]).strip()
            role_parts = [part.split("：", 1)[-1].split(":", 1)[-1] for part in parts[1:] if re.match(r"^(?:角色|roles?)\s*[：:]", part, re.I)]
            dep_parts = [part.split("：", 1)[-1].split(":", 1)[-1] for part in parts[1:] if re.match(r"^(?:依赖|depends?)\s*[：:]", part, re.I)]
            accept_parts = [part.split("：", 1)[-1].split(":", 1)[-1] for part in parts[1:] if re.match(r"^(?:验收|acceptance)\s*[：:]", part, re.I)]
            source_parts = [part.split("：", 1)[-1].split(":", 1)[-1] for part in parts[1:] if re.match(r"^(?:来源|source)\s*[：:]", part, re.I)]
            tasks.append(LockedTask(
                task_id=task_id, order=task_order, name=name_part,
                roles=_dedupe(value for part in role_parts for value in _split_values(part)) or phase_roles,
                acceptance_criteria=_dedupe(value for part in accept_parts for value in _split_values(part)),
                dependencies=_dedupe(value for part in dep_parts for value in _split_values(part)),
                source_constraints=_dedupe(value for part in source_parts for value in _split_values(part)),
            ))
        if english_headings and not tasks:
            task_block = re.search(
                r"(?:^|\n)\s*tasks?\s*:\s*\n(?P<body>.*?)(?="
                r"^\s*(?:acceptance|owner|owners|files?|phase)\s*:|\Z)",
                body,
                re.MULTILINE | re.IGNORECASE | re.DOTALL,
            )
            task_names: List[str] = []
            if task_block:
                current: List[str] = []
                for line in task_block.group("body").splitlines():
                    bullet = re.match(r"^\s*[-*+]\s+(.+?)\s*$", line)
                    if bullet:
                        if current:
                            task_names.append(" ".join(current))
                        current = [bullet.group(1).strip()]
                    elif current and line.strip():
                        current.append(line.strip())
                if current:
                    task_names.append(" ".join(current))
            for task_order, task_name in enumerate(task_names, 1):
                tasks.append(LockedTask(
                    task_id=f"{phase_id}-task-{task_order}",
                    order=task_order,
                    name=task_name,
                    roles=phase_roles,
                ))
        phase_acceptance = (
            _extract_labeled(body, ("acceptance",))
            if english_headings
            else _extract_labeled(body, ("验收标准", "验收"))
        )
        if phase_acceptance:
            tasks = [
                replace(
                    task,
                    acceptance_criteria=task.acceptance_criteria or phase_acceptance,
                )
                for task in tasks
            ]
        phases.append(PhaseContract(
            phase_id=phase_id, order=order, name=name or f"阶段 {number or order}", roles=phase_roles, tasks=tuple(tasks),
            forbidden_scope=_extract_labeled(body, ("禁止范围", "不得", "禁止")),
            acceptance_criteria=phase_acceptance,
            dependencies=_extract_labeled(body, ("阶段依赖", "依赖")),
            source_constraints=_extract_labeled(body, ("来源约束", "来源", "source constraints")),
            planning_placeholder=generated_phase_skeleton,
        ))
    return tuple(phases)


_ROOT_INTEGRATION_FILES = {
    "package.json", "dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".env.example", ".dockerignore", "readme.md",
}


def _normalize_delivery_path(path: Any) -> str:
    normalized = str(path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if (
        not normalized
        or "\x00" in normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or ".." in PurePosixPath(normalized).parts
    ):
        return ""
    return str(PurePosixPath(normalized))


def _canonical_role_type(role: Any) -> str:
    value = str(role or "").strip()
    return canonical_expert_type(value) or value.lower().replace(" ", "_")


def required_file_owner_type(path: str) -> str:
    """Return the single canonical owner for a delivery path."""
    normalized = _normalize_delivery_path(path).lower()
    name = PurePosixPath(normalized).name
    qa_directories = (
        "tests/", "test/", "cypress/", "playwright/", "e2e/", "integration/",
    )
    if (
        normalized.startswith(qa_directories)
        or any(f"/{directory}" in normalized for directory in qa_directories)
        or any(
            marker in name for marker in (
                ".test.", ".spec.", ".e2e.", "test_", "_test.", "_e2e.",
            )
        )
        or name in {
            "cypress.json",
            "cypress.config.js",
            "cypress.config.ts",
            "jest.config.js",
            "jest.config.ts",
            "playwright.config.js",
            "playwright.config.ts",
            "pytest.ini",
            "vitest.config.js",
            "vitest.config.ts",
        }
    ):
        return "qa"
    if normalized.startswith(("frontend/", "public/")):
        return "frontend"
    if "/" not in normalized and PurePosixPath(name).suffix in {
        ".html", ".css",
    }:
        return "frontend"
    if normalized.startswith(("backend/prisma/", "backend/migrations/", "database/")):
        return "database"
    if normalized.startswith("backend/"):
        return "backend"
    if normalized.startswith("security/"):
        return "security"
    if normalized.startswith("docs/architecture/"):
        return "architecture"
    if normalized.startswith("deploy/") or name in _ROOT_INTEGRATION_FILES:
        return "devops"
    return "backend"


_SHARED_ROOT_FILE_OWNERS = {
    ".dockerignore": {
        "devops", "fullstack_engineer",
    },
    ".env.example": {
        "frontend", "backend", "devops", "security", "fullstack_engineer",
    },
    "package.json": {
        "frontend", "backend", "qa", "devops", "fullstack_engineer",
    },
    "readme.md": {
        "frontend", "backend", "database", "qa", "devops", "security",
        "architecture", "data", "fullstack_engineer",
    },
}


def _owner_permitted_for_path(path: str, owner_type: str) -> bool:
    normalized = _normalize_delivery_path(path).lower()
    expected_owner = required_file_owner_type(path)
    protected_prefixes = (
        "frontend/",
        "public/",
        "backend/",
        "database/",
        "security/",
        "docs/architecture/",
        "deploy/",
        "tests/",
        "test/",
        "cypress/",
        "playwright/",
        "e2e/",
        "integration/",
    )
    ambiguous_nested_path = (
        "/" in normalized
        and not normalized.startswith(protected_prefixes)
    )
    root_script = (
        "/" not in normalized
        and PurePosixPath(normalized).suffix in {
            ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
        }
    )
    return (
        owner_type == expected_owner
        or owner_type in _SHARED_ROOT_FILE_OWNERS.get(normalized, set())
        or (
            ambiguous_nested_path
            and owner_type in {
                "frontend",
                "backend",
                "database",
                "qa",
                "devops",
                "security",
                "architecture",
                "data",
                "fullstack_engineer",
            }
        )
        or (
            expected_owner == "qa"
            and owner_type in {
                "frontend", "backend", "qa", "fullstack_engineer",
            }
        )
        or (
            root_script
            and owner_type in {
                "frontend", "backend", "qa", "devops", "fullstack_engineer",
            }
        )
    )


def _role_allows_owner(role_types: Sequence[str], owner_type: str) -> bool:
    roles = set(role_types)
    engineering_roles = {
        "frontend", "backend", "database", "qa", "devops", "security",
        "architecture", "data", "fullstack_engineer",
    }
    # Product/user roles such as admin/employee/technician are not execution
    # Agent roles.  They must not make an otherwise valid delivery manifest
    # impossible to confirm; the explicit owner_type remains authoritative.
    if not roles.intersection(engineering_roles):
        return True
    return owner_type in roles or "fullstack_engineer" in roles


def _stack_required_paths(stack: Sequence[str]) -> List[str]:
    technologies = {str(item).lower() for item in stack}
    result = {"README.md"}
    node = bool(technologies & {"node.js", "javascript", "typescript", "express", "react", "vite"})
    frontend = bool(technologies & {"react", "vite", "vue", "angular"})
    if node:
        result.add("package.json")
    if frontend:
        result.add("frontend/package.json")
    # These are runtime/build entry contracts, not optional implementation
    # details.  Lock them before execution so a strict task scope cannot omit
    # the files and then silence the downstream delivery validator.
    if technologies & {"react", "vite"}:
        result.add("frontend/index.html")
    if frontend and "typescript" in technologies:
        result.add("frontend/tsconfig.json")
    if "docker" in technologies:
        result.update(("Dockerfile", ".env.example"))
    return sorted(result, key=str.lower)


def _requirement_phase_file_mentions(
    requirements: str,
    phases: Sequence[PhaseContract],
) -> Dict[str, List[str]]:
    """Map files in explicit requirement phase sections to their phase IDs."""
    text = str(requirements or "")
    if not text.strip() or not phases:
        return {}
    lower = text.lower()
    starts: List[Tuple[int, PhaseContract]] = []
    cursor = 0
    for phase in sorted(phases, key=lambda item: item.order):
        name = str(phase.name or "").strip().lower()
        if not name:
            return {}
        start = lower.find(name, cursor)
        if start < 0:
            # Partial matches are unsafe: a missing heading would make the
            # previous phase absorb another phase's delivery files.
            return {}
        starts.append((start, phase))
        cursor = start + len(name)

    mentions: Dict[str, List[str]] = {}
    for index, (start, phase) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
        for path in collect_required_file_paths(text[start:end]):
            mentions.setdefault(path.lower(), []).append(phase.phase_id)
    return mentions


def build_required_files_manifest(
    requirements: str,
    plan: Mapping[str, Any],
    phases: Sequence[PhaseContract],
    stack: Sequence[str],
    explicit_files: Sequence[Mapping[str, Any]] | None = None,
) -> Tuple[RequiredFileContract, ...]:
    """Create a deterministic, explicit file ownership manifest.

    An explicit manifest wins, while legacy plans receive a compatible manifest
    derived from declared paths and technology-specific package boundaries.
    """
    raw_explicit = list(explicit_files or plan.get("required_files") or [])
    explicit_by_path: Dict[str, Mapping[str, Any]] = {}
    for item in raw_explicit:
        if isinstance(item, Mapping):
            path = _normalize_delivery_path(item.get("path"))
            if path:
                explicit_by_path.setdefault(path.lower(), item)

    has_structured_task_files = any(
        isinstance(task, Mapping) and bool(task.get("required_files"))
        for phase in (plan.get("phases") or [])
        if isinstance(phase, Mapping)
        for task in (phase.get("task_contract") or phase.get("tasks") or [])
    )
    stack_required_paths = set(_stack_required_paths(stack))
    if has_structured_task_files:
        paths = set(collect_required_file_paths(requirements))
        for phase in (plan.get("phases") or []):
            if not isinstance(phase, Mapping):
                continue
            paths.update(collect_required_file_paths(
                "",
                {"required_files": phase.get("required_files") or []},
            ))
            for task in (phase.get("task_contract") or phase.get("tasks") or []):
                if isinstance(task, Mapping):
                    paths.update(collect_required_file_paths(
                        "",
                        {"required_files": task.get("required_files") or []},
                    ))
        paths.update(stack_required_paths)
    else:
        paths = set(collect_required_file_paths(requirements, plan))
        paths.update(stack_required_paths)
    technologies = {str(item).lower() for item in stack}
    if (
        technologies & {"express", "node.js"}
        and any(
            str(path).replace("\\", "/").lower().startswith("backend/")
            for path in paths
        )
    ):
        paths.add("backend/package.json")
    paths.update(_normalize_delivery_path(item.get("path")) for item in raw_explicit if isinstance(item, Mapping))
    paths.discard("")

    requirement_phase_mentions = _requirement_phase_file_mentions(
        requirements, phases,
    )
    phase_mentions: Dict[str, List[str]] = {
        path: list(phase_ids)
        for path, phase_ids in requirement_phase_mentions.items()
    }
    structured_task_mentions: Dict[str, List[str]] = {}
    plan_phase_mentions: Dict[str, List[str]] = {}
    raw_phases = [item for item in plan.get("phases") or [] if isinstance(item, Mapping)]
    raw_tasks_by_phase = {
        str(item.get("phase_id") or ""): [
            task for task in (item.get("task_contract") or item.get("tasks") or [])
            if isinstance(task, Mapping)
        ]
        for item in raw_phases
    }
    locked_tasks_by_phase = {
        phase.phase_id: tuple(phase.tasks)
        for phase in phases
    }

    def declared_task_paths(task: Mapping[str, Any]) -> set[str]:
        delivery_fields = {
            key: task.get(key)
            for key in (
                "deliverable", "deliverables", "path", "paths", "file", "files",
                "required_file", "required_files",
            )
            if task.get(key)
        }
        return {
            item.casefold()
            for item in collect_required_file_paths("", delivery_fields)
        }

    for raw_phase in raw_phases:
        phase_id = str(raw_phase.get("phase_id") or "").strip()
        for task in raw_tasks_by_phase.get(phase_id, []):
            for path in declared_task_paths(task):
                structured_task_mentions.setdefault(path, []).append(phase_id)
        for path in collect_required_file_paths("", raw_phase):
            phase_mentions.setdefault(path.lower(), []).append(phase_id)
            plan_phase_mentions.setdefault(path.lower(), []).append(phase_id)

    role_types_by_phase = {
        phase.phase_id: tuple(_canonical_role_type(role) for role in phase.roles)
        for phase in phases
    }
    manifest: List[RequiredFileContract] = []
    for path in sorted(paths, key=str.lower):
        if not is_delivery_file_path(path):
            continue
        explicit = explicit_by_path.get(path.lower(), {})
        explicit_owner = str(explicit.get("owner_type") or "").strip().lower()
        owner_type = explicit_owner or required_file_owner_type(path)
        phase_id = str(explicit.get("phase_id") or "").strip()
        # A structured plan mention is more precise than prose extraction.
        # Requirement-section mentions fill only paths the plan did not place.
        raw_mentions = (
            structured_task_mentions.get(path.lower())
            or plan_phase_mentions.get(path.lower())
            or phase_mentions.get(path.lower(), [])
        )
        mentions = list(dict.fromkeys(item for item in raw_mentions if item))
        if not phase_id and len(mentions) == 1:
            phase_id = mentions[0]
        elif not phase_id and len(mentions) > 1:
            eligible = [
                item for item in mentions
                if _role_allows_owner(role_types_by_phase.get(item, ()), owner_type)
            ] or mentions
            phase_id = eligible[-1] if path.lower() in _ROOT_INTEGRATION_FILES else eligible[0]
        if not phase_id:
            candidates = [
                phase.phase_id for phase in phases
                if _role_allows_owner(role_types_by_phase.get(phase.phase_id, ()), owner_type)
            ]
            if candidates:
                # Root integration artifacts are deliberately assigned to a
                # single integration-capable phase, never copied across phases.
                phase_id = candidates[0]
            elif phases:
                phase_id = phases[0].phase_id
        if not explicit_owner and phase_id:
            path_task_roles = [
                _canonical_role_type(role)
                for task in raw_tasks_by_phase.get(phase_id, [])
                if path.casefold() in declared_task_paths(task)
                for role in _as_strings(
                    task.get("roles")
                    or task.get("roles_needed")
                    or task.get("required_role")
                )
            ]
            preferred_roles = tuple(
                dict.fromkeys(
                    role for role in (
                        path_task_roles
                        or list(role_types_by_phase.get(phase_id, ()))
                    )
                    if role
                )
            )
            compatible_owners = [
                role for role in preferred_roles
                if _owner_permitted_for_path(path, role)
            ]
            if (
                compatible_owners
                and owner_type not in preferred_roles
                and (
                    not _role_allows_owner(preferred_roles, owner_type)
                    or path.casefold() == "readme.md"
                )
            ):
                owner_type = compatible_owners[0]
        locked_tasks = locked_tasks_by_phase.get(phase_id, ())
        valid_task_ids = {task.task_id for task in locked_tasks}
        matching_task_ids = [
            str(task.get("task_id") or task.get("id") or "")
            for task in raw_tasks_by_phase.get(phase_id, [])
            if (
                path.casefold() in declared_task_paths(task)
                and str(task.get("task_id") or task.get("id") or "")
                in valid_task_ids
            )
        ]
        if len(set(matching_task_ids)) != 1:
            owner_task_ids = [
                task.task_id
                for task in locked_tasks
                if _role_allows_owner(
                    tuple(
                        _canonical_role_type(role)
                        for role in task.roles
                    ),
                    owner_type,
                )
            ]
            if len(set(owner_task_ids)) == 1:
                matching_task_ids = owner_task_ids
        if len(set(matching_task_ids)) != 1 and len(locked_tasks) == 1:
            matching_task_ids = [locked_tasks[0].task_id]
        if (
            len(set(matching_task_ids)) != 1
            and path.casefold() in {
                item.casefold() for item in stack_required_paths
            }
            and locked_tasks
        ):
            compatible_task_ids = [
                task.task_id
                for task in locked_tasks
                if _role_allows_owner(
                    tuple(_canonical_role_type(role) for role in task.roles),
                    owner_type,
                )
            ]
            matching_task_ids = [
                (compatible_task_ids or [locked_tasks[0].task_id])[0]
            ]
        inferred_task_id = (
            matching_task_ids[0] if len(set(matching_task_ids)) == 1
            else ""
        )
        explicit_task_id = str(explicit.get("task_id") or "").strip()
        bound_task_id = (
            explicit_task_id
            if explicit_task_id in valid_task_ids
            else inferred_task_id
        )
        manifest.append(RequiredFileContract(
            path=path,
            owner_type=owner_type,
            phase_id=phase_id,
            required=bool(explicit.get("required", True)),
            task_id=bound_task_id,
            criterion=str(explicit.get("criterion") or (
                f"{path} matches its locked delivery digest" if bound_task_id else ""
            )),
            evidence_spec=(
                "registry_byte_digest"
                if bound_task_id
                else str(explicit.get("evidence_spec") or "")
            ),
        ))
    explicit_seen: set[str] = set()
    for item in raw_explicit:
        if not isinstance(item, Mapping):
            continue
        path = _normalize_delivery_path(item.get("path"))
        key = path.lower()
        if not path or key not in explicit_seen:
            explicit_seen.add(key)
            continue
        manifest.append(RequiredFileContract(
            path=path,
            owner_type=str(item.get("owner_type") or required_file_owner_type(path)).strip().lower(),
            phase_id=str(item.get("phase_id") or "").strip(),
            required=bool(item.get("required", True)),
            task_id=str(item.get("task_id") or ""),
            criterion=str(item.get("criterion") or f"{path} matches its locked delivery digest"),
            evidence_spec=str(item.get("evidence_spec") or "registry_byte_digest"),
        ))
    return tuple(manifest)


def validate_required_files_manifest(
    manifest: Sequence[RequiredFileContract], phases: Sequence[PhaseContract]
) -> Tuple[ValidationIssue, ...]:
    """Validate exclusive phase/role ownership and path permissions."""
    issues: List[ValidationIssue] = []
    by_phase = {phase.phase_id: phase for phase in phases}
    seen: Dict[str, int] = {}
    for index, item in enumerate(manifest):
        key = item.path.lower()
        if key in seen:
            issues.append(_issue(
                "project_contract", "duplicate_file_owner", f"$.required_files[{index}].path",
                f"required file has more than one owner: {item.path}", seen[key], index,
            ))
        seen[key] = index
        if not _normalize_delivery_path(item.path) or not is_delivery_file_path(item.path):
            issues.append(_issue(
                "project_contract", "invalid_delivery_path", f"$.required_files[{index}].path",
                f"invalid required delivery path: {item.path}",
            ))
        phase = by_phase.get(item.phase_id)
        if phase is None:
            issues.append(_issue(
                "project_contract", "unknown_file_phase", f"$.required_files[{index}].phase_id",
                f"required file {item.path} has no unique responsible phase", sorted(by_phase), item.phase_id,
            ))
            continue
        phase_task_ids = {task.task_id for task in phase.tasks}
        if item.required and (not item.task_id or item.task_id not in phase_task_ids):
            issues.append(_issue(
                "project_contract", "file_task_unbound",
                f"$.required_files[{index}].task_id",
                f"required file {item.path} must bind one locked task in phase {item.phase_id}",
                sorted(phase_task_ids), item.task_id,
            ))
        if item.required and (
            not item.criterion or item.evidence_spec != "registry_byte_digest"
        ):
            issues.append(_issue(
                "project_contract", "file_evidence_spec_invalid",
                f"$.required_files[{index}].evidence_spec",
                f"required file {item.path} must use registry_byte_digest evidence",
            ))
        bound_task = next((task for task in phase.tasks if task.task_id == item.task_id), None)
        if bound_task and bound_task.roles:
            task_role_types = {_canonical_role_type(role) for role in bound_task.roles}
            if not _role_allows_owner(tuple(task_role_types), item.owner_type):
                issues.append(_issue(
                    "project_contract", "file_task_role_mismatch",
                    f"$.required_files[{index}].task_id",
                    f"task {item.task_id} role cannot own {item.path}",
                    sorted(task_role_types), item.owner_type,
                ))
        role_types = tuple(_canonical_role_type(role) for role in phase.roles)
        if not _role_allows_owner(role_types, item.owner_type):
            issues.append(_issue(
                "project_contract", "file_owner_out_of_scope", f"$.required_files[{index}].owner_type",
                f"owner {item.owner_type} cannot write {item.path} in phase {item.phase_id}",
                sorted(set(role_types)), item.owner_type,
            ))
        expected_owner = required_file_owner_type(item.path)
        if (
            not _owner_permitted_for_path(item.path, item.owner_type)
            and "fullstack_engineer" not in role_types
        ):
            issues.append(_issue(
                "project_contract", "file_path_permission", f"$.required_files[{index}].owner_type",
                f"owner {item.owner_type} is not permitted for path {item.path}", expected_owner, item.owner_type,
            ))
    return tuple(issues)


def parse_project_contract(requirements: str, plan: Mapping[str, Any] | None = None) -> ProjectContract:
    """Parse the authoritative requirement into a deeply immutable contract."""
    text = str(requirements or "")
    lower = text.lower()
    requirements_summary = " ".join(text.split())[:2000]
    requirements_digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    stack_source = _explicit_required_technology_source(text)
    if not stack_source and plan is not None:
        stack_source = _flatten_text(
            (plan or {}).get("technology_stack")
            or (plan or {}).get("tech_stack", {})
            or (plan or {}).get("required_tech", ())
        )
    forbidden_technologies = _explicit_forbidden_technologies(text)
    stack = tuple(
        technology
        for technology in _detect_technologies(stack_source)
        if technology not in forbidden_technologies
    )
    roles = _extract_labeled(text, ("角色集合", "允许角色", "用户角色", "roles"))
    forbidden = list(forbidden_technologies)
    forbidden.extend(term for term in _KNOWN_FORBIDDEN if term in lower)
    forbidden.extend(_extract_labeled(text, ("禁止范围", "禁止实现", "不实现", "forbidden scope")))
    phase_count = _extract_phase_count(text)
    minimum_phase_count = _extract_minimum_phase_count(text)
    phases = _phases_from_plan(plan or {}) if (plan or {}).get("phases") else _phases_from_text(text, phase_count, roles)
    counts = _extract_locked_phase_task_counts(text, phase_count)
    if counts:
        adjusted: List[PhaseContract] = []
        by_id = {phase.phase_id: phase for phase in phases}
        phase_total = phase_count or max((_number(key.rsplit("-", 1)[-1]) for key in counts), default=0)
        for index in range(1, phase_total + 1):
            phase_id = f"phase-{index}"
            phase = by_id.get(phase_id) or PhaseContract(phase_id, index, f"阶段 {index}", roles)
            count = counts.get(phase_id)
            if count and len(phase.tasks) != count:
                existing = phase.tasks[:count]
                generated = tuple(
                    LockedTask(
                        f"{phase_id}-task-{task_no}",
                        task_no,
                        f"任务 {task_no}",
                        phase.roles,
                        planning_placeholder=True,
                    )
                    for task_no in range(len(existing) + 1, count + 1)
                )
                phase = PhaseContract(
                    phase.phase_id, phase.order, phase.name, phase.roles,
                    existing + generated,
                    phase.forbidden_scope, phase.acceptance_criteria,
                    phase.dependencies, phase.source_constraints,
                    phase.planning_placeholder,
                )
            adjusted.append(phase)
        phases = tuple(adjusted)
    # Passing an already structured plan is the legacy import API. Preserve
    # its v2 execution semantics unless the caller explicitly identifies it
    # as v3; all new PM synthesis calls omit ``plan`` and therefore create v3.
    imported_version = (
        int((plan or {}).get("contract_version") or 2)
        if plan is not None else CONTRACT_VERSION
    )
    return ProjectContract(
        contract_version=imported_version, technology_stack=stack,
        phase_count=phase_count or (len(phases) or None),
        minimum_phase_count=minimum_phase_count,
        phases=phases, roles=roles,
        forbidden_scope=_dedupe(forbidden),
        acceptance_criteria=_extract_labeled(text, ("全局验收标准", "验收标准", "acceptance criteria")),
        dependencies=_extract_labeled(text, ("全局依赖", "依赖约束", "dependencies")),
        source_constraints=_extract_labeled(text, ("来源约束", "source constraints")),
        source_requirements=text, locked=bool(stack or phases or roles or forbidden),
        # Delivery ownership is finalized only when the user confirms the
        # structured plan; draft/model output cannot define this boundary.
        required_files=(),
        requirements_summary=requirements_summary,
        requirements_digest=requirements_digest,
        requirement_units=extract_requirement_units(text),
        requirements_revision=int((plan or {}).get("requirements_revision") or 0),
        requirement_event_ids=_as_strings(
            (plan or {}).get("requirement_event_ids")
        ),
        requirement_lineage_digest=str(
            (plan or {}).get("requirement_lineage_digest") or ""
        ),
    )


def extract_project_contract(requirements: str, plan: Dict[str, Any] | None = None) -> FrozenContractDict:
    """Compatibility API returning an immutable JSON-compatible mapping."""
    return parse_project_contract(requirements, plan).as_mapping()


def finalize_project_contract(
    contract: ProjectContract | Mapping[str, Any],
    plan: Mapping[str, Any],
    explicit_files: Sequence[Mapping[str, Any]] | None = None,
) -> ProjectContract:
    """Return the immutable contract snapshot stored at plan confirmation.

    Confirming never edits an already persisted contract object.  Callers must
    create a new contract version through a separate workflow to change this
    snapshot after confirmation.
    """
    model = _contract_model(contract)
    planned_phases = _phases_from_plan(plan) if plan.get("phases") else ()
    planned_stack = _dedupe(
        list(_as_strings(
            plan.get("technology_stack")
            or plan.get("tech_stack")
            or plan.get("required_tech")
        ))
        + [
            technology
            for phase in (plan.get("phases") or [])
            if isinstance(phase, Mapping)
            for technology in _as_strings(
                phase.get("technology_stack") or phase.get("tech_stack")
            )
        ]
        + [
            technology
            for phase in (plan.get("phases") or [])
            if isinstance(phase, Mapping)
            for task in phase_task_contract(phase)
            for technology in _as_strings(
                task.get("technology_stack") or task.get("tech_stack")
            )
        ]
    )
    if str(plan.get("source") or "") == "deterministic_contract_fallback":
        planned_stack = ()
    if not model.phases and planned_phases:
        model = replace(
            model,
            technology_stack=model.technology_stack or planned_stack,
            phase_count=model.phase_count or (len(planned_phases) or None),
            phases=planned_phases,
            roles=model.roles or _as_strings(plan.get("roles")),
        )
    elif model.phases and planned_phases:
        planned_by_id = {phase.phase_id: phase for phase in planned_phases}
        merged_phase_rows: List[PhaseContract] = []
        for phase in model.phases:
            planned = planned_by_id.get(phase.phase_id, phase)
            merged_roles = phase.roles or planned.roles
            planned_tasks = {task.task_id: task for task in planned.tasks}
            merged_tasks: List[LockedTask] = []
            for task in phase.tasks:
                if task.planning_placeholder and task.task_id in planned_tasks:
                    chosen = planned_tasks[task.task_id]
                    merged_tasks.append(replace(
                        chosen,
                        task_id=task.task_id,
                        order=task.order,
                        planning_placeholder=False,
                    ))
                else:
                    merged_tasks.append(task)
            merged_phase_rows.append(replace(
                phase,
                name=planned.name if phase.planning_placeholder else phase.name,
                roles=merged_roles,
                tasks=tuple(merged_tasks) or planned.tasks,
                acceptance_criteria=(
                    phase.acceptance_criteria or planned.acceptance_criteria
                ),
                dependencies=(
                    planned.dependencies
                    if phase.planning_placeholder
                    else phase.dependencies
                ),
                planning_placeholder=False,
            ))
        merged_phases = tuple(merged_phase_rows)
        model = replace(
            model,
            technology_stack=model.technology_stack or planned_stack,
            phases=merged_phases,
            roles=model.roles or _dedupe(
                role for phase in merged_phases for role in phase.roles
            ),
        )
    manifest = build_required_files_manifest(
        model.source_requirements, plan, model.phases, model.technology_stack,
        explicit_files,
    )
    return replace(model, locked=True, required_files=manifest)


def hydrate_plan_required_files(
    plan: Dict[str, Any],
    contract: ProjectContract | Mapping[str, Any],
) -> Dict[str, Any]:
    """Write canonical manifest paths back to their bound task contracts."""
    model = _contract_model(contract)
    files_by_task: Dict[Tuple[str, str], List[str]] = {}
    for item in model.required_files:
        if item.required and item.phase_id and item.task_id:
            files_by_task.setdefault(
                (item.phase_id, item.task_id), [],
            ).append(item.path)
    for paths in files_by_task.values():
        paths.sort(key=str.lower)

    for phase in plan.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        phase_id = str(phase.get("phase_id") or "").strip()
        tasks = phase.get("task_contract") or phase.get("tasks") or []
        if not isinstance(tasks, list):
            continue
        for index, task in enumerate(tasks, 1):
            if not isinstance(task, dict):
                continue
            task_id = str(
                task.get("task_id") or task.get("id")
                or f"{phase_id}-task-{index}"
            ).strip()
            task["required_files"] = list(
                files_by_task.get((phase_id, task_id), ())
            )
    return plan


def freeze_confirmed_project_contract(
    contract: ProjectContract | Mapping[str, Any],
) -> FrozenContractDict:
    """Deep-freeze a persisted confirmed contract without re-planning it."""
    return replace(_contract_model(contract), locked=True).as_mapping()


def attach_contract(plan: Dict[str, Any], requirements: str) -> Dict[str, Any]:
    # Never let a model-produced plan define its own boundary.  Callers that
    # deliberately migrate a previously confirmed structured plan can invoke
    # ``parse_project_contract(requirements, confirmed_plan)`` explicitly.
    contract = extract_project_contract(requirements)
    plan["project_contract"] = contract
    plan.setdefault("contract_version", contract["contract_version"])
    plan.setdefault("plan_version", PLAN_VERSION)
    return plan


def _contract_model(contract: ProjectContract | Mapping[str, Any] | None) -> ProjectContract:
    if isinstance(contract, ProjectContract):
        return contract
    raw = contract or {}
    phases: List[PhaseContract] = []
    for index, phase in enumerate(raw.get("phases") or [], 1):
        if not isinstance(phase, Mapping):
            continue
        roles = _as_strings(phase.get("roles") or phase.get("roles_needed"))
        tasks = tuple(_task_from_mapping(task, str(phase.get("phase_id") or f"phase-{index}"), task_no, roles) for task_no, task in enumerate(phase.get("tasks") or phase.get("task_contract") or [], 1) if isinstance(task, Mapping))
        phases.append(PhaseContract(
            str(phase.get("phase_id") or f"phase-{index}"),
            int(phase.get("order") or index),
            str(phase.get("name") or f"阶段 {index}"),
            roles,
            tasks,
            _as_strings(phase.get("forbidden_scope")),
            _as_strings(phase.get("acceptance_criteria")),
            _as_strings(phase.get("dependencies")),
            _as_strings(phase.get("source_constraints")),
            bool(phase.get("planning_placeholder", False)),
        ))
    required_files = tuple(
        RequiredFileContract(
            path=_normalize_delivery_path(item.get("path")),
            owner_type=str(item.get("owner_type") or required_file_owner_type(str(item.get("path") or ""))).strip().lower(),
            phase_id=str(item.get("phase_id") or "").strip(),
            required=bool(item.get("required", True)),
            task_id=str(item.get("task_id") or ""),
            criterion=str(item.get("criterion") or ""),
            evidence_spec=str(item.get("evidence_spec") or "registry_byte_digest"),
        )
        for item in raw.get("required_files") or []
        if isinstance(item, Mapping)
    )
    requirement_units = tuple(
        RequirementUnit(
            unit_id=str(item.get("unit_id") or ""),
            order=int(item.get("order") or index),
            exact_text=str(item.get("exact_text") or ""),
            digest=str(item.get("digest") or ""),
            kind=str(item.get("kind") or "context"),
            binding=bool(item.get("binding", False)),
        )
        for index, item in enumerate(raw.get("requirement_units") or [], 1)
        if isinstance(item, Mapping)
    )
    return ProjectContract(
        # Persisted artifacts predating explicit versioning are legacy v2.
        # New contracts are always created through parse_project_contract().
        int(raw.get("contract_version") or raw.get("version") or 2),
        _as_strings(raw.get("technology_stack") or raw.get("required_tech")),
        raw.get("phase_count") or raw.get("required_phase_count"),
        raw.get("minimum_phase_count") or raw.get("min_phase_count"),
        tuple(phases),
        _as_strings(raw.get("roles")), _as_strings(raw.get("forbidden_scope")),
        _as_strings(raw.get("acceptance_criteria")), _as_strings(raw.get("dependencies")),
        _as_strings(raw.get("source_constraints")), str(raw.get("source_requirements") or ""),
        bool(raw.get("locked", True)), required_files,
        str(raw.get("requirements_summary") or " ".join(str(raw.get("source_requirements") or "").split())[:2000]),
        str(raw.get("requirements_digest") or (
            "sha256:" + hashlib.sha256(str(raw.get("source_requirements") or "").encode("utf-8")).hexdigest()
        )),
        requirement_units,
        int(raw.get("requirements_revision") or 0),
        _as_strings(raw.get("requirement_event_ids")),
        str(raw.get("requirement_lineage_digest") or ""),
    )


def phase_task_contract(phase: Mapping[str, Any]) -> List[Dict[str, Any]]:
    existing = phase.get("task_contract") or phase.get("tasks")
    if isinstance(existing, (list, tuple)) and existing:
        normalized: List[Dict[str, Any]] = []
        for index, item in enumerate(existing, 1):
            if not isinstance(item, Mapping):
                continue
            task = dict(item)
            task.setdefault("task_id", f"{phase.get('phase_id', 'phase')}-task-{index}")
            task.setdefault("name", task.get("task_name") or task.get("deliverable") or f"任务 {index}")
            task.setdefault("order", index)
            task.setdefault("deliverable", task.get("description") or task["name"])
            normalized.append(task)
        return normalized
    return [{"task_id": f"{phase.get('phase_id', 'phase')}-deliverable-{index}", "order": index, "name": str(item), "deliverable": str(item)} for index, item in enumerate(phase.get("deliverables") or [], 1) if str(item).strip()]


def canonical_phase_requirements(phase: Mapping[str, Any]) -> List[Dict[str, Any]]:
    roles = _as_strings(phase.get("roles_needed") or phase.get("roles"))
    phase_tech_stack = _real_strings(
        phase.get("tech_stack") or phase.get("technology_stack")
    )
    phase_acceptance = _real_strings(phase.get("acceptance_criteria"))
    result = []
    for index, task in enumerate(phase_task_contract(phase), 1):
        task_roles = _as_strings(task.get("roles") or task.get("required_role")) or roles
        name = next(
            iter(_real_strings(
                task.get("name")
                or task.get("task_name")
                or task.get("deliverable")
            )),
            f"任务 {index}",
        )
        description = next(
            iter(_real_strings(
                task.get("task_description")
                or task.get("description")
                or task.get("implementation_details")
                or task.get("deliverable")
            )),
            f"完成{name}的详细实现和验证",
        )
        role = next(
            (
                item for item in task_roles
                if not _is_placeholder_value(item)
            ),
            "执行专家",
        )
        task_tech_stack = _real_strings(
            task.get("tech_stack") or task.get("technology_stack")
        ) or phase_tech_stack or ("通用工程技术栈",)
        responsibilities = _real_strings(
            task.get("responsibilities") or task.get("duties")
        ) or (f"{role}：负责完成{name}的实现、验证和交付",)
        raw_personnel_count = (
            task.get("personnel_count")
            or task.get("agent_count")
            or task.get("headcount")
            or 1
        )
        try:
            personnel_count = max(1, int(raw_personnel_count))
        except (TypeError, ValueError):
            personnel_count = 1
        personnel_allocation = _real_strings(
            task.get("personnel_allocation")
            or task.get("staffing")
        ) or (f"{role}：{personnel_count} 人",)
        acceptance = _real_strings(
            task.get("acceptance_criteria")
        ) or phase_acceptance or (f"{name}达到可自动验证的完成标准",)
        result.append({
            "task_id": str(task["task_id"]), "task_name": name,
            "task_description": description,
            "implementation": next(
                iter(_real_strings(
                    task.get("implementation")
                    or task.get("implementation_method")
                    or task.get("approach")
                )),
                f"按依赖顺序实现并自动验证：{description}",
            ),
            "tech_stack": list(task_tech_stack),
            "required_role": role,
            "responsibilities": list(responsibilities),
            "personnel_count": personnel_count,
            "personnel_allocation": list(personnel_allocation),
            "priority": task.get("priority") if task.get("priority") in ("high", "normal", "low") else "normal",
            "acceptance_criteria": list(acceptance),
            "required_files": list(dict.fromkeys(
                collect_required_file_paths(
                    "",
                    {"required_files": task.get("required_files") or []},
                )
            )),
            "dependencies": list(_as_strings(task.get("dependencies"))),
            "source_constraints": list(_as_strings(task.get("source_constraints"))),
            "source_requirement_ids": list(_as_strings(task.get("source_requirement_ids"))),
        })
    return result


def _schema_issues(value: Any, schema: Mapping[str, Any], path: str = "$") -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    allowed = schema.get("type")
    allowed_types = allowed if isinstance(allowed, list) else [allowed]
    checks = {"object": lambda item: isinstance(item, Mapping), "array": lambda item: isinstance(item, (list, tuple)), "string": lambda item: isinstance(item, str), "integer": lambda item: isinstance(item, int) and not isinstance(item, bool)}
    if allowed and not any(checks[kind](value) for kind in allowed_types if kind in checks):
        return [ValidationIssue("json_schema", "type", path, f"{path} must be {allowed}", allowed, type(value).__name__)]
    if isinstance(value, Mapping):
        for key in schema.get("required") or []:
            if key not in value or value[key] is None:
                issues.append(ValidationIssue("json_schema", "required", f"{path}.{key}", f"required field missing: {key}"))
        for key, child in schema.get("properties", {}).items():
            if key in value:
                issues.extend(_schema_issues(value[key], child, f"{path}.{key}"))
    if isinstance(value, (list, tuple)):
        if len(value) < int(schema.get("minItems") or 0):
            issues.append(ValidationIssue("json_schema", "min_items", path, f"{path} must contain at least {schema['minItems']} item(s)"))
        if schema.get("items"):
            for index, item in enumerate(value):
                issues.extend(_schema_issues(item, schema["items"], f"{path}[{index}]"))
    return issues


def _issue(layer: str, code: str, path: str, message: str, expected: Any = None, actual: Any = None) -> ValidationIssue:
    return ValidationIssue(layer, code, path, message, expected, actual)


def _traceability_and_dag_issues(
    phases: Sequence[Mapping[str, Any]], contract: ProjectContract
) -> List[ValidationIssue]:
    if contract.contract_version < 3:
        return []
    issues: List[ValidationIssue] = []
    rebuilt = extract_requirement_units(contract.source_requirements)
    if not contract.requirement_units:
        issues.append(_issue(
            "project_contract", "requirement_units_missing",
            "$.project_contract.requirement_units",
            "version 3 contract must contain deterministic requirement units",
        ))
        return issues
    expected_units = [item.to_dict() for item in rebuilt]
    actual_units = [item.to_dict() for item in contract.requirement_units]
    if actual_units != expected_units:
        issues.append(_issue(
            "project_contract", "requirement_unit_digest_mismatch",
            "$.project_contract.requirement_units",
            "requirement units do not match the preserved source requirements",
            expected_units, actual_units,
        ))

    known_units = {item.unit_id for item in contract.requirement_units}
    units_by_id = {item.unit_id: item for item in contract.requirement_units}
    binding_units = {item.unit_id for item in contract.requirement_units if item.binding}
    required_units = {
        item.unit_id
        for item in contract.requirement_units
        if _unit_requires_task_substantiation(item)
    }
    # A legacy one-sentence requirement can combine a feature and a planning
    # constraint into one unit. Preserve that unit as a usable source only when
    # no independently executable unit exists.
    task_source_units = required_units or known_units
    covered: set[str] = set()
    task_rows: List[Tuple[int, int, str, Mapping[str, Any]]] = []
    task_locations: Dict[str, Tuple[int, int]] = {}
    for phase_index, phase in enumerate(phases):
        for task_index, task in enumerate(phase_task_contract(phase)):
            task_id = str(task.get("task_id") or "")
            task_rows.append((phase_index, task_index, task_id, task))
            if task_id:
                previous = task_locations.get(task_id)
                if previous is not None:
                    issues.append(_issue(
                        "phase_contract", "task_id_duplicate",
                        f"$.phases[{phase_index}].tasks[{task_index}].task_id",
                        f"task_id must be globally unique across phases: {task_id}",
                        "globally unique task_id",
                        task_id,
                    ))
                else:
                    task_locations[task_id] = (phase_index, task_index)
            source_ids = _as_strings(task.get("source_requirement_ids"))
            path = f"$.phases[{phase_index}].tasks[{task_index}]"
            if not source_ids:
                issues.append(_issue(
                    "phase_contract", "task_source_requirements_empty",
                    f"{path}.source_requirement_ids",
                    f"task {task_id} must cite at least one source requirement",
                ))
            elif not task_source_units.intersection(source_ids):
                issues.append(_issue(
                    "phase_contract", "task_functional_source_missing",
                    f"{path}.source_requirement_ids",
                    (
                        f"task {task_id} must cite at least one executable "
                        "feature requirement"
                    ),
                    sorted(task_source_units),
                    list(source_ids),
                ))
            for source_id in source_ids:
                if source_id not in known_units:
                    issues.append(_issue(
                        "project_contract", "requirement_unit_unknown",
                        f"{path}.source_requirement_ids",
                        f"task {task_id} cites unknown requirement unit: {source_id}",
                    ))
                else:
                    covered.add(source_id)
                    unit = units_by_id[source_id]
                    if (
                        _unit_requires_task_substantiation(unit)
                        and not _task_mapping_is_substantiated(unit, task)
                    ):
                        phase_evidence = {
                            "name": task.get("name") or task.get("task_name"),
                            "description": task.get("description"),
                            "acceptance_criteria": task.get("acceptance_criteria"),
                            "phase_name": phase.get("name"),
                            "phase_description": phase.get("description"),
                            "phase_implementation": phase.get("implementation"),
                            "phase_tech_stack": (
                                phase.get("tech_stack")
                                or phase.get("technology_stack")
                            ),
                            "phase_responsibilities": phase.get(
                                "responsibilities"
                            ),
                            "phase_acceptance_criteria": phase.get("acceptance_criteria"),
                            "phase_tasks": phase_task_contract(phase),
                        }
                        if (
                            not _unit_allows_phase_aggregate(unit)
                            or not _task_mapping_is_substantiated(
                                unit, phase_evidence
                            )
                        ):
                            issues.append(_issue(
                                "project_contract", "task_source_mapping_unsubstantiated",
                                f"{path}.source_requirement_ids",
                                f"task {task_id} text does not substantiate source requirement {source_id}",
                                unit.exact_text,
                            ))
            if not _as_strings(task.get("acceptance_criteria")):
                issues.append(_issue(
                    "phase_contract", "task_acceptance_criteria_empty",
                    f"{path}.acceptance_criteria",
                    f"task {task_id} must contain task-level acceptance criteria",
                ))

    for unit_id in sorted(required_units - covered):
        binding = unit_id in binding_units
        issues.append(_issue(
            "project_contract",
            "binding_requirement_uncovered" if binding else "requirement_unit_uncovered",
            "$.phases",
            f"source requirement is not implemented by any task: {unit_id}",
            unit_id,
        ))

    graph: Dict[str, Tuple[str, ...]] = {}
    for phase_index, task_index, task_id, task in task_rows:
        dependencies = _as_strings(task.get("dependencies"))
        graph[task_id] = dependencies
        seen: set[str] = set()
        for dependency in dependencies:
            path = f"$.phases[{phase_index}].tasks[{task_index}].dependencies"
            if dependency in seen:
                issues.append(_issue(
                    "phase_contract", "dependency_duplicate", path,
                    f"task {task_id} repeats dependency {dependency}",
                ))
            seen.add(dependency)
            if dependency == task_id:
                issues.append(_issue(
                    "phase_contract", "dependency_self_reference", path,
                    f"task {task_id} cannot depend on itself",
                ))
            location = task_locations.get(dependency)
            if location is None:
                issues.append(_issue(
                    "phase_contract", "dependency_unknown", path,
                    f"task {task_id} depends on unknown task {dependency}",
                    sorted(task_locations), dependency,
                ))
            else:
                dependency_phase, dependency_order = location
                if dependency_phase > phase_index:
                    issues.append(_issue(
                        "phase_contract", "dependency_future_phase", path,
                        f"task {task_id} depends on future-phase task {dependency}",
                    ))
                elif dependency_phase == phase_index and dependency_order >= task_index:
                    issues.append(_issue(
                        "phase_contract", "dependency_reverse_order", path,
                        f"task {task_id} depends on a non-prior task {dependency}",
                    ))

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> bool:
        if task_id in visiting:
            return True
        if task_id in visited:
            return False
        visiting.add(task_id)
        cyclic = any(dep in graph and visit(dep) for dep in graph.get(task_id, ()))
        visiting.remove(task_id)
        visited.add(task_id)
        return cyclic

    if any(visit(task_id) for task_id in graph):
        issues.append(_issue(
            "phase_contract", "dependency_cycle", "$.phases",
            "task dependency graph contains a cycle",
        ))
    return issues


_EN_TRACE_STOPWORDS = {
    "must", "shall", "should", "need", "needs", "required", "requirement",
    "implement", "support", "feature", "phase", "task", "then", "finally",
    "with", "from", "that", "this", "the", "and", "for",
}
_CN_TRACE_PREFIXES = (
    "必须", "不得", "禁止", "固定", "严格", "只能", "只允许", "需要", "应当",
    "先", "再", "然后", "最后", "实现", "支持", "阶段", "任务", "功能", "验收",
)


def _unit_requires_task_substantiation(unit: RequirementUnit) -> bool:
    text = " ".join(unit.exact_text.lower().split())
    if re.match(
        r"^(?:阶段\s*[一二三四五六七八九十\d]+|phase\s*\d+|"
        r"角色集合|允许角色|roles?\s*:|owners?\s*:|files?\s*:)",
        text,
        re.IGNORECASE,
    ):
        return False
    if re.fullmatch(r"tasks?\s*:", text, re.IGNORECASE):
        return False
    if re.search(
        r"(?:严格|固定|exactly|strictly).*?"
        r"(?:\d+|[一二三四五六七八九十]+|one|two|three|four|five|six|"
        r"seven|eight|nine|ten)\s*(?:个)?\s*(?:阶段|phases?)",
        text,
        re.IGNORECASE,
    ):
        return False
    if re.search(
        r"(?:每|各)(?:个)?\s*阶段.*?"
        r"(?:任务|实现细节|实现方式|技术栈|职责|人员(?:数量|分配)?|验收标准)",
        text,
        re.IGNORECASE,
    ):
        return False
    if re.search(
        r"(?:每|各)(?:个)?\s*任务.*?"
        r"(?:required_files|实现细节|实现方式|技术栈|职责|"
        r"人员(?:数量|分配)?|验收标准)",
        text,
        re.IGNORECASE,
    ):
        return False
    if re.search(
        r"(?:共享|公共).{0,12}(?:配置|文件).*?"
        r"(?:归属|所有|重复声明|依赖)",
        text,
        re.IGNORECASE,
    ):
        return False
    if re.search(
        r"(?:测试任务|test\s+tasks?).*?"
        r"(?:测试源码|测试文件|测试配置|required_files|test\s+(?:source|config))",
        text,
        re.IGNORECASE,
    ):
        return False
    if re.search(
        r"(?:具体)?(?:技术|实现)?方案.{0,8}"
        r"(?:由|交由).{0,12}(?:pm|专家|expert).{0,8}(?:选择|决定)",
        text,
        re.IGNORECASE,
    ):
        return False
    if re.fullmatch(
        r"(?:并且|同时|另外|以及)?\s*不得扩展(?:需求|范围|功能)?[。.!！]?",
        text,
        re.IGNORECASE,
    ):
        return False
    if re.search(
        r"\bpreserve\b.*\b(?:names?|responsibilit(?:y|ies)|order)\b",
        text,
        re.IGNORECASE,
    ):
        return False
    if (
        _detect_technologies(text)
        and re.search(
            r"(?:使用|采用|技术栈|use|using|technology|tech\s*stack)",
            text,
            re.IGNORECASE,
        )
        and not re.search(
            r"(?:"
            r"\b(?:implement|build|create|verify|validate|probe|exercise|"
            r"assert|check|return(?:ing|s)?|listen(?:ing|s)?|"
            r"run(?:ning|s)?)\b|"
            r"实现|构建|创建|验证|校验|探测|运行|返回|监听"
            r")",
            text,
            re.IGNORECASE,
        )
    ):
        return False
    return True


def _unit_allows_phase_aggregate(unit: RequirementUnit) -> bool:
    text = " ".join(unit.exact_text.lower().split())
    return unit.kind == "outline" or bool(re.match(
        r"^(?:验收标准|acceptance(?: criteria)?|required repository contract)\s*[:：]",
        text,
        re.IGNORECASE,
    ))


def _trace_keywords(text: str) -> set[str]:
    lower = str(text or "").lower()
    keywords = {
        word for word in re.findall(r"[a-z0-9][a-z0-9_.+-]*", lower)
        if len(word) >= 3 and word not in _EN_TRACE_STOPWORDS
    }
    keywords.update(
        word[:-1]
        for word in tuple(keywords)
        if len(word) > 4 and word.endswith("s")
    )
    for sequence in re.findall(r"[\u4e00-\u9fff]+", lower):
        reduced = sequence
        for cue in _CN_TRACE_PREFIXES:
            reduced = reduced.replace(cue, " ")
        for part in reduced.split():
            if len(part) >= 2:
                # Bigrams tolerate natural wording differences while still
                # requiring domain evidence such as 库存/审批/登录.
                keywords.update(part[index:index + 2] for index in range(len(part) - 1))
    return keywords


def _task_mapping_is_substantiated(
    unit: RequirementUnit, task: Mapping[str, Any]
) -> bool:
    evidence = " ".join(_strings({
        "name": task.get("name") or task.get("task_name"),
        "description": task.get("description") or task.get("task_description")
        or task.get("deliverable"),
        "implementation": task.get("implementation")
        or task.get("implementation_details"),
        "implementation_method": task.get("implementation_method"),
        "responsibilities": task.get("responsibilities"),
        "required_files": task.get("required_files"),
        "acceptance_criteria": task.get("acceptance_criteria"),
        "phase_name": task.get("phase_name"),
        "phase_acceptance_criteria": task.get("phase_acceptance_criteria"),
        "phase_tasks": task.get("phase_tasks"),
    }))
    unit_text = " ".join(unit.exact_text.lower().split())
    evidence_text = " ".join(evidence.lower().split())
    if unit_text and unit_text in evidence_text:
        return True
    forbidden_terms = [
        term for term in _KNOWN_FORBIDDEN if term in unit_text
    ]
    if forbidden_terms and re.search(
        r"(?:遵守|符合|验证).{0,12}(?:禁止范围|禁用约束)|"
        r"(?:forbidden|prohibited)\s+(?:scope|constraint)",
        evidence_text,
        re.IGNORECASE,
    ):
        # The immutable unit/source ID carries the exact prohibited entity.
        # Repeating that entity in executable plan prose would itself trigger
        # the fail-closed forbidden-scope scanner.
        return True
    reduced = unit_text
    for cue in _CN_TRACE_PREFIXES:
        reduced = reduced.replace(cue, "")
    meaningful_cjk = "".join(re.findall(r"[\u4e00-\u9fff]", reduced))
    meaningful_en = [
        word for word in re.findall(r"[a-z0-9][a-z0-9_.+-]*", reduced)
        if word not in _EN_TRACE_STOPWORDS
    ]
    if len(meaningful_cjk) <= 2 and not meaningful_en:
        return False
    trace_sentences = []
    for sentence in re.split(r"(?<=[.!?])\s+", unit.exact_text):
        normalized = " ".join(sentence.lower().split())
        if re.search(
            r"\bevery\s+(?:phase|task)\b.*\bmust\s+"
            r"(?:contain|include|define)\b",
            normalized,
            re.IGNORECASE,
        ):
            continue
        if (
            _detect_technologies(normalized)
            and re.search(
                r"\b(?:use|using|technology|tech\s*stack)\b",
                normalized,
                re.IGNORECASE,
            )
            and not re.search(
                r"\b(?:implement|build|create|verify|validate|probe|exercise|"
                r"assert|check|return(?:ing|s)?|listen(?:ing|s)?|"
                r"run(?:ning|s)?)\b",
                normalized,
                re.IGNORECASE,
            )
        ):
            continue
        trace_sentences.append(sentence)
    trace_text = " ".join(trace_sentences)
    clauses = [
        part.strip(" \t,.;:，。；：")
        for part in re.split(
            r"\b(?:and|then|as\s+well\s+as)\b|(?:并且|以及|同时|然后|并需|并要)",
            trace_text,
            flags=re.IGNORECASE,
        )
        if part.strip(" \t,.;:，。；：")
    ]
    keyword_groups = [
        keywords for clause in clauses
        if (keywords := _trace_keywords(clause))
    ]
    # A source ID is not evidence. Ambiguous requirements need explicit
    # clarification or a dedicated task, rather than an automatic pass.
    if not keyword_groups:
        return False
    evidence_keywords = _trace_keywords(evidence)
    # A compound binding requirement represents multiple atomic obligations.
    # Every conjunction-delimited obligation must have task evidence; matching
    # only the audit half of "encrypt ... and audit ..." is not traceability.
    return all(bool(keywords & evidence_keywords) for keywords in keyword_groups)


_FORBIDDEN_NEGATIVE_PREFIX = re.compile(
    r"(?:"
    r"不(?:实现|提供|支持|允许|开放|包含|创建|建立|启用|硬编码)|"
    r"不得|禁止|禁用|排除|避免|"
    r"\bdo\s+not\b|\bdon't\b|\bmust\s+not\b|\bwithout\b|\bno\b|\bnever\b"
    r")\s*[^。！？!?；;]{0,24}$",
    re.IGNORECASE,
)
_FORBIDDEN_NEGATIVE_SUFFIX = re.compile(
    r"^[^。！？!?；;]{0,24}(?:"
    r"不(?:实现|提供|支持|允许|开放|启用)|被禁止|已禁用|"
    r"\bis\s+(?:not\s+(?:implemented|provided|supported|allowed)|disabled|forbidden)\b"
    r")",
    re.IGNORECASE,
)


def _contains_positive_forbidden_scope(text: str, term: str) -> bool:
    """Ignore explicit compliance statements while rejecting positive scope."""
    lowered = str(text or "").casefold()
    needle = str(term or "").strip().casefold()
    if not needle:
        return False
    for match in re.finditer(re.escape(needle), lowered):
        prefix = lowered[max(0, match.start() - 40):match.start()]
        suffix = lowered[match.end():match.end() + 40]
        if (
            _FORBIDDEN_NEGATIVE_PREFIX.search(prefix)
            or _FORBIDDEN_NEGATIVE_SUFFIX.search(suffix)
        ):
            continue
        return True
    return False


def _contract_plan_issues(plan: Mapping[str, Any], contract: ProjectContract) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = list(
        validate_required_files_manifest(contract.required_files, contract.phases)
    )
    phases = [phase for phase in plan.get("phases") or [] if isinstance(phase, Mapping)]
    if contract.phase_count and len(phases) != contract.phase_count:
        issues.append(_issue("project_contract", "phase_count_drift", "$.phases", f"phase count {len(phases)} does not match required {contract.phase_count}", contract.phase_count, len(phases)))
    if (
        contract.minimum_phase_count
        and len(phases) < contract.minimum_phase_count
    ):
        issues.append(_issue(
            "project_contract",
            "minimum_phase_count_not_met",
            "$.phases",
            (
                f"phase count {len(phases)} is below required minimum "
                f"{contract.minimum_phase_count}"
            ),
            contract.minimum_phase_count,
            len(phases),
        ))
    plan_technology_fields = {
        "technical_requirements": plan.get("technical_requirements", []),
        "tech_stack": plan.get("tech_stack", {}),
        "technology_stack": plan.get("technology_stack", []),
        "required_tech": plan.get("required_tech", []),
        "phase_tech": [
            phase.get("technical_requirements")
            or phase.get("technology_stack")
            or phase.get("tech_stack")
            for phase in phases
        ],
        "task_tech": [
            task.get("implementation_technologies")
            or task.get("technical_requirements")
            or task.get("technology_stack")
            or task.get("tech_stack")
            for phase in phases
            for task in phase_task_contract(phase)
        ],
    }
    plan_tech = set(_detect_technologies(_flatten_text(
        plan_technology_fields
    )))
    plan_tech.update(
        str(item).strip().lower()
        for item in _strings(plan_technology_fields)
        if str(item).strip()
    )
    required: set[str] = set()
    for declared in contract.technology_stack:
        detected = _detect_technologies(str(declared))
        if detected:
            required.update(detected)
        elif str(declared).strip():
            required.add(str(declared).strip().lower())
    for tech in sorted(required - plan_tech):
        issues.append(_issue("project_contract", "technology_missing", "$.tech_stack", f"required technology missing: {tech}", tech))
    if contract.locked:
        for group in _CONFLICT_GROUPS:
            for tech in sorted((plan_tech & group) - required):
                if required & group:
                    issues.append(_issue("project_contract", "technology_conflict", "$.tech_stack", f"technology outside confirmed contract: {tech}", sorted(required & group), tech))
    contract_phases = {phase.phase_id: phase for phase in contract.phases}
    actual_ids = [str(phase.get("phase_id") or "") for phase in phases]
    expected_ids = [phase.phase_id for phase in contract.phases]
    if expected_ids and actual_ids != expected_ids:
        issues.append(_issue("project_contract", "phase_id_or_order_drift", "$.phases", "phase IDs or order changed", expected_ids, actual_ids))
    if len(actual_ids) != len(set(actual_ids)):
        issues.append(_issue("project_contract", "duplicate_phase_id", "$.phases", "phase_id values must be unique"))
    for index, raw in enumerate(phases):
        phase = contract_phases.get(str(raw.get("phase_id") or ""))
        if not phase:
            continue
        if (
            not phase.planning_placeholder
            and raw.get("name") is not None
            and str(raw.get("name")) != phase.name
        ):
            issues.append(_issue("project_contract", "phase_name_drift", f"$.phases[{index}].name", f"locked phase {phase.phase_id} name changed", phase.name, raw.get("name")))
        raw_roles = _as_strings(raw.get("roles_needed") or raw.get("roles"))
        if phase.roles and raw_roles != phase.roles:
            issues.append(_issue("phase_contract", "phase_role_drift", f"$.phases[{index}].roles_needed", f"locked phase {phase.phase_id} roles changed", list(phase.roles), list(raw_roles)))
        raw_tasks = phase_task_contract(raw)
        if phase.tasks:
            expected_task_ids = [task.task_id for task in phase.tasks]
            actual_task_ids = [str(task.get("task_id") or "") for task in raw_tasks]
            if len(actual_task_ids) != len(expected_task_ids):
                issues.append(_issue("phase_contract", "task_count_drift", f"$.phases[{index}].tasks", f"phase {phase.phase_id} locked task count {len(actual_task_ids)} does not match required {len(expected_task_ids)}", len(expected_task_ids), len(actual_task_ids)))
            if actual_task_ids != expected_task_ids:
                issues.append(_issue("phase_contract", "task_id_or_order_drift", f"$.phases[{index}].tasks", f"locked phase tasks IDs or order changed in {phase.phase_id}", expected_task_ids, actual_task_ids))
            by_id = {task.task_id: task for task in phase.tasks}
            for task_no, task in enumerate(raw_tasks):
                locked = by_id.get(str(task.get("task_id") or ""))
                if (
                    locked
                    and not locked.planning_placeholder
                    and str(task.get("name") or task.get("task_name") or "") != locked.name
                ):
                    issues.append(_issue("phase_contract", "task_name_drift", f"$.phases[{index}].tasks[{task_no}].name", f"locked task {locked.task_id} name changed", locked.name, task.get("name") or task.get("task_name")))
                if locked and not locked.planning_placeholder:
                    task_roles = _as_strings(task.get("roles") or task.get("required_role")) or raw_roles
                    if locked.roles and task_roles != locked.roles:
                        issues.append(_issue("phase_contract", "task_role_drift", f"$.phases[{index}].tasks[{task_no}].roles", f"locked task {locked.task_id} roles changed", list(locked.roles), list(task_roles)))
                    dependencies = _as_strings(task.get("dependencies"))
                    if dependencies != locked.dependencies:
                        issues.append(_issue("phase_contract", "dependency_drift", f"$.phases[{index}].tasks[{task_no}].dependencies", f"locked task {locked.task_id} dependencies changed", list(locked.dependencies), list(dependencies)))
                    sources = _as_strings(task.get("source_constraints") or task.get("source_refs"))
                    if locked.source_constraints and sources != locked.source_constraints:
                        issues.append(_issue("phase_contract", "source_constraint_drift", f"$.phases[{index}].tasks[{task_no}].source_constraints", f"locked task {locked.task_id} source constraints changed", list(locked.source_constraints), list(sources)))
                    acceptance = _as_strings(task.get("acceptance_criteria"))
                    if locked.acceptance_criteria and acceptance != locked.acceptance_criteria:
                        issues.append(_issue("phase_contract", "task_acceptance_criteria_drift", f"$.phases[{index}].tasks[{task_no}].acceptance_criteria", f"locked task {locked.task_id} acceptance criteria changed", list(locked.acceptance_criteria), list(acceptance)))
    text = _flatten_text({"phases": phases, "subprojects": plan.get("subprojects", [])})
    for term in contract.forbidden_scope:
        if _contains_positive_forbidden_scope(text, term):
            issues.append(_issue("project_contract", "forbidden_scope", "$", f"forbidden scope present: {term}", None, term))
    for left, right in _TECH_CHOICE_PAIRS:
        if re.search(rf"(?<![a-z0-9]){re.escape(left)}\s*(?:/|或|\bor\b)\s*{re.escape(right)}(?![a-z0-9])", text):
            issues.append(_issue("project_contract", "unresolved_option", "$", f"plan contains unresolved technology choice: {left}/{right}"))
    return issues


def _phase_contract_issues(phase: Mapping[str, Any], requirements: Sequence[Mapping[str, Any]], contract: ProjectContract) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    phase_id = str(phase.get("phase_id") or "")
    locked_phase = next((item for item in contract.phases if item.phase_id == phase_id), None)
    raw_locked = phase_task_contract(phase)
    if not locked_phase and raw_locked:
        roles = _as_strings(phase.get("roles_needed") or phase.get("roles"))
        locked_phase = PhaseContract(phase_id, 1, str(phase.get("name") or ""), roles, tuple(_task_from_mapping(item, phase_id, index, roles) for index, item in enumerate(raw_locked, 1)))
    locked_tasks = locked_phase.tasks if locked_phase else ()
    raw_locked_by_id = {
        str(item.get("task_id") or item.get("id") or ""): item
        for item in raw_locked
        if isinstance(item, Mapping)
    }
    expected_ids = [task.task_id for task in locked_tasks]
    actual_ids = [str(task.get("task_id") or "") for task in requirements]
    if locked_tasks and len(actual_ids) != len(expected_ids):
        issues.append(_issue("phase_contract", "task_count_drift", "$", f"phase {phase_id} task count {len(actual_ids)} does not match locked count {len(expected_ids)}", len(expected_ids), len(actual_ids)))
    if locked_tasks and actual_ids != expected_ids:
        missing = [item for item in expected_ids if item not in actual_ids]
        extra = [item for item in actual_ids if item not in expected_ids]
        if missing:
            issues.append(_issue("phase_contract", "tasks_missing", "$", f"locked phase tasks missing: {', '.join(missing)}", expected_ids, actual_ids))
        if extra:
            issues.append(_issue("phase_contract", "tasks_outside_phase", "$", f"tasks outside locked phase contract: {', '.join(extra)}", expected_ids, actual_ids))
        if not missing and not extra:
            issues.append(_issue("phase_contract", "task_order_drift", "$", "locked phase task order changed", expected_ids, actual_ids))
    by_id = {task.task_id: task for task in locked_tasks}
    all_contract_task_ids = {task.task_id for item in contract.phases for task in item.tasks}
    traceable_task_ids = all_contract_task_ids | set(actual_ids)
    allowed_cross_dependencies = {dependency for item in contract.phases for task in item.tasks for dependency in task.dependencies}
    allowed_phase_roles = set((locked_phase.roles if locked_phase else ()) or _as_strings(phase.get("roles_needed") or phase.get("roles")))
    for index, task in enumerate(requirements):
        task_id = actual_ids[index]
        locked = by_id.get(task_id)
        if locked and str(task.get("task_name") or "") != locked.name:
            issues.append(_issue("phase_contract", "task_name_drift", f"$[{index}].task_name", f"locked task {task_id} name changed", locked.name, task.get("task_name")))
        role = str(task.get("required_role") or "").strip()
        task_allowed_roles = set(locked.roles if locked and locked.roles else allowed_phase_roles)
        if task_allowed_roles and role not in task_allowed_roles:
            issues.append(_issue("phase_contract", "role_out_of_scope", f"$[{index}].required_role", f"task {task_id} requires role outside phase roles: {role}", sorted(task_allowed_roles), role))
        task_text = _flatten_text(task)
        all_task_text = _flatten_text(requirements)
        if ("employee" in all_task_text or "员工" in all_task_text) and re.search(
            r"(?<![a-z0-9])admin\s*/\s*user(?![a-z0-9])", task_text
        ):
            issues.append(_issue("project_contract", "inconsistent_role_model", f"$[{index}]", f"task {task_id} uses inconsistent role model: admin/user vs admin/employee"))
        if (
            ("默认管理员" in task_text or "default admin" in task_text)
            and "环境变量" not in task_text
            and "environment variable" not in task_text
        ):
            issues.append(_issue("project_contract", "credential_source", f"$[{index}]", f"task {task_id} must load administrator credentials from environment variables"))
        for left, right in _TECH_CHOICE_PAIRS:
            if re.search(rf"(?<![a-z0-9]){re.escape(left)}\s*(?:/|或|\bor\b)\s*{re.escape(right)}(?![a-z0-9])", task_text):
                issues.append(_issue("project_contract", "unresolved_option", f"$[{index}]", f"task {task_id} contains unresolved technology choice: {left}/{right}"))
        task_tech = set(_detect_technologies(task_text))
        required_tech = set(contract.technology_stack)
        if contract.locked:
            for group in _CONFLICT_GROUPS:
                for tech in sorted((task_tech & group) - required_tech):
                    if required_tech & group:
                        issues.append(_issue("project_contract", "technology_conflict", f"$[{index}]", f"task {task_id} uses technology outside contract: {tech}", sorted(required_tech & group), tech))
        for term in contract.forbidden_scope:
            if _contains_positive_forbidden_scope(task_text, term):
                issues.append(_issue("project_contract", "forbidden_scope", f"$[{index}]", f"task {task_id} contains forbidden scope: {term}"))
        dependencies = _as_strings(task.get("dependencies"))
        if locked and dependencies != locked.dependencies:
            issues.append(_issue("phase_contract", "dependency_drift", f"$[{index}].dependencies", f"locked task {task_id} dependencies changed", list(locked.dependencies), list(dependencies)))
        canonical_files = tuple(sorted(
            (
                item.path
                for item in contract.required_files
                if item.required
                and item.phase_id == phase_id
                and item.task_id == task_id
            ),
            key=str.lower,
        ))
        raw_locked_task = raw_locked_by_id.get(task_id)
        has_raw_file_contract = (
            raw_locked_task is not None
            and "required_files" in raw_locked_task
        )
        if canonical_files or has_raw_file_contract:
            expected_files = canonical_files or tuple(
                collect_required_file_paths(
                    "",
                    {
                        "required_files": (
                            raw_locked_task.get("required_files") or []
                        ),
                    },
                )
            )
            actual_files = tuple(collect_required_file_paths(
                "",
                {"required_files": task.get("required_files") or []},
            ))
            if expected_files != actual_files:
                issues.append(_issue(
                    "phase_contract",
                    "required_files_drift",
                    f"$[{index}].required_files",
                    f"locked task {task_id} required files changed",
                    list(expected_files),
                    list(actual_files),
                ))
        for dependency in dependencies:
            if dependency not in traceable_task_ids:
                issues.append(_issue("phase_contract", "dependency_untraceable", f"$[{index}].dependencies", f"task {task_id} dependency is not traceable: {dependency}"))
            elif locked_tasks and dependency not in by_id and dependency not in allowed_cross_dependencies:
                issues.append(_issue("phase_contract", "cross_phase_dependency", f"$[{index}].dependencies", f"task {task_id} has undeclared cross-phase dependency: {dependency}"))
        if locked and locked.source_constraints and _as_strings(task.get("source_constraints") or task.get("source_refs")) != locked.source_constraints:
            issues.append(_issue("phase_contract", "source_constraint_drift", f"$[{index}].source_constraints", f"locked task {task_id} source constraints changed", list(locked.source_constraints), list(_as_strings(task.get("source_constraints") or task.get("source_refs")))))
    return issues


def _task_execution_detail_issues(
    task: Mapping[str, Any],
    path: str,
) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    task_id = str(task.get("task_id") or "")

    def missing(code: str, field: str, message: str) -> None:
        issues.append(_issue(
            "execution_contract",
            code,
            f"{path}.{field}",
            f"task {task_id or path} {message}",
        ))

    if not _real_strings(
        task.get("name")
        or task.get("task_name")
        or task.get("deliverable")
    ):
        missing("task_name_missing", "name", "must contain a real task name")
    if not _real_strings(
        task.get("task_description")
        or task.get("description")
        or task.get("implementation_details")
        or task.get("details")
    ):
        missing(
            "task_implementation_details_missing",
            "description",
            "must contain implementation details",
        )
    if not _real_strings(
        task.get("implementation")
        or task.get("implementation_method")
        or task.get("approach")
    ):
        missing(
            "task_implementation_method_missing",
            "implementation",
            "must contain an implementation method",
        )
    if not _real_strings(
        task.get("tech_stack") or task.get("technology_stack")
    ):
        missing(
            "task_technology_stack_missing",
            "tech_stack",
            "must contain a non-placeholder technology stack",
        )
    if not _real_strings(
        task.get("roles")
        or task.get("required_role")
        or task.get("role")
    ):
        missing(
            "task_role_missing",
            "roles",
            "must assign at least one real role",
        )
    if not _real_strings(
        task.get("responsibilities") or task.get("duties")
    ):
        missing(
            "task_responsibilities_missing",
            "responsibilities",
            "must define role responsibilities",
        )
    if (
        _positive_int(
            task.get("personnel_count")
            or task.get("agent_count")
            or task.get("headcount")
        ) is None
        and not _real_strings(
            task.get("personnel_allocation") or task.get("staffing")
        )
    ):
        missing(
            "task_personnel_missing",
            "personnel_count",
            "must define a positive personnel count or allocation",
        )
    if not _real_strings(task.get("acceptance_criteria")):
        missing(
            "task_acceptance_criteria_missing",
            "acceptance_criteria",
            "must contain non-placeholder acceptance criteria",
        )
    return issues


def _plan_execution_detail_issues(
    phases: Sequence[Mapping[str, Any]],
) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    for phase_index, phase in enumerate(phases):
        path = f"$.phases[{phase_index}]"
        phase_id = str(phase.get("phase_id") or phase_index + 1)

        def missing(code: str, field: str, message: str) -> None:
            issues.append(_issue(
                "execution_contract",
                code,
                f"{path}.{field}",
                f"phase {phase_id} {message}",
            ))

        if not _real_strings(phase.get("name") or phase.get("phase_name")):
            missing(
                "phase_name_missing",
                "name",
                "must contain a real phase name",
            )
        if not _real_strings(
            phase.get("description")
            or phase.get("implementation_details")
            or phase.get("details")
        ):
            missing(
                "phase_implementation_details_missing",
                "description",
                "must contain implementation details",
            )
        if not _real_strings(
            phase.get("implementation")
            or phase.get("implementation_method")
            or phase.get("approach")
        ):
            missing(
                "phase_implementation_method_missing",
                "implementation",
                "must contain an implementation method",
            )
        if not _real_strings(
            phase.get("tech_stack") or phase.get("technology_stack")
        ):
            missing(
                "phase_technology_stack_missing",
                "tech_stack",
                "must contain a non-placeholder technology stack",
            )
        if not _real_strings(
            phase.get("roles_needed")
            or phase.get("roles")
            or phase.get("role")
        ):
            missing(
                "phase_role_missing",
                "roles_needed",
                "must assign at least one real role",
            )
        if not _real_strings(
            phase.get("responsibilities") or phase.get("duties")
        ):
            missing(
                "phase_responsibilities_missing",
                "responsibilities",
                "must define role responsibilities",
            )
        if (
            _positive_int(
                phase.get("agent_count")
                or phase.get("personnel_count")
                or phase.get("headcount")
            ) is None
            and not _real_strings(
                phase.get("personnel_allocation") or phase.get("staffing")
            )
        ):
            missing(
                "phase_personnel_missing",
                "agent_count",
                "must define a positive personnel count or allocation",
            )
        if not _real_strings(phase.get("acceptance_criteria")):
            missing(
                "phase_acceptance_criteria_missing",
                "acceptance_criteria",
                "must contain non-placeholder acceptance criteria",
            )
        for task_index, task in enumerate(phase_task_contract(phase)):
            issues.extend(_task_execution_detail_issues(
                task,
                f"{path}.tasks[{task_index}]",
            ))
    return issues


def validate_plan_layers(plan: Any, contract: ProjectContract | Mapping[str, Any] | None = None) -> ValidationResult:
    model = _contract_model(contract or (plan.get("project_contract") if isinstance(plan, Mapping) else None))
    issues = _schema_issues(plan, PLAN_SCHEMA)
    if isinstance(plan, Mapping):
        issues.extend(_contract_plan_issues(plan, model))
    return ValidationResult(not issues, "plan", tuple(issues), model.contract_version, PLAN_VERSION)


_NPM_SHARED_DEPENDENCIES = (
    "express",
    "fastify",
    "koa",
    "better-sqlite3",
    "sqlite3",
    "sequelize",
    "prisma",
    "mongoose",
    "jest",
    "supertest",
    "cypress",
    "playwright",
    "mocha",
    "chai",
    "react",
    "vite",
    "typescript",
)


def _package_script_command(
    script: str,
    rows: Sequence[tuple[int, int, Mapping[str, Any], Mapping[str, Any], list[str]]],
    dependencies: set[str],
) -> str | None:
    paths = [
        path
        for _phase_index, _task_index, _phase, _task, task_paths in rows
        for path in task_paths
    ]
    source_paths = [
        path for path in paths
        if path.casefold().endswith((".js", ".mjs", ".cjs", ".ts"))
        and not re.search(r"(^|/)(?:tests?|__tests__)(/|$)", path, re.IGNORECASE)
        and not re.search(r"(?:^|[./-])(?:test|spec|config)\.", path, re.IGNORECASE)
    ]
    test_paths = [
        path for path in paths
        if path.casefold().endswith((".js", ".mjs", ".cjs", ".ts"))
        and (
            re.search(r"(^|/)(?:tests?|__tests__)(/|$)", path, re.IGNORECASE)
            or re.search(r"(?:^|[./-])(?:test|spec)\.", path, re.IGNORECASE)
        )
    ]
    normalized = script.casefold()
    if normalized in {"start", "dev"}:
        preferred = next(
            (
                path for path in source_paths
                if PurePosixPath(path).stem.casefold()
                in {"server", "app", "index", "main"}
            ),
            source_paths[0] if source_paths else None,
        )
        if preferred:
            return f"node {preferred}"
        if "vite" in dependencies:
            return "vite"
    if normalized == "build":
        if "vite" in dependencies:
            return "vite build"
        if "typescript" in dependencies:
            return "tsc"
    if normalized == "test" or normalized.startswith("test:"):
        if "cypress" in dependencies and (
            "e2e" in normalized or "browser" in normalized
        ):
            return "cypress run"
        if "playwright" in dependencies and (
            "e2e" in normalized or "browser" in normalized
        ):
            return "playwright test"
        if "jest" in dependencies:
            return "jest"
        if "mocha" in dependencies:
            return "mocha"
        if test_paths:
            return "node --test " + " ".join(test_paths)
        return "node --test"
    return None


def normalize_shared_package_contract(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Consolidate planned npm dependencies and scripts on the sole root owner."""
    phases = [
        phase for phase in (plan.get("phases") or [])
        if isinstance(phase, Mapping)
    ]
    rows: list[
        tuple[int, int, Mapping[str, Any], Mapping[str, Any], list[str]]
    ] = []
    for phase_index, phase in enumerate(phases):
        raw_tasks = phase.get("task_contract") or phase.get("tasks") or []
        for task_index, task in enumerate(raw_tasks):
            if not isinstance(task, Mapping):
                continue
            paths = [
                str(item.get("path") if isinstance(item, Mapping) else item)
                .strip().replace("\\", "/")
                for item in (task.get("required_files") or [])
                if str(
                    item.get("path") if isinstance(item, Mapping) else item
                ).strip()
            ]
            rows.append((phase_index, task_index, phase, task, paths))
    owners = [
        row for row in rows
        if any(
            str(PurePosixPath(path)).casefold() == "package.json"
            for path in row[4]
        )
    ]
    if len(owners) != 1:
        return plan

    def corpus(phase: Mapping[str, Any], task: Mapping[str, Any]) -> str:
        return json.dumps(
            {
                "phase_tech_stack": phase.get("tech_stack")
                or phase.get("technology_stack"),
                "task": task,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).casefold()

    def mentions(text: str, token: str) -> bool:
        return bool(re.search(
            rf"(?<![A-Za-z0-9_@/-]){re.escape(token)}"
            r"(?![A-Za-z0-9_/-])",
            text,
            re.IGNORECASE,
        ))

    dependencies: set[str] = set()
    for _phase_index, _task_index, _phase, task, _paths in rows:
        raw_dependencies = task.get("package_dependencies")
        if isinstance(raw_dependencies, (list, tuple, set)):
            dependencies.update(
                str(dependency).strip()
                for dependency in raw_dependencies
                if str(dependency).strip()
            )
    dependencies.update(
        dependency
        for _phase_index, _task_index, phase, task, _paths in rows
        for dependency in _NPM_SHARED_DEPENDENCIES
        if mentions(corpus(phase, task), dependency)
    )
    scripts: Dict[str, str] = {}
    required_scripts: set[str] = set()
    for _phase_index, _task_index, phase, task, _paths in rows:
        text = corpus(phase, task)
        required_scripts.update(
            match.group(1).casefold()
            for match in re.finditer(
                r"\bnpm\s+(?:run\s+)?"
                r"([A-Za-z0-9_:@/-]+(?:\.[A-Za-z0-9_:@/-]+)*)",
                text,
                re.IGNORECASE,
            )
            if match.group(1).casefold()
            not in {"ci", "install", "exec", "audit", "publish"}
        )
        raw_scripts = task.get("package_scripts")
        if isinstance(raw_scripts, Mapping):
            for name, command in raw_scripts.items():
                clean_name = str(name).strip().casefold()
                clean_command = str(command).strip()
                if clean_name and clean_command:
                    required_scripts.add(clean_name)
                    scripts.setdefault(clean_name, clean_command)
        elif isinstance(raw_scripts, (list, tuple, set)):
            required_scripts.update(
                str(name).strip().casefold()
                for name in raw_scripts
                if str(name).strip()
            )

    for script in sorted(required_scripts):
        if script not in scripts:
            command = _package_script_command(script, rows, dependencies)
            if command:
                scripts[script] = command

    owner_task = owners[0][3]
    if isinstance(owner_task, dict):
        owner_task["package_dependencies"] = sorted(
            dependencies,
            key=str.casefold,
        )
        if scripts:
            owner_task["package_scripts"] = {
                name: scripts[name] for name in sorted(scripts)
            }
        for row in rows:
            task = row[3]
            if task is owner_task or not isinstance(task, dict):
                continue
            task.pop("package_dependencies", None)
            task.pop("package_scripts", None)
    return plan


def _shared_package_contract_issues(
    phases: Sequence[Mapping[str, Any]],
) -> List[ValidationIssue]:
    """Require one shared npm manifest owner to declare all planned packages."""
    rows: list[tuple[int, int, Mapping[str, Any], Mapping[str, Any], list[str]]] = []
    for phase_index, phase in enumerate(phases):
        for task_index, task in enumerate(phase_task_contract(phase)):
            paths = [
                str(item.get("path") if isinstance(item, Mapping) else item)
                .strip().replace("\\", "/")
                for item in (task.get("required_files") or [])
                if str(
                    item.get("path") if isinstance(item, Mapping) else item
                ).strip()
            ]
            rows.append((phase_index, task_index, phase, task, paths))
    def corpus(phase: Mapping[str, Any], task: Mapping[str, Any]) -> str:
        return json.dumps(
            {
                "phase_tech_stack": phase.get("tech_stack")
                or phase.get("technology_stack"),
                "task": task,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).casefold()

    def mentions(text: str, dependency: str) -> bool:
        return bool(re.search(
            rf"(?<![A-Za-z0-9_@/-]){re.escape(dependency)}"
            r"(?![A-Za-z0-9_/-])",
            text,
            re.IGNORECASE,
        ))

    def declared_scripts(text: str) -> set[str]:
        scripts = {
            match.group(1).casefold()
            for match in re.finditer(
                r"\bnpm\s+(?:run\s+)?"
                r"([A-Za-z0-9_:@/-]+(?:\.[A-Za-z0-9_:@/-]+)*)",
                text,
                re.IGNORECASE,
            )
            if match.group(1).casefold()
            not in {"ci", "install", "exec", "audit", "publish"}
        }
        scripts.update(
            match.group(1).casefold()
            for match in re.finditer(
                r"(?<![A-Za-z0-9_.:@/-])['\"`]?"
                r"([A-Za-z][A-Za-z0-9_.:@/-]*)['\"`]?\s+script\b",
                text,
                re.IGNORECASE,
            )
            if match.group(1).casefold()
            not in {"a", "an", "the", "npm", "package"}
        )
        return scripts

    def structured_scripts(task: Mapping[str, Any]) -> set[str]:
        raw = task.get("package_scripts")
        if isinstance(raw, Mapping):
            return {
                str(name).strip().casefold()
                for name, command in raw.items()
                if str(name).strip() and str(command).strip()
            }
        return {
            str(name).strip().casefold()
            for name in (raw or [])
            if str(name).strip()
        } if isinstance(raw, (list, tuple, set)) else set()

    required = {
        dependency
        for _phase_index, _task_index, phase, task, _paths in rows
        for dependency in _NPM_SHARED_DEPENDENCIES
        if mentions(corpus(phase, task), dependency)
    }
    required_scripts = {
        script
        for _phase_index, _task_index, phase, task, _paths in rows
        for script in (
            declared_scripts(corpus(phase, task))
            | structured_scripts(task)
        )
    }
    package_owners = [
        row for row in rows
        if any(
            str(PurePosixPath(path)).casefold() == "package.json"
            for path in row[4]
        )
    ]
    has_npm_delivery_path = any(
        path.casefold().endswith(
            (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
        )
        or PurePosixPath(path).name.casefold() == "package.json"
        for _phase_index, _task_index, _phase, _task, paths in rows
        for path in paths
    )
    npm_relevant = has_npm_delivery_path and (
        bool(required)
        or any(
            mentions(corpus(phase, task), "node.js")
            or mentions(corpus(phase, task), "npm")
            or "package.json" in corpus(phase, task)
            for _phase_index, _task_index, phase, task, _paths in rows
        )
    )
    if not npm_relevant:
        return []
    if len(package_owners) != 1:
        code = (
            "shared_package_owner_missing"
            if not package_owners
            else "shared_package_owner_ambiguous"
        )
        return [_issue(
            "phase_contract",
            code,
            "$.phases",
            "Node/npm plans must bind the root package.json to exactly one task",
            1,
            len(package_owners),
        )]

    phase_index, task_index, owner_phase, owner_task, _paths = package_owners[0]
    owner_text = corpus(owner_phase, owner_task)
    missing = sorted(
        dependency for dependency in required
        if not mentions(owner_text, dependency)
    )
    owner_scripts = declared_scripts(owner_text) | structured_scripts(owner_task)
    missing_scripts = sorted(required_scripts - owner_scripts)
    if not missing and not missing_scripts:
        return []
    missing_items = [
        *missing,
        *(f"script:{script}" for script in missing_scripts),
    ]
    return [_issue(
        "phase_contract",
        "shared_package_dependency_contract_incomplete",
        (
            f"$.phases[{phase_index}].tasks[{task_index}].package_dependencies"
            if missing
            else f"$.phases[{phase_index}].tasks[{task_index}].package_scripts"
        ),
        "the sole package.json owner must explicitly declare every planned npm "
        "dependency and script before later phases are locked; missing: "
        + ", ".join(missing_items),
        sorted(required) + [f"script:{script}" for script in sorted(required_scripts)],
        sorted(required - set(missing))
        + [f"script:{script}" for script in sorted(owner_scripts & required_scripts)],
    )]


def validate_plan_for_confirmation(
    plan: Any,
    contract: ProjectContract | Mapping[str, Any] | None = None,
    *,
    expected_source_requirements: str | None = None,
) -> ValidationResult:
    """Apply the stricter gate required before a draft can be confirmed."""
    base = validate_plan_layers(plan, contract)
    model = _contract_model(
        contract or (plan.get("project_contract") if isinstance(plan, Mapping) else None)
    )
    issues = list(base.issues)
    if isinstance(plan, Mapping):
        phases = [
            phase for phase in plan.get("phases") or []
            if isinstance(phase, Mapping)
        ]
        issues.extend(_traceability_and_dag_issues(phases, model))
        issues.extend(_plan_execution_detail_issues(phases))
        issues.extend(_shared_package_contract_issues(phases))
        for index, phase in enumerate(phases):
            if not phase_task_contract(phase):
                issues.append(_issue(
                    "phase_contract", "phase_tasks_empty",
                    f"$.phases[{index}].tasks",
                    f"phase {phase.get('phase_id') or index + 1} must contain at least one task",
                ))
            if not _as_strings(phase.get("roles_needed") or phase.get("roles")):
                issues.append(_issue(
                    "phase_contract", "phase_roles_empty",
                    f"$.phases[{index}].roles_needed",
                    f"phase {phase.get('phase_id') or index + 1} must contain at least one role",
                ))
            if not _as_strings(phase.get("acceptance_criteria")):
                issues.append(_issue(
                    "phase_contract", "phase_acceptance_criteria_empty",
                    f"$.phases[{index}].acceptance_criteria",
                    f"phase {phase.get('phase_id') or index + 1} must contain acceptance criteria",
                ))

    source = model.source_requirements
    if not source.strip():
        issues.append(_issue(
            "project_contract", "source_requirements_missing",
            "$.project_contract.source_requirements",
            "confirmed contract must preserve the user's source requirements",
        ))
    expected = expected_source_requirements
    if expected is not None and source != expected:
        issues.append(_issue(
            "project_contract", "source_requirements_mismatch",
            "$.project_contract.source_requirements",
            "confirmed contract source requirements differ from the synthesized input",
        ))
    normalized_summary = " ".join(source.split())[:2000]
    if source and model.requirements_summary != normalized_summary:
        issues.append(_issue(
            "project_contract", "requirements_summary_mismatch",
            "$.project_contract.requirements_summary",
            "confirmed contract requirements summary does not match source requirements",
        ))
    expected_digest = (
        "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()
        if source else ""
    )
    if source and model.requirements_digest != expected_digest:
        issues.append(_issue(
            "project_contract", "requirements_digest_mismatch",
            "$.project_contract.requirements_digest",
            "confirmed contract requirements digest does not match source requirements",
        ))
    return ValidationResult(
        not issues,
        "plan_confirmation",
        tuple(issues),
        model.contract_version,
        PLAN_VERSION,
    )


def validate_phase_plan_layers(phase: Mapping[str, Any], requirements: Any, contract: ProjectContract | Mapping[str, Any] | None = None) -> ValidationResult:
    model = _contract_model(contract)
    issues = _schema_issues(requirements, PHASE_PLAN_SCHEMA)
    if isinstance(requirements, (list, tuple)):
        task_rows = [
            item for item in requirements if isinstance(item, Mapping)
        ]
        issues.extend(_phase_contract_issues(phase, task_rows, model))
        for index, task in enumerate(task_rows):
            issues.extend(_task_execution_detail_issues(task, f"$[{index}]"))
    return ValidationResult(not issues, "phase_plan", tuple(issues), model.contract_version, PHASE_PLAN_VERSION)


def validate_plan(plan: Dict[str, Any], contract: Mapping[str, Any] | ProjectContract | None = None) -> List[str]:
    return validate_plan_layers(plan, contract).violations


def validate_phase_requirements(phase: Dict[str, Any], requirements: List[Dict[str, Any]], contract: Mapping[str, Any] | ProjectContract | None = None) -> List[str]:
    return validate_phase_plan_layers(phase, requirements, contract).violations


def classify_phase_requirements(phase: Dict[str, Any], requirements: List[Dict[str, Any]], contract: Mapping[str, Any] | ProjectContract | None = None) -> Dict[str, List[str]]:
    return {"hard": validate_phase_requirements(phase, requirements, contract), "warnings": []}


def _hydrate_fallback_execution_fields(
    phase_rows: Sequence[Dict[str, Any]],
    technology_stack: Sequence[str],
) -> None:
    selected_stack = list(technology_stack) or [
        "React", "Vite", "TypeScript", "Node.js", "Express", "SQLite",
    ]
    for phase_index, phase in enumerate(phase_rows, 1):
        phase_id = str(phase.get("phase_id") or f"phase-{phase_index}")
        phase_name = next(
            iter(_real_strings(
                phase.get("name") or phase.get("phase_name")
            )),
            f"需求交付阶段 {phase_index}",
        )
        phase["name"] = phase_name
        phase["description"] = next(
            iter(_real_strings(
                phase.get("description")
                or phase.get("implementation_details")
            )),
            f"完成{phase_name}的需求实现、联调和自动化验证",
        )
        phase["implementation"] = next(
            iter(_real_strings(
                phase.get("implementation")
                or phase.get("implementation_method")
                or phase.get("approach")
            )),
            "按任务依赖顺序逐项实现，并以可运行结果和自动化测试验收",
        )
        phase["tech_stack"] = list(
            _real_strings(
                phase.get("tech_stack") or phase.get("technology_stack")
            )
            or tuple(selected_stack)
        )
        roles = (
            _real_strings(phase.get("roles_needed") or phase.get("roles"))
            or ("全栈工程师",)
        )
        phase["roles_needed"] = list(roles)
        phase["responsibilities"] = list(
            _real_strings(
                phase.get("responsibilities") or phase.get("duties")
            )
            or tuple(
                f"{role}：负责本阶段实现、联调、验证和交付"
                for role in roles
            )
        )
        personnel_count = _positive_int(
            phase.get("agent_count")
            or phase.get("personnel_count")
            or phase.get("headcount")
        ) or len(roles)
        phase["agent_count"] = personnel_count
        phase["personnel_allocation"] = list(
            _real_strings(
                phase.get("personnel_allocation") or phase.get("staffing")
            )
            or tuple(f"{role}：1 人" for role in roles)
        )

        tasks = phase_task_contract(phase)
        for task_index, task in enumerate(tasks, 1):
            task_id = str(
                task.get("task_id")
                or f"{phase_id}-task-{task_index}"
            )
            task["task_id"] = task_id
            task_name = next(
                iter(_real_strings(
                    task.get("name")
                    or task.get("task_name")
                    or task.get("deliverable")
                )),
                f"任务 {task_index}",
            )
            task["name"] = task_name
            description = next(
                iter(_real_strings(
                    task.get("task_description")
                    or task.get("description")
                    or task.get("implementation_details")
                    or task.get("deliverable")
                )),
                f"完成{task_name}的详细实现和验证",
            )
            task["description"] = description
            task["implementation"] = next(
                iter(_real_strings(
                    task.get("implementation")
                    or task.get("implementation_method")
                    or task.get("approach")
                )),
                f"按依赖顺序实现、联调并自动验证：{description}",
            )
            task["tech_stack"] = list(
                _real_strings(
                    task.get("tech_stack")
                    or task.get("technology_stack")
                )
                or tuple(phase["tech_stack"])
            )
            task_roles = (
                _real_strings(
                    task.get("roles")
                    or task.get("required_role")
                    or task.get("role")
                )
                or (roles[(task_index - 1) % len(roles)],)
            )
            task["roles"] = list(task_roles)
            task["responsibilities"] = list(
                _real_strings(
                    task.get("responsibilities") or task.get("duties")
                )
                or (
                    f"{task_roles[0]}：负责{task_name}的实现、验证和交付",
                )
            )
            task_personnel = _positive_int(
                task.get("personnel_count")
                or task.get("agent_count")
                or task.get("headcount")
            ) or 1
            task["personnel_count"] = task_personnel
            task["personnel_allocation"] = list(
                _real_strings(
                    task.get("personnel_allocation")
                    or task.get("staffing")
                )
                or (f"{task_roles[0]}：{task_personnel} 人",)
            )
            task["acceptance_criteria"] = list(
                _real_strings(task.get("acceptance_criteria"))
                or (f"{task_name}具备可运行结果并通过自动化验证",)
            )
        phase["task_contract"] = tasks
        phase["acceptance_criteria"] = list(
            _real_strings(phase.get("acceptance_criteria"))
            or tuple(
                criterion
                for task in tasks
                for criterion in _real_strings(
                    task.get("acceptance_criteria")
                )
            )
            or ("本阶段全部任务具备可运行结果并通过自动化验证",)
        )


def deterministic_plan_fallback(contract: ProjectContract | Mapping[str, Any]) -> Dict[str, Any]:
    model = _contract_model(contract)
    executable_units = tuple(
        unit
        for unit in model.requirement_units
        if _unit_requires_task_substantiation(unit)
    )
    task_source_units = executable_units or model.requirement_units
    phase_rows: List[Dict[str, Any]] = []
    task_rows: List[Tuple[int, int, Dict[str, Any], LockedTask]] = []
    for phase_index, phase in enumerate(model.phases):
        phase_row = {
            "phase_id": phase.phase_id, "order": phase.order, "name": phase.name,
            "roles_needed": list(phase.roles),
            "task_contract": [task.to_dict() for task in phase.tasks],
            "acceptance_criteria": list(phase.acceptance_criteria),
            "dependencies": list(phase.dependencies),
            "source_constraints": list(phase.source_constraints),
        }
        phase_rows.append(phase_row)
        for task_index, (task_row, locked_task) in enumerate(zip(
            phase_row["task_contract"], phase.tasks
        )):
            task_rows.append((phase_index, task_index, task_row, locked_task))

    # A numeric phase constraint creates identity-only phase skeletons during
    # parsing. If every skeleton is taskless, it is not a user-authored
    # executable plan and must use the same deterministic recovery path as a
    # free-form requirement. Any genuinely explicit task keeps the original
    # phase contract untouched.
    generated_empty_skeleton = (
        not model.phases
        or (
            model.phase_count is not None
            and len(model.phases) == model.phase_count
            and all(
                not phase.tasks
                and phase.name == (
                    "阶段"
                    + str(next(
                        (
                            key for key, value in _CN_NUMBERS.items()
                            if value == phase_index
                        ),
                        phase_index,
                    ))
                )
                for phase_index, phase in enumerate(model.phases, 1)
            )
        )
    )
    if task_source_units and generated_empty_skeleton:
        selected_stack = list(model.technology_stack) or [
            "React", "Vite", "TypeScript", "Node.js", "Express", "SQLite",
        ]
        derived_phase_count = max(
            1,
            (len(task_source_units) + 3) // 4,
        )
        generated_phase_count = (
            model.phase_count
            or max(derived_phase_count, model.minimum_phase_count or 0)
        )
        phase_rows = []
        task_rows = []
        previous_task_id: str | None = None
        previous_phase_id: str | None = None
        unit_count = len(task_source_units)

        for phase_index in range(generated_phase_count):
            existing_phase = (
                model.phases[phase_index]
                if phase_index < len(model.phases) else None
            )
            phase_id = (
                existing_phase.phase_id
                if existing_phase else f"phase-{phase_index + 1}"
            )
            phase_name = (
                existing_phase.name
                if existing_phase else f"需求交付阶段 {phase_index + 1}"
            )
            phase_roles = (
                existing_phase.roles
                if existing_phase and existing_phase.roles
                else model.roles or ("全栈工程师",)
            )
            phase_dependencies = [previous_phase_id] if previous_phase_id else []
            start = phase_index * unit_count // generated_phase_count
            end = (phase_index + 1) * unit_count // generated_phase_count
            units = task_source_units[start:end]
            if not units:
                units = (
                    task_source_units[
                        min(phase_index, unit_count - 1)
                    ],
                )

            tasks: List[Dict[str, Any]] = []
            for task_index, unit in enumerate(units):
                task_id = f"{phase_id}-task-{task_index + 1}"
                dependencies = [previous_task_id] if previous_task_id else []
                role = phase_roles[task_index % len(phase_roles)]
                acceptance = (
                    f"提供可运行、可自动验证的证据，满足 {unit.unit_id}："
                    f"{unit.exact_text}"
                )
                task = {
                    "task_id": task_id,
                    "order": task_index + 1,
                    "name": unit.exact_text,
                    "deliverable": unit.exact_text,
                    "description": (
                        f"实现需求 {unit.unit_id}：{unit.exact_text}"
                    ),
                    "implementation": (
                        f"由{role}使用所选技术栈实现、联调并自动验证："
                        f"{unit.exact_text}"
                    ),
                    "tech_stack": selected_stack,
                    "roles": [role],
                    "responsibilities": [f"{role}：实现、联调、测试和交付"],
                    "personnel_count": 1,
                    "personnel_allocation": [f"{role}：1 人"],
                    "source_requirement_ids": [unit.unit_id],
                    "acceptance_criteria": [acceptance],
                    "dependencies": dependencies,
                    "source_constraints": [unit.unit_id],
                }
                locked_task = LockedTask(
                    task_id=task_id,
                    order=task_index + 1,
                    name=unit.exact_text,
                    roles=(role,),
                    acceptance_criteria=(acceptance,),
                    dependencies=tuple(dependencies),
                    source_constraints=(unit.unit_id,),
                    source_requirement_ids=(unit.unit_id,),
                )
                tasks.append(task)
                task_rows.append((
                    phase_index,
                    task_index,
                    task,
                    locked_task,
                ))
                previous_task_id = task_id

            phase_rows.append({
                "phase_id": phase_id,
                "order": phase_index + 1,
                "name": phase_name,
                "description": "完成本阶段需求实现、联调和自动化验证",
                "implementation": (
                    "按任务依赖顺序逐项实现，并以可运行结果和自动化测试验收"
                ),
                "duration": "待定",
                "tech_stack": selected_stack,
                "roles_needed": list(phase_roles),
                "agent_count": len(phase_roles),
                "responsibilities": [
                    f"{role}：实现、联调、测试和交付"
                    for role in phase_roles
                ],
                "personnel_allocation": [
                    f"{role}：1 人" for role in phase_roles
                ],
                "task_contract": tasks,
                "acceptance_criteria": [
                    "本阶段全部任务具备可运行结果并通过自动化验证",
                ],
                "dependencies": phase_dependencies,
                "source_constraints": [unit.unit_id for unit in units],
            })
            previous_phase_id = phase_id

    # Explicit plans often lock phase/task identity but omit the mechanical v3
    # trace fields. Hydrate only semantically supported mappings. Source text
    # must never be copied into an unrelated task to manufacture that support.
    if model.contract_version >= 3 and model.requirement_units:
        initially_covered = {
            source_id
            for _phase_index, _task_index, task, _locked_task in task_rows
            for source_id in _as_strings(task.get("source_requirement_ids"))
        }

        def mapping_score(
            unit: RequirementUnit,
            row: Tuple[int, int, Dict[str, Any], LockedTask],
        ) -> Tuple[int, int, int, int]:
            phase_index, task_index, task, locked_task = row
            evidence = " ".join((
                phase_rows[phase_index]["name"],
                locked_task.name,
                " ".join(locked_task.acceptance_criteria),
                " ".join(locked_task.source_constraints),
            ))
            overlap = len(_trace_keywords(unit.exact_text) & _trace_keywords(evidence))
            explicit_orders = {
                int(value) for value in re.findall(
                    r"(?:原始需求|需求|requirement)\s*#?\s*(\d+)",
                    " ".join(locked_task.source_constraints),
                    re.IGNORECASE,
                )
            }
            explicit = 1 if unit.order in explicit_orders else 0
            # Earlier task order is the stable final tie-breaker.
            return (
                explicit,
                overlap,
                -len(_as_strings(task.get("source_requirement_ids"))),
                -(phase_index * 10000 + task_index),
            )

        def is_generated_task_placeholder(
            row: Tuple[int, int, Dict[str, Any], LockedTask],
        ) -> bool:
            _phase_index, _task_index, task, locked_task = row
            return bool(
                re.fullmatch(r"任务\s*\d+", locked_task.name)
                and not locked_task.acceptance_criteria
                and not locked_task.source_constraints
                and not _real_strings(
                    task.get("description")
                    or task.get("task_description")
                    or task.get("deliverable")
                )
            )

        def substantiate_generated_task(
            unit: RequirementUnit,
            row: Tuple[int, int, Dict[str, Any], LockedTask],
        ) -> None:
            if (
                not _unit_requires_task_substantiation(unit)
                or not is_generated_task_placeholder(row)
            ):
                return
            _phase_index, _task_index, task, _locked_task = row
            task["description"] = f"实现用户需求：{unit.exact_text}"
            task["deliverable"] = unit.exact_text
            task["implementation"] = (
                f"按锁定阶段和任务边界实现并自动验证：{unit.exact_text}"
            )

        def can_receive(
            unit: RequirementUnit,
            row: Tuple[int, int, Dict[str, Any], LockedTask],
        ) -> bool:
            explicit, overlap, _load, _tie_breaker = mapping_score(unit, row)
            if not _unit_requires_task_substantiation(unit):
                return bool(explicit or task_rows)
            return bool(explicit or overlap or is_generated_task_placeholder(row))

        ordered_units = sorted(executable_units, key=lambda item: item.order)
        for unit in ordered_units:
            if unit.unit_id in initially_covered:
                continue
            candidates = [row for row in task_rows if can_receive(unit, row)]
            if not candidates:
                open_phase_index = next((
                    index for index, phase in enumerate(model.phases)
                    if (
                        not phase_rows[index]["task_contract"]
                        and phase.roles
                    )
                ), None)
                if (
                    open_phase_index is None
                    or not _unit_requires_task_substantiation(unit)
                ):
                    continue
                phase = model.phases[open_phase_index]
                task_index = len(phase_rows[open_phase_index]["task_contract"])
                task_id = f"{phase.phase_id}-requirement-{unit.order}"
                acceptance = (
                    f"Server-verifiable evidence satisfies {unit.unit_id}: "
                    f"{unit.exact_text}"
                )
                task = {
                    "task_id": task_id,
                    "order": task_index + 1,
                    "name": f"Implement {unit.exact_text}",
                    "deliverable": unit.exact_text,
                    "description": (
                        f"Execute canonical requirement {unit.unit_id}: "
                        f"{unit.exact_text}"
                    ),
                    "roles": list(phase.roles),
                    "source_requirement_ids": [unit.unit_id],
                    "acceptance_criteria": [acceptance],
                    "dependencies": [],
                    "source_constraints": [unit.unit_id],
                }
                locked_task = LockedTask(
                    task_id=task_id,
                    order=task_index + 1,
                    name=task["name"],
                    roles=phase.roles,
                    acceptance_criteria=(acceptance,),
                    source_constraints=(unit.unit_id,),
                    source_requirement_ids=(unit.unit_id,),
                )
                phase_rows[open_phase_index]["task_contract"].append(task)
                task_rows.append((
                    open_phase_index, task_index, task, locked_task,
                ))
                initially_covered.add(unit.unit_id)
                continue
            chosen = max(candidates, key=lambda row: mapping_score(unit, row))
            phase_index, task_index, task, locked_task = chosen
            substantiate_generated_task(unit, chosen)
            task["source_requirement_ids"] = list(_dedupe((
                *task.get("source_requirement_ids", ()), unit.unit_id,
            )))
            if not locked_task.acceptance_criteria:
                task["acceptance_criteria"] = list(_dedupe((
                    *task.get("acceptance_criteria", ()),
                    f"{locked_task.name} produces server-verifiable acceptance evidence",
                )))

        # Every executable task must cite a source, including tasks whose
        # nearest unit is contextual rather than binding.
        for phase_index, task_index, task, locked_task in task_rows:
            if task.get("source_requirement_ids"):
                continue
            row = (phase_index, task_index, task, locked_task)
            candidates = [
                unit for unit in task_source_units
                if can_receive(unit, row)
            ]
            if not candidates:
                continue
            chosen = max(candidates, key=lambda unit: mapping_score(unit, row))
            substantiate_generated_task(chosen, row)
            task["source_requirement_ids"] = [chosen.unit_id]
            if not locked_task.acceptance_criteria:
                task["acceptance_criteria"] = [
                    f"{locked_task.name} produces server-verifiable acceptance evidence"
                ]

        for phase_index, phase in enumerate(model.phases):
            if phase_rows[phase_index]["acceptance_criteria"]:
                continue
            criteria = _dedupe(
                criterion
                for task in phase_rows[phase_index]["task_contract"]
                for criterion in _as_strings(task.get("acceptance_criteria"))
            )
            if criteria:
                phase_rows[phase_index]["acceptance_criteria"] = list(criteria)

    _hydrate_fallback_execution_fields(
        phase_rows,
        model.technology_stack,
    )

    # A count-only task contract locks identity and cardinality, not PM-owned
    # execution details.  Give deterministic fallback tasks a valid prior-only
    # DAG; model-generated non-empty dependencies remain untouched.
    previous_task_id: str | None = None
    previous_phase_id: str | None = None
    for phase in phase_rows:
        tasks = [
            task for task in (phase.get("task_contract") or [])
            if isinstance(task, dict)
        ]
        has_placeholder = any(
            bool(task.get("planning_placeholder")) for task in tasks
        )
        if (
            has_placeholder
            and previous_phase_id
            and not _as_strings(phase.get("dependencies"))
        ):
            phase["dependencies"] = [previous_phase_id]
        for task in tasks:
            if (
                task.get("planning_placeholder")
                and previous_task_id
                and not _as_strings(task.get("dependencies"))
            ):
                task["dependencies"] = [previous_task_id]
            previous_task_id = str(task.get("task_id") or "") or previous_task_id
        previous_phase_id = str(phase.get("phase_id") or "") or previous_phase_id

    return {
        "plan_version": PLAN_VERSION, "contract_version": model.contract_version,
        "source": "deterministic_contract_fallback",
        "tech_stack": (
            list(model.technology_stack)
            or (
                list(phase_rows[0].get("tech_stack") or [])
                if phase_rows else []
            )
        ),
        "phases": phase_rows,
        "project_contract": model.as_mapping(),
    }


def deterministic_phase_fallback(phase: Mapping[str, Any], contract: ProjectContract | Mapping[str, Any] | None = None) -> List[Dict[str, Any]]:
    model = _contract_model(contract)
    locked = next((item for item in model.phases if item.phase_id == str(phase.get("phase_id") or "")), None)
    phase_data = dict(phase)
    if locked:
        if locked.tasks:
            original_by_id = {
                str(task.get("task_id") or ""): dict(task)
                for task in phase_task_contract(phase)
                if isinstance(task, Mapping)
            }
            phase_data["task_contract"] = []
            for task in locked.tasks:
                enriched = original_by_id.get(task.task_id, {})
                enriched.update(task.to_dict())
                canonical_files = [
                    item.path
                    for item in model.required_files
                    if item.required
                    and item.phase_id == locked.phase_id
                    and item.task_id == task.task_id
                ]
                if canonical_files:
                    enriched["required_files"] = canonical_files
                phase_data["task_contract"].append(enriched)
        if locked.roles:
            phase_data["roles_needed"] = list(locked.roles)
        if model.technology_stack and not (
            phase_data.get("tech_stack")
            or phase_data.get("technology_stack")
        ):
            phase_data["tech_stack"] = list(model.technology_stack)
    return canonical_phase_requirements(phase_data)


def validation_metadata(result: ValidationResult) -> Dict[str, Any]:
    return result.to_dict()


def artifact_metadata(artifact_type: str, version: int, source: str, validation: ValidationResult, corrections: Sequence[Mapping[str, Any]] = ()) -> Dict[str, Any]:
    """Create deterministic persistence metadata; callers own timestamps."""
    return {
        "artifact_type": artifact_type, "version": int(version), "source": str(source),
        "contract_version": validation.contract_version, "validation": validation.to_dict(),
        "auto_corrections": [dict(item) for item in corrections],
    }
