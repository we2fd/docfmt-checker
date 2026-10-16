"""领域异常：统一携带 HTTP 状态码与机器可读的错误码。

设计原则（答辩可讲）：错误必须「可操作」——不只告诉用户失败了，
还要给出下一步该做什么（hint 字段），避免出现「只看到失败、不知道怎么办」。
"""

from __future__ import annotations

from typing import Any


class DocFmtError(Exception):
    code = "docfmt_error"
    http_status = 400
    message = "请求处理失败"

    def __init__(self, message: str | None = None, *, hint: str | None = None, detail: Any = None) -> None:
        self.message = message or self.message
        self.hint = hint
        self.detail = detail
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


class UnsupportedFileType(DocFmtError):
    code = "unsupported_file_type"
    http_status = 400
    message = "不支持的文件类型"


class FileTooLarge(DocFmtError):
    code = "file_too_large"
    http_status = 413
    message = "文件超过大小限制"


class CorruptedDocument(DocFmtError):
    code = "corrupted_document"
    http_status = 400
    message = "文档无法解析"


class NotFound(DocFmtError):
    code = "not_found"
    http_status = 404
    message = "资源不存在"


class RulesetInvalid(DocFmtError):
    code = "ruleset_invalid"
    http_status = 422
    message = "规则集不合法"
