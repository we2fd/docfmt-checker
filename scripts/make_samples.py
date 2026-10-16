"""生成演示用的样例文档（4 份）。

设计原则：样例不是随便生成几个 docx，而是**每一份都精确对应一组规则**，
这样「演示 → 出报告 → 逐条对照」形成闭环，答辩时现场可复现。

  01-规范样本.docx             应得高分（≥ 95），用来证明「工具不会乱报」
  02-排版问题样本.docx         页边距 / 字号 / 行距 / 缩进 / 页码
  03-结构问题样本.docx         标题跳级 / 图表编号断裂重复 / 图表未引用 / 章节缺失
  04-一致性与标点样本.docx     段落内字体混用 / 标题字号不统一 / 中英标点混用 / 参考文献要素缺失

用法：python scripts/make_samples.py [--out samples]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

SANS = "Times New Roman"

ABSTRACT = (
    "本文设计与实现了一套面向中文学术文档的格式自动检查与规范化系统。针对毕业论文排版中普遍存在的"
    "页边距不一致、字号混用、标题层级混乱与参考文献著录不规范等问题，系统以规则驱动为核心思想，"
    "把学校格式规范抽象为可配置的规则集，并构建了文档解析引擎与检查引擎两级结构。文档解析引擎基于"
    "开放文档格式读取段落、样式与节属性，通过直接格式、样式与文档默认值三级回退得到每个属性的生效值"
    "及其来源，从而避免只读直接格式造成的漏报；检查引擎在此基础上提供确定性检查、结构检查、一致性"
    "检查与内容规范检查四类共五十六条规则，输出带精确位置、实际值、期望值与整改建议的问题清单，"
    "并给出分维度量化评分。系统采用前后端分离架构，后端使用轻量级服务框架与嵌入式数据库，"
    "前端提供拖拽上传、问题定位与报告导出功能，整个系统无需联网即可运行。实验表明，"
    "系统对典型排版问题的识别较为准确，能够显著减少人工逐页校对的工作量。"
)

KEYWORDS = "关键词：文档格式检查；规则引擎；开放文档格式；排版自动化；质量评估"

SHORT_ABSTRACT = (
    "本文实现了一个文档格式检查系统，使用规则引擎对论文进行自动检查，能够发现常见的格式问题，"
    "提高排版效率，减少人工校对的时间成本。"
)

REFERENCES = [
    "[1] 王明. 学术论文排版规范化的实践与思考[J]. 中国科技期刊研究, 2021, 32(4): 512-517.",
    "[2] 李静, 张伟. 基于规则引擎的文档质量检测系统设计[J]. 计算机应用与软件, 2022, 39(7): 88-94.",
    "[3] 陈晓东. 面向中文科技文档的自动校对技术研究[D]. 北京: 清华大学, 2020.",
    "[4] 刘洋, 赵敏. 开放文档格式在文档处理中的应用[J]. 计算机工程与设计, 2021, 42(11): 3120-3126.",
    "[5] 孙丽. 毕业论文格式规范自动核查方法研究[J]. 现代教育技术, 2023, 33(2): 45-52.",
    "[6] 周强, 吴丹. 基于样式的文档一致性检测算法[J]. 软件学报, 2022, 33(9): 3411-3425.",
    "[7] 郑华. 高校本科毕业论文质量监控体系构建研究[M]. 北京: 高等教育出版社, 2019: 120-135.",
    "[8] 冯磊, 许倩. 文档结构识别中的标题层级判定方法[J]. 中文信息学报, 2021, 35(6): 71-79.",
]

BAD_REFERENCES = [
    "[1] 王明. 学术论文排版规范化.",
    "[2] 李静, 张伟. 基于规则引擎的文档质量检测系统设计.",
    "[3] 陈晓东. 面向中文科技文档的自动校对技术研究.",
    "[4] 刘洋, 赵敏. 开放文档格式在文档处理中的应用.",
]

BODY_1 = (
    "毕业论文的格式规范是学术训练的一部分，但格式校对本身并不产生学术价值。调查发现，"
    "多数学生在提交前的最后一两天才集中处理格式问题，而此时的修改往往顾此失彼。"
    "本课题希望把这类重复性劳动交给程序完成，让作者把精力放回内容本身。"
)

BODY_2 = (
    "本文要解决的问题可以拆成三部分：第一，如何从文档中准确读取段落与样式的真实生效值；"
    "第二，如何把学校的格式规范表达成程序可以执行、并且非程序员也能修改的形式；"
    "第三，如何在发现问题之后给出足够具体的修改指引，而不是只报一个错。"
)

BODY_3 = (
    "系统的总体结构分为三层。最上层是交互界面，负责文件上传、问题展示与报告导出。"
    "中间层是业务编排，负责把上传、解析、检查、修复串成一条流程并落库。"
    "最下层是能力层，包含文档解析引擎、规则集与检查引擎。图1给出了系统的总体架构。"
    "图2进一步说明了检查流程中各个处理环节的先后关系，相关的规则分类统计见表1。"
)

BODY_4 = (
    "模块划分遵循单一职责原则。文档解析模块只负责把开放文档格式转换成内存中的结构化模型，"
    "不参与任何判定；规则集模块只负责描述期望值，不包含执行逻辑；检查引擎只认识通用的检查器，"
    "不关心具体规则的业务含义。三者之间的边界清晰，使得新增规则时不需要改动引擎代码。"
)

BODY_5 = (
    "文档解析的关键在于格式的继承关系。同一段文字的字号可能来自直接格式、段落样式或文档默认值，"
    "其优先级依次升高。解析模块对每个属性同时记录生效值与来源，既保证判定准确，"
    "也让报告能够告诉用户应当修改样式定义还是修改单个段落。"
)

BODY_ACK = "感谢指导教师在选题与实现过程中给予的耐心指导，也感谢同学在试用阶段提出的改进建议。"


# ---------------------------------------------------------------- 基础工具


def set_font(run, east: str = "宋体", size: float = 12, bold: bool = False, ascii_name: str = SANS) -> None:
    run.font.name = ascii_name
    run.font.size = Pt(size)
    run.bold = bold
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), ascii_name)
    rfonts.set(qn("w:hAnsi"), ascii_name)
    rfonts.set(qn("w:eastAsia"), east)


def add(
    doc: Document,
    text: str,
    *,
    style: str | None = None,
    east: str = "宋体",
    size: float = 12,
    bold: bool = False,
    align: str | None = None,
    indent_cm: float | None = None,
    line: float | None = 1.5,
):
    paragraph = doc.add_paragraph(style=style) if style else doc.add_paragraph()
    run = paragraph.add_run(text)
    set_font(run, east=east, size=size, bold=bold)
    fmt = paragraph.paragraph_format
    if align == "center":
        fmt.alignment = WD_ALIGN_PARAGRAPH.CENTER
    elif align == "left":
        fmt.alignment = WD_ALIGN_PARAGRAPH.LEFT
    elif align == "justify":
        fmt.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    if indent_cm is not None:
        fmt.first_line_indent = Cm(indent_cm)
    if line is not None:
        fmt.line_spacing = line
    return paragraph


def add_paragraph_mixed(doc: Document, specs: list[dict]):
    paragraph = doc.add_paragraph()
    for spec in specs:
        run = paragraph.add_run(spec["text"])
        set_font(run, east=spec.get("east", "宋体"), size=spec.get("size", 12), bold=spec.get("bold", False))
    paragraph.paragraph_format.line_spacing = 1.5
    paragraph.paragraph_format.first_line_indent = Cm(0.85)
    paragraph.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    return paragraph


def setup_page(doc: Document, top=2.5, bottom=2.5, left=3.0, right=2.5, a4=True) -> None:
    for section in doc.sections:
        section.top_margin = Cm(top)
        section.bottom_margin = Cm(bottom)
        section.left_margin = Cm(left)
        section.right_margin = Cm(right)
        if a4:
            section.page_width = Cm(21.0)
            section.page_height = Cm(29.7)


def add_page_number(doc: Document) -> None:
    for section in doc.sections:
        paragraph = section.footer.paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = paragraph.add_run()
        begin = OxmlElement("w:fldChar")
        begin.set(qn("w:fldCharType"), "begin")
        instr = OxmlElement("w:instrText")
        instr.set(qn("xml:space"), "preserve")
        instr.text = "PAGE"
        end = OxmlElement("w:fldChar")
        end.set(qn("w:fldCharType"), "end")
        run._element.append(begin)
        run._element.append(instr)
        run._element.append(end)
        set_font(run, size=10.5)


TITLE = "基于规则驱动的中文学术文档格式自动检查系统设计与实现"


# ---------------------------------------------------------------- 四份样例


def sample_ok(path: Path) -> None:
    doc = Document()
    setup_page(doc)
    add_page_number(doc)

    add(doc, TITLE, east="黑体", size=22, bold=True, align="center", line=1.5)
    add(doc, "摘要", style="Heading 1", east="黑体", size=16, bold=True, align="center", line=None)
    add(doc, ABSTRACT, indent_cm=0.85, align="justify")
    add(doc, KEYWORDS, indent_cm=0.85, align="justify")
    add(doc, "第一章 绪论", style="Heading 1", east="黑体", size=16, bold=True, align="center", line=None)
    add(doc, "1.1 研究背景", style="Heading 2", east="黑体", size=14, bold=True, line=None)
    add(doc, BODY_1, indent_cm=0.85, align="justify")
    add(doc, "1.2 研究内容", style="Heading 2", east="黑体", size=14, bold=True, line=None)
    add(doc, BODY_2, indent_cm=0.85, align="justify")
    add(doc, "第二章 系统设计", style="Heading 1", east="黑体", size=16, bold=True, align="center", line=None)
    add(doc, "2.1 总体架构", style="Heading 2", east="黑体", size=14, bold=True, line=None)
    add(doc, BODY_3, indent_cm=0.85, align="justify")
    add(doc, "图1 系统总体架构图", east="宋体", size=10.5, align="center", indent_cm=0, line=1.0)
    add(doc, "图2 检查流程示意图", east="宋体", size=10.5, align="center", indent_cm=0, line=1.0)
    add(doc, "表1 规则分类统计表", east="宋体", size=10.5, align="center", indent_cm=0, line=1.0)
    add(doc, "2.1.1 模块划分", style="Heading 3", east="黑体", size=12, bold=True, line=None)
    add(doc, BODY_4, indent_cm=0.85, align="justify")
    add(doc, "第三章 系统实现", style="Heading 1", east="黑体", size=16, bold=True, align="center", line=None)
    add(doc, "3.1 文档解析", style="Heading 2", east="黑体", size=14, bold=True, line=None)
    add(doc, BODY_5, indent_cm=0.85, align="justify")
    add(doc, "参考文献", style="Heading 1", east="黑体", size=16, bold=True, align="center", line=None)
    for item in REFERENCES:
        add(doc, item, east="宋体", size=10.5, indent_cm=0, line=1.0)
    add(doc, "致谢", style="Heading 1", east="黑体", size=16, bold=True, align="center", line=None)
    add(doc, BODY_ACK, indent_cm=0.85, align="justify")
    doc.save(str(path))


def sample_layout(path: Path) -> None:
    doc = Document()
    setup_page(doc, top=1.5, bottom=1.5, left=2.0, right=2.0, a4=False)  # 页边距与纸张都不合规

    add(doc, TITLE, east="黑体", size=22, bold=True, align="center")
    add(doc, "摘要", style="Heading 1", east="黑体", size=18, bold=True)  # 一级标题字号应为 16
    add(doc, ABSTRACT, indent_cm=None, align="left", line=1.0)
    add(doc, KEYWORDS, indent_cm=None, align="left")
    add(doc, "第一章 绪论", style="Heading 1", east="黑体", size=18, bold=True)
    add(doc, "1.1 研究背景", style="Heading 2", east="黑体", size=14, bold=True)
    add(doc, BODY_1, east="宋体", size=14, indent_cm=None, align="left", line=1.0)
    add(doc, BODY_2, east="黑体", size=10.5, indent_cm=None, align="left", line=1.0)
    add(doc, BODY_3, east="宋体", size=12, indent_cm=None, align="left", line=1.0)
    add(doc, "第二章 系统设计", style="Heading 1", east="黑体", size=18, bold=True)
    add(doc, BODY_4, east="宋体", size=12, indent_cm=None, align="left", line=1.0)
    add(doc, "参考文献", style="Heading 1", east="黑体", size=18, bold=True)
    for item in REFERENCES:
        add(doc, item, east="宋体", size=12, indent_cm=None, align="left", line=1.0)
    doc.save(str(path))


def sample_structure(path: Path) -> None:
    doc = Document()
    setup_page(doc)
    add_page_number(doc)

    add(doc, TITLE, east="黑体", size=22, bold=True, align="center")
    add(doc, "摘要", style="Heading 1", east="黑体", size=16, bold=True, align="center", line=None)
    add(doc, ABSTRACT, indent_cm=0.85, align="justify")
    add(doc, "第一章 绪论", style="Heading 1", east="黑体", size=16, bold=True, align="center", line=None)
    # 层级跳级：一级标题之后直接出现三级标题
    add(doc, "1.1.1 研究背景", style="Heading 3", east="黑体", size=12, bold=True, line=None)
    add(doc, BODY_1, indent_cm=0.85, align="justify")
    add(doc, "第二章 系统设计", style="Heading 1", east="黑体", size=16, bold=True, align="center", line=None)
    add(doc, "2.1 总体架构", style="Heading 2", east="黑体", size=14, bold=True, line=None)
    add(doc, BODY_2, indent_cm=0.85, align="justify")
    # 编号断裂 + 重号（图1、图3、图3；表从 2 开始），且正文中不引用
    add(doc, "图1 系统总体架构图", east="宋体", size=10.5, align="center", indent_cm=0, line=1.0)
    add(doc, "图3 检查流程示意图", east="宋体", size=10.5, align="center", indent_cm=0, line=1.0)
    add(doc, "图3 模块划分示意图", east="宋体", size=10.5, align="center", indent_cm=0, line=1.0)
    add(doc, "表2 规则分类统计表", east="宋体", size=10.5, align="center", indent_cm=0, line=1.0)
    add(doc, BODY_4, indent_cm=0.85, align="justify")
    # 标题下无正文：2.2 之后紧跟同级标题 2.3
    add(doc, "2.2 数据模型", style="Heading 2", east="黑体", size=14, bold=True, line=None)
    add(doc, "2.3 接口设计", style="Heading 2", east="黑体", size=14, bold=True, line=None)
    add(doc, BODY_5, indent_cm=0.85, align="justify")
    add(doc, "参考文献", style="Heading 1", east="黑体", size=16, bold=True, align="center", line=None)
    for item in REFERENCES:
        add(doc, item, east="宋体", size=10.5, indent_cm=0, line=1.0)
    doc.save(str(path))


def sample_consistency(path: Path) -> None:
    doc = Document()
    setup_page(doc)
    add_page_number(doc)

    add(doc, TITLE, east="黑体", size=22, bold=True, align="center")
    add(doc, "摘要", style="Heading 1", east="黑体", size=16, bold=True, align="center", line=None)
    add(doc, SHORT_ABSTRACT, indent_cm=0.85, align="justify")  # 摘要过短
    add(doc, "关键词：格式检查", indent_cm=0.85, align="justify")  # 关键词只有 1 个
    add(doc, "第一章 绪论", style="Heading 1", east="黑体", size=16, bold=True, align="center", line=None)
    add(doc, "1.1 研究背景", style="Heading 2", east="黑体", size=14, bold=True, line=None)
    # 中文语句里混用英文标点
    add(doc, "本章介绍课题的来源与意义,并对国内外的研究现状进行梳理.已有工作主要集中在两个方面,"
             "一是格式模板的自动化套用,二是文档要素的完整性校验.", indent_cm=0.85, align="justify")
    # 同一段落内字体字号混用（复制粘贴的典型后果）
    add_paragraph_mixed(
        doc,
        [
            {"text": "在同一段落内混用多种字体是常见问题，"},
            {"text": "例如这一段的后半部分", "east": "黑体", "size": 12},
            {"text": "就换成了另一种字号，", "east": "宋体", "size": 14},
        ],
    )
    add(doc, "第二章 系统设计", style="Heading 1", east="黑体", size=14, bold=True, align="center", line=None)
    add(doc, "2.1 总体架构", style="Heading 2", east="黑体", size=14, bold=True, line=None)
    add(doc, BODY_2, indent_cm=0.85, align="justify")
    # 连续空段落
    add(doc, "", indent_cm=None, line=1.5)
    add(doc, "", indent_cm=None, line=1.5)
    add(doc, "", indent_cm=None, line=1.5)
    add(doc, BODY_3, east="宋体", size=12, indent_cm=0.85, align="justify")
    # 正文里出现另一种字体（宋体以外的段落）
    add(doc, "这一段使用了与正文不同的字体，用来演示全文一致性检查。", east="黑体", size=12,
        indent_cm=0.85, align="justify")
    add(doc, "参考文献", style="Heading 1", east="黑体", size=16, bold=True, align="center", line=None)
    for item in BAD_REFERENCES:
        add(doc, item, east="宋体", size=10.5, indent_cm=0, line=1.0)
    add(doc, "致谢", style="Heading 1", east="黑体", size=16, bold=True, align="center", line=None)
    add(doc, BODY_ACK, indent_cm=0.85, align="justify")
    doc.save(str(path))


BUILDERS = [
    ("01-规范样本.docx", sample_ok, "全部按规范排版，用于证明工具不会乱报（预期高分）"),
    ("02-排版问题样本.docx", sample_layout, "页边距 / 字号 / 行距 / 缩进 / 页码 均不合规"),
    ("03-结构问题样本.docx", sample_structure, "标题跳级 / 图表编号断裂重复 / 未引用 / 缺章节"),
    ("04-一致性与标点样本.docx", sample_consistency, "字体混用 / 标题字号不统一 / 中英标点混用 / 参考文献要素缺失"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="生成格致演示样例文档")
    parser.add_argument("--out", default=None, help="输出目录，默认 <工程根>/samples")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    out_dir = Path(args.out).resolve() if args.out else root / "samples"
    out_dir.mkdir(parents=True, exist_ok=True)

    for name, builder, description in BUILDERS:
        target = out_dir / name
        builder(target)
        print(f"[生成] {target}  ({target.stat().st_size / 1024:.1f} KB) —— {description}")
    print(f"\n共 {len(BUILDERS)} 份样例已生成到：{out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
