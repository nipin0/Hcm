const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  HeadingLevel, AlignmentType, LevelFormat, BorderStyle, WidthType,
  ShadingType, PageOrientation, Header, Footer, PageNumber,
} = require("docx");
const fs = require("fs");

const A4 = { width: 11906, height: 16838 };
const MARGIN = 1440;
const CONTENT = A4.width - MARGIN * 2; // 9026

const border = { style: BorderStyle.SINGLE, size: 1, color: "CCCCCC" };
const borders = { top: border, bottom: border, left: border, right: border };
const cellMargins = { top: 60, bottom: 60, left: 120, right: 120 };

function h1(t) {
  return new Paragraph({ heading: HeadingLevel.HEADING_1, spacing: { before: 280, after: 120 }, children: [new TextRun({ text: t, bold: true })] });
}
function h2(t) {
  return new Paragraph({ heading: HeadingLevel.HEADING_2, spacing: { before: 200, after: 80 }, children: [new TextRun({ text: t, bold: true })] });
}
function p(t, opts = {}) {
  return new Paragraph({ spacing: { after: 60 }, children: [new TextRun({ text: t, ...opts })] });
}
function bullets(items) {
  return items.map((it) =>
    new Paragraph({ numbering: { reference: "bullets", level: 0 }, spacing: { after: 40 }, children: [new TextRun({ text: it })] })
  );
}
function num(items) {
  return items.map((it) =>
    new Paragraph({ numbering: { reference: "numbers", level: 0 }, spacing: { after: 40 }, children: [new TextRun({ text: it })] })
  );
}

// 表格：列宽数组需与 CONTENT 之和一致
function table(colWidths, header, rows, headerFill = "1F3864") {
  const headerRow = new TableRow({
    children: header.map((c, i) =>
      new TableCell({
        borders, width: { size: colWidths[i], type: WidthType.DXA }, shading: { fill: headerFill, type: ShadingType.CLEAR },
        margins: cellMargins, verticalAlign: "center",
        children: [new Paragraph({ children: [new TextRun({ text: c, bold: true, color: "FFFFFF" })] })],
      })
    ),
  });
  const body = rows.map((r, ri) =>
    new TableRow({
      children: r.map((c, i) =>
        new TableCell({
          borders, width: { size: colWidths[i], type: WidthType.DXA },
          shading: { fill: ri % 2 === 0 ? "F2F5FA" : "FFFFFF", type: ShadingType.CLEAR },
          margins: cellMargins, verticalAlign: "center",
          children: [new Paragraph({ children: [new TextRun({ text: c })] })],
        })
      ),
    })
  );
  return new Table({ width: { size: CONTENT, type: WidthType.DXA }, columnWidths: colWidths, rows: [headerRow, ...body] });
}

const children = [];

// 封面
children.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 600, after: 100 }, children: [new TextRun({ text: "和乘幂（HEXP）信号数据看板", bold: true, size: 40 })] }));
children.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 60 }, children: [new TextRun({ text: "设计方案 · Design Specification", size: 28, color: "1F3864" })] }));
children.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 40 }, children: [new TextRun({ text: "HCM-V2 信号塔 · 和乘幂独立信号源观测面板", size: 22, color: "555555" })] }));
children.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 200 }, children: [new TextRun({ text: "状态：待审核（审核通过后再动工构建）  |  日期：2026-08-08", size: 20, color: "888888" })] }));

// 0 目录
children.push(h2("目录"));
children.push(...bullets([
  "1. 文档目的与范围",
  "2. 设计目标（直观 / 协调 / 分类 / 完整）",
  "3. 信息架构：七大信息分类",
  "4. 各面板详细规范（字段 · 可视化 · 数据来源）",
  "5. 视觉系统（主题 · 配色语义 · 字型 · 栅格）",
  "6. 数据来源与刷新机制",
  "7. 技术构建方案（路由 · 组件 · 数据流）",
  "8. 与现有页面 / 信号源的关系",
  "9. 待确认问题（请审核）",
]));

// 1 目的
children.push(h1("1. 文档目的与范围"));
children.push(p("本文档为「和乘幂（HEXP）信号数据看板」的设计方案，用于在生产环境直观观测 HEXP 独立信号源的运行状态、决策逻辑与实时信号质量。本看板只读观测，不下达交易指令（下单由引擎既有闸门负责）。"));
children.push(p("看板数据严格来自 HEXP 引擎已发布的实时快照与配置中心，不新增任何信号计算逻辑，仅做可视化呈现。所有字段均以 hcm-v2/hcm-signal-tower/signal_tower/hexp_engine.py 的发布契约为准。"));

// 2 目标
children.push(h1("2. 设计目标"));
children.push(table([CONTENT * 0.22, CONTENT * 0.78], ["目标", "说明"],
  [
    ["直观", "一眼看清当前信号方向、等级与是否过闸；用颜色与图形代替数字堆砌，交易员 3 秒内可判读。"],
    ["协调", "与 HCM 现有 Web 控制台（hcm-web）视觉语言统一：深色交易终端风、统一间距/圆角/字型，不突兀。"],
    ["信息分类", "按「决策 → 算法 → 共振 → 评分 → 动量 → 执行 → 历史」七类分组，避免信息噪声混杂。"],
    ["完整", "覆盖 HEXP 引擎全部对外字段（HP-Score、k、7 因子、4 周期状态、共振裁决、6 维评分卡、MM、执行预案），无遗漏盲区。"],
  ]
));

// 3 信息架构
children.push(h1("3. 信息架构：七大信息分类"));
children.push(p("看板采用单页分区栅格，自上而下、由决策到细节组织。布局比例为：顶部核心决策区全宽，下方三列卡片网格。"));
children.push(table([CONTENT * 0.12, CONTENT * 0.22, CONTENT * 0.30, CONTENT * 0.36],
  ["#", "分类", "位置 / 尺寸", "承载内容"],
  [
    ["A", "核心决策区", "顶部全宽条", "信号方向灯、等级徽章、HP-Score 仪表、闸门状态+原因、现价/ATR/主周期"],
    ["B", "和乘幂核心", "左列卡片", "幂指数 k 与体制区间、方向裁决、7 因子贡献条形图"],
    ["C", "多周期共振矩阵", "中列卡片", "M5/H1/H4/D1 状态色块 + TrendScore、共振裁决滑杆"],
    ["D", "6 维评分卡", "右列卡片", "6 维雷达图、加权总分、分级门槛标记"],
    ["E", "微结构动量 MM", "左列卡片", "M1 动量仪表、加速/衰竭/反转三态预警"],
    ["F", "执行预案", "中列卡片", "等级→手数、SL/TP、降仓系数、入场模式指示"],
    ["G", "信号日志", "底部全宽", "滚动时间线：每次决策的字段快照"],
  ]
));

// 4 各面板详细规范
children.push(h1("4. 各面板详细规范"));

children.push(h2("A. 核心决策区（最醒目）"));
children.push(table([CONTENT * 0.24, CONTENT * 0.30, CONTENT * 0.46],
  ["元素", "可视化", "数据字段 / 来源"],
  [
    ["信号方向灯", "大号圆形指示灯：BUY=红 / SELL=绿 / NO_TRADE=灰", "direction"],
    ["信号等级", "徽章 S/A/B/C/RED（色阶）", "grade"],
    ["HP-Score 仪表", "0–100 半圆仪表 + 数值", "hp_score"],
    ["闸门状态", "✓ 放行 / ✗ 拦截 双态 + 拦截原因", "passed + reason（如 hexp_grade_red / hexp_mtf_long_only）"],
    ["行情底栏", "现价、ATR、主执行周期", "close / atr / primary_period"],
  ]
));

children.push(h2("B. 和乘幂核心（HP-Score 算法可视化）"));
children.push(p("直观展示「和乘幂」数学框架：广义均值幂指数 k 随市况自适应，7 个归一化因子按权重参与求和。"));
children.push(table([CONTENT * 0.24, CONTENT * 0.30, CONTENT * 0.46],
  ["元素", "可视化", "数据字段 / 来源"],
  [
    ["幂指数 k", "数值 + 体制区间指示（趋势 1.8–2.5 / 转换 1.0 / 震荡 0.5–0.8）", "k（hexp.k.* 配置为区间参考）"],
    ["方向裁决 dir_sum", "横滑块 -1…0…+1，红绿双色", "factor_scores 加权求和（前端算）"],
    ["7 因子贡献", "横向条形图：adx/er/ma/bbw/hurst/rsi/mm，归一 -1…1，正=红 负=绿", "factor_scores"],
    ["共振后强度", "标注 hp_100 已含共振加成/惩罚", "hp_score"],
  ]
));

children.push(h2("C. 多周期共振矩阵"));
children.push(table([CONTENT * 0.24, CONTENT * 0.30, CONTENT * 0.46],
  ["元素", "可视化", "数据字段 / 来源"],
  [
    ["周期状态", "4 色块 M5/H1/H4/D1：TREND_UP=红 / TREND_DOWN=绿 / RANGE=灰 / TRANSITION=琥珀；主周期 M5 描边高亮", "period_states"],
    ["TrendScore", "每周期 0–100 进度条", "trend_scores"],
    ["共振裁决 verdict", "横滑块 -1…0…+1，长/短阈值刻度线", "resonance_verdict"],
  ]
));

children.push(h2("D. 6 维评分卡（闸门核心）"));
children.push(table([CONTENT * 0.24, CONTENT * 0.30, CONTENT * 0.46],
  ["元素", "可视化", "数据字段 / 来源"],
  [
    ["六维雷达", "resonance/state/entry/position/vol/session 雷达图（各 0–100）", "scorecard"],
    ["加权总分", "大号数字 0–100", "scorecard_total"],
    ["分级门槛", "横向刻度标注 pass_threshold / b_threshold / a_threshold / s_hp_min / hp_floor，标出当前总分档位", "hexp.scorecard.* 配置 + scorecard_total"],
  ]
));

children.push(h2("E. 微结构动量 MM（M1 前置预警）"));
children.push(table([CONTENT * 0.24, CONTENT * 0.30, CONTENT * 0.46],
  ["元素", "可视化", "数据字段 / 来源"],
  [
    ["M1 动量", "仪表 -1…0…+1", "mm"],
    ["三态预警", "加速 / 衰竭 / 反转 指示（前置信号，先于主周期转向）", "mm + 阈值（hexp.mm.*）前端推断"],
  ]
));

children.push(h2("F. 执行预案（若放行时的动作预览）"));
children.push(table([CONTENT * 0.24, CONTENT * 0.30, CONTENT * 0.46],
  ["元素", "可视化", "数据字段 / 来源"],
  [
    ["等级→手数", "映射表 S/A/B/C 各 lot", "hexp.exec.grade_lot_* + lot_mult（配置）"],
    ["止损/目标", "SL=ATR 倍数、RR 目标", "hexp.exec.sl_atr_mult / rrr_min（配置）"],
    ["降仓系数", "transition 降仓倍率", "hexp.exec.transition_lot_mult（配置）"],
    ["入场模式", "回踩(A) / 突破(B) 指示（注：引擎当前未显式输出，见待确认 Q4）", "待定"],
  ]
));

children.push(h2("G. 信号日志（时间线）"));
children.push(p("底部全宽滚动列表，逐条记录每次决策快照，用于回看信号质量、与 co_source 做 shadow 对比。字段：时间戳、方向、等级、HP、k、verdict、总分、闸门、原因。"));
children.push(table([CONTENT * 0.24, CONTENT * 0.30, CONTENT * 0.46],
  ["元素", "可视化", "数据字段 / 来源"],
  [
    ["日志流", "表格/时间线，可滚动，最多保留 N 条", "见 §6 数据来源（日志方案）"],
    ["筛选", "按 方向 / 等级 / 是否过闸 过滤", "前端"],
  ]
));

// 5 视觉系统
children.push(h1("5. 视觉系统"));
children.push(h2("5.1 主题"));
children.push(p("推荐深色交易终端风格（与行情软件一致，长时间盯盘不刺眼），并与 hcm-web 现有主题协调。如现有控制台为浅色，则看板跟随浅色（待确认 Q3）。"));
children.push(h2("5.2 配色语义（遵循中国习惯：涨=红 跌=绿）"));
children.push(table([CONTENT * 0.22, CONTENT * 0.24, CONTENT * 0.54],
  ["语义", "颜色", "应用"],
  [
    ["涨 / BUY / 正因子", "红 #E04848", "方向灯 BUY、因子正向条、TREND_UP"],
    ["跌 / SELL / 负因子", "绿 #2BBF6A", "方向灯 SELL、因子负向条、TREND_DOWN"],
    ["中性 / NO_TRADE", "灰 #6B7280", "无信号、RANGE 状态"],
    ["转换预警", "琥珀 #F5A623", "TRANSITION 状态、阈值预警"],
    ["等级 S/A/B/C", "金/橙/黄/蓝 #F5C542/#F0913E/#E8C84B/#4A90D9", "等级徽章色阶（由强到弱）"],
    ["RED 级", "灰红 #9AA0A6", "未过闸等级"],
  ]
));
children.push(h2("5.3 字型与栅格"));
children.push(...bullets([
  "数字统一等宽（tabular-nums），保证刷新时位数对齐不跳动。",
  "卡片圆角统一 8px，分组间距统一 16px，栅格 12 列响应式。",
  "标题/分组层级清晰：区标题 16px 粗体、卡片标题 14px、数值 20–32px。",
]));

// 6 数据来源
children.push(h1("6. 数据来源与刷新机制"));
children.push(table([CONTENT * 0.26, CONTENT * 0.30, CONTENT * 0.44],
  ["数据", "来源", "刷新"],
  [
    ["实时信号快照", "GET /api/v1/hexp/signal/{symbol}（读 Redis hcm:live:hexp:{symbol}，TTL 15s）", "轮询 3–5s"],
    ["阈值/配置参考", "GET /api/v1/hexp/config（配置中心）", "进入页面时拉取 + 配置变更时"],
    ["信号日志", "方案A：引擎新增 capped list hcm:live:hexp:log:{symbol} 落每条决策；方案B：前端轮询环形缓冲（无后端改动）", "见 Q2"],
  ]
));
children.push(p("注：实时端点与配置端点均已存在，看板无需新增后端即可运行（方案B）。日志持久化（方案A）需少量后端改动，列为可选。"));

// 7 技术构建
children.push(h1("7. 技术构建方案"));
children.push(num([
  "新增前端页面：frontend/src/pages/hexp/HexpDashboard.tsx。",
  "路由：新增 /hexp/dashboard（或并入信号塔「和乘幂」Tab 下，见 Q1）。",
  "数据流：封装 useHexpLive(symbol) 轮询 hook，统一注入各面板。",
  "图表：沿用现有前端图表组件；雷达图/仪表若无现成组件则用轻量 SVG 自绘（与栈一致，不引入重依赖）。",
  "后端：默认零改动；日志持久化（方案A）另议。",
]));

// 8 关系
children.push(h1("8. 与现有页面 / 信号源的关系"));
children.push(...bullets([
  "与「和乘幂配置页（HexpConfig）」并列：配置页管参数，看板管观测，二者同属信号塔和乘幂模块。",
  "与 co_source 关系：当前 active_model 互斥（手动/双源/和乘幂三选一）。看板只显示当前激活源（hexp）的数据；并行对比需另建 shadow（见 Q5）。",
  "不动 scheduler / hexp_engine 计算逻辑，纯前端可视化。",
]));

// 9 待确认
children.push(h1("9. 待确认问题（请审核）"));
children.push(num([
  "看板入口：独立页面 /hexp/dashboard，还是并入信号塔「和乘幂」Tab 下？",
  "信号日志：前端环形缓冲（v1，零后端改动）还是 后端落库持久化（方案A）？",
  "主题：深色交易终端（推荐）还是 跟随 hcm-web 现有浅色主题？",
  "入场模式（回踩A/突破B）：引擎当前未显式输出该字段，看板是否做「模式推断」展示，或后端补字段？",
  "是否需在看板叠加 co_source 实时对比（并行 shadow），为「和双源并行」提供观测基础？",
  "图表库：沿用现有前端组件，还是允许引入新图表库（如 Recharts）？",
]));

children.push(new Paragraph({ spacing: { before: 300 }, alignment: AlignmentType.CENTER, children: [new TextRun({ text: "— 方案待审核，审核通过后方可进入构建阶段 —", color: "888888", italics: true })] }));

const doc = new Document({
  styles: {
    default: { document: { run: { font: "Arial", size: 21 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true, run: { size: 30, bold: true, font: "Arial", color: "1F3864" }, paragraph: { spacing: { before: 240, after: 120 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true, run: { size: 25, bold: true, font: "Arial", color: "2E5496" }, paragraph: { spacing: { before: 160, after: 80 }, outlineLevel: 1 } },
    ],
  },
  numbering: {
    config: [
      { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
      { reference: "numbers", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
    ],
  },
  sections: [{
    properties: { page: { size: { width: A4.width, height: A4.height }, margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN } } },
    headers: { default: new Header({ children: [new Paragraph({ alignment: AlignmentType.RIGHT, children: [new TextRun({ text: "和乘幂信号数据看板 · 设计方案", size: 16, color: "999999" })] })] }) },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: "第 ", size: 16, color: "999999" }), new TextRun({ children: [PageNumber.CURRENT], size: 16, color: "999999" }), new TextRun({ text: " 页", size: 16, color: "999999" })] })] }) },
    children,
  }],
});

Packer.toBuffer(doc).then((buf) => {
  const out = "D:/HCM_ASST/docs/和乘幂信号数据看板设计方案.docx";
  fs.writeFileSync(out, buf);
  console.log("OK written:", out, buf.length, "bytes");
});
