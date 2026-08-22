#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hand-rolled minimal OOXML .docx generator (no external deps)."""
import zipfile, os

def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))

def run(text, bold=False, size=None, color=None):
    rpr = "<w:rPr>"
    if bold: rpr += "<w:b/>"
    if color: rpr += f'<w:color w:val="{color}"/>'
    if size: rpr += f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>'
    rpr += "</w:rPr>"
    if rpr == "<w:rPr></w:rPr>": rpr = ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{esc(text)}</w:t></w:r>'

def para(text="", style=None, bold=False, size=None, color=None, after=80, before=0, runs=None):
    ppr = "<w:pPr>"
    if style: ppr += f'<w:pStyle w:val="{style}"/>'
    if before or after:
        ppr += f'<w:spacing w:before="{before}" w:after="{after}"/>'
    ppr += "</w:pPr>"
    if ppr == "<w:pPr></w:pPr>": ppr = ""
    body = runs if runs else (run(text, bold, size, color) if text else "")
    return f"<w:p>{ppr}{body}</w:p>"

def bullet(text):
    ppr = '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr><w:spacing w:after="40"/></w:pPr>'
    return f"<w:p>{ppr}{run(text)}</w:p>"

def numbered(text):
    ppr = '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr><w:spacing w:after="40"/></w:pPr>'
    return f"<w:p>{ppr}{run(text)}</w:p>"

def table(col_widths, header, rows, header_fill="1F3864"):
    total = sum(col_widths)
    border = '<w:tblBorders><w:top w:val="single" w:sz="4" w:color="CCCCCC"/><w:bottom w:val="single" w:sz="4" w:color="CCCCCC"/><w:left w:val="single" w:sz="4" w:color="CCCCCC"/><w:right w:val="single" w:sz="4" w:color="CCCCCC"/><w:insideH w:val="single" w:sz="4" w:color="CCCCCC"/><w:insideV w:val="single" w:sz="4" w:color="CCCCCC"/></w:tblBorders>'
    tblpr = f'<w:tblPr><w:tblW w:w="{total}" w:type="dxa"/>{border}<w:tblLook w:val="04A0"/></w:tblPr>'
    grid = "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{w}"/>' for w in col_widths) + "</w:tblGrid>"
    def cell(text, w, fill, bold=False, color="000000"):
        tcpr = f'<w:tcPr><w:tcW w:w="{w}" w:type="dxa"/><w:shd w:val="clear" w:color="auto" w:fill="{fill}"/><w:tcMar><w:top w:w="60" w:type="dxa"/><w:bottom w:w="60" w:type="dxa"/><w:left w:w="120" w:type="dxa"/><w:right w:w="120" w:type="dxa"/></w:tcMar></w:tcPr>'
        return f'<w:tc>{tcpr}<w:p><w:pPr><w:spacing w:after="0"/></w:pPr>{run(text, bold, color=color)}</w:p></w:tc>'
    def row(cells, fill):
        return "<w:tr>" + "".join(cell(c, col_widths[i], fill, bold=(fill==header_fill), color=("FFFFFF" if fill==header_fill else "000000")) for i, c in enumerate(cells)) + "</w:tr>"
    body = row(header, header_fill)
    for ri, r in enumerate(rows):
        body += row(r, "F2F5FA" if ri % 2 == 0 else "FFFFFF")
    return f"<w:tbl>{tblpr}{grid}{body}</w:tbl>"

# ---- content ----
C = 9026  # A4 content width DXA
parts = []
parts.append(para("和乘幂（HEXP）信号数据看板", bold=True, size=40, color="1F3864", after=60))
parts.append(para("设计方案 · Design Specification", size=28, color="1F3864", after=40))
parts.append(para("HCM-V2 信号塔 · 和乘幂独立信号源观测面板", size=22, color="555555", after=40))
parts.append(para("状态：待审核（审核通过后再动工构建）  |  日期：2026-08-08", size=20, color="888888", after=200))

parts.append(para("目录", style="Heading2", after=80))
for t in ["1. 文档目的与范围","2. 设计目标（直观 / 协调 / 分类 / 完整）","3. 信息架构：七大信息分类",
          "4. 各面板详细规范","5. 视觉系统","6. 数据来源与刷新机制","7. 技术构建方案",
          "8. 与现有页面 / 信号源的关系","9. 待确认问题（请审核）"]:
    parts.append(bullet(t))

parts.append(para("1. 文档目的与范围", style="Heading1", before=240))
parts.append(para("本文档为「和乘幂（HEXP）信号数据看板」的设计方案，用于在生产环境直观观测 HEXP 独立信号源的运行状态、决策逻辑与实时信号质量。本看板只读观测，不下达交易指令（下单由引擎既有闸门负责）。"))
parts.append(para("看板数据严格来自 HEXP 引擎已发布的实时快照与配置中心，不新增任何信号计算逻辑，仅做可视化呈现。所有字段均以 hcm-v2/hcm-signal-tower/signal_tower/hexp_engine.py 的发布契约为准。"))

parts.append(para("2. 设计目标", style="Heading1", before=240))
parts.append(table([int(C*0.18), int(C*0.82)],
  ["目标","说明"],
  [["直观","一眼看清当前信号方向、等级与是否过闸；用颜色与图形代替数字堆砌，交易员 3 秒内可判读。"],
   ["协调","与 HCM 现有 Web 控制台（hcm-web）视觉语言统一：深色交易终端风、统一间距/圆角/字型，不突兀。"],
   ["信息分类","按「决策 → 算法 → 共振 → 评分 → 动量 → 执行 → 历史」七类分组，避免信息噪声混杂。"],
   ["完整","覆盖 HEXP 引擎全部对外字段（HP-Score、k、7 因子、4 周期状态、共振裁决、6 维评分卡、MM、执行预案），无遗漏盲区。"]]))

parts.append(para("3. 信息架构：七大信息分类", style="Heading1", before=240))
parts.append(para("看板采用单页分区栅格，自上而下、由决策到细节组织。布局比例为：顶部核心决策区全宽，下方三列卡片网格。"))
parts.append(table([int(C*0.08), int(C*0.18), int(C*0.26), int(C*0.48)],
  ["#","分类","位置 / 尺寸","承载内容"],
  [["A","核心决策区","顶部全宽条","信号方向灯、等级徽章、HP-Score 仪表、闸门状态+原因、现价/ATR/主周期"],
   ["B","和乘幂核心","左列卡片","幂指数 k 与体制区间、方向裁决、7 因子贡献条形图"],
   ["C","多周期共振矩阵","中列卡片","M5/H1/H4/D1 状态色块 + TrendScore、共振裁决滑杆"],
   ["D","6 维评分卡","右列卡片","6 维雷达图、加权总分、分级门槛标记"],
   ["E","微结构动量 MM","左列卡片","M1 动量仪表、加速/衰竭/反转三态预警"],
   ["F","执行预案","中列卡片","等级→手数、SL/TP、降仓系数、入场模式指示"],
   ["G","信号日志","底部全宽","滚动时间线：每次决策的字段快照"]]))

parts.append(para("4. 各面板详细规范", style="Heading1", before=240))
parts.append(para("A. 核心决策区（最醒目）", style="Heading2"))
parts.append(table([int(C*0.22), int(C*0.30), int(C*0.48)],
  ["元素","可视化","数据字段 / 来源"],
  [["信号方向灯","大号圆形指示灯：BUY=红 / SELL=绿 / NO_TRADE=灰","direction"],
   ["信号等级","徽章 S/A/B/C/RED（色阶）","grade"],
   ["HP-Score 仪表","0–100 半圆仪表 + 数值","hp_score"],
   ["闸门状态","✓ 放行 / ✗ 拦截 双态 + 拦截原因","passed + reason"],
   ["行情底栏","现价、ATR、主执行周期","close / atr / primary_period"]]))
parts.append(para("B. 和乘幂核心（HP-Score 算法可视化）", style="Heading2"))
parts.append(para("直观展示「和乘幂」数学框架：广义均值幂指数 k 随市况自适应，7 个归一化因子按权重参与求和。"))
parts.append(table([int(C*0.22), int(C*0.30), int(C*0.48)],
  ["元素","可视化","数据字段 / 来源"],
  [["幂指数 k","数值 + 体制区间指示（趋势 1.8–2.5 / 转换 1.0 / 震荡 0.5–0.8）","k"],
   ["方向裁决 dir_sum","横滑块 -1…0…+1，红绿双色","factor_scores 加权求和（前端算）"],
   ["7 因子贡献","横向条形图：adx/er/ma/bbw/hurst/rsi/mm，归一 -1…1，正=红 负=绿","factor_scores"],
   ["共振后强度","标注 hp_100 已含共振加成/惩罚","hp_score"]]))
parts.append(para("C. 多周期共振矩阵", style="Heading2"))
parts.append(table([int(C*0.22), int(C*0.30), int(C*0.48)],
  ["元素","可视化","数据字段 / 来源"],
  [["周期状态","4 色块 M5/H1/H4/D1：TREND_UP=红 / TREND_DOWN=绿 / RANGE=灰 / TRANSITION=琥珀；主周期 M5 描边高亮","period_states"],
   ["TrendScore","每周期 0–100 进度条","trend_scores"],
   ["共振裁决 verdict","横滑块 -1…0…+1，长/短阈值刻度线","resonance_verdict"]]))
parts.append(para("D. 6 维评分卡（闸门核心）", style="Heading2"))
parts.append(table([int(C*0.22), int(C*0.30), int(C*0.48)],
  ["元素","可视化","数据字段 / 来源"],
  [["六维雷达","resonance/state/entry/position/vol/session 雷达图（各 0–100）","scorecard"],
   ["加权总分","大号数字 0–100","scorecard_total"],
   ["分级门槛","横向刻度标注 pass/b/a/s_hp/hp_floor，标出当前总分档位","hexp.scorecard.* + scorecard_total"]]))
parts.append(para("E. 微结构动量 MM（M1 前置预警）", style="Heading2"))
parts.append(table([int(C*0.22), int(C*0.30), int(C*0.48)],
  ["元素","可视化","数据字段 / 来源"],
  [["M1 动量","仪表 -1…0…+1","mm"],
   ["三态预警","加速 / 衰竭 / 反转 指示（先于主周期转向）","mm + 阈值前端推断"]]))
parts.append(para("F. 执行预案（若放行时的动作预览）", style="Heading2"))
parts.append(table([int(C*0.22), int(C*0.30), int(C*0.48)],
  ["元素","可视化","数据字段 / 来源"],
  [["等级→手数","映射表 S/A/B/C 各 lot","hexp.exec.grade_lot_* + lot_mult"],
   ["止损/目标","SL=ATR 倍数、RR 目标","hexp.exec.sl_atr_mult / rrr_min"],
   ["降仓系数","transition 降仓倍率","hexp.exec.transition_lot_mult"],
   ["入场模式","回踩(A) / 突破(B) 指示（引擎未显式输出，见 Q4）","待定"]]))
parts.append(para("G. 信号日志（时间线）", style="Heading2"))
parts.append(para("底部全宽滚动列表，逐条记录每次决策快照，用于回看信号质量、与 co_source 做 shadow 对比。字段：时间戳、方向、等级、HP、k、verdict、总分、闸门、原因。"))
parts.append(table([int(C*0.22), int(C*0.30), int(C*0.48)],
  ["元素","可视化","数据字段 / 来源"],
  [["日志流","表格/时间线，可滚动，最多保留 N 条","见 §6 数据来源（日志方案）"],
   ["筛选","按 方向 / 等级 / 是否过闸 过滤","前端"]]))

parts.append(para("5. 视觉系统", style="Heading1", before=240))
parts.append(para("5.1 主题", style="Heading2"))
parts.append(para("推荐深色交易终端风格（与行情软件一致，长时间盯盘不刺眼），并与 hcm-web 现有主题协调。如现有控制台为浅色，则看板跟随浅色（待确认 Q3）。"))
parts.append(para("5.2 配色语义（遵循中国习惯：涨=红 跌=绿）", style="Heading2"))
parts.append(table([int(C*0.20), int(C*0.22), int(C*0.58)],
  ["语义","颜色","应用"],
  [["涨 / BUY / 正因子","红 #E04848","方向灯 BUY、因子正向条、TREND_UP"],
   ["跌 / SELL / 负因子","绿 #2BBF6A","方向灯 SELL、因子负向条、TREND_DOWN"],
   ["中性 / NO_TRADE","灰 #6B7280","无信号、RANGE 状态"],
   ["转换预警","琥珀 #F5A623","TRANSITION 状态、阈值预警"],
   ["等级 S/A/B/C","金/橙/黄/蓝","等级徽章色阶（由强到弱）"],
   ["RED 级","灰红 #9AA0A6","未过闸等级"]]))
parts.append(para("5.3 字型与栅格", style="Heading2"))
parts.append(bullet("数字统一等宽（tabular-nums），保证刷新时位数对齐不跳动。"))
parts.append(bullet("卡片圆角统一 8px，分组间距统一 16px，栅格 12 列响应式。"))
parts.append(bullet("标题/分组层级清晰：区标题 16px 粗体、卡片标题 14px、数值 20–32px。"))

parts.append(para("6. 数据来源与刷新机制", style="Heading1", before=240))
parts.append(table([int(C*0.24), int(C*0.28), int(C*0.48)],
  ["数据","来源","刷新"],
  [["实时信号快照","GET /api/v1/hexp/signal/{symbol}（读 Redis hcm:live:hexp:{symbol}，TTL 15s）","轮询 3–5s"],
   ["阈值/配置参考","GET /api/v1/hexp/config（配置中心）","进入页面时 + 配置变更时"],
   ["信号日志","方案A：引擎新增 capped list hcm:live:hexp:log:{symbol}；方案B：前端轮询环形缓冲（无后端改动）","见 Q2"]]))
parts.append(para("注：实时端点与配置端点均已存在，看板无需新增后端即可运行（方案B）。日志持久化（方案A）需少量后端改动，列为可选。"))

parts.append(para("7. 技术构建方案", style="Heading1", before=240))
parts.append(numbered("新增前端页面：frontend/src/pages/hexp/HexpDashboard.tsx。"))
parts.append(numbered("路由：新增 /hexp/dashboard（或并入信号塔「和乘幂」Tab 下，见 Q1）。"))
parts.append(numbered("数据流：封装 useHexpLive(symbol) 轮询 hook，统一注入各面板。"))
parts.append(numbered("图表：沿用现有前端图表组件；雷达图/仪表若无现成组件则用轻量 SVG 自绘（与栈一致，不引入重依赖）。"))
parts.append(numbered("后端：默认零改动；日志持久化（方案A）另议。"))

parts.append(para("8. 与现有页面 / 信号源的关系", style="Heading1", before=240))
parts.append(bullet("与「和乘幂配置页（HexpConfig）」并列：配置页管参数，看板管观测，二者同属信号塔和乘幂模块。"))
parts.append(bullet("与 co_source 关系：当前 active_model 互斥（手动/双源/和乘幂三选一）。看板只显示当前激活源（hexp）的数据；并行对比需另建 shadow（见 Q5）。"))
parts.append(bullet("不动 scheduler / hexp_engine 计算逻辑，纯前端可视化。"))

parts.append(para("9. 待确认问题（请审核）", style="Heading1", before=240))
parts.append(numbered("看板入口：独立页面 /hexp/dashboard，还是并入信号塔「和乘幂」Tab 下？"))
parts.append(numbered("信号日志：前端环形缓冲（v1，零后端改动）还是 后端落库持久化（方案A）？"))
parts.append(numbered("主题：深色交易终端（推荐）还是 跟随 hcm-web 现有浅色主题？"))
parts.append(numbered("入场模式（回踩A/突破B）：引擎当前未显式输出该字段，看板是否做「模式推断」展示，或后端补字段？"))
parts.append(numbered("是否需在看板叠加 co_source 实时对比（并行 shadow），为「和双源并行」提供观测基础？"))
parts.append(numbered("图表库：沿用现有前端组件，还是允许引入新图表库（如 Recharts）？"))

parts.append(para("— 方案待审核，审核通过后方可进入构建阶段 —", size=20, color="888888", before=200))

sectpr = '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
document_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + \
  '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">' + \
  f'<w:body>{"".join(parts)}{sectpr}</w:body></w:document>'

content_types = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + \
  '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' + \
  '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' + \
  '<Default Extension="xml" ContentType="application/xml"/>' + \
  '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>' + \
  '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>' + \
  '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>' + \
  '</Types>'

rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + \
  '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + \
  '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>' + \
  '</Relationships>'

doc_rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + \
  '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + \
  '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>' + \
  '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>' + \
  '</Relationships>'

styles_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + \
  '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">' + \
  '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr><w:rPr><w:b/><w:sz w:val="30"/><w:szCs w:val="30"/><w:color w:val="1F3864"/></w:rPr></w:style>' + \
  '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="160" w:after="80"/></w:pPr><w:rPr><w:b/><w:sz w:val="25"/><w:szCs w:val="25"/><w:color w:val="2E5496"/></w:rPr></w:style>' + \
  '</w:styles>'

numbering_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + \
  '<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">' + \
  '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="&#8226;"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>' + \
  '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>' + \
  '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>' + \
  '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>' + \
  '</w:numbering>'

out = "D:/HCM_ASST/docs/和乘幂信号数据看板设计方案.docx"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("[Content_Types].xml", content_types)
    z.writestr("_rels/.rels", rels)
    z.writestr("word/document.xml", document_xml)
    z.writestr("word/_rels/document.xml.rels", doc_rels)
    z.writestr("word/styles.xml", styles_xml)
    z.writestr("word/numbering.xml", numbering_xml)
print("OK written:", out, os.path.getsize(out), "bytes")
