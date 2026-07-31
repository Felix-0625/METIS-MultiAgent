"""文件管理路由"""
import asyncio
import hashlib
import html
import io
import os
import time
import json
import logging
import shutil
import uuid
import zipfile
import urllib.parse
import xml.etree.ElementTree as ET
from functools import wraps
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from starlette.responses import FileResponse, StreamingResponse
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from core.app_state import (
    app, projects, hermes_client, global_sm_agent, gitee_sync,
    config_loader, agents_api_config, DEFAULT_API_CONFIG,
    global_pm_team, global_supervisor_team, global_ccb_agent,
    _get_project, _get_hermes, _get_idea_landing,
    _persist_all_async, _persist_idea_landing,
    _pm_teams, _phase_managers, _supervisor_leaders,
    ProjectContext, WORKSPACE_ROOT, _project_workspace,
    repair_registry, DefectStatus, ArbiterDecision,
    MCPToolHandler, handle_mcp_request, TOOL_DEFINITIONS,
    HybridMemory, PhaseManager, GiteeSync,
    PMLeaderAgent, PMMemberAgent, SupervisorLeaderAgent,
    ExecutionAgent, IdeaLandingAgent,
    logger,
)
from models.schemas import (
    ProjectRequest, AnalyzeRequest, SubprojectConfirmRequest,
    SubprojectRequest, AgentCreateRequest, AgentApiConfigRequest,
    SkillImportRequest, SkillSearchRequest, ChangeRequest,
    GiteeConfigRequest, GiteePushRequest, DefaultApiConfigRequest,
    SupervisorChatRequest, RepairStartRequest, ProposalSubmitRequest,
    ProposalReviewRequest, ArbiterForcePassRequest,
    SkillIngestRequest, SkillConfirmRequest, ChatHistorySaveRequest,
    FileWriteRequest, FileStagingRequest, FileCommitRequest,
    FileRollbackRequest, HRReassignRequest, PMTeamChatRequest,
    PlanConfirmRequest, SupervisorReviewChatRequest,
    PhasePMChatRequest, PhaseDescUpdateRequest,
    IssueSubmitToPMRequest, BatchIssueSubmitToPMRequest,
    PhasePlanExpertRequest, ExpertRequirement, PhaseExpertMatchRequest,
    ExpertAssignment, PhaseExpertConfirmRequest,
    EmployeeCreateRequest, EmployeeUpdateRequest,
    ExpertCreateRequest, ExpertUpdateRequest,
    ExpertMemoryRequest, ExpertMatchRequest,
    ExpertTrainingRequest, ExpertWorkModeRequest,
    ExpertConfigRequest, ExpertChatTrainRequest,
    ExpertFeedbackRequest, ExpertKnowledgeRequest,
    CCBCheckDeleteMemberRequest, CCBCheckDeleteExpertRequest,
    CCBConfirmDeleteRequest, EngineerRepairChatRequest,
    EngineerApplyFixRequest, EngineerManualRequest,
    EngineerQAChatRequest, EngineerQAInspectRequest,
    AdjustmentChatRequest, AdjustmentConfirmRequest,
    AdjustmentPhaseConfirmRequest, InjectQCRequest,
    IdeaChatRequest, IdeaNewConvRequest, IdeaConvMetaRequest,
    IdeaPinRequest, IdeaAdvancePhaseRequest, IdeaUserMemoryRequest,
    ProjectTeamAssignRequest,
)
from core.workspace_integrity import (
    collect_delivery_artifact,
    release_gate_error,
)
from core.project_write_fence import (
    ProjectWriteFenceConflict,
    project_write_guard,
)

router = APIRouter(tags=["files"])

_HIDDEN_WORKSPACE_ROOTS = {".project", ".git"}
_SECRET_OR_RUNTIME_PARTS = {"logs", "tmp", "temp", "backup", "backups"}
_ROOT_GENERATED_OR_RUNTIME_DIRECTORIES = {
    "dist", "build", "output", "data", ".next", ".nuxt", "coverage",
    "vendor",
}
_SECRET_OR_RUNTIME_SUFFIXES = {
    ".pem", ".key", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3",
}
_OOXML_MAX_ENTRIES = 2_000
_OOXML_MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
_OOXML_MAX_ENTRY_BYTES = 10 * 1024 * 1024
_OOXML_MAX_COMPRESSION_RATIO = 100
_UPLOAD_MAX_EXTRACTED_CHARS = 50_000


def _is_secret_or_runtime_state_path(parts: tuple[str, ...]) -> bool:
    lowered = tuple(part.casefold() for part in parts)
    if not lowered or lowered[0] in _HIDDEN_WORKSPACE_ROOTS:
        return True
    name = lowered[-1]
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    if name in {"id_rsa", "id_ed25519", "secrets.json", "credentials.json"}:
        return True
    if any(name.endswith(suffix) for suffix in _SECRET_OR_RUNTIME_SUFFIXES):
        return True
    return any(part in _SECRET_OR_RUNTIME_PARTS for part in lowered)


def _project_write_fenced_route(handler):
    """Hold the project-wide write guard for the complete route transaction."""
    @wraps(handler)
    async def guarded(project_id: str, *args, **kwargs):
        ctx = _get_project(project_id)
        try:
            with project_write_guard(project_id, ctx.workspace):
                return await handler(project_id, *args, **kwargs)
        except ProjectWriteFenceConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return guarded


def _safe_archive_name(name: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in name)
    return safe.strip("._") or "project"


def _portable_path_conflicts(paths: List[str]) -> List[tuple[str, str]]:
    """Return path-prefix collisions on case-insensitive filesystems."""
    seen: Dict[str, str] = {}
    conflicts: List[tuple[str, str]] = []
    for raw_path in paths:
        parts: List[str] = []
        for part in str(raw_path).replace("\\", "/").split("/"):
            if not part:
                continue
            parts.append(part)
            prefix = "/".join(parts)
            key = prefix.casefold()
            previous = seen.get(key)
            if previous is not None and previous != prefix:
                pair = (previous, prefix)
                if pair not in conflicts:
                    conflicts.append(pair)
                break
            else:
                seen[key] = prefix
    return conflicts


def _resolve_within_workspace(workspace: Path, rel_path: str, action: str = "访问") -> Path:
    """解析工作区内路径并严格校验，防止路径穿越（relative_to 精确判断，避免 startswith 前缀绕过）"""
    normalized = str(rel_path or "").replace("\\", "/").strip()
    parts = tuple(part for part in normalized.split("/") if part not in {"", "."})
    if (
        not normalized
        or Path(normalized).is_absolute()
        or bool(Path(normalized).drive)
        or ".." in parts
        or _is_secret_or_runtime_state_path(parts)
    ):
        raise HTTPException(
            status_code=403,
            detail=f"unsafe or sensitive project path cannot be used for {action}",
        )
    unresolved = workspace.resolve()
    for part in parts:
        unresolved = unresolved / part
        if unresolved.is_symlink():
            raise HTTPException(
                status_code=403,
                detail=f"symbolic links cannot be used for {action}",
            )
    target = (workspace / "/".join(parts)).resolve()
    try:
        target.relative_to(workspace.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail=f"禁止{action}工作区外的文件")
    return target


def _canonical_delivery_path(
    workspace: Path,
    raw_path: str,
    *,
    action: str,
    allow_internal: bool = False,
    required_paths: tuple[str, ...] | List[str] = (),
) -> tuple[str, Path]:
    """Return a portable relative path and reject internal/traversal aliases."""
    normalized = str(raw_path or "").replace("\\", "/").strip()
    candidate = Path(normalized)
    parts = tuple(part for part in normalized.split("/") if part not in {"", "."})
    if (
        not normalized
        or candidate.is_absolute()
        or bool(candidate.drive)
        or any(part == ".." for part in parts)
    ):
        raise HTTPException(status_code=403, detail=f"禁止{action}非法项目路径")
    canonical = "/".join(parts)
    if (
        not allow_internal
        and parts
        and parts[0].casefold() in _HIDDEN_WORKSPACE_ROOTS
    ):
        raise HTTPException(status_code=403, detail="禁止修改项目内部状态")
    normalized_required = {
        str(item or "").replace("\\", "/").strip("/")
        for item in required_paths
        if str(item or "").strip()
    }
    required = any(
        canonical == item or canonical.startswith(item.rstrip("/") + "/")
        for item in normalized_required
    )
    if (
        parts
        and parts[0].casefold() in _ROOT_GENERATED_OR_RUNTIME_DIRECTORIES
        and not required
    ):
        raise HTTPException(
            status_code=403,
            detail=f"non-delivery runtime paths cannot be used for {action}",
        )
    unresolved = workspace.resolve()
    for part in parts:
        unresolved = unresolved / part
        if unresolved.is_symlink():
            raise HTTPException(
                status_code=403,
                detail=f"symbolic links are not valid project paths for {action}",
            )
    target = _resolve_within_workspace(workspace, canonical, action)
    return canonical, target


def _canonical_safe_read_path(
    ctx: ProjectContext,
    raw_path: str,
    *,
    action: str,
) -> tuple[str, Path]:
    canonical, target = _canonical_delivery_path(
        ctx.workspace,
        raw_path,
        action=action,
        required_paths=_required_delivery_paths(ctx),
    )
    _, delivery_files = collect_delivery_artifact(
        ctx.workspace,
        required_paths=_required_delivery_paths(ctx),
    )
    if canonical not in delivery_files:
        raise HTTPException(
            status_code=403,
            detail=f"non-delivery, sensitive, or runtime files cannot be {action}",
        )
    return canonical, target


def _atomic_replace_bytes(target: Path, content: bytes) -> None:
    """Replace one file without exposing a truncated intermediate state."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _resolve_staging_source(staging_dir: Path, relative: str) -> Path:
    source = (staging_dir / relative).resolve()
    try:
        source.relative_to(staging_dir.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="暂存索引包含非法路径") from exc
    if not source.is_file():
        raise HTTPException(status_code=409, detail=f"暂存文件缺失：{relative}")
    return source


def _required_delivery_paths(ctx: ProjectContext) -> List[str]:
    phase_manager = _phase_managers.get(str(getattr(ctx, "project_id", "") or ""))
    contract = getattr(phase_manager, "project_contract", {}) if phase_manager else {}
    return sorted({
        str(item.get("path") or "").replace("\\", "/").strip("/")
        for item in (contract.get("required_files") or [])
        if isinstance(item, dict)
        and item.get("required", True)
        and str(item.get("path") or "").strip()
    })


def _path_match_key(path: Any) -> str:
    value = str(path or "").replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    return value.casefold()


def _invalidate_qa_after_workspace_mutation(
    ctx: ProjectContext,
    *,
    reason: str,
    changed_paths: List[str],
) -> None:
    """Invalidate every projection that was bound to the previous bytes."""
    changed = {
        _path_match_key(path)
        for path in changed_paths
        if str(path or "").strip()
    }
    whole = ctx.qc_results.setdefault("__whole_project__", {})
    qa_entry = whole.setdefault("qa", {}) if isinstance(whole, dict) else {}
    if isinstance(qa_entry, dict):
        qa_entry["passed"] = False
        qa_entry["status"] = "stale"
        qa_entry["stale_reason"] = reason
        qa_entry.pop("workspace_digest", None)
        qa_entry.pop("workspace_digest_algorithm", None)
        qa_entry.pop("delivery_manifest", None)
    for value in (ctx.qc_results or {}).values():
        if not isinstance(value, dict):
            continue
        qa = value.get("qa") if isinstance(value.get("qa"), dict) else value
        for issue in qa.get("issues_detail", []) or []:
            if not isinstance(issue, dict):
                continue
            issue_path = str(
                issue.get("file_path") or issue.get("file") or ""
            )
            issue_path = _path_match_key(issue_path)
            if issue_path not in changed:
                continue
            if str(issue.get("status") or "") in {"fixed", "verified"}:
                issue["status"] = "pending_verification"
                issue["verification_result"] = "stale_after_workspace_mutation"
                issue["verification_stale_reason"] = reason
                issue.pop("verified_at", None)
                issue.pop("fixed_at", None)
    phase_manager = _phase_managers.get(str(getattr(ctx, "project_id", "") or ""))
    if phase_manager is not None:
        affected_phase_ids = {
            str(owner.get("phase_id") or "")
            for path, owner in getattr(phase_manager, "file_registry", {}).items()
            if _path_match_key(path) in changed
            and isinstance(owner, dict)
            and owner.get("phase_id")
        }
        for phase in getattr(phase_manager, "phases", []) or []:
            phase_id = str(phase.get("phase_id") or "")
            if affected_phase_ids and phase_id not in affected_phase_ids:
                continue
            if phase.get("review_passed") or phase.get("status") == "completed":
                phase["reviewed"] = False
                phase["review_passed"] = False
                phase["user_confirmed"] = False
                phase["status"] = "awaiting_authoritative_qa"
                phase.pop("completed_at", None)
    try:
        from api import routes_adjustments

        routes_adjustments._final_qa_status[ctx.project_id] = {
            "status": "not_started",
            "all_passed": False,
            "message": "Workspace changed; authoritative Final QA is required",
            "stale_reason": reason,
        }
    except (AttributeError, ImportError):
        pass
    ctx.status = "running"


def _validate_ooxml_archive(content: bytes) -> zipfile.ZipFile:
    """Open an OOXML archive only after bounded metadata validation."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        infos = archive.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("DOCX is not a valid OOXML zip archive") from exc
    total_uncompressed = 0
    if len(infos) > _OOXML_MAX_ENTRIES:
        archive.close()
        raise ValueError("DOCX contains too many archive entries")
    for info in infos:
        normalized = str(info.filename or "").replace("\\", "/")
        path = Path(normalized)
        if (
            not normalized
            or path.is_absolute()
            or bool(path.drive)
            or ".." in path.parts
        ):
            archive.close()
            raise ValueError("DOCX contains an unsafe archive path")
        total_uncompressed += int(info.file_size or 0)
        if int(info.file_size or 0) > _OOXML_MAX_ENTRY_BYTES:
            archive.close()
            raise ValueError("DOCX contains an oversized archive entry")
        compressed = int(info.compress_size or 0)
        ratio = (
            float(info.file_size) / max(1, compressed)
            if info.file_size
            else 1.0
        )
        if ratio > _OOXML_MAX_COMPRESSION_RATIO:
            archive.close()
            raise ValueError("DOCX compression ratio exceeds the safety limit")
    if total_uncompressed > _OOXML_MAX_UNCOMPRESSED_BYTES:
        archive.close()
        raise ValueError("DOCX uncompressed size exceeds the safety limit")
    if archive.testzip() is not None:
        archive.close()
        raise ValueError("DOCX archive integrity check failed")
    return archive


def _extract_file_text_result(filename: str, content: bytes) -> Dict[str, Any]:
    """Extract uploaded requirements with explicit completeness metadata."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        if ext in {
            "txt", "md", "markdown", "log", "yaml", "yml", "toml", "ini",
            "env", "csv",
        }:
            text = content.decode("utf-8", errors="strict")
        elif ext == "json":
            decoded = content.decode("utf-8", errors="strict")
            text = json.dumps(
                json.loads(decoded),
                ensure_ascii=False,
                indent=2,
            )
        elif ext == "pdf":
            try:
                import pdfplumber
            except ImportError as exc:
                raise ValueError("PDF extraction dependency is unavailable") from exc
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        elif ext == "docx":
            import re

            with _validate_ooxml_archive(content) as archive:
                names = [
                    name for name in archive.namelist()
                    if name == "word/document.xml"
                    or (
                        name.startswith("word/")
                        and Path(name).name.startswith(
                            ("header", "footer", "footnotes", "endnotes", "comments")
                        )
                        and name.endswith(".xml")
                    )
                ]
                if "word/document.xml" not in names:
                    raise ValueError("DOCX is missing word/document.xml")
                fragments: List[str] = []
                for name in sorted(set(names)):
                    xml_text = archive.read(name).decode("utf-8", errors="strict")
                    fragments.extend(
                        html.unescape(item)
                        for item in re.findall(
                            r"<w:t[^>]*>(.*?)</w:t>",
                            xml_text,
                            flags=re.DOTALL,
                        )
                    )
                text = "\n".join(item for item in fragments if item)
        elif ext == "doc":
            raise ValueError(
                "legacy .doc cannot be parsed losslessly; convert it to DOCX or text"
            )
        else:
            raise ValueError(f"unsupported attachment format: {ext or 'unknown'}")
    except Exception as exc:
        return {
            "complete": False,
            "text": "",
            "char_count": 0,
            "error": str(exc)[:500],
            "format": ext,
        }
    return {
        "complete": True,
        "text": text,
        "char_count": len(text),
        "error": "",
        "format": ext,
    }



_MAX_EXTRACTED_CHARS = 50_000
_MAX_DOCX_ENTRIES = 2_048
_MAX_DOCX_ENTRY_BYTES = 10 * 1024 * 1024
_MAX_DOCX_TOTAL_BYTES = 20 * 1024 * 1024
_MAX_DOCX_COMPRESSION_RATIO = 100.0


def _extraction(
    text: str = "",
    *,
    complete: bool,
    reason: str,
) -> Dict[str, Any]:
    return {"text": text, "complete": complete, "reason": reason}


def _bounded_extraction(text: str, *, reason: str) -> Dict[str, Any]:
    if len(text) > _MAX_EXTRACTED_CHARS:
        return _extraction(
            text[:_MAX_EXTRACTED_CHARS],
            complete=False,
            reason=reason,
        )
    return _extraction(text, complete=True, reason="ok")


def _extract_docx_text(content: bytes) -> Dict[str, Any]:
    """Extract DOCX text only after bounded central-directory validation."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError, ValueError):
        return _extraction(complete=False, reason="docx_archive_invalid")

    with archive:
        entries = archive.infolist()
        if len(entries) > _MAX_DOCX_ENTRIES:
            return _extraction(
                complete=False, reason="docx_entry_count_exceeded",
            )
        total_uncompressed = 0
        by_name: Dict[str, zipfile.ZipInfo] = {}
        for entry in entries:
            normalized = entry.filename.replace("\\", "/").lstrip("/")
            by_name[normalized] = entry
            if entry.flag_bits & 0x1:
                return _extraction(
                    complete=False, reason="docx_encrypted_entry",
                )
            if entry.file_size < 0 or entry.compress_size < 0:
                return _extraction(
                    complete=False, reason="docx_entry_size_invalid",
                )
            if entry.file_size > _MAX_DOCX_ENTRY_BYTES:
                return _extraction(
                    complete=False, reason="docx_entry_size_exceeded",
                )
            total_uncompressed += entry.file_size
            if total_uncompressed > _MAX_DOCX_TOTAL_BYTES:
                return _extraction(
                    complete=False, reason="docx_total_size_exceeded",
                )
            if entry.file_size:
                if entry.compress_size == 0:
                    return _extraction(
                        complete=False,
                        reason="docx_compression_ratio_exceeded",
                    )
                ratio = entry.file_size / entry.compress_size
                if ratio > _MAX_DOCX_COMPRESSION_RATIO:
                    return _extraction(
                        complete=False,
                        reason="docx_compression_ratio_exceeded",
                    )

        document = by_name.get("word/document.xml")
        if document is None:
            return _extraction(
                complete=False, reason="docx_document_xml_missing",
            )
        if document.file_size > _MAX_DOCX_ENTRY_BYTES:
            return _extraction(
                complete=False, reason="docx_document_xml_too_large",
            )
        try:
            xml_bytes = archive.read(document)
        except (RuntimeError, zipfile.BadZipFile, OSError, EOFError):
            return _extraction(
                complete=False, reason="docx_document_xml_read_failed",
            )

    if len(xml_bytes) != document.file_size:
        return _extraction(
            complete=False, reason="docx_document_xml_size_mismatch",
        )
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return _extraction(
            complete=False, reason="docx_document_xml_invalid",
        )
    paragraphs: List[str] = []
    for paragraph in root.iter():
        if not paragraph.tag.endswith("}p"):
            continue
        parts = [
            str(node.text or "")
            for node in paragraph.iter()
            if node.tag.endswith("}t")
        ]
        rendered = "".join(parts)
        if rendered:
            paragraphs.append(rendered)
    return _bounded_extraction(
        "\n".join(paragraphs),
        reason="docx_text_limit_exceeded",
    )


def _extract_file_text(filename: str, content: bytes) -> Dict[str, Any]:
    """Return structured extraction status for an uploaded requirement file."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext in ("txt", "md", "markdown", "log", "yaml", "yml", "toml", "ini", "env"):
        try:
            return _extraction(
                content.decode("utf-8"), complete=True, reason="ok",
            )
        except UnicodeDecodeError:
            return _extraction(
                content.decode("utf-8", errors="replace"),
                complete=False,
                reason="invalid_utf8",
            )

    if ext == "json":
        import json as _json
        try:
            obj = _json.loads(content)
            return _extraction(
                _json.dumps(obj, ensure_ascii=False, indent=2),
                complete=True,
                reason="ok",
            )
        except (UnicodeDecodeError, _json.JSONDecodeError, TypeError):
            return _extraction(
                complete=False, reason="invalid_json",
            )

    if ext == "csv":
        try:
            return _extraction(
                content.decode("utf-8"), complete=True, reason="ok",
            )
        except UnicodeDecodeError:
            return _extraction(
                complete=False, reason="invalid_utf8",
            )

    if ext == "pdf":
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                return _bounded_extraction(
                    "\n".join(
                        page.extract_text() or "" for page in pdf.pages
                    ),
                    reason="pdf_text_limit_exceeded",
                )
        except ImportError:
            return _extraction(
                complete=False, reason="pdf_parser_unavailable",
            )
        except Exception:
            return _extraction(
                complete=False, reason="pdf_parse_failed",
            )

    if ext == "docx":
        return _extract_docx_text(content)

    if ext == "doc":
        return _extraction(
            complete=False, reason="legacy_doc_unsupported",
        )

    # Unknown formats are not authoritative requirement sources.
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    return _extraction(
        text, complete=False, reason="unsupported_file_format",
    )

@router.post("/projects/{project_id}/upload-file")
async def upload_file_for_agent(
    project_id: str,
    agent_type: str = Form(...),   # "pm" 或 "supervisor"
    files: List[UploadFile] = File(...),
):
    """
    上传一个或多个文件供 PM Agent 或 Supervisor Agent 读取。
    
    - 支持格式：txt / md / json / csv / pdf / docx
    - 每个文件内容提取为纯文本，合并后返回
    - 单文件最大 10MB，所有文件合并内容最大 50000 字符
    
    返回：
    - files: 每个文件的解析结果列表
    - combined_text: 所有文件内容合并后的文本
    - total_chars: 合并后总字符数
    """
    _get_project(project_id)

    if agent_type not in ("pm", "supervisor"):
        raise HTTPException(
            status_code=400,
            detail="agent_type must be pm or supervisor",
        )
    max_size = 10 * 1024 * 1024
    results: List[Dict[str, Any]] = []
    combined_parts: List[str] = []
    for upload in files:
        raw = await upload.read()
        filename = upload.filename or "unknown"
        if len(raw) > max_size:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "attachment_too_large",
                    "filename": filename,
                    "complete": False,
                    "size": len(raw),
                    "max_size": max_size,
                    **({"canonical_requirements_accepted": False}
                       if agent_type == "pm" else {}),
                },
            )
        extracted = _extract_file_text_result(filename, raw)
        if extracted.get("complete") is not True:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "attachment_extraction_incomplete",
                    "filename": filename,
                    "complete": False,
                    "error": extracted.get("error") or "attachment extraction failed",
                    "format": extracted.get("format"),
                    **({"canonical_requirements_accepted": False}
                       if agent_type == "pm" else {}),
                },
            )
        text = str(extracted.get("text") or "")
        if len(text) > _UPLOAD_MAX_EXTRACTED_CHARS:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "attachment_text_too_large",
                    "filename": filename,
                    "complete": False,
                    "char_count": len(text),
                    "total_chars": len(text),
                    "max_chars": _UPLOAD_MAX_EXTRACTED_CHARS,
                    **({"canonical_requirements_accepted": False}
                       if agent_type == "pm" else {}),
                },
            )
        results.append({
            "filename": filename,
            "success": True,
            "complete": True,
            "size": len(raw),
            "char_count": len(text),
            "format": extracted.get("format"),
        })
        combined_parts.append(f"=== file: {filename} ===\n{text}")
    combined_text = "\n\n".join(combined_parts)
    if len(combined_text) > _UPLOAD_MAX_EXTRACTED_CHARS:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "combined_attachment_text_too_large",
                "complete": False,
                "char_count": len(combined_text),
                "total_chars": len(combined_text),
                "max_chars": _UPLOAD_MAX_EXTRACTED_CHARS,
                "files": results,
                **({"canonical_requirements_accepted": False}
                   if agent_type == "pm" else {}),
            },
        )
    return {
        "success": True,
        "complete": True,
        "files": results,
        "combined_text": combined_text,
        "total_chars": len(combined_text),
        "truncated": False,
        "canonical_requirements_accepted": agent_type == "pm",
        "agent_type": agent_type,
        "project_id": project_id,
        "message": (
            f"Parsed {len(results)}/{len(files)} complete attachments "
            f"({len(combined_text)} characters)"
        ),
    }


def _list_dir_recursive(
    base: Path,
    rel: Path = Path("."),
    visited: Optional[set[Path]] = None,
    allowed_paths: Optional[set[str]] = None,
) -> List[Dict]:
    """递归列出目录，返回树形结构"""
    root = base.resolve()
    target = root / rel
    if target.is_symlink():
        return []
    try:
        resolved = target.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return []
    seen = visited if visited is not None else set()
    if resolved in seen:
        return []
    seen.add(resolved)
    result: List[Dict[str, Any]] = []
    try:
        entries = sorted(
            resolved.iterdir(),
            key=lambda p: (p.is_file(), p.name.casefold()),
        )
    except (OSError, PermissionError):
        return result
    for entry in entries:
        if entry.is_symlink():
            continue
        entry_rel = rel / entry.name
        canonical = entry_rel.as_posix()
        if canonical.startswith("./"):
            canonical = canonical[2:]
        parts = tuple(part.casefold() for part in entry_rel.parts if part != ".")
        if (
            not canonical
            or (parts and parts[0] in _HIDDEN_WORKSPACE_ROOTS)
        ):
            continue
        if entry.is_dir():
            children = _list_dir_recursive(
                base, entry_rel, seen, allowed_paths,
            )
            if allowed_paths is not None and not children:
                continue
            result.append({
                "key": canonical,
                "title": entry.name,
                "type": "folder",
                "children": children,
            })
        else:
            if allowed_paths is not None and canonical not in allowed_paths:
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            result.append({
                "key": canonical,
                "title": entry.name,
                "type": "file",
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "isLeaf": True,
            })
    return result

@router.get("/projects/{project_id}/files")
async def list_project_files(project_id: str):
    """列出项目工作区所有文件（树形结构）"""
    ctx = _get_project(project_id)
    try:
        _, delivery_files = collect_delivery_artifact(
            ctx.workspace,
            required_paths=_required_delivery_paths(ctx),
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="project delivery files cannot be listed safely",
        ) from exc
    tree = _list_dir_recursive(
        ctx.workspace,
        allowed_paths=set(delivery_files),
    )
    return {
        "project_id": project_id,
        "workspace": ".",
        "workspace_rel": ".",
        "tree": tree,
    }

@router.get("/projects/{project_id}/files/read")
async def read_project_file(project_id: str, path: str):
    """读取项目文件内容（path 为相对于 workspace 的路径）"""
    ctx = _get_project(project_id)
    canonical, target = _canonical_safe_read_path(
        ctx, path, action="read",
    )
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在：{path}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="不是文件")
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取失败：{e}")
    return {"path": canonical, "content": content, "size": target.stat().st_size}

@router.post("/projects/{project_id}/files/write")
@_project_write_fenced_route
async def write_project_file(project_id: str, request: FileWriteRequest):
    """写入/创建项目文件"""
    ctx = _get_project(project_id)
    canonical, target = _canonical_delivery_path(
        ctx.workspace,
        request.path,
        action="写入",
        required_paths=_required_delivery_paths(ctx),
    )
    _atomic_replace_bytes(target, request.content.encode("utf-8"))
    _invalidate_qa_after_workspace_mutation(
        ctx,
        reason=f"files.write:{canonical}",
        changed_paths=[canonical],
    )
    await _persist_all_async()
    return {"success": True, "path": canonical, "size": len(request.content)}

@router.delete("/projects/{project_id}/files/delete")
@_project_write_fenced_route
async def delete_project_file(project_id: str, path: str):
    """删除项目文件"""
    ctx = _get_project(project_id)
    canonical, target = _canonical_delivery_path(
        ctx.workspace,
        path,
        action="删除",
        required_paths=_required_delivery_paths(ctx),
    )
    if not target.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    changed_paths = [canonical]
    if target.is_dir():
        # 安全限制：目录内文件数不超过 500，防止误删大型目录
        all_files = list(target.rglob("*"))
        if len(all_files) > 500:
            raise HTTPException(
                status_code=400,
                detail=f"目录内容过多（{len(all_files)} 项），请逐个删除文件"
            )
        changed_paths = [
            item.relative_to(ctx.workspace.resolve()).as_posix()
            for item in all_files
            if item.is_file() and not item.is_symlink()
        ]
        shutil.rmtree(target)
    else:
        target.unlink()
    _invalidate_qa_after_workspace_mutation(
        ctx,
        reason=f"files.delete:{canonical}",
        changed_paths=changed_paths,
    )
    await _persist_all_async()
    return {"success": True, "path": canonical}

@router.get("/projects/{project_id}/files/download")
async def download_project_file(project_id: str, path: str):
    """下载项目文件"""
    ctx = _get_project(project_id)
    _, target = _canonical_safe_read_path(
        ctx, path, action="download",
    )
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(str(target), filename=target.name)

@router.get("/projects/{project_id}/archive/download")
@_project_write_fenced_route
async def download_project_archive(project_id: str):
    """Download the whole project workspace as a zip archive."""
    ctx = _get_project(project_id)
    workspace = ctx.workspace.resolve()
    if not workspace.exists() or not workspace.is_dir():
        raise HTTPException(status_code=404, detail="project workspace not found")
    persisted_whole = (ctx.qc_results or {}).get("__whole_project__", {})
    persisted_qa = (
        persisted_whole.get("qa", persisted_whole)
        if isinstance(persisted_whole, dict) else {}
    )
    expected_manifest = (
        persisted_qa.get("delivery_manifest")
        if isinstance(persisted_qa, dict) else {}
    ) or {}
    current_manifest, artifact_files = collect_delivery_artifact(
        workspace,
        required_paths=(
            expected_manifest.get("required_paths")
            or _required_delivery_paths(ctx)
        ),
    )
    gate_error = release_gate_error(
        ctx.qc_results,
        workspace,
        current_manifest=current_manifest,
    )
    if gate_error:
        raise HTTPException(status_code=409, detail=gate_error)

    archive_names = sorted(artifact_files)

    conflicts = _portable_path_conflicts(archive_names)
    if conflicts:
        rendered = ", ".join(f"{left} <-> {right}" for left, right in conflicts[:20])
        raise HTTPException(
            status_code=409,
            detail=f"project contains case-insensitive path conflicts: {rendered}",
        )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for archive_name in archive_names:
            zf.writestr(archive_name, artifact_files[archive_name])

        manifest = {
            "project_id": project_id,
            "project_name": ctx.name,
            "created_at": time.time(),
            "file_count": current_manifest["file_count"],
            "artifact_sha256": current_manifest["artifact_sha256"],
            "artifact_manifest_rule_version": current_manifest["rule_version"],
        }
        zf.writestr("archive_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    buffer.seek(0)
    ascii_stem = _safe_archive_name(ctx.name).encode("ascii", "ignore").decode("ascii").strip("._")
    ascii_filename = f"{ascii_stem or 'project'}_{project_id}.zip"
    utf8_filename = f"{ctx.name}_{project_id}.zip"
    quoted_filename = urllib.parse.quote(utf8_filename, safe="")
    headers = {
        "Content-Disposition": (
            f'attachment; filename="{ascii_filename}"; '
            f"filename*=UTF-8''{quoted_filename}"
        )
    }
    return StreamingResponse(buffer, media_type="application/zip", headers=headers)


@router.post("/projects/{project_id}/files/stage")
@_project_write_fenced_route
async def stage_file(project_id: str, request: FileStagingRequest):
    """
    将文件写入暂存区（不覆盖工作区）
    
    工作流：stage → commit → 工作区更新
    暂存区路径：ctx.workspace/.project/staging/<file_path>
    """
    ctx = _get_project(project_id)
    canonical_path, _ = _canonical_delivery_path(
        ctx.workspace,
        request.path,
        action="暂存",
        required_paths=_required_delivery_paths(ctx),
    )

    # 直接操作 ctx.workspace（不经过 PGAgent 的 project_id 子目录）
    staging_root = (ctx.workspace / ".project" / "staging").resolve()
    staging_path = (staging_root / canonical_path).resolve()
    try:
        staging_path.relative_to(staging_root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="暂存路径越界") from exc
    old_stage = staging_path.read_bytes() if staging_path.is_file() else None
    index_file = ctx.workspace / ".project" / "staging_index.json"

    # 更新暂存区索引
    index_file = ctx.workspace / ".project" / "staging_index.json"
    index: Dict = {}
    if index_file.exists():
        try:
            index = json.loads(index_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(
                status_code=409,
                detail="staging index is corrupt; refusing to discard it",
            ) from exc
    if not isinstance(index, dict):
        raise HTTPException(status_code=409, detail="staging index is invalid")
    index[canonical_path] = {"staged_at": time.time(), "size": len(request.content)}
    old_index = index_file.read_bytes() if index_file.is_file() else None
    try:
        _atomic_replace_bytes(staging_path, request.content.encode("utf-8"))
        _atomic_replace_bytes(
            index_file,
            json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    except Exception:
        if old_stage is None:
            staging_path.unlink(missing_ok=True)
        else:
            _atomic_replace_bytes(staging_path, old_stage)
        if old_index is None:
            index_file.unlink(missing_ok=True)
        else:
            _atomic_replace_bytes(index_file, old_index)
        raise

    return {
        "success": True,
        "staged": canonical_path,
        "staging_path": str(staging_path.relative_to(ctx.workspace)).replace("\\", "/"),
    }

@router.post("/projects/{project_id}/files/commit")
@_project_write_fenced_route
async def commit_staged(project_id: str, request: FileCommitRequest):
    """
    提交暂存区：将暂存区所有文件覆盖到工作区，并生成版本快照
    """
    import hashlib as _hashlib
    ctx = _get_project(project_id)
    staging_dir = (ctx.workspace / ".project" / "staging").resolve()
    index_file = ctx.workspace / ".project" / "staging_index.json"

    if not index_file.exists():
        return {"success": False, "error": "暂存区为空，没有可提交的内容"}
    try:
        index = json.loads(index_file.read_text(encoding="utf-8"))
    except Exception:
        return {"success": False, "error": "暂存区索引损坏"}
    if not index:
        return {"success": False, "error": "暂存区为空"}

    canonical_paths: List[str] = []
    staged_sources: Dict[str, Path] = {}
    for raw_path in index:
        canonical, _ = _canonical_delivery_path(
            ctx.workspace,
            raw_path,
            action="提交",
            required_paths=_required_delivery_paths(ctx),
        )
        if canonical != str(raw_path).replace("\\", "/"):
            raise HTTPException(status_code=409, detail="暂存索引包含非规范路径")
        canonical_paths.append(canonical)
        staged_sources[canonical] = _resolve_staging_source(staging_dir, canonical)
    conflicts = _portable_path_conflicts(canonical_paths)
    if conflicts:
        raise HTTPException(status_code=409, detail="暂存索引包含大小写路径冲突")

    # 生成 commit_id 和版本号
    commit_id = _hashlib.sha256(f"{project_id}:{time.time()}".encode()).hexdigest()[:12]
    versions_dir = ctx.workspace / ".project" / "versions"
    existing = [int(d.name) for d in versions_dir.iterdir() if d.is_dir() and d.name.isdigit()] if versions_dir.exists() else []
    version = max(existing, default=0) + 1

    snapshot_dir = versions_dir / str(version)
    recovery_id = uuid.uuid4().hex
    transaction_dir = ctx.workspace / ".project" / "transactions" / recovery_id
    backup_dir = transaction_dir / "before"
    prepared_dir = transaction_dir / "prepared"
    committed: List[str] = []
    existed_before: Dict[str, bool] = {}
    cleanup_transaction = True
    try:
        for rel_path in canonical_paths:
            _, destination = _canonical_delivery_path(
                ctx.workspace,
                rel_path,
                action="提交",
                required_paths=_required_delivery_paths(ctx),
            )
            prepared = prepared_dir / rel_path
            prepared.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged_sources[rel_path], prepared)
            existed_before[rel_path] = destination.exists()
            if destination.exists():
                if not destination.is_file():
                    raise HTTPException(
                        status_code=409,
                        detail=f"目标不是普通文件：{rel_path}",
                    )
                backup = backup_dir / rel_path
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup)

        for rel_path in canonical_paths:
            _, destination = _canonical_delivery_path(
                ctx.workspace,
                rel_path,
                action="提交",
                required_paths=_required_delivery_paths(ctx),
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(prepared_dir / rel_path, destination)
            committed.append(rel_path)

        delivery_manifest, delivery_files = collect_delivery_artifact(
            ctx.workspace,
            required_paths=_required_delivery_paths(ctx),
        )
        snapshot_dir.mkdir(parents=True, exist_ok=False)
        for rel_path, content in delivery_files.items():
            snap = snapshot_dir / rel_path
            snap.parent.mkdir(parents=True, exist_ok=True)
            snap.write_bytes(content)
        meta = {
            "commit_id": commit_id,
            "version": version,
            "message": request.message,
            "committed_at": time.time(),
            "files": committed,
            "full_snapshot": True,
            "delivery_manifest": delivery_manifest,
        }
        (snapshot_dir / "commit.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as commit_error:
        try:
            for rel_path in reversed(committed):
                _, destination = _canonical_delivery_path(
                    ctx.workspace,
                    rel_path,
                    action="恢复",
                    required_paths=_required_delivery_paths(ctx),
                )
                backup = backup_dir / rel_path
                if existed_before.get(rel_path) and backup.exists():
                    restore = transaction_dir / "restore" / rel_path
                    restore.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, restore)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(restore, destination)
                elif not existed_before.get(rel_path):
                    destination.unlink(missing_ok=True)
            shutil.rmtree(snapshot_dir, ignore_errors=True)
        except Exception as compensation_error:
            cleanup_transaction = False
            try:
                _atomic_replace_bytes(
                    transaction_dir / "recovery.json",
                    json.dumps(
                        {
                            "status": "manual_recovery_required",
                            "recovery_id": recovery_id,
                            "commit_error": type(commit_error).__name__,
                            "compensation_error": type(compensation_error).__name__,
                            "committed_paths": committed,
                        },
                        sort_keys=True,
                        indent=2,
                    ).encode("utf-8"),
                )
            except Exception:
                pass
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "commit_compensation_failed",
                    "recovery_id": recovery_id,
                },
            ) from compensation_error
        raise
    finally:
        if cleanup_transaction:
            shutil.rmtree(transaction_dir, ignore_errors=True)

    # Clear staging only after the complete workspace+snapshot transaction.
    shutil.rmtree(staging_dir, ignore_errors=True)
    index_file.unlink(missing_ok=True)
    _invalidate_qa_after_workspace_mutation(
        ctx,
        reason=f"files.commit:{commit_id}",
        changed_paths=committed,
    )
    await _persist_all_async()

    return {"success": True, "commit_id": commit_id, "version": version,
            "committed_files": committed, "message": request.message}

@router.get("/projects/{project_id}/files/versions")
async def list_file_versions(project_id: str):
    """列出所有提交版本（版本历史）"""
    ctx = _get_project(project_id)
    versions_dir = ctx.workspace / ".project" / "versions"
    if not versions_dir.exists():
        return {"project_id": project_id, "versions": [], "count": 0}
    result = []
    for vdir in sorted(versions_dir.iterdir(), key=lambda d: d.name):
        if not vdir.is_dir():
            continue
        meta_file = vdir / "commit.json"
        if meta_file.exists():
            try:
                result.append(json.loads(meta_file.read_text(encoding="utf-8")))
            except Exception:
                pass
    return {"project_id": project_id, "versions": result, "count": len(result)}

@router.post("/projects/{project_id}/files/rollback")
@_project_write_fenced_route
async def rollback_file_version(project_id: str, request: FileRollbackRequest):
    """
    回滚到指定版本（将版本快照覆盖回工作区）
    """
    ctx = _get_project(project_id)
    versions_dir = ctx.workspace / ".project" / "versions" / str(request.version)
    if not versions_dir.exists():
        raise HTTPException(status_code=404, detail=f"版本 {request.version} 不存在")
    meta_path = versions_dir / "commit.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=409, detail="版本快照元数据损坏") from exc
    if meta.get("full_snapshot") is not True:
        raise HTTPException(
            status_code=409,
            detail="旧版本仅包含增量文件，不能声明或执行完整工作区回滚",
        )

    desired: Dict[str, bytes] = {}
    for source in versions_dir.rglob("*"):
        if not source.is_file() or source == meta_path:
            continue
        relative = str(source.relative_to(versions_dir)).replace("\\", "/")
        canonical, _ = _canonical_delivery_path(
            ctx.workspace,
            relative,
            action="回滚",
            required_paths=(
                (meta.get("delivery_manifest") or {}).get("required_paths")
                or _required_delivery_paths(ctx)
            ),
        )
        desired[canonical] = source.read_bytes()
    expected_manifest = meta.get("delivery_manifest") or {}
    snapshot_payload = {
        "rule_version": expected_manifest.get("rule_version"),
        "required_paths": expected_manifest.get("required_paths") or [],
        "files": [
            {
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
            for path, content in sorted(desired.items())
        ],
    }
    snapshot_digest = hashlib.sha256(json.dumps(
        snapshot_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")).hexdigest()
    if snapshot_digest != expected_manifest.get("artifact_sha256"):
        raise HTTPException(status_code=409, detail="版本快照字节与清单摘要不一致")

    required_paths = (
        (meta.get("delivery_manifest") or {}).get("required_paths")
        or _required_delivery_paths(ctx)
    )
    _, before = collect_delivery_artifact(
        ctx.workspace, required_paths=required_paths,
    )
    recovery_id = uuid.uuid4().hex
    transaction_dir = ctx.workspace / ".project" / "transactions" / recovery_id
    prepared_root = transaction_dir / "prepared"
    before_root = transaction_dir / "before"
    cleanup_transaction = True
    try:
        for relative, content in before.items():
            backup = before_root / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(content)
        _atomic_replace_bytes(
            transaction_dir / "transaction.json",
            json.dumps(
                {
                    "operation": "rollback",
                    "target_version": request.version,
                    "required_paths": required_paths,
                    "before_paths": sorted(before),
                    "desired_paths": sorted(desired),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ).encode("utf-8"),
        )
        for relative, content in desired.items():
            prepared = prepared_root / relative
            prepared.parent.mkdir(parents=True, exist_ok=True)
            prepared.write_bytes(content)
        for relative in sorted(set(before) - set(desired), reverse=True):
            _, target = _canonical_delivery_path(
                ctx.workspace,
                relative,
                action="回滚",
                required_paths=required_paths,
            )
            target.unlink(missing_ok=True)
        for relative in sorted(desired):
            _, target = _canonical_delivery_path(
                ctx.workspace,
                relative,
                action="回滚",
                required_paths=required_paths,
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(prepared_root / relative, target)
        restored_manifest = collect_delivery_artifact(
            ctx.workspace,
            required_paths=required_paths,
        )[0]
        if (
            restored_manifest.get("artifact_sha256")
            != expected_manifest.get("artifact_sha256")
        ):
            raise RuntimeError(
                "restored workspace does not match the target delivery manifest"
            )
    except Exception as rollback_error:
        try:
            _, partial = collect_delivery_artifact(
                ctx.workspace, required_paths=required_paths,
            )
            for relative in sorted(set(partial) - set(before), reverse=True):
                _, target = _canonical_delivery_path(
                    ctx.workspace,
                    relative,
                    action="恢复",
                    required_paths=required_paths,
                )
                target.unlink(missing_ok=True)
            for relative, content in before.items():
                _, target = _canonical_delivery_path(
                    ctx.workspace,
                    relative,
                    action="恢复",
                    required_paths=required_paths,
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                restore = transaction_dir / "restore" / relative
                restore.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(before_root / relative, restore)
                os.replace(restore, target)
            compensated_manifest = collect_delivery_artifact(
                ctx.workspace,
                required_paths=required_paths,
            )[0]
            before_payload = {
                "rule_version": compensated_manifest.get("rule_version"),
                "required_paths": compensated_manifest.get("required_paths") or [],
                "files": [
                    {
                        "path": path,
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                    }
                    for path, content in sorted(before.items())
                ],
            }
            before_digest = hashlib.sha256(
                json.dumps(
                    before_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            if compensated_manifest.get("artifact_sha256") != before_digest:
                raise RuntimeError("rollback compensation manifest mismatch")
        except Exception as compensation_error:
            cleanup_transaction = False
            try:
                _atomic_replace_bytes(
                    transaction_dir / "recovery.json",
                    json.dumps(
                        {
                            "status": "manual_recovery_required",
                            "recovery_id": recovery_id,
                            "rollback_error": type(rollback_error).__name__,
                            "compensation_error": type(compensation_error).__name__,
                        },
                        sort_keys=True,
                        indent=2,
                    ).encode("utf-8"),
                )
            except Exception:
                pass
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "rollback_compensation_failed",
                    "recovery_id": recovery_id,
                },
            ) from compensation_error
        raise
    finally:
        if cleanup_transaction:
            shutil.rmtree(transaction_dir, ignore_errors=True)
    rolled_back = sorted(desired)
    _invalidate_qa_after_workspace_mutation(
        ctx,
        reason=f"files.rollback:version-{request.version}",
        changed_paths=sorted(set(before) | set(desired)),
    )
    await _persist_all_async()
    return {
        "success": True,
        "version": request.version,
        "rolled_back_files": rolled_back,
        "message": f"已回滚到版本 {request.version}，共恢复 {len(rolled_back)} 个文件",
    }

@router.get("/projects/{project_id}/files/staging")
async def get_staging_index(project_id: str):
    """获取当前暂存区文件列表"""
    ctx = _get_project(project_id)
    index_file = ctx.workspace / ".project" / "staging_index.json"
    if not index_file.exists():
        return {"project_id": project_id, "staged_files": {}, "count": 0}
    try:
        index = json.loads(index_file.read_text(encoding="utf-8"))
    except Exception:
        index = {}
    return {"project_id": project_id, "staged_files": index, "count": len(index)}

