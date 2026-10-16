"""文档类接口：上传、列表、预览、检查、保守修复、导出、删除。"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..errors import CorruptedDocument, FileTooLarge, NotFound, UnsupportedFileType
from ..services import DocFmtService

router = APIRouter(tags=["documents"])


def get_service(request: Request) -> DocFmtService:
    return request.app.state.service


class CheckRequest(BaseModel):
    ruleset_id: str | None = None


class FixRequest(BaseModel):
    actions: list[str] = Field(default_factory=lambda: ["blank_lines", "punctuation", "font_size", "caption_numbering"])


def _disposition(filename: str) -> str:
    ascii_fallback = "".join(ch if ord(ch) < 128 else "_" for ch in filename)
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"


@router.post("/documents", summary="上传 .docx 并解析")
async def upload(request: Request, file: UploadFile = File(...)) -> dict:
    service = get_service(request)
    settings = request.app.state.settings
    filename = file.filename or "document.docx"
    suffix = Path(filename).suffix.lower()

    if suffix in settings.known_unsupported:
        raise UnsupportedFileType(
            f"暂不支持 {suffix} 格式。",
            hint="请在 Word / WPS 中打开该文件，选择「另存为」→ 文件类型选「Word 文档 (*.docx)」，再上传转换后的文件。",
        )
    if suffix not in settings.allowed_extensions:
        raise UnsupportedFileType(
            f"不支持的文件类型：{suffix or '（无扩展名）'}。",
            hint=f"本系统目前只接受 {'、'.join(settings.allowed_extensions)} 格式。",
        )

    content = await file.read()
    if len(content) > settings.max_upload_bytes:
        raise FileTooLarge(
            f"文件 {len(content) / 1024 / 1024:.1f} MB，超过 {settings.max_upload_mb} MB 上限。",
            hint="请删除文档中无关的大图后重试，或调整 DOCFMT_MAX_UPLOAD_MB 环境变量。",
        )
    if not content:
        raise CorruptedDocument("上传的文件是空的。", hint="请确认选择的是完整的 .docx 文件。")

    record = service.upload(filename, content)
    record.pop("stored_path", None)
    record.pop("report", None)
    record["sha256"] = (record.get("sha256") or "")[:12]
    return record


@router.get("/samples", summary="内置演示样例列表")
def list_samples(request: Request) -> dict:
    items = get_service(request).list_samples()
    return {"total": len(items), "items": items}


@router.post("/samples/{name}/import", summary="一键导入演示样例")
def import_sample(request: Request, name: str) -> dict:
    record = get_service(request).import_sample(name)
    record.pop("stored_path", None)
    record.pop("report", None)
    record["sha256"] = (record.get("sha256") or "")[:12]
    return record


@router.get("/documents", summary="文档列表")
def list_documents(request: Request) -> dict:
    rows = get_service(request).list_documents()
    items = []
    for row in rows:
        items.append(
            {
                "id": row["id"],
                "filename": row["filename"],
                "status": row["status"],
                "uploaded_at": row["uploaded_at"],
                "size_bytes": row["size_bytes"],
                "paragraph_count": row["paragraph_count"],
                "char_count": row["char_count"],
                "heading_count": row["heading_count"],
                "score": row["score"],
                "issue_summary": row.get("issue_summary"),
                "error_message": row.get("error_message"),
            }
        )
    return {"total": len(items), "items": items}


@router.get("/documents/{doc_id}", summary="文档详情（含最近一次检查摘要）")
def document_detail(request: Request, doc_id: str) -> dict:
    row = get_service(request).get_document(doc_id)
    row.pop("stored_path", None)
    row.pop("report", None)
    row.pop("sha256", None)
    return row


@router.get("/documents/{doc_id}/paragraphs", summary="段落列表（左栏原文预览）")
def document_paragraphs(request: Request, doc_id: str, offset: int = 0, limit: int = 400) -> dict:
    return get_service(request).paragraphs(doc_id, offset=offset, limit=limit)


@router.get("/documents/{doc_id}/outline", summary="文档大纲")
def document_outline(request: Request, doc_id: str) -> dict:
    return {"items": get_service(request).outline(doc_id)}


@router.post("/documents/{doc_id}/check", summary="执行格式检查")
def run_check(request: Request, doc_id: str, payload: CheckRequest | None = None) -> dict:
    ruleset_id = payload.ruleset_id if payload else None
    report = get_service(request).check(doc_id, ruleset_id)
    return report


@router.get("/documents/{doc_id}/report", summary="读取最近一次检查报告")
def latest_report(request: Request, doc_id: str) -> dict:
    row = get_service(request).get_document(doc_id)
    if not row.get("report"):
        raise NotFound(
            "该文档还没有检查记录。",
            hint="请先点击「开始检查」。",
        )
    return row["report"]


@router.post("/documents/{doc_id}/fix", summary="保守修复并生成新文件")
def fix_document(request: Request, doc_id: str, payload: FixRequest | None = None) -> dict:
    actions = payload.actions if payload else None
    if not actions:
        actions = ["blank_lines", "punctuation", "font_size", "caption_numbering"]
    return get_service(request).fix(doc_id, actions)


@router.get("/documents/{doc_id}/export", summary="导出整改清单（markdown / html）")
def export_report(request: Request, doc_id: str, kind: str = "markdown") -> Response:
    payload = get_service(request).export(doc_id, kind=kind)
    return Response(
        content=payload["content"],
        media_type=payload["media_type"],
        headers={"Content-Disposition": _disposition(payload["filename"])},
    )


@router.get("/fixed/{fixed_id}/download", summary="下载修复后的文档")
def download_fixed(request: Request, fixed_id: str) -> Response:
    record = get_service(request).fixed_file(fixed_id)
    path = Path(record["stored_path"])
    if not path.exists():
        raise NotFound("修复产物文件已不在磁盘上。")
    return Response(
        content=path.read_bytes(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": _disposition(record["filename"])},
    )


@router.delete("/documents/{doc_id}", summary="删除文档及其记录")
def delete_document(request: Request, doc_id: str) -> dict:
    removed = get_service(request).delete_document(doc_id)
    return {"deleted": removed, "id": doc_id}
