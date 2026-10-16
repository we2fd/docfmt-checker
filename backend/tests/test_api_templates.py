"""接口层测试：阈值快速调整 + 参考模板推导。

这两条链路合起来回答的是同一个问题——「这套规则的阈值凭什么、能不能换成我们学校的」：
    /rules/{id}/quick         直接在界面上改数字
    /templates/profile|apply   上传学校模板，反推出阈值再写回
"""

from __future__ import annotations

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _template_bytes(factory, name: str = "学校模板.docx") -> bytes:
    """一份「格式和内置规则不一样」的模板：正文四号（14 磅）、页边距 4 厘米。"""
    doc = factory.new(margins=(4.0, 4.0, 4.0, 4.0))
    factory.paragraph(doc, "毕业论文题目", east="黑体", size=22, indent=None, line=None)
    factory.heading(doc, "摘要", level=1)
    factory.paragraph(doc, "这是摘要正文，用于让模板有可解析的段落。" * 12, size=14)
    factory.paragraph(doc, "关键词：格式检查；规则引擎；模板推导", size=14)
    factory.heading(doc, "第一章 绪论", level=1)
    # 必须有真正的正文段落（不属于摘要 / 关键词 / 参考文献区），否则正文字号无从测量
    for index in range(4):
        factory.paragraph(doc, f"第 {index} 段正文，本模板统一使用四号字。" * 4, size=14)
    factory.heading(doc, "参考文献", level=1)
    factory.paragraph(doc, "[1] 王明. 排版规范化研究[J]. 计算机学报, 2021: 1-10.", size=10.5)
    path = factory.save(doc, name)
    return path.read_bytes()


def _rules(client, ruleset_id: str = "default") -> dict:
    return {item["id"]: item for item in client.get(f"/api/v1/rules/{ruleset_id}").json()["rules"]}


# ---------------------------------------------------------------- 阈值快速调整


def test_quick_groups_endpoint_lists_editable_thresholds(client):
    payload = client.get("/api/v1/rules/default/quick").json()
    assert payload["groups"], "快速调整页必须至少有一组可改的阈值"

    flat = {item["rule_id"]: item for group in payload["groups"] for item in group["items"]}
    size = flat["layout.body.size"]
    assert size["kind"] == "number"
    assert size["unit"] == "磅"
    assert size["value"] == 12
    assert size["limits"] == [5.0, 100.0]
    # 界面要能显示「这一项属于正文段落」，否则用户不知道改的是谁
    assert size["scope_label"]


def test_quick_update_saves_and_takes_effect(client):
    response = client.put(
        "/api/v1/rules/default/quick",
        json={
            "updates": [
                {"rule_id": "layout.body.size", "params": {"expected": 13.5, "tol": 0.5}},
                {"rule_id": "layout.margin.left", "params": {"expected": 3.5}},
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["applied"] == 2
    assert response.json()["saved"] is True

    rules = _rules(client)
    assert rules["layout.body.size"]["params"]["expected"] == 13.5
    assert rules["layout.margin.left"]["params"]["expected"] == 3.5
    # 只改阈值，不该顺手把 checker / scope 改掉
    assert rules["layout.body.size"]["params"]["scope"] == "body"
    assert rules["layout.body.size"]["checker"] == "prop_equals"

    client.post("/api/v1/rules/default/reset", json={})
    assert _rules(client)["layout.body.size"]["params"]["expected"] == 12


def test_quick_update_reports_per_item_problems(client):
    """一条越界不能让整批白改——能改的要改成功，不能改的要说明原因。"""
    response = client.put(
        "/api/v1/rules/default/quick",
        json={
            "updates": [
                {"rule_id": "layout.body.size", "params": {"expected": 13}},
                {"rule_id": "layout.body.size", "params": {"expected": 9999}},
                {"rule_id": "layout.body.font", "params": {"allowed": []}},
            ]
        },
    )
    body = response.json()
    assert body["applied"] == 1
    assert body["rejected"] == 2
    assert "超出合理范围" in body["problems"][0]["message"]
    assert "不能为空" in body["problems"][1]["message"]
    assert body["problems"][0]["hint"], "失败原因必须附带可操作的做法"
    client.post("/api/v1/rules/default/reset", json={})


def test_quick_update_does_not_mutate_meta(client):
    """只改阈值时，规则集名称与说明不应被顺手改写。"""
    before = client.get("/api/v1/rules/default").json()
    client.put(
        "/api/v1/rules/default/quick",
        json={"updates": [{"rule_id": "layout.body.size", "params": {"expected": 13}}]},
    )
    after = client.get("/api/v1/rules/default").json()
    assert after["name"] == before["name"]
    assert after["description"] == before["description"]
    client.post("/api/v1/rules/default/reset", json={})


def test_patch_single_rule_params(client):
    ok = client.patch("/api/v1/rules/default/rules/layout.body.line_spacing/params", json={"params": {"min": 1.4, "max": 1.8}})
    assert ok.status_code == 200
    assert ok.json()["detail"]["after"] == {"min": 1.4, "max": 1.8}

    min_max = client.patch(
        "/api/v1/rules/default/rules/layout.body.line_spacing/params", json={"params": {"min": 2.0, "max": 1.0}}
    )
    assert min_max.status_code == 422
    assert "不能大于" in min_max.json()["message"]
    client.post("/api/v1/rules/default/reset", json={})


def test_patch_rejects_non_threshold_and_unknown_rule(client):
    not_allowed = client.patch(
        "/api/v1/rules/default/rules/layout.body.size/params", json={"params": {"scope": "heading"}}
    )
    assert not_allowed.status_code == 422
    assert "不支持" in not_allowed.json()["message"]
    assert "YAML" in not_allowed.json()["hint"]

    missing = client.patch("/api/v1/rules/default/rules/nope/params", json={"params": {"expected": 1}})
    assert missing.status_code == 404


# ---------------------------------------------------------------- 参考模板


def test_template_profile_returns_proposals_without_touching_rules(client, factory):
    before = _rules(client)["layout.body.size"]["params"]["expected"]

    response = client.post(
        "/api/v1/templates/profile",
        files={"file": ("学校模板.docx", _template_bytes(factory), DOCX_MIME)},
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["filename"] == "学校模板.docx"
    assert payload["template_id"]
    assert payload["counts"]["total"] == 56

    proposals = {item["rule_id"]: item for item in payload["proposals"]}
    size = proposals["layout.body.size"]
    assert size["status"] == "proposed"
    assert size["template_value"] == 14
    assert size["proposal"]["expected"] == 14
    assert size["proposal"]["tol"] == 0.5
    assert size["confidence"] == 1.0

    margin = proposals["layout.margin.left"]
    assert margin["proposal"]["expected"] == 4.0
    assert margin["scope_label"] == "页面设置（全部节）"

    # 「结构类」规则应被明确标为不可推导，而不是留个空白让人猜
    assert proposals["structure.heading.no_skip"]["status"] == "not_applicable"
    # 「一致性」规则没有阈值，但要量出参考值
    assert proposals["consistency.body.font"]["status"] == "reference"

    # 推导阶段绝不能改动规则
    assert _rules(client)["layout.body.size"]["params"]["expected"] == before


def test_template_apply_writes_selected_proposals_only(client, factory):
    profile = client.post(
        "/api/v1/templates/profile",
        files={"file": ("学校模板.docx", _template_bytes(factory), DOCX_MIME)},
    ).json()
    proposals = {item["rule_id"]: item for item in profile["proposals"]}

    response = client.post(
        "/api/v1/templates/apply",
        json={
            "ruleset_id": "default",
            "source_name": profile["filename"],
            "updates": [
                {"rule_id": "layout.body.size", "params": proposals["layout.body.size"]["proposal"]},
                {"rule_id": "layout.margin.left", "params": proposals["layout.margin.left"]["proposal"]},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["applied"] == 2

    rules = _rules(client)
    assert rules["layout.body.size"]["params"]["expected"] == 14
    assert rules["layout.margin.left"]["params"]["expected"] == 4.0
    # 没勾选的规则必须原样保留
    assert rules["layout.margin.right"]["params"]["expected"] == 2.5

    detail = client.get("/api/v1/rules/default").json()
    assert "学校模板.docx" in detail["description"], "规则集说明里要留下这套阈值的出处"

    # 改完仍然是合法规则集：直接拿它跑一遍检查，确保没有把结构写坏
    doc_id = client.post(
        "/api/v1/documents",
        files={"file": ("paper.docx", _template_bytes(factory, "paper.docx"), DOCX_MIME)},
    ).json()["id"]
    report = client.post(f"/api/v1/documents/{doc_id}/check", json={}).json()
    assert len(report["rule_results"]) == 56

    client.post("/api/v1/rules/default/reset", json={})
    assert _rules(client)["layout.body.size"]["params"]["expected"] == 12


def test_template_apply_rejects_out_of_range_without_partial_damage(client):
    response = client.post(
        "/api/v1/templates/apply",
        json={
            "updates": [
                {"rule_id": "layout.body.size", "params": {"expected": 13}},
                {"rule_id": "layout.body.size", "params": {"expected": 0.1}},
            ]
        },
    )
    body = response.json()
    assert body["applied"] == 1 and body["rejected"] == 1
    assert "超出合理范围" in body["problems"][0]["message"]
    client.post("/api/v1/rules/default/reset", json={})


def test_template_rejects_non_docx(client):
    response = client.post(
        "/api/v1/templates/profile",
        files={"file": ("模板.doc", b"\xd0\xcf\x11\xe0", "application/msword")},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "unsupported_file_type"
    assert "另存为" in response.json()["hint"]


def test_template_rejects_corrupted_docx(client):
    response = client.post(
        "/api/v1/templates/profile",
        files={"file": ("坏模板.docx", b"this is not a zip", DOCX_MIME)},
    )
    assert response.status_code == 400


def test_template_list_derive_and_delete(client, factory):
    profile = client.post(
        "/api/v1/templates/profile",
        files={"file": ("学校模板.docx", _template_bytes(factory), DOCX_MIME)},
    ).json()
    template_id = profile["template_id"]

    listing = client.get("/api/v1/templates").json()
    assert listing["total"] == 1
    assert listing["items"][0]["template_id"] == template_id
    assert listing["items"][0]["filename"] == "学校模板.docx"

    # 用户改过规则之后可以重新推导，不必再传一次
    again = client.post(f"/api/v1/templates/{template_id}/derive", params={"ruleset_id": "default"})
    assert again.status_code == 200
    assert again.json()["counts"]["total"] == 56

    assert client.delete(f"/api/v1/templates/{template_id}").json()["deleted"] is True
    assert client.post(f"/api/v1/templates/{template_id}/derive").status_code == 404
    assert client.get("/api/v1/templates").json()["total"] == 0


def test_template_id_traversal_is_rejected(client):
    """template_id 会拼进文件路径，必须挡住路径穿越。"""
    response = client.post("/api/v1/templates/..%2F..%2Fetc/derive")
    assert response.status_code in (404, 405, 422)
    assert client.delete("/api/v1/templates/not-a-valid-id").status_code == 404


def test_template_is_not_registered_as_document(client, factory):
    """模板是「标准」不是「待检查的稿子」，不应混进文档列表。"""
    client.post(
        "/api/v1/templates/profile",
        files={"file": ("学校模板.docx", _template_bytes(factory), DOCX_MIME)},
    )
    assert client.get("/api/v1/documents").json()["total"] == 0
