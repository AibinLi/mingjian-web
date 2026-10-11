"""Build the Phase 10 competition report by filling the official DOCX template in place.

The template path is supplied at runtime and is never modified.  This script is
intentionally a document-production helper, not application functionality.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.shared import Cm, Pt


BODY_FONT = "宋体"
HEADING_FONT = "黑体"


def set_font(run, *, size=12, bold=False, name=BODY_FONT):
    run.font.name = name
    run._element.rPr.rFonts.set(qn("w:eastAsia"), name)
    run.font.size = Pt(size)
    run.font.bold = bold


def set_paragraph(paragraph, text, *, heading=False, size=None, align=None, first_indent=True):
    paragraph.clear()
    run = paragraph.add_run(text)
    set_font(run, size=size or (16 if heading else 12), bold=heading, name=HEADING_FONT if heading else BODY_FONT)
    fmt = paragraph.paragraph_format
    fmt.line_spacing = 1.5
    fmt.space_before = Pt(0)
    fmt.space_after = Pt(0)
    fmt.first_line_indent = Cm(0.74) if first_indent and not heading else Cm(0)
    if align is not None:
        paragraph.alignment = align
    return paragraph


def delete_paragraph(paragraph):
    paragraph._element.getparent().remove(paragraph._element)


def insert_before(anchor, text, *, heading=False, size=None, first_indent=True, align=None):
    xml = OxmlElement("w:p")
    anchor._p.addprevious(xml)
    paragraph = Paragraph(xml, anchor._parent)
    return set_paragraph(paragraph, text, heading=heading, size=size, first_indent=first_indent, align=align)


def insert_after(anchor, text, *, heading=False, size=None, first_indent=True, align=None):
    xml = OxmlElement("w:p")
    anchor._p.addnext(xml)
    paragraph = Paragraph(xml, anchor._parent)
    return set_paragraph(paragraph, text, heading=heading, size=size, first_indent=first_indent, align=align)


def add_bullet_before(anchor, text):
    p = insert_before(anchor, text, first_indent=False)
    p.paragraph_format.left_indent = Cm(0.74)
    p.paragraph_format.first_line_indent = Cm(-0.37)
    p.text = "• " + text
    for run in p.runs:
        set_font(run)
    return p


def add_table_before(doc, anchor, headers, rows, widths=None):
    table = doc.add_table(rows=1, cols=len(headers))
    # The official WPS template has no named Word table style; preserve its
    # minimal visual system and use Word's default grid directly.
    table.autofit = False
    for idx, value in enumerate(headers):
        cell = table.rows[0].cells[idx]
        cell.text = value
        for p in cell.paragraphs:
            set_paragraph(p, value, first_indent=False, align=WD_ALIGN_PARAGRAPH.CENTER)
            for r in p.runs:
                set_font(r, size=10.5, bold=True)
    for row in rows:
        cells = table.add_row().cells
        for idx, value in enumerate(row):
            cells[idx].text = value
            for p in cells[idx].paragraphs:
                set_paragraph(p, value, first_indent=False, align=WD_ALIGN_PARAGRAPH.CENTER if idx > 0 else WD_ALIGN_PARAGRAPH.LEFT)
                for r in p.runs:
                    set_font(r, size=10.5)
    if widths:
        for row in table.rows:
            for idx, width in enumerate(widths):
                row.cells[idx].width = Cm(width)
    anchor._p.addprevious(table._tbl)
    return table


def update_fields(doc):
    settings = doc.settings.element
    element = settings.find(qn("w:updateFields"))
    if element is None:
        element = OxmlElement("w:updateFields")
        settings.append(element)
    element.set(qn("w:val"), "true")


def add_field(run, field_name):
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = field_name
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instr)
    run._r.append(end)


def add_page_footer(doc):
    footer = doc.sections[0].footer
    paragraph = footer.paragraphs[0]
    paragraph.clear()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for value in ("第 ",):
        run = paragraph.add_run(value)
        set_font(run, size=10.5)
    field = paragraph.add_run()
    add_field(field, "PAGE")
    set_font(field, size=10.5)
    run = paragraph.add_run(" 页  共 ")
    set_font(run, size=10.5)
    field = paragraph.add_run()
    add_field(field, "NUMPAGES")
    set_font(field, size=10.5)
    run = paragraph.add_run(" 页")
    set_font(run, size=10.5)


def fill(doc):
    p = doc.paragraphs
    # Preserve the official cover wording but fill only the permitted anonymous fields.
    set_paragraph(p[12], "作品名称：明鉴 Web", size=16, first_indent=False)
    set_paragraph(p[13], "作品类型：开放式自由命题", size=16, first_indent=False)
    set_paragraph(p[14], "", first_indent=False)
    set_paragraph(p[15], "", first_indent=False)

    # Delete the template's explicit writing instructions and all sample prompts.
    for idx in range(17, 23):
        # Keep the source paragraph/section structure but remove every visible
        # instruction line as required by the official template.
        set_paragraph(p[idx], "", first_indent=False)
    p = doc.paragraphs
    # The official directory is preserved as a static directory; Word refreshes fields on open.
    toc = [
        "摘    要\t3",
        "第一章 作品背景\t4",
        "第二章 作品方案与实现\t5",
        "第三章 作品测试与评估\t6",
        "第四章 创新特色\t7",
        "第五章 总结\t8",
        "参考文献\t9",
    ]
    for idx, text in zip(range(29, 36), toc):
        set_paragraph(p[idx], text, size=12, first_indent=False)

    # Remove remaining template prompts before inserting final prose.
    prompt_indices = [44, 51, 58, 65, 73, 81, 88, 89]
    for idx in prompt_indices:
        set_paragraph(p[idx], "", first_indent=False)
    p = doc.paragraphs

    # Keep template headings, normalize anonymous final headings, and insert content in each official chapter slot.
    headings = {43: "摘 要", 50: "第一章 作品背景", 57: "第二章 作品方案与实现", 64: "第三章 作品测试与评估", 72: "第四章 创新特色", 80: "第五章 总结", 87: "参考文献"}
    for idx, text in headings.items():
        set_paragraph(p[idx], text, heading=True, first_indent=False)

    abstract_anchor = p[50]
    insert_before(abstract_anchor, "明鉴 Web 是面向人工复核场景的本地媒体风险分析原型。作品将媒体真实性、内容可信度和来源可靠性分为三条独立轴，避免把不同证据类型压缩为单一“真/假”结论。系统对上传媒体执行受限预处理，保存 SHA-256、派生物、关键帧、质量状态与配置审计信息，并在 Web 报告中呈现限制与可追溯证据。")
    insert_before(abstract_anchor, "在安全边界上，系统仅提供 localhost 单用户部署：API 位于 internal-only 网络，固定 digest 的非 root gateway 仅发布 127.0.0.1:8000；默认 URL 任务不联网下载，模型也不自动下载。综合建议只用于人工复核，不自动封禁、处罚或作法律判断。")
    insert_before(abstract_anchor, "唯一完成正式性能评测的是 DeepfakeBench Meso4 视觉基线。获授权 Celeb-DF v2 官方 518 视频冻结输出的 ROC-AUC 为 0.513648、FPR 为 0.994382，表明该基线高误报且不具备可靠的通用深伪检测能力。音频、事实核查、三轴融合、消融和鲁棒性均因缺少获授权冻结标签 manifest 标为 not_applicable，不报告性能数字。")
    insert_before(abstract_anchor, "关键词：AI安全；媒体取证；可审计报告；最小权限；人工复核", first_indent=False)

    chapter2 = p[57]
    insert_before(chapter2, "第一章围绕 AI 生成或篡改媒体的风险展开。媒体是否有篡改迹象、媒体中的可验证主张是否被证据支持、媒体出处与传播链是否可信，分别对应三个不同问题。单一真假标签会掩盖证据来源、缺失模态和模型不可用状态，容易诱发不当自动决策。")
    insert_before(chapter2, "作品的目标不是输出自动定性结论，而是在受限本地环境中完成媒体输入检查、派生物生成、独立三轴报告与审计回看。系统将无法判断、不可用、缺失、超时与 Mock 作为显式状态；这些状态均不能被解释为低风险。")
    insert_before(chapter2, "本作品属于开放式自由命题，聚焦 AI 应用安全与安全媒体处理。它不提供攻击样本生成、检测绕过、登录绕过、未经授权抓取或真实 URL 下载能力。")

    chapter3 = p[64]
    insert_before(chapter3, "系统由同源 Web 前端、FastAPI 服务、SQLite 作业库、本地 Worker 和受控 FFmpeg/FFprobe 边界组成。用户文件名不成为路径或对象键；服务端以文件 SHA-256 生成对象键。Worker 以不经 shell 的子进程检查容器、时长、尺寸、帧率和派生物预算，并生成标准化媒体、元数据、关键帧、镜头列表与质量工件。")
    insert_before(chapter3, "媒体真实性轴使用 DeepfakeBench Meso4 的未校准 fake-class 分数、采样视觉帧序列和可疑片段。该分数不是媒体真实性概率。AST 音频反欺骗适配器仅在本地冻结工件完整且哈希匹配时推理；Haar/运动音画同步模块只是受质量门控的启发式筛查，不是 SyncNet、唇读或同步事实判定。")
    insert_before(chapter3, "内容可信度轴只从带可定位时间戳的 ASR/OCR 文本创建主张。默认 ASR/OCR、检索和来源轴仍是 Mock/不联网契约，受控夹具只用于测试状态与追溯，不表示真实事实核查、真实外部证据或来源可信度。前端只展示已保存结果，不重新计算模型结论。")
    insert_before(chapter3, "部署采用最小权限 Compose：API 不发布宿主机端口，gateway 是唯一入口；容器为非 root、只读根文件系统、无 Linux capabilities，并限制 PID、内存、CPU 与临时空间。用户媒体默认留存 900 秒，终态作业可主动删除。")
    insert_before(chapter3, "表 1  冻结视觉实验指标（唯一可报告性能实验）", first_indent=False, align=WD_ALIGN_PARAGRAPH.CENTER)
    add_table_before(doc, chapter3,
        ["指标", "冻结输出"],
        [["样本数（real / fake）", "518（178 / 340）"], ["F1", "0.792056"], ["ROC-AUC", "0.513648"], ["FPR / FNR", "0.994382 / 0.002941"], ["TN / FP / FN / TP", "1 / 177 / 1 / 339"], ["detect() p95（秒）", "0.047"]],
        [7.0, 8.0])
    insert_before(chapter3, "图 1  系统工作流：浏览器提交受限媒体 → API 创建作业与对象键 → Worker 进行受控预处理 → 独立三轴与质量门控 → Web 报告、JSON 下载或删除。所有结论与限制均由后端持久化，前端不重算。", first_indent=False)
    chapter4 = p[72]
    insert_before(chapter4, "测试方案包含冻结评测复算、完整真实媒体回归、localhost Compose 启动、浏览器报告和受控媒体生命周期检查。冻结评测只从保留原始预测 JSONL 重算，且禁止以官方测试预测进行拟合、阈值选择或校准。")
    insert_before(chapter4, "阶段十实际运行完整真实 FFmpeg/FFprobe 冒烟回归，结果为 121 passed、2 warnings；两条 warning 均来自 AST Mel 滤波器配置提示。该回归是代码与处理链测试，不是音频或事实核查的性能评测。空卷 Compose 启动后 API 与 gateway 均为 healthy；受控 PNG 完成上传、报告读取、JSON 下载路径和删除，浏览器 console 无 error。")
    insert_before(chapter4, "实验结果必须如实解释：Meso4 在 178 个 real 样本中出现 177 个假阳性，ROC-AUC 接近随机。F1 与高召回不能被包装为可靠检测能力。没有冻结标签数据的音频、事实核查、融合、消融和鲁棒性不填报准确率、AUC、F1、延迟或其他性能数字。")
    insert_before(chapter4, "模型启用演示配置仅通过已核验的只读挂载提供 Meso4 权重；不自动下载。若权重或依赖不存在，报告仍诚实显示 unavailable 或 not_applicable，并提示补充证据或人工复核。")

    chapter5 = p[80]
    insert_before(chapter5, "第四章的特色不在于宣称新的高性能检测模型，而在于安全、可审计和不确定性优先的系统设计。第一，三轴独立建模与存储，Mock 内容/来源轴不得伪装成融合信号。第二，质量不足、缺失模态、模型不可用和超时均显式报告。第三，报告可回看受控媒体、关键帧、时间戳、工件、哈希与原始 JSON。第四，localhost 最小权限容器将上传、留存、网络和执行资源纳入边界。")

    references = p[87]
    insert_before(references, "第五章总结：明鉴 Web 已完成可运行的本地安全媒体处理、独立三轴展示、冻结视觉评测复算和诚实降级。它不应被用于自动封禁、处罚、执法或对个人作不利决定。Meso4 视觉基线的高误报已作为失败分析和人工复核理由明确呈现。")
    insert_before(references, "局限与伦理边界：当前没有 active 校准器；阶段六候选因 evaluation-only 上 Brier 和 Log Loss 同时变差而被拒绝。默认 URL 不下载，默认检索不联网，真实传播链核验和真实事实核查未实现。任何用户媒体处理须获得合法授权；短留存、删除和脱敏日志不能替代组织级合规、访问控制或数据保护。")
    insert_before(references, "后续工作必须在取得授权冻结标签 manifest、许可与独立评测条件后再开展，且不得使用官方测试预测进行调参或校准。")
    for reference in reversed([
        "[1] DeepfakeBench. Meso4 release v1.0.1. https://github.com/SCLBD/DeepfakeBench/releases/tag/v1.0.1",
        "[2] Li, Y. et al. Celeb-DF: A Large-scale Challenging Dataset for DeepFake Forensics. CVPR 2020. 数据使用以参赛方授权与主办方要求为准。",
        "[3] 明鉴 Web 冻结评测记录：evaluation/phase8_suite.celebdf_v2_20260715.json 及其受控审计输出。",
    ]):
        insert_after(references, reference, first_indent=False)

    # Remove template sample references left after final content.
    for paragraph in list(doc.paragraphs):
        if "李剑" in paragraph.text or "需要说明" in paragraph.text or "2000字" in paragraph.text or "5000字" in paragraph.text or "1000字" in paragraph.text or "500字" in paragraph.text:
            delete_paragraph(paragraph)
    # Remove the now-empty instruction-page scaffolding between the official
    # cover's work-type field and the preserved directory.
    paragraphs = list(doc.paragraphs)
    cover_end = next(i for i, paragraph in enumerate(paragraphs) if "作品类型：开放式自由命题" in paragraph.text)
    toc_start = next(i for i, paragraph in enumerate(paragraphs) if paragraph.text.replace(" ", "").strip() == "目录")
    for paragraph in reversed(paragraphs[cover_end + 1:toc_start]):
        if not paragraph.text.strip():
            delete_paragraph(paragraph)
    # The original template contains section breaks around empty prompt pages.
    # Keep only the cover-to-directory break and replace later breaks with real
    # page-break-before headings, avoiding empty pages while retaining the
    # official cover, directory and chapter structure.
    section_breaks = [paragraph for paragraph in doc.paragraphs if paragraph._p.pPr is not None and paragraph._p.pPr.sectPr is not None]
    for paragraph in section_breaks:
        paragraph._p.pPr.remove(paragraph._p.pPr.sectPr)
    for paragraph in doc.paragraphs:
        if paragraph.text.replace(" ", "").strip() in {"目录", "摘要", "第一章作品背景", "第二章作品方案与实现", "第三章作品测试与评估", "第四章创新特色", "第五章总结", "参考文献"}:
            paragraph.paragraph_format.page_break_before = True
    add_page_footer(doc)
    update_fields(doc)


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: build_phase10_report.py <official-template.docx> <output.docx>")
    source, output = map(Path, sys.argv[1:])
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, output)
    doc = Document(output)
    fill(doc)
    doc.core_properties.author = ""
    doc.core_properties.last_modified_by = ""
    doc.core_properties.title = "明鉴 Web 作品报告"
    doc.core_properties.subject = "开放式自由命题"
    doc.core_properties.keywords = ""
    doc.save(output)


if __name__ == "__main__":
    main()
