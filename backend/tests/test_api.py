"""接口层测试（走 TestClient，覆盖正常流程与各类错误分支）。

`client` 与 `factory` 装置在 conftest.py 里定义，供本文件与 test_api_templates.py 共用。
"""

from __future__ import annotations


def _docx_bytes(factory, name: str = "paper.docx") -> bytes:
    doc = factory.new()
    factory.paragraph(doc, "文档题目", east="黑体", size=22, indent=None, line=None)
    factory.heading(doc, "摘要", level=1)
    factory.paragraph(doc, "这是摘要正文，用于接口测试。" * 12)
    factory.paragraph(doc, "关键词：格式检查；规则引擎；文档解析")
    factory.heading(doc, "参考文献", level=1)
    factory.paragraph(doc, "[1] 王明. 排版规范化[J]. 期刊, 2021: 1-10.")
    path = factory.save(doc, name)
    return path.read_bytes()


def _upload(client, factory, name: str = "paper.docx") -> str:
    response = client.post(
        "/api/v1/documents",
        files={"file": (name, _docx_bytes(factory, name), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


# ---------------------------------------------------------------- 系统接口


def test_health_and_config(client):
    health = client.get("/api/v1/health").json()
    assert health["status"] == "ok"

    config = client.get("/api/v1/config").json()
    assert config["ruleset"]["rule_count"] == 56
    assert config["max_upload_mb"] > 0
    assert ".doc" in config["known_unsupported"]
    assert len(config["fix_actions"]) == 4
    assert config["honesty"]["unknown_policy"]


def test_stats_starts_empty(client):
    stats = client.get("/api/v1/stats").json()
    assert stats["documents"] == 0
    assert stats["total_errors"] == 0
    assert stats["top_issues"] == []


# ---------------------------------------------------------------- 上传与检查


def test_upload_parse_and_list(client, factory):
    doc_id = _upload(client, factory)
    detail = client.get(f"/api/v1/documents/{doc_id}").json()
    assert detail["status"] == "parsed"
    assert detail["paragraph_count"] > 0
    assert "stored_path" not in detail

    listing = client.get("/api/v1/documents").json()
    assert listing["total"] == 1

    paragraphs = client.get(f"/api/v1/documents/{doc_id}/paragraphs").json()
    assert paragraphs["total"] == detail["paragraph_count"]
    assert paragraphs["items"][0]["kind"] == "title"

    outline = client.get(f"/api/v1/documents/{doc_id}/outline").json()
    assert any(item["text"] == "摘要" for item in outline["items"])


def test_upload_rejects_unsupported_extension(client):
    response = client.post("/api/v1/documents", files={"file": ("old.doc", b"\xd0\xcf\x11\xe0", "application/msword")})
    assert response.status_code == 400
    payload = response.json()
    assert payload["code"] == "unsupported_file_type"
    assert "另存为" in payload["hint"], "错误信息必须给出可操作的做法"


def test_upload_rejects_empty_file(client):
    response = client.post(
        "/api/v1/documents",
        files={"file": ("empty.docx", b"", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert response.status_code == 400


def test_upload_rejects_corrupted_docx(client):
    response = client.post(
        "/api/v1/documents",
        files={"file": ("broken.docx", b"not a zip at all", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert response.status_code == 400
    assert response.json()["code"] in ("corrupted_document", "unsupported_file_type")


def test_check_returns_report_and_persists(client, factory):
    doc_id = _upload(client, factory)
    report = client.post(f"/api/v1/documents/{doc_id}/check", json={}).json()

    assert 0 <= report["score"] <= 100
    assert report["document_id"] == doc_id
    assert len(report["category_scores"]) == 4
    assert len(report["rule_results"]) == 56
    assert report["elapsed_ms"] >= 0
    assert any("总分算法" in note for note in report["notes"])

    again = client.get(f"/api/v1/documents/{doc_id}/report").json()
    assert again["score"] == report["score"]

    stats = client.get("/api/v1/stats").json()
    assert stats["documents"] == 1
    assert stats["checked_documents"] == 1


def test_check_with_unknown_ruleset_returns_404(client, factory):
    doc_id = _upload(client, factory)
    response = client.post(f"/api/v1/documents/{doc_id}/check", json={"ruleset_id": "no-such-ruleset"})
    assert response.status_code == 404


def test_export_markdown_and_html(client, factory):
    doc_id = _upload(client, factory)
    client.post(f"/api/v1/documents/{doc_id}/check", json={})

    markdown = client.get(f"/api/v1/documents/{doc_id}/export", params={"kind": "markdown"})
    assert markdown.status_code == 200
    assert "格式体检报告与整改清单" in markdown.text
    assert "attachment" in markdown.headers["content-disposition"]

    html = client.get(f"/api/v1/documents/{doc_id}/export", params={"kind": "html"})
    assert html.status_code == 200
    assert "<!DOCTYPE html>" in html.text
    assert "分维度得分" in html.text


def test_fix_and_download(client, factory):
    doc_id = _upload(client, factory)
    result = client.post(f"/api/v1/documents/{doc_id}/fix", json={"actions": ["blank_lines", "punctuation"]}).json()
    assert result["fixed_id"]
    assert result["filename"].endswith(".docx")
    assert "原文件未被修改" in result["notice"]

    download = client.get(f"/api/v1/fixed/{result['fixed_id']}/download")
    assert download.status_code == 200
    assert download.content[:2] == b"PK", "修复产物应当是合法的 docx（zip）文件"


def test_delete_document(client, factory):
    doc_id = _upload(client, factory)
    assert client.delete(f"/api/v1/documents/{doc_id}").json()["deleted"] is True
    assert client.get(f"/api/v1/documents/{doc_id}").status_code == 404


def test_delete_succeeds_even_if_file_cleanup_fails(client, factory, monkeypatch):
    """磁盘文件清理失败时，「删除文档」仍必须成功。

    删除是用户可见的动作，不能因为残留文件删不掉（被占用、权限、
    或宿主安全策略拦截）就让整次请求报错。
    """
    from pathlib import Path

    doc_id = _upload(client, factory)

    def boom(self, *args, **kwargs):
        raise OSError("模拟：文件被占用或删除被拦截")

    monkeypatch.setattr(Path, "unlink", boom)

    response = client.delete(f"/api/v1/documents/{doc_id}")
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert client.get(f"/api/v1/documents/{doc_id}").status_code == 404
    assert all(item["id"] != doc_id for item in client.get("/api/v1/documents").json()["items"])


def test_unknown_document_returns_404_with_hint(client):
    response = client.get("/api/v1/documents/not-exists")
    assert response.status_code == 404
    assert response.json()["hint"]


def test_report_before_check_returns_actionable_404(client, factory):
    doc_id = _upload(client, factory)
    response = client.get(f"/api/v1/documents/{doc_id}/report")
    assert response.status_code == 404
    assert "开始检查" in response.json()["hint"]


# ---------------------------------------------------------------- 规则库


def test_ruleset_list_and_detail(client):
    listing = client.get("/api/v1/rules").json()
    assert listing["total"] >= 1
    assert listing["items"][0]["is_builtin"] is True

    detail = client.get("/api/v1/rules/default").json()
    assert detail["rule_count"] == 56
    assert detail["rules"][0]["suggestion"]


def test_ruleset_yaml_roundtrip(client):
    text = client.get("/api/v1/rules/default/yaml").text
    assert "meta:" in text
    saved = client.put("/api/v1/rules/default", json={"yaml_text": text})
    assert saved.status_code == 200
    assert saved.json()["saved"] is True


def test_ruleset_invalid_yaml_is_rejected_with_hint(client):
    response = client.put("/api/v1/rules/default", json={"yaml_text": "meta: {id: default}\nrules:\n  - id: a\n    name: 坏: 值\n"})
    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == "ruleset_invalid"
    assert "双引号" in payload["hint"]


def test_toggle_rule(client):
    response = client.post("/api/v1/rules/default/toggle", json={"rule_id": "layout.body.size", "enabled": False})
    assert response.status_code == 200
    rules = {item["id"]: item for item in response.json()["ruleset"]["rules"]}
    assert rules["layout.body.size"]["enabled"] is False

    # 停用后不再参与计分
    client.post("/api/v1/rules/default/reset", json={})
    detail = client.get("/api/v1/rules/default").json()
    rules = {item["id"]: item for item in detail["rules"]}
    assert rules["layout.body.size"]["enabled"] is True


def test_toggle_unknown_rule_returns_404(client):
    response = client.post("/api/v1/rules/default/toggle", json={"rule_id": "nope", "enabled": False})
    assert response.status_code == 404
