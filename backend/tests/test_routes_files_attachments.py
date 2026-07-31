import asyncio
import io
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from api import routes_files


def _docx_bytes(
    text: str,
    *,
    compression: int = zipfile.ZIP_STORED,
) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
        f"{text}"
        "</w:t></w:r></w:p></w:body></w:document>"
    ).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def test_docx_extraction_returns_structured_complete_result() -> None:
    result = routes_files._extract_file_text(
        "requirements.docx",
        _docx_bytes("登录与订单需求"),
    )

    assert result == {
        "text": "登录与订单需求",
        "complete": True,
        "reason": "ok",
    }


def test_pm_upload_rejects_docx_internal_text_truncation(
    monkeypatch,
) -> None:
    payload = _docx_bytes("x" * (routes_files._MAX_EXTRACTED_CHARS + 1))
    upload = UploadFile(
        io.BytesIO(payload),
        filename="long-requirements.docx",
    )
    monkeypatch.setattr(
        routes_files, "_get_project", lambda _project_id: SimpleNamespace(),
    )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_files.upload_file_for_agent(
            "attachment-project",
            agent_type="pm",
            files=[upload],
        ))

    assert caught.value.status_code == 413
    detail = caught.value.detail
    assert detail["code"] == "attachment_text_too_large"
    assert detail["complete"] is False
    assert detail["char_count"] == routes_files._MAX_EXTRACTED_CHARS + 1
    assert detail["max_chars"] == routes_files._MAX_EXTRACTED_CHARS


def test_pm_upload_rejects_docx_zip_bomb_before_xml_read(
    monkeypatch,
) -> None:
    payload = _docx_bytes(
        "A" * 500_000,
        compression=zipfile.ZIP_DEFLATED,
    )
    direct = routes_files._extract_file_text("bomb.docx", payload)
    assert direct["complete"] is False
    assert direct["reason"] == "docx_compression_ratio_exceeded"

    upload = UploadFile(io.BytesIO(payload), filename="bomb.docx")
    monkeypatch.setattr(
        routes_files, "_get_project", lambda _project_id: SimpleNamespace(),
    )
    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_files.upload_file_for_agent(
            "attachment-project",
            agent_type="pm",
            files=[upload],
        ))

    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "attachment_extraction_incomplete"
    assert caught.value.detail["complete"] is False
