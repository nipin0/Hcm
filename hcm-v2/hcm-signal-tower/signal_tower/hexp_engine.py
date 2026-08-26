"""和乘幂信号引擎 (Hexp / Harmonic Power-Mean Engine) — 独立信号源.

依据《和乘幂信号策略开发文档》实现：
  - 核心算法：广义均值 HP-Score = (Σ w_i·|f_i|^k)^(1/k) × sign(Σ w_i·f_i)
  - 幂指数 k 自适应：趋势市凸增强(k>1)、震荡市凹收敛(k<1)、转换中中性(k=1)
  - M5 主执行周期（量化交易），H1/H4 迟滞状态机，D1 轻量方向
  - 新信号源：微结构动量幂律信号 MM(t)=Σ(i+1)^(-α)·r(t-i)（M1 级）
  - 多周期共振矩阵 + 6 维评分卡 + S/A/B/C/红灯分级
  - 双模式入场（回踩/突破）元数据 + 动态仓位/跟踪止损参数

禁硬编码：所有参数经 config_provider 读取（hexp.* 命名空间），缺省回退 _DEFAULTS。
与 co_source 完全独立：不读写 co.*/scoring.* 键，通过 signal.active_model='hexp' 激活。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any, Awaitable, Callable, Optional

import numpy as np

from .scoring_engine import ScoreResult

logger = logging.getLogger(__name__)

# ── 和乘幂配置默认值（与 web/api/hexp.py HEXP_KEYS 白名单完全对齐）──
# 引擎只读 hexp.* 键；未命中配置中心时回退此表（零硬编码：逻辑内不出现字面量参数）。
_DEFAULTS: dict[str, Any] = {
    # 总开关与周期
    "hexp.enabled": True,
    "hexp.periods": "M5,M30,H1,H4,D1",
    "hexp.period_minutes": "M1=1,M5=5,M15=15,M30=30,H1=60,H2=120,H4=240,D1=1440",
    "hexp.primary_period": "M5",
    # 配置热重载节流（秒）：produce() 惰性自愈刷新的最大陈旧时长（BUG-16）。
    # 正常由 scheduler._config_reload_loop 每 30s 显式调 load_config() 强制刷新；
    # 本键仅作调度器未接线（回测/单测/独立调用）时的兜底上限。
    "hexp.config_reload_interval": 60,
    # 和乘幂核心
    "hexp.k.base": 1.5,
    "hexp.k.min": 0.5,
    "hexp.k.max": 3.0,
    "hexp.k.alpha": 0.8,
    "hexp.k.beta": 0.4,
    # 以下 state_* 键已废弃：文档 2.2 定义 k_base 为恒定常数，状态自适应由单一公式承载，
    # 引擎不再读取这些「状态专属 k_base」（见 produce() 第 4 步）。保留仅为配置中心/前端兼容。
    "hexp.k.state_trend": 2.0,
    "hexp.k.state_range": 0.65,
    "hexp.k.state_transition": 1.0,
    "hexp.k.state_fade": 2.5,
    # 六因子基础权重（运行时归一）
    "hexp.factor.adx_weight": 25.0,
    "hexp.factor.er_weight": 25.0,
    "hexp.factor.ma_weight": 20.0,
    "hexp.factor.bbw_weight": 15.0,
    "hexp.factor.hurst_weight": 10.0,
    "hexp.factor.rsi_weight": 5.0,
    "hexp.factor.mm_weight": 15.0,
    # 体制感知因子权重方案（BUG-3 修复）：三套 7 因子权重（adx/er/ma/bbw/hurst/rsi/mm），
    # 运行时按 M5 regime_result 体制 + 强度在方案间连续混合。缺省回退旧固定权重基线。
    "hexp.factor_weights_json": json.dumps({
        # BUG-2 修复(2026-08-13)：trend 方案 rsi 0.0→8.0 —— RSI 是 7 因子中唯一的
        # 超买超卖反转感知因子，趋势市权重清零使"高多低空"在趋势体制下毫无制衡。
        # 数值与 PG 生产值/parse-fallback 对齐(26/21/14/11)。
        "trend":   {"adx": 28.0, "er": 26.0, "ma": 21.0, "bbw": 3.0,  "hurst": 14.0, "rsi": 8.0, "mm": 11.0},
        "range":   {"adx": 9.0,  "er": 9.0,  "ma": 9.0,  "bbw": 26.0, "hurst": 9.0,  "rsi": 22.0, "mm": 16.0},
        "neutral": {"adx": 25.0, "er": 25.0, "ma": 20.0, "bbw": 15.0, "hurst": 10.0, "rsi": 5.0, "mm": 15.0},
    }),
    # 因子参数
    "hexp.adx.min": 15.0,
    "hexp.adx.max": 35.0,
    "hexp.er.period": 20,
    "hexp.er.min": 0.10,
    "hexp.er.max": 0.40,
    "hexp.ma.ema_fast": 20,
    "hexp.ma.ema_mid": 50,
    "hexp.ma.ema_long": 100,
    # 【2026-08-25 MA 因子失真修复】align_score 50→30。原 50 占满 0~100 一半，
    # 使「EMA 排列(±50) + 斜率(±40)」在方向同向时立即饱和(多头锁100/空头锁0)，
    # MA 因子退化成方向开关（历史 87.7% 锁死在 ±1 两档）。降为 30 后：
    #   多头排列 ma_raw∈[40,100]、空头∈[0,60]，未锁宽度 60/100，
    #   能区分「刚启动/强趋势/转弱」，恢复斜率项连续表达能力，保住趋势启动捕捉。
    "hexp.ma.align_score": 30.0,
    "hexp.ma.slope_norm_bp": 3.0,  # EMA 回归斜率达到±40贡献上限所需的bp/根（文档4.1:斜率贡献=±40）。该常数为bp/bar→分数换算的饱和点，文档未给出但必需，须补进文档4.1。
    "hexp.ma.slope_bars": 10,
    "hexp.bbw.window": 120,
    "hexp.bbw.boll_period": 20,
    "hexp.bbw.boll_std": 2.0,
    "hexp.hurst.min": 0.40,
    "hexp.hurst.max": 0.60,
    "hexp.hurst.max_lag": 32,
    # 状态机（迟滞）
    "hexp.state.enter_score": 60.0,
    "hexp.state.exit_score": 40.0,
    "hexp.state.confirm_bars": 1,
    # MTF 迟滞机动量翻转（2026-08-10 方案 A：消除高周期滞后，让裁决"跟手"）
    # 当前处于确定性趋势态(TREND_UP/DOWN)时，若近 flip_window 根该周期收盘斜率
    # 决定性反向(斜率方向强度 >= flip_slope_mult)持续 flip_bars 次 update → 立即翻向，
    # 不等 trend_score 跌破 exit(40)。N=flip_window, K=flip_bars。
    "hexp.mtf.flip_enabled": True,
    "hexp.mtf.flip_window": 8,        # N：算近 N 根该周期收盘斜率
    "hexp.mtf.flip_bars": 2,          # K：决定性反向持续 K 次 update 即翻向
    "hexp.mtf.flip_slope_mult": 1.0,  # 斜率方向强度阈值(净位移/波动包络 >= 此即"决定性")
    # 微结构动量（幂律）
    "hexp.mm.alpha": 0.5,
    "hexp.mm.window": 20,
    "hexp.mm.period": "M1",
    "hexp.mm.scale": 0.002,
    "hexp.mm.accel_k_boost": 0.3,
    "hexp.mm.accel_threshold": 0.7,
    # 共振矩阵
    "hexp.mtf.weight_D1": 0.25,
    "hexp.mtf.weight_H4": 0.35,
    "hexp.mtf.weight_H1": 0.25,
    "hexp.mtf.weight_M30": 0.15,
    # 方案 B 对称降分（2026-08-10）：原"顺风无脑加成(bonus) + 逆风硬封成 NO_TRADE"已改为
    # "顺/逆风同尺降分"。tailwind_bonus=顺风加成系数(默认0=完全对称，不再虚涨推闸)；
    # penalty=逆风折扣系数(硬封已移除，仅降分)。请勿再用 hexp.resonance.bonus。
    "hexp.resonance.tailwind_bonus": 0.0,
    "hexp.resonance.penalty": 0.15,
    # 已删除的弃用键（2026-08-11 清理，勿再引入）：
    #   hexp.resonance.bonus       —— 顺风无脑加成，已由 tailwind_bonus 取代
    #   hexp.mtf.long_threshold    —— verdict 硬封"只做多"铁律，方案 B 已废除硬封
    #   hexp.mtf.short_threshold   —— 同上（"只做空"铁律）
    # 三者在代码中均无读取点，留在默认表里只会让面板误以为仍在生效。
    # 评分卡
    # 2026-08-18 对齐生产 PG/Redis 值（旧默认 25/20/20 + pass45/b60 已漂移，Redis 键
    # 丢失时回退旧值会偏离生产意图）。生产倾向=降共振、升结构(state)/入场(entry)、
    # 门槛提高：resonance 25→12 / state 20→27 / entry 20→26 / pass 45→50 / b 60→52。
    "hexp.scorecard.weight_resonance": 12.0,
    "hexp.scorecard.weight_state": 27.0,
    "hexp.scorecard.weight_entry": 26.0,
    "hexp.scorecard.weight_position": 15.0,
    "hexp.scorecard.weight_vol": 10.0,
    "hexp.scorecard.weight_session": 10.0,
    "hexp.scorecard.pass_threshold": 50.0,
    "hexp.scorecard.b_threshold": 52.0,
    "hexp.scorecard.a_threshold": 75.0,
    "hexp.scorecard.s_hp_min": 60.0,
    "hexp.scorecard.hp_floor": 30.0,
    # 执行参数
    "hexp.exec.sl_atr_mult": 2.0,
    "hexp.exec.rr_min": 1.5,
    "hexp.exec.lot_mult": 1.0,
    "hexp.exec.grade_lot_s": 1.2,
    "hexp.exec.grade_lot_a": 1.0,
    "hexp.exec.grade_lot_b": 0.5,
    "hexp.exec.grade_lot_c": 0.5,
    # 反转态(_REVERSAL)减仓系数（2026-08-11 B-1 修复）：
    # 高周期"干净反转"(TREND_UP↔TREND_DOWN)会绕过 _TRANSITION 犹豫带直接翻向，
    # 导致旧 transition_lot_mult 折扣对"越干净的反转"反而失效（反转越果断手数越大）。
    # 此处用独立键（默认 0.5）专门对反转态减仓；与 transition_lot_mult 互不影响、
    # 取两者中更谨慎者，避免被当前部署的 transition_lot_mult=1.0 拉平。
    "hexp.exec.reversal_lot_mult": 0.5,
    # 反转态持有窗口（单位=主周期棒数）：反转是「态」不是「瞬时事件」。
    # 若只在翻转发生的那一次 produce() 减仓，紧随其后的信号立刻恢复满仓 —— 等于没减
    # （且 live 发布器每 3s 调一次 produce，翻转当次多半并不产出可下单信号）。
    # 自触发起持有 N 根主周期棒（默认 3；设 0 = 仅触发当次），窗口内一律减仓。
    "hexp.exec.reversal_hold_bars": 3,
    # 是否把主执行周期(M5)的方向切换也计入反转态。默认 False：M5 方向切换过于频繁，
    # 计入会让减仓常驻（等价于直接把 lot_mult 砍半），且共振矩阵同样排除主周期。
    "hexp.exec.reversal_include_primary": False,
    "hexp.exec.transition_lot_mult": 0.5,
    # 极值区手数递减（2026-08-21 方案2）：处于 Donchian 极值区(_in_extreme)且未被护栏封单的
    # 放行单，按此折扣降仓（与 transition/reversal 用 min() 聚合，不叠加重复打折）。
    # 解决「高位动量减弱反转仍满档手数」——没拦住也减半仓。
    "hexp.exec.extreme_lot_mult": 0.5,
    # 行情波动率缩放手数（2026-08-21 方案2·链动风控面板基础手数按行情调整）：
    # co_exec_lot_mult 经 suggested_lot_ratio 透传到风控 final_lot = risk.lot_base × 档位 × co_ai_mult，
    # 故在此把 ATR 相对常态的缩放编入 co_exec_lot_mult，即可让「风控面板基础手数」随行情缩放。
    "hexp.exec.vol_scale_enabled": True,      # 总开关；False→不缩放(恒1.0)，向后兼容
    "hexp.exec.vol_scale_atr_ref": 7.0,       # 常态 ATR 基准（波动率中性点）
    "hexp.exec.vol_scale_min": 0.5,           # 波动放大时最低缩到 0.5（防满仓接大波动）
    "hexp.exec.vol_scale_max": 1.0,           # 波动收窄时最高 1.0（绝不超配基础手数）
    # 方向判定
    "hexp.direction_min_score": 0.20,
    # 位置因子（BUG-1 修复 2026-08-13）：Donchian 分位 → 均值回归方向因子参与方向裁决。
    # f_pos=(0.5-pos_pct)*2 ∈[-1,+1]：底部→正(托BUY)、顶部→负(压SELL)、中部≈0 无影响。
    # weight 为 dir_sum 的绝对权重（7 因子归一后和为 1，0.15≈单个大因子权重）。
    "hexp.pos_factor.enabled": True,
    "hexp.pos_factor.weight": 0.15,
    # 精确信号闸门（2026-08-10）：最低可下单分级 S/A/B/C。
    # 低于此级的信号仍完整落库供观测/影子对照，但不产生交易方向（direction=NO_TRADE），
    # 根治「C 级(45~60分)低质量信号占比 75% 全部下单」导致的信号泛滥。
    "hexp.min_grade": "C",
    # B' 延伸（2026-08-17）：逆风/回踩单综合评分(total)额外下压系数。
    # 1.0 = 不额外罚（维持既有仅折扣 hp_100 行为，向后兼容）；
    # <1.0 = 逆风时 total 乘性下压，使 grade 更难达 min_grade 闸门——仅降分不硬封，
    # 分数够高仍可能过闸（保留"顺势回踩单若 M5 评分足够高仍可过闸门"语义）。
    "hexp.resonance.pullback_penalty": 1.0,
    # 极值动量感知闸门（2026-08-13 升级·替换无条件硬封）：
    # 价格相对 Donchian 通道分位触及极值时，仅当「微动量 f_mm 回撤」才封 NO_TRADE；
    # mm 仍朝原方向则允许极值追单（可能扫损，由减少止损方案另议，本次未做）。
    "hexp.extreme.high_pct": 0.85,
    "hexp.extreme.low_pct": 0.15,
    "hexp.extreme.donchian_look": 20,
    # 极值区封单条件：仅当 mm 动量相对方向的同向分量 < mm_retreat_min 才封。
    "hexp.extreme.mm_retreat_enabled": True,
    # BUG-3 修复(2026-08-13)：0.05→0.20。M1 幂律动量带权重衰减，极值回落初期仍微正
    # (>0.05 即放行 → 实测放行率 40%)，0.20 要求动量"明显同向"才准极值追单。
    "hexp.extreme.mm_retreat_min": 0.20,
    # 回踩支撑位诊断（B）：回看 support_lookback 棒取最近摆动低(BUY)/高(SELL)，
    # 现价落在其 ±support_atr×ATR 内 → 标记 extreme_support_pullback（价格已回踩到位，
    # 方向由 7 因子在非极值区自然重裁），仅作观测/日志，不强制改方向。
    "hexp.extreme.support_lookback": 20,
    "hexp.extreme.support_atr": 1.0,
    # 极值追单收紧 SL 距离（C）：extreme_chase 时 SL 的 ATR 倍数 ×此值（默认 0.7），TP 不变→R:R 改善
    "hexp.extreme.chase_sl_mult": 0.7,
    # 极值识别·k 补充阈值（2026-08-21 修复·方案B）：_in_extreme 用 k(由 ADX/bbw 驱动,
    # 无方向性, 可>1.5) 作 _pos_pct 的 OR 补充，捕捉"单边行情拉宽通道后价格仍创极值"的场景。
    # 但 k 补充必须【双条件且 pos 已接近极值侧】：k>k_extreme 且 pos>k_pos_high(BUY)/
    # pos<k_pos_low(SELL) 才视为真极值追单——避免剧烈行情下通道中部(pos 0.3~0.7)正常回调
    # 被 k>阈值误判为极值而批量拦截卡死(实锤: 01:58-59 连续 pos=0.57~0.64 被 extreme_guard 拦)。
    "hexp.extreme.k_extreme": 1.8,
    "hexp.extreme.k_pos_high": 0.7,
    "hexp.extreme.k_pos_low": 0.3,
    # 极值反转护栏（2026-08-18）：顶/底极值区 + 动量减弱且反向 + 长影线 → 拦原趋势延续单
    # （顶部只拦 BUY 续涨追顶、底部只拦 SELL 续跌追底）。阈值与 LightGBM extreme_reversal
    # 特征同源（训练侧硬编码默认，推理侧经此热调）。
    "hexp.extreme.reversal_enabled": True,   # 总开关；False→秒级退回无此护栏（仅保留旧 extreme_guard 兜底）
    "hexp.extreme.wick_min": 0.60,           # 长影线阈值：上/下影占全幅比 ≥ 此值视为长影线
    "hexp.extreme.reversal_sl_atr_mult": 0.5,  # 极值区放行单的 SL 收紧倍数（< 常规 sl_atr_mult）
    # 极值护栏自动开/关（2026-08-19）：auto_mode=auto 时按 M5 regime 自动推导 _rev_enabled。
    #   off  = 沿用人工开关 reversal_enabled（默认，行为与历史一致）
    #   auto = regime ∈ auto_on_regimes → 开；∈ auto_off_regimes → 关；其余回退 manual
    "hexp.extreme.auto_mode": "off",
    "hexp.extreme.auto_on_regimes": "RANGE,NEUTRAL",       # 震荡/极值行情 → 自动开启硬封
    "hexp.extreme.auto_off_regimes": "TREND,PRE_TREND",    # 趋势确认 → 自动关闭硬封
    # 动量枯竭保护（2026-08-21）：极值护栏盲区补充——_in_extreme 只认 pos>0.85 或
    # k>k_extreme(热值2.3)，当价格在宽通道内 pos≈0.7~0.8 且 k 未超阈时，高位(pos) +
    # 趋势质量差(er 低) + 微动量枯竭(mm 近 0/反向) 的追单会被裸放行（实锤: 388639521
    # BUY@4538 pos=0.77 er=0.18 mm=0.03 浮亏）。此分支独立于 _in_extreme，专门拦截
    # 「高位接刀 + 动能枯竭」的趋势延续单；真趋势延续 er 通常>0.22 不受影响。
    "hexp.momentum_drain_enabled": True,   # 总开关；False→退回旧行为（仅极值护栏）
    "hexp.momentum_drain_hi": 0.65,        # 高位判据：BUY pos>此 / SELL pos<(1-此)
    "hexp.momentum_drain_er": 0.20,        # 效率比(er)枯竭阈值：<此视为趋势质量差
    "hexp.momentum_drain_mm": 0.15,        # 微动量(mm)枯竭阈值：mm_aligned<此视为动能枯竭
    # 动量方向否决（2026-08-21）：动量明确反向时拦逆动量单（BUY 而 mm<0、SELL 而 mm>0），
    # 不依赖 pos/趋势结构，让"动量转负即不再追多/追空"（比 momentum_drain 更敏捷的硬护栏）。
    "hexp.momentum_flip_enabled": True,    # 总开关；False→退回旧行为
    # 反向阈值与 web HEXP_KEYS / 读取点 fallback 统一为 0.04（2026-08-21 修复值漂移：
    # 此前 _DEFAULTS=0.02 与 web 白名单/引擎 cfg.get fallback=0.04 不一致，未 seed 的
    # 环境引擎用 0.02 而面板显示 0.04，属「保存刷新又复原」一类根因）。
    "hexp.momentum_flip_mm": 0.04,         # 反向阈值：|f_mm|≥此视为动量明确反转（防小回调误杀）
    # 【2026-08-26 高位分级加固】momentum_flip 只拦"明确反向"(mm<-flip_mm)，漏掉
    # ma 高位 + mm 微负(0>mm>-flip_mm)的顶部追多（实证 sid=388640542 BUY@4670.95，
    # ma=97.23 且 mm=-0.0066 仍成交）。现按 ma 多头度动态收紧阈值：多头度≥高位分界时
    # 用更严的 flip_ma_mm（微负即拦），正常位置仍用基础 flip_mm（明确反向才拦，防误杀）。
    "hexp.momentum_flip_ma_threshold": 90.0,   # 高位分界：BUY 时 _ma_raw≥此视为极高多头位
    "hexp.momentum_flip_ma_low": 10.0,          # 低位分界：SELL 时 _ma_raw≤此视为极高空头位
    "hexp.momentum_flip_ma_mm": 0.005,          # 极端位下更严的反向阈值（|mm|≥此即拦逆动量单）
    # 【2026-08-26 P0-1 高位微正枯竭加固】momentum_flip 只拦"mm 反向(负)"，漏掉
    # ma 极高位 + mm 微正枯竭(0<mm<弱阈值)的顶部追多（实证 sig=388640598 BUY@4666.08
    # ma=100 pos=0.846 mm=+0.0124 regime=NEUTRAL 高位追多被止损）。现扩展：多头度≥高位
    # 分界且 mm 为正但 <weak_mm（动能枯竭未反转）时同样拦 BUY 追高（SELL 低位对称）。
    # 仅拦"高位+微正枯竭"，正常位置/动量明显同向不拦，防误杀。
    "hexp.momentum_hi_weak_enabled": True,     # 高位微正枯竭拦截总开关；False→退回旧行为(仅拦反向)
    "hexp.momentum_hi_weak_mm": 0.02,          # 高位微正枯竭阈值：BUY 时 0<mm<此 / SELL 时 0>mm>-此 → 拦
    # 【2026-08-26 P0-2 震荡市均值回归校验】NEUTRAL/RANGE 市(hurst<0.5 均值回归态)中，
    # 高位(Donchian 分位接近极值)顺势追单易被均值回归反打（实证 sig=388640598 BUY@4666.08
    # pos=0.846 hurst=0.449 regime=NEUTRAL 高位追多被止损）。震荡市应等回撤/极值反向，
    # 不应在高低位顺势追单。本闸门：NEUTRAL/RANGE + hurst<hurst_max + pos 高位追高(BUY)/低位追低(SELL) → 拦。
    "hexp.range_hurst_enabled": True,          # 震荡市 hurst 均值回归校验总开关；False→关闭(向后兼容)
    "hexp.range_hurst_max": 0.50,              # hurst 均值回归阈值：<此视为均值回归态(反持续)
    "hexp.range_hurst_hi": 0.80,               # 高位分界：BUY pos>此 / SELL pos<(1-此) 视为极值区追单
    "hexp.range_hurst_regimes": "NEUTRAL,RANGE",  # 启用本校验的体制（逗号分隔）
    # 反向单观测（2026-08-21，先观测不下单）：momentum_flip 判动量反向且处于高位/低位时，
    # 记录反向候选(dir/pos/er/mm/close/verdict) 落库到 indicator_values._hexp.reverse_candidate，
    # 供后续 SQL 对照未来 K 线评估"若做反向单的胜率"，验证后再启用真下单。零实盘影响。
    "hexp.reverse_candidate_enabled": True,   # 观测开关；False→不记录反向候选
    "hexp.reverse_candidate_hi": 0.7,          # BUY被拦→SELL候选 的高位分位阈值(pos>此)
    "hexp.reverse_candidate_lo": 0.3,          # SELL被拦→BUY候选 的低位分位阈值(pos<此)
}

# 分级序：用于 hexp.min_grade 门槛比较（RED 恒不可交易）
_GRADE_RANK: dict[str, int] = {"RED": 0, "C": 1, "B": 2, "A": 3, "S": 4}

# 状态机状态常量
_RANGE = "RANGE"
_TREND_UP = "TREND_UP"
_TREND_DOWN = "TREND_DOWN"
_TRANSITION = "TRANSITION"
# 反转态（2026-08-11 B-1）：不是迟滞状态机的第五种 state，而是叠加在周期态之上的
# 「事件态」标签 —— 该周期确定性趋势态发生 TREND_UP↔TREND_DOWN 切换后的一段窗口。
# 刻意不写进 period_states（否则会污染共振裁决，把该周期的 ±1 票变成 0 票），
# 只用于 produce() 第 8 步的减仓聚合与观测输出。
_REVERSAL = "REVERSAL"

# 周期分钟映射兜底（解析 hexp.period_minutes 失败时使用）
_MINUTES_FALLBACK = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H2": 120, "H4": 240, "D1": 1440}


class _HysteresisState:
    """单周期迟滞状态机（进入 60 / 退出 40，确认根数防抖）。

    2026-08-10 方案 A：附加「动量翻转」覆盖路径——当前处于确定性趋势态时，
    若高周期近 N 根收盘斜率决定性反向持续 K 次 update，立即翻向，不等
    trend_score 跌破 exit(40)。消除黄金转跌时 SELL 被饿死、转折滞后数小时的缺陷。
    斜率判定沿用 H1RegimeClassifier._recent_trend_state 的成熟口径
    (slope=末-首，vol=均值|逐棒差|，|slope| > mult×vol 即决定性)。
    """

    def __init__(self) -> None:
        self.state: str = _RANGE
        self.pending: str = ""
        self.confirm_count: int = 0
        self._flip_count: int = 0
        # B-1 修复（2026-08-11）：本次 update 是否发生"动量翻转"式反转。
        # 仅在动量翻转块内置 True，每次 update 入口重置，仅反映最近一次 update 的结果。
        self.reversed: bool = False

    @staticmethod
    def _slope_dir(closes: Any, n: int, mult: float) -> int:
        """近 n 根该周期收盘的「决定性」方向：1=决定性上行，-1=决定性下行，0=无/噪音。

        与 h1_regime_classifier._recent_trend_state 同口径：slope=seg[-1]-seg[0]，
        vol=均值(|逐棒差|)，仅当 |slope| > mult×vol（真实趋势性移动而非噪音）才判方向。
        """
        try:
            arr = np.asarray(closes, dtype=np.float64)
        except Exception:
            return 0
        if arr.size < n + 1:
            return 0
        seg = arr[-(n + 1):]
        slope = float(seg[-1] - seg[0])
        diffs = np.abs(np.diff(seg))
        vol = float(np.mean(diffs)) if diffs.size else 0.0
        if vol <= 1e-12:
            return 0
        if slope > mult * vol:
            return 1
        if slope < -mult * vol:
            return -1
        return 0

    def update(self, trend_score: float, direction: int, enter: float, exit_: float,
               confirm_bars: int, closes: Any = None, label: str = "",
               flip_enabled: bool = True, flip_window: int = 8, flip_bars: int = 2,
               flip_slope_mult: float = 1.0    ) -> str:
        # B-1 修复（2026-08-11）：每次 update 入口重置反转标记，仅反映本次调用是否发生动量翻转。
        self.reversed = False
        # ── 动量翻转（消除高周期迟滞滞后）──
        # 当前处于确定性趋势态，但近 flip_window 根该周期收盘斜率决定性反向
        # 持续 flip_bars 次 update → 立即翻向，绕过 enter/exit 迟滞阈值。
        if (flip_enabled and self.state in (_TREND_UP, _TREND_DOWN)
                and closes is not None and flip_bars >= 1 and flip_window >= 1):
            sdir = self._slope_dir(closes, flip_window, flip_slope_mult)
            opposite = (self.state == _TREND_UP and sdir == -1) or \
                       (self.state == _TREND_DOWN and sdir == 1)
            if opposite:
                self._flip_count += 1
            else:
                self._flip_count = 0
            if self._flip_count >= flip_bars:
                forced = _TREND_DOWN if self.state == _TREND_UP else _TREND_UP
                logger.info(
                    "hexp MTF momentum flip %s: %s→%s slope_dir=%d flip_bars=%d mult=%.2f",
                    label, self.state, forced, sdir, flip_bars, flip_slope_mult)
                self.state = forced
                self.pending = ""
                self.confirm_count = 0
                self._flip_count = 0
                # 干净反转：标记本次 update 为"动量翻转反转"，供 produce() 聚合减仓。
                self.reversed = True
                return self.state
        else:
            self._flip_count = 0

        target: Optional[str] = None
        if trend_score >= enter and direction != 0:
            target = _TREND_UP if direction > 0 else _TREND_DOWN
        elif trend_score < exit_:
            target = _RANGE
        elif self.state in (_TREND_UP, _TREND_DOWN):
            target = _TRANSITION
        # 未达任何迁移条件 → 保持原状态（迟滞缓冲）
        if target is None or target == self.state:
            self.pending = ""
            self.confirm_count = 0
            return self.state
        # 迟滞确认：需连续 confirm_bars 根指向同一目标才切换
        if target == self.pending:
            self.confirm_count += 1
        else:
            self.pending = target
            self.confirm_count = 1
        if self.confirm_count >= confirm_bars:
            self.state = target
            self.pending = ""
            self.confirm_count = 0
        return self.state


class HexpEngine:
    """和乘幂信号引擎。compute_pre_score 为异步（需拉取多周期 K 线）。

    注入：
      config_provider: 配置中心（hexp.* 读取，PG↔Redis 双写热读）
      kline_fetcher:   async callable(symbol, timeframe, limit) -> list[dict]
                       （复用 scheduler._fetch_klines，含 Redis 实时 bar 合并）
      redis_client:    可选；发布 hcm:live:hexp:{symbol} 实时快照（TTL15s）
    """

    def __init__(
        self,
        config_provider: Any = None,
        kline_fetcher: Optional[Callable[[str, str, int], Awaitable[list]]] = None,
        redis_client: Any = None,
    ) -> None:
        self._config = config_provider
        self._fetch = kline_fetcher
        self._redis = redis_client
        self._sm: dict[str, _HysteresisState] = {}  # 每周期独立迟滞状态机
        # ── 反转态(_REVERSAL)跟踪（2026-08-11 B-1）──
        # key = f"{symbol}:{period}"。_prev_period_state 只记录「确定性趋势态」
        # (TREND_UP/TREND_DOWN)，因此 UP→RANGE→DOWN 这类经中间态的翻转同样能识别。
        self._prev_period_state: dict[str, str] = {}
        self._reversal_until: dict[str, float] = {}   # 反转态到期时间戳（epoch 秒）
        self._reversal_src: dict[str, str] = {}       # 触发来源：momentum / hysteresis
        # ── 配置热重载快照（BUG-16）──
        # 未加载前用 _DEFAULTS 副本兜底；load_config() 成功后整体换引用（原子，
        # 避免 produce() 逐键 await 期间被改配置而读到"半新半旧"的撕裂快照）。
        self._cfg: dict[str, Any] = dict(_DEFAULTS)
        self._cfg_loaded: bool = False
        # B' 延伸（2026-08-17）：逆风综合分折扣中间态，默认 1.0（不罚），produce() 内按需覆写。
        self._pending_pullback_mult: float = 1.0
        self._cfg_loaded_at: float = 0.0
        self._cfg_lock = asyncio.Lock()  # 并发去重：symbol_loop 与 live 发布器共用引擎

    # ─────────────────────── 配置读取（零硬编码）───────────────────────
    async def _get(self, key: str) -> Any:
        default = _DEFAULTS.get(key)
        if self._config is None:
            return default
        try:
            # 2026-08-21 修复「保存刷新又复原」根因：优先读取用户显式保存的
            # current_value（get_current 不做 PG default_value 兜底）。早期 seed
            # 的 PG default_value（如 pass=45/b=60/resonance=25）已与代码 _DEFAULTS
            # 漂移，若沿用 get() 的 COALESCE 会把这些陈旧 seed 默认当成生效值，
            # 引擎实际参数与代码意图不符。未显式保存的键回退本表 _DEFAULTS。
            raw = await self._config.get_current(key)
            if raw is None:
                # get_current 仅在当前实现存在时可用；兜底走原 get()（行为不变）
                raw = await self._config.get(key)
        except Exception:
            raw = None
        if raw is None:
            return default
        # 配置中心按字符串存；按默认值类型做兜底转换
        try:
            if isinstance(default, bool):
                return str(raw).strip().lower() in ("true", "1", "yes")
            if isinstance(default, int) and not isinstance(default, bool):
                return int(float(raw))
            if isinstance(default, float):
                return float(raw)
            # 【2026-08-25 配置污染修复】所有字符串配置统一 strip，杜绝跨平台
            # 换行符污染（CRLF 混入值尾，如 hexp.mm.period 曾被存成 'M1\r'）导致
            # time_frame 匹配不到 klines 数据、MM 因子恒 0 的隐藏卡点。
            return str(raw).strip()
        except (TypeError, ValueError):
            return default

    # ─────────────────────── 配置热重载（BUG-16 修复）───────────────────────
    async def load_config(self) -> None:
        """显式热重载入口：一次性快照全部 ``hexp.*`` 键。

        由 ``scheduler.load_config()`` 在 30s 热重载循环中调用（与 scoring_engine /
        co_source / micro_state 同构），使面板"保存即热生效"，无需重启信号塔。

        fail-safe：任何异常都不外抛、不清空既有快照——保留上一次成功值继续交易，
        绝不因配置中心抖动让 hexp 退化为默认参数或让调度器循环崩溃。
        """
        async with self._cfg_lock:
            await self._reload_locked()

    async def _reload_locked(self) -> None:
        """在 _cfg_lock 保护下重建配置快照（调用方须已持锁）。"""
        try:
            snapshot = {k: await self._get(k) for k in _DEFAULTS}
        except Exception as exc:
            # 保留上次成功快照；刷新时间戳以节流重试，避免持续故障时每轮 produce 都重试
            self._cfg_loaded_at = time.time()
            logger.warning("HexpEngine config reload failed, keep previous snapshot: %s", exc)
            return

        prev = self._cfg if self._cfg_loaded else None
        self._cfg = snapshot
        self._cfg_loaded = True
        self._cfg_loaded_at = time.time()

        # 可观测：首次加载打印生效值；后续仅在真正变化时打印差异（避免每 30s 刷屏）
        if prev is None:
            logger.info(
                "HexpEngine config loaded | enabled=%s periods=%s primary=%s "
                "k.base=%s k.alpha=%s k.beta=%s reload_interval=%ss keys=%d",
                snapshot.get("hexp.enabled"), snapshot.get("hexp.periods"),
                snapshot.get("hexp.primary_period"), snapshot.get("hexp.k.base"),
                snapshot.get("hexp.k.alpha"), snapshot.get("hexp.k.beta"),
                snapshot.get("hexp.config_reload_interval"), len(snapshot),
            )
        else:
            changed = [f"{k}: {prev.get(k)}→{v}" for k, v in snapshot.items() if prev.get(k) != v]
            if changed:
                logger.info("HexpEngine config hot-reloaded | %s", "; ".join(changed))

    async def _load(self) -> dict[str, Any]:
        """返回当前配置快照（热路径）。

        BUG-16 前：每次 produce() 都对 71 个键逐一 ``await config.get()``；未 seed 的键
        在 config_provider 中不做负缓存（L1 未命中 → 2×Redis hget → 1×PG 查询 → 返回
        None 且不写缓存），而 ``_live_score_publisher`` 每 3s 调一次 produce()，
        导致热路径持续打满 Redis/PG，且逐键 await 期间改配置会读到撕裂快照。

        现在：读快照（O(1)）。快照由 scheduler 30s 循环显式刷新；若调度器未接线，
        这里按 ``hexp.config_reload_interval`` 惰性自愈刷新，保证任何调用方都热生效。
        """
        try:
            interval = float(self._cfg.get("hexp.config_reload_interval") or 0.0)
        except (TypeError, ValueError):
            interval = 0.0
        if interval <= 0:
            interval = float(_DEFAULTS["hexp.config_reload_interval"])

        if self._cfg_loaded and (time.time() - self._cfg_loaded_at) < interval:
            return self._cfg

        async with self._cfg_lock:
            # 双检：并发协程只让一个真正刷新，其余直接复用新快照
            if self._cfg_loaded and (time.time() - self._cfg_loaded_at) < interval:
                return self._cfg
            await self._reload_locked()
        return self._cfg

    # ─────────────────────── 体制感知因子权重（BUG-3 修复）───────────────────────
    # 7 因子键（与 _factors 返回一致）
    _FACTORS = ("adx", "er", "ma", "bbw", "hurst", "rsi", "mm")

    async def _load_weight_schemes(self, cfg: dict[str, Any]) -> dict[str, dict[str, float]]:
        """读取体制感知权重方案（hexp.factor_weights_json）。

        缺失/解析失败 → 回退到固定权重基线构成的 neutral 方案（向后兼容旧行为）。
        每个方案必须含全部 7 因子；缺失因子用 neutral 基线补齐。
        """
        fallback = {
            # 2026-08-13 修复：trend 方案 rsi 权重 0 → 8。原 rsi=0 使 hexp 在趋势市完全
            # 无视超买/超卖，纯动量追逐在极值点追单(高位做多/低位做空)。保留轻微 RSI
            # 反向拉力，不破坏顺趋势主逻辑，仅遏制「极值处仍追原方向」。
            "trend":   {"adx": 28.0, "er": 26.0, "ma": 21.0, "bbw": 3.0,  "hurst": 14.0, "rsi": 8.0,  "mm": 11.0},
            "range":   {"adx": 9.0,  "er": 9.0,  "ma": 9.0,  "bbw": 26.0, "hurst": 9.0,  "rsi": 22.0, "mm": 16.0},
            "neutral": {"adx": cfg.get("hexp.factor.adx_weight", 25.0),
                        "er": cfg.get("hexp.factor.er_weight", 25.0),
                        "ma": cfg.get("hexp.factor.ma_weight", 20.0),
                        "bbw": cfg.get("hexp.factor.bbw_weight", 15.0),
                        "hurst": cfg.get("hexp.factor.hurst_weight", 10.0),
                        "rsi": cfg.get("hexp.factor.rsi_weight", 5.0),
                        "mm": cfg.get("hexp.factor.mm_weight", 15.0)},
        }
        raw = cfg.get("hexp.factor_weights_json")
        if not raw:
            return fallback
        try:
            if isinstance(raw, dict):
                parsed = raw
            elif isinstance(raw, str):
                parsed = json.loads(raw)
            else:
                return fallback
            for arch in ("trend", "range", "neutral"):
                if arch not in parsed or not isinstance(parsed.get(arch), dict):
                    parsed[arch] = dict(fallback[arch])
                for f in self._FACTORS:
                    if f not in parsed[arch] or not isinstance(parsed[arch].get(f), (int, float)):
                        parsed[arch][f] = fallback[arch].get(f, 0.0)
            return parsed
        except Exception as exc:
            logger.warning("hexp weight schemes parse failed, fallback neutral: %s", exc)
            return fallback

    async def _select_factor_weights(self, regime_result: Any, cfg: dict[str, Any]) -> dict[str, float]:
        """依 M5 体制 + 强度在趋势/震荡/中性三方案间连续混合，返回 7 因子归一化权重。

        选择逻辑：
          - regime ∈ {TREND, PRE_TREND, TREND_FADE} → 目标=trend（偏重趋势跟随因子）
          - regime == RANGE                          → 目标=range（偏重均值回归因子）
          - 其它/NEUTRAL/未知                         → 目标=neutral（固定权重基线）
        强度 strength∈[0,1] 作混合系数：strength 越高越偏目标方案，降低体制切换时权重跳变。
        regime_result 不可用 → 纯 neutral（旧行为）。
        """
        schemes = await self._load_weight_schemes(cfg)
        neutral = schemes["neutral"]
        regime = getattr(regime_result, "regime", None)
        rval = str(regime) if regime is not None else ""
        if rval in ("TREND", "PRE_TREND", "TREND_FADE"):
            target = schemes["trend"]
        elif rval == "RANGE":
            target = schemes["range"]
        else:
            target = neutral
        strength = float(getattr(regime_result, "strength", 0.0) or 0.0)
        strength = max(0.0, min(1.0, strength))
        blended = {
            k: (1.0 - strength) * neutral.get(k, 0.0) + strength * target.get(k, 0.0)
            for k in self._FACTORS
        }
        # 归一化，避免某方案权重和不一时漂移
        tot = max(sum(blended.values()), 1e-9)
        return {k: v / tot for k, v in blended.items()}

    # ─────────────────────── 因子计算（纯函数）───────────────────────
    @staticmethod
    def _ema(arr: np.ndarray, period: int) -> np.ndarray:
        if len(arr) == 0:
            return arr
        alpha = 2.0 / (period + 1.0)
        out = np.empty_like(arr, dtype=np.float64)
        out[0] = arr[0]
        for i in range(1, len(arr)):
            out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
        return out

    @staticmethod
    def _er(closes: np.ndarray, period: int) -> float:
        if len(closes) < period + 1:
            return 0.0
        change = abs(closes[-1] - closes[-1 - period])
        path = float(np.sum(np.abs(np.diff(closes[-1 - period:]))))
        return float(change / path) if path > 0 else 0.0

    @staticmethod
    def _hurst(closes: np.ndarray, max_lag: int) -> float:
        """简化 R/S：Var(log(P_t/P_{t-τ})) ~ τ^(2H)，回归 log(var)~log(τ) 斜率/2。"""
        if len(closes) < max_lag * 2 + 8:
            return 0.5
        logp = np.log(np.maximum(closes, 1e-12))
        taus, vars_ = [], []
        for tau in (2, 4, 8, 16, max_lag):
            if tau >= len(logp):
                continue
            diffs = logp[tau:] - logp[:-tau]
            v = float(np.var(diffs))
            if v > 0:
                taus.append(math.log(tau))
                vars_.append(math.log(v))
        if len(taus) < 3:
            return 0.5
        slope = float(np.polyfit(taus, vars_, 1)[0])
        return max(0.0, min(1.0, slope / 2.0))

    def _factors(self, closes: np.ndarray, highs: np.ndarray, lows: np.ndarray,
                 adx: float, plus_di: float, minus_di: float, cfg: dict[str, Any]) -> dict[str, float]:
        """计算六因子方向分 f∈[-1,+1]（正=多头，负=空头，绝对值=强度）。"""
        n = len(closes)
        close = float(closes[-1]) if n else 0.0
        di_sign = 1.0 if plus_di >= minus_di else -1.0

        # F1 ADX：强度归一 × DI 方向
        adx_norm = max(0.0, min(1.0, (adx - cfg["hexp.adx.min"]) /
                                max(cfg["hexp.adx.max"] - cfg["hexp.adx.min"], 1e-9)))
        f_adx = adx_norm * di_sign

        # F2 ER：效率比归一 × 价格净位移方向
        er = self._er(closes, cfg["hexp.er.period"])
        er_norm = max(0.0, min(1.0, (er - cfg["hexp.er.min"]) /
                               max(cfg["hexp.er.max"] - cfg["hexp.er.min"], 1e-9)))
        if n > cfg["hexp.er.period"]:
            er_sign = 1.0 if closes[-1] >= closes[-1 - cfg["hexp.er.period"]] else -1.0
        else:
            er_sign = di_sign
        f_er = er_norm * er_sign

        # F3 MA：EMA 三腿排列(±align_score) + EMA20 回归斜率
        ef, em, el = cfg["hexp.ma.ema_fast"], cfg["hexp.ma.ema_mid"], cfg["hexp.ma.ema_long"]
        f_ma = 0.0
        ma_raw = 50.0
        if n >= el + 2:
            ema_f = self._ema(closes, ef)
            ema_m = self._ema(closes, em)
            ema_l = self._ema(closes, el)
            align = 0.0
            if ema_f[-1] > ema_m[-1] > ema_l[-1]:
                align = cfg["hexp.ma.align_score"]
            elif ema_f[-1] < ema_m[-1] < ema_l[-1]:
                align = -cfg["hexp.ma.align_score"]
            bars = min(cfg["hexp.ma.slope_bars"], n - 1)
            if bars >= 3 and close > 0:
                seg = ema_f[-bars:]
                slope = float(np.polyfit(np.arange(bars), seg, 1)[0])
                slope_norm = slope / close * 10000.0  # bp/根
                # BUG-7 修复：文档 4.1 规定 EMA20 回归斜率贡献为 ±40（L156），
                # 原代码用 ±50 封顶（*50 + clamp(-50,50)），斜率项对 MA 因子贡献
                # 比文档大 25%。现改回 ±40 上限，与文档一致。
                # slope_norm_bp = 斜率达到 ±40 贡献上限所需的 bp/根（饱和点），
                # 是文档未给出、但换算 bp/bar→分数所必需的合法可配置超参
                # （非“臆造”，须补充进文档 4.1）。
                f_slope = max(-40.0, min(40.0, slope_norm /
                                         max(cfg["hexp.ma.slope_norm_bp"], 1e-9) * 40.0))
            else:
                f_slope = 0.0
            ma_raw = max(0.0, min(100.0, 50.0 + align + f_slope))  # 0-100 多头度
            f_ma = (ma_raw - 50.0) / 50.0  # → [-1,+1]

        # F4 BBW：带宽分位数（0-1）× 价格相对中轨方向
        bp, bs = cfg["hexp.bbw.boll_period"], cfg["hexp.bbw.boll_std"]
        f_bbw = 0.0
        bbw_pct = 50.0
        if n >= bp + 5:
            ma = np.convolve(closes, np.ones(bp) / bp, mode="valid")
            sd = np.array([np.std(closes[i - bp:i]) for i in range(bp, n + 1)])
            with np.errstate(divide="ignore", invalid="ignore"):
                bbw_series = np.where(ma > 0, (2.0 * bs * sd) / ma, 0.0)
            win = min(cfg["hexp.bbw.window"], len(bbw_series))
            if win >= 5:
                recent = bbw_series[-win:]
                cur = recent[-1]
                bbw_pct = float(np.sum(recent <= cur) / len(recent) * 100.0)
                mid = float(ma[-1])
                bbw_sign = 1.0 if close >= mid else -1.0
                f_bbw = (bbw_pct / 100.0) * bbw_sign

        # F5 Hurst：H>0.5 趋势持续(顺 DI)，H<0.5 均值回归(逆 DI)
        h = self._hurst(closes, cfg["hexp.hurst.max_lag"])
        h_norm = max(0.0, min(1.0, (h - cfg["hexp.hurst.min"]) /
                              max(cfg["hexp.hurst.max"] - cfg["hexp.hurst.min"], 1e-9)))
        f_hurst = h_norm * di_sign * (1.0 if h >= 0.5 else -1.0)

        # F6 RSI：Wilder RMA(14) 对齐 MT5（原简单均值口径不一致）
        rsi = 50.0
        _rsi_period = 14
        if n >= _rsi_period + 1:
            _deltas = np.diff(closes)
            _gains = np.where(_deltas > 0, _deltas, 0.0)
            _losses = np.where(_deltas < 0, -_deltas, 0.0)
            # Wilder 平滑：首段简单均值初始化，后续 RMA 递推（数据窗口足够时生效）
            ag = float(np.sum(_gains[:_rsi_period]) / _rsi_period)
            al = float(np.sum(_losses[:_rsi_period]) / _rsi_period)
            for _i in range(_rsi_period, len(_gains)):
                ag = (ag * (_rsi_period - 1) + _gains[_i]) / _rsi_period
                al = (al * (_rsi_period - 1) + _losses[_i]) / _rsi_period
            rsi = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
        if adx >= 25.0:  # 趋势模式
            if rsi < 30: f_rsi = 1.0
            elif rsi <= 50: f_rsi = (50.0 - rsi) / 20.0
            elif rsi <= 70: f_rsi = -(rsi - 50.0) / 20.0
            else: f_rsi = -1.0
        else:  # 震荡模式
            if rsi < 25: f_rsi = 1.0
            elif rsi <= 35: f_rsi = (35.0 - rsi) / 10.0
            elif rsi <= 65: f_rsi = 0.0
            elif rsi <= 75: f_rsi = -(rsi - 65.0) / 10.0
            else: f_rsi = -1.0

        return {
            "adx": float(f_adx), "er": float(f_er), "ma": float(f_ma),
            "bbw": float(f_bbw), "hurst": float(f_hurst), "rsi": float(f_rsi),
            "_adx_raw": float(adx), "_bbw_pct": float(bbw_pct), "_rsi_raw": float(rsi),
            "_hurst_raw": float(h), "_er_raw": float(er), "_ma_raw": float(ma_raw),
        }

    def _mm_signal(self, m1_closes: np.ndarray, cfg: dict[str, Any]) -> float:
        """微结构动量幂律信号：MM(t)=Σ(i+1)^(-α)·r(t-i)，tanh 归一 → [-1,+1]。"""
        n = len(m1_closes)
        win = cfg["hexp.mm.window"]
        if n < win + 2:
            return 0.0
        logp = np.log(np.maximum(m1_closes[-(win + 1):], 1e-12))
        rets = np.diff(logp)  # r(t-i), 最新在最后
        alpha = cfg["hexp.mm.alpha"]
        weights = np.array([(i + 1) ** (-alpha) for i in range(win)])[::-1]  # 最新权重最大
        mm = float(np.sum(weights * rets) / np.sum(weights))
        scale = max(cfg["hexp.mm.scale"], 1e-9)
        return float(math.tanh(mm / scale))

    # ─────────────────────── 反转态(_REVERSAL)跟踪 ───────────────────────
    def _track_reversal(self, symbol: str, period: str, state: str,
                        hold_sec: float, momentum_flip: bool = False) -> bool:
        """检测并维持单周期反转态，返回该周期当前是否处于 ``_REVERSAL``。

        反转判据 = 该周期**确定性趋势态**发生 ``TREND_UP↔TREND_DOWN`` 切换。
        以「态的切换」而非「某条代码路径」为判据，一次性覆盖此前三条互相漏网的通道：

          1. 方案 A 动量翻转（``_HysteresisState`` 内 flip 覆盖，state 直接翻向）；
          2. 迟滞确认路径（trend_score 达 enter 且方向相反，未经 ``_TRANSITION`` 中转
             即从 UP 切到 DOWN）—— 这条最"干净"，恰恰是旧逻辑完全漏掉的；
          3. D1 / 主周期这类**无状态机的轻量方向态**（原先根本不参与反转识别）。

        ``hold_sec``：反转态自触发起的持有时长（= reversal_hold_bars × 主周期分钟）。
        反转是「态」不是瞬时事件 —— 只在翻转当次减仓的话，紧随其后的信号立刻恢复
        满仓，「越干净的反转手数越大」依旧存在。用绝对时间戳而非计数器，是因为
        produce() 既被 symbol_loop 按棒调用、也被 live 发布器每 3s 调用，
        计数器会随调用频率漂移，时间窗则与调用频率无关。
        """
        key = f"{symbol}:{period}"
        now = time.time()
        prev = self._prev_period_state.get(key, "")
        flipped = (prev in (_TREND_UP, _TREND_DOWN)
                   and state in (_TREND_UP, _TREND_DOWN) and prev != state)
        # 只在确定性趋势态上更新基准；RANGE/TRANSITION 不覆盖，保留上一个趋势方向做对比
        if state in (_TREND_UP, _TREND_DOWN):
            self._prev_period_state[key] = state
        if flipped:
            hold = max(0.0, float(hold_sec))
            self._reversal_until[key] = now + hold
            self._reversal_src[key] = "momentum" if momentum_flip else "hysteresis"
            logger.info(
                "hexp %s reversal %s: %s→%s src=%s hold=%.0fs",
                symbol, period, prev, state, self._reversal_src[key], hold)
            return True
        until = float(self._reversal_until.get(key, 0.0))
        if until > now:
            return True
        if until:  # 窗口已过期：清理，避免长跑内存增长
            self._reversal_until.pop(key, None)
            self._reversal_src.pop(key, None)
        return False

    # ─────────────────────── 主流程 ───────────────────────
    async def produce(
        self,
        symbol: str,
        m5_indicators: Any,
        regime_result: Any,
        *,
        live: bool = False,
    ) -> ScoreResult:
        """和乘幂完整信号管线。返回与下游兼容的 ScoreResult（附加 hexp 元数据）。"""
        cfg = await self._load()
        sr = ScoreResult()
        sr.direction = "NO_TRADE"
        # 体制感知：记录实际生效的权重方案与体制，便于诊断（step3 计算后覆盖为具体方案）
        sr.weight_scheme = "HEXP"

        if not cfg["hexp.enabled"]:
            sr.fallback_reason = "hexp_disabled"
            return sr
        if self._fetch is None:
            sr.fallback_reason = "hexp_no_fetcher"
            return sr

        # 1) 解析周期组合，拉取各周期 K 线
        minutes_map = dict(_MINUTES_FALLBACK)
        try:
            for pair in str(cfg["hexp.period_minutes"]).split(","):
                if "=" in pair:
                    pk, pv = pair.split("=", 1)
                    minutes_map[pk.strip()] = int(pv.strip())
        except Exception:
            pass
        periods = [p.strip() for p in str(cfg["hexp.periods"]).split(",") if p.strip()]
        periods = [p for p in periods if p in minutes_map]
        if not periods:
            sr.fallback_reason = "hexp_no_periods"
            return sr
        periods.sort(key=lambda p: minutes_map[p])
        primary = cfg["hexp.primary_period"] if cfg["hexp.primary_period"] in periods else periods[0]

        period_data: dict[str, dict[str, Any]] = {}
        for p in periods:
            try:
                ks = await self._fetch(symbol, p, 160)
            except Exception as exc:
                logger.warning("hexp fetch %s %s failed: %s", symbol, p, exc)
                continue
            if not ks or len(ks) < 60:
                continue
            closes = np.array([k["close"] for k in ks], dtype=np.float64)
            highs = np.array([k["high"] for k in ks], dtype=np.float64)
            lows = np.array([k["low"] for k in ks], dtype=np.float64)
            opens = np.array([k["open"] for k in ks], dtype=np.float64)
            # ATR(14) Wilder + ADX 近似（复用主周期指标口径：用 m5 指标当周期=M5，否则自算）
            adx_v, pdi, mdi = self._adx(highs, lows, closes, 14)
            fac = self._factors(closes, highs, lows, adx_v, pdi, mdi, cfg)
            period_data[p] = {
                "closes": closes, "highs": highs, "lows": lows, "opens": opens,
                "adx": adx_v, "plus_di": pdi, "minus_di": mdi,
                "factors": fac, "close": float(closes[-1]),
            }
        if primary not in period_data:
            sr.fallback_reason = "hexp_no_primary_data"
            return sr

        # 2) 微结构动量（M1）
        f_mm = 0.0
        mm_period = str(cfg["hexp.mm.period"])
        try:
            m1 = await self._fetch(symbol, mm_period, 40)
            if m1 and len(m1) >= cfg["hexp.mm.window"] + 2:
                f_mm = self._mm_signal(
                    np.array([k["close"] for k in m1], dtype=np.float64), cfg)
        except Exception as exc:
            logger.debug("hexp mm fetch failed: %s", exc)

        # 3) 每周期状态机 + TrendScore（五因子加权和，运行时归一）
        # 3) 体制感知因子权重（BUG-3）：依 M5 regime_result 在趋势/震荡/中性三方案间连续混合。
        #    factor_scheme 为 7 因子归一化权重（adx/er/ma/bbw/hurst/rsi/mm）；
        #    wn 取其中 6 因子（不含 mm）用于本步 TrendScore，step5 HP-Score 用全 7 因子。
        factor_scheme = await self._select_factor_weights(regime_result, cfg)
        regime_tag = str(getattr(regime_result, "regime", "UNKNOWN") or "UNKNOWN")
        sr.weight_scheme = f"HEXP:{regime_tag}"
        try:
            sr.regime_weights = {k: round(factor_scheme[k], 4) for k in self._FACTORS}
        except Exception:
            pass
        w = {k: factor_scheme[k] for k in ("adx", "er", "ma", "bbw", "hurst", "rsi")}
        wsum = max(sum(w.values()), 1e-9)
        wn = {k: v / wsum for k, v in w.items()}

        period_states: dict[str, str] = {}
        trend_scores: dict[str, float] = {}
        # B-1（2026-08-11）：记录各周期本次 update 是否走了"动量翻转"路径。
        # 仅用于标注反转来源（momentum / hysteresis）；反转态本身由下方 3b 段
        # 以「趋势态切换」统一判定，不依赖此标记，故迟滞路径反转不会漏网。
        momentum_flips: dict[str, bool] = {}
        for p, d in period_data.items():
            fac = d["factors"]
            # 【2026-08-26 状态机识别缺陷修复】原 `sum(wn[k]*abs(fac[k]))*100` 对所有因子
            # 取绝对值求和，把「震荡特征」当成「趋势强度」计入 TrendScore：
            #   · hurst<0.5(均值回归) → fac 为负，abs 后反而抬高分数
            #   · rsi 超买/超卖(反转前兆) → fac 为负，abs 后同样抬高
            #   实证：D1 300/300 恒 TREND_UP、M5 73% 判趋势、75% 信号多周期不一致，
            #   震荡市仍出趋势单（主周期 M5/D1 轻量态只看方向不看强度）。
            # 现改为：adx/er/ma/bbw 用 abs（强度），hurst/rsi 带符号（均值回归/超买超卖
            # 反向抑制），使震荡市 TrendScore 跌破 exit 判 RANGE。
            ts = (
                wn["adx"] * abs(fac["adx"]) +
                wn["er"] * abs(fac["er"]) +
                wn["ma"] * abs(fac["ma"]) +
                wn["bbw"] * abs(fac["bbw"]) +
                wn["hurst"] * fac["hurst"] +   # 趋势持续(h>0.5,正)加分；均值回归(h<0.5,负)减分
                wn["rsi"] * fac["rsi"]          # 超买/超卖(负)减分，抑制反转前兆
            ) * 100.0
            # 该周期方向：ma 与 DI 同向取之，矛盾记 0 且 TrendScore 7 折
            di_dir = 1 if d["plus_di"] >= d["minus_di"] else -1
            ma_dir = 1 if fac["ma"] > 0 else (-1 if fac["ma"] < 0 else 0)
            if ma_dir == 0:
                pdir = di_dir
            elif ma_dir == di_dir:
                pdir = ma_dir
            else:
                pdir = 0
                ts *= 0.7
            trend_scores[p] = ts
            if p in (primary, "D1"):
                # 【2026-08-26 状态机识别缺陷修复】轻量态原仅看方向(pdir)，不看 TrendScore
                # 强度 → 震荡中"方向一致但强度低"仍判 TREND（实证 D1 300/300 恒 TREND_UP，
                # 因 D1 只认 ma/DI 同向方向，永不判 RANGE）。现加入强度门槛：
                #   方向确定 + ts≥enter → TREND；ts<exit → RANGE；否则 TRANSITION。
                # 使震荡市(ts 低)正确判 RANGE，不再恒出趋势单。
                _enter_th = float(cfg["hexp.state.enter_score"])
                _exit_th = float(cfg["hexp.state.exit_score"])
                if pdir != 0 and ts >= _enter_th:
                    period_states[p] = _TREND_UP if pdir > 0 else _TREND_DOWN
                elif ts < _exit_th:
                    period_states[p] = _RANGE
                else:
                    period_states[p] = _TRANSITION
            else:  # 迟滞状态机（M30/H1/H4，含动量翻转）
                sm = self._sm.setdefault(f"{symbol}:{p}", _HysteresisState())
                period_states[p] = sm.update(
                    ts, pdir,
                    cfg["hexp.state.enter_score"],
                    cfg["hexp.state.exit_score"],
                    cfg["hexp.state.confirm_bars"],
                    closes=d["closes"],
                    label=p,
                    flip_enabled=cfg["hexp.mtf.flip_enabled"],
                    flip_window=int(cfg["hexp.mtf.flip_window"]),
                    flip_bars=int(cfg["hexp.mtf.flip_bars"]),
                    flip_slope_mult=float(cfg["hexp.mtf.flip_slope_mult"]),
                )
                if sm.reversed:
                    momentum_flips[p] = True

        # 3b) 反转态(_REVERSAL)聚合（2026-08-11 B-1）
        # 统一以「确定性趋势态 TREND_UP↔TREND_DOWN 切换」为判据，一次覆盖动量翻转 /
        # 迟滞确认 / D1 轻量方向态三条路径，并按 reversal_hold_bars 维持一段反转窗口，
        # 供第 8 步减仓。主周期默认排除（M5 方向切换过频，计入会让减仓常驻），
        # 与共振矩阵排除主周期的口径一致，可用 reversal_include_primary 打开。
        _hold_sec = (max(0.0, float(cfg["hexp.exec.reversal_hold_bars"]))
                     * float(minutes_map.get(primary, 5)) * 60.0)
        _incl_primary = bool(cfg["hexp.exec.reversal_include_primary"])
        reversal_periods: list[str] = []
        for _p, _st in period_states.items():
            if _p == primary and not _incl_primary:
                continue
            if self._track_reversal(symbol, _p, _st, _hold_sec, momentum_flips.get(_p, False)):
                reversal_periods.append(_p)

        # 4) k 自适应（文档 2.2：单一公式，k_base 为中性常数，不随市场状态切换）
        #    市场状态对 k 的影响完全由下方公式的 ADX_norm / BBW_pct 项承载。
        #    禁止再用迟滞状态机挑一个「状态专属 k_base」后再套同一公式 —— 那会让
        #    同一批市场状态信号（ADX 强度 / 布林带宽）被乘两次，即 BUG-1「k 双重计数」：
        #    强趋势 k_base=2.0 叠高 ADX → k 顶到上限 3.0（文档要 1.8~2.5）；
        #    震荡 k_base=0.65 叠低 ADX → k 压到下限 0.5（文档公式本应≈1.0~1.14）。
        #    修复：k_base 恒取 hexp.k.base（=1.5），状态自适应交给公式本身。
        pf = period_data[primary]["factors"]
        main_state = period_states.get(primary, _RANGE)
        k_base = cfg["hexp.k.base"]  # 文档 2.2：k_base = 1.5（中性基准），恒定
        adx_norm = min(1.0, period_data[primary]["adx"] / 50.0)
        k = k_base * (1.0 + cfg["hexp.k.alpha"] * (adx_norm - 0.5)
                      + cfg["hexp.k.beta"] * (pf["_bbw_pct"] - 50.0) / 100.0)
        # 微结构共振加速：|f_mm| 超阈且另有大因子共振 → k 临时提升
        big_factor = max(abs(pf["adx"]), abs(pf["er"]), abs(pf["ma"]))
        if abs(f_mm) > cfg["hexp.mm.accel_threshold"] and big_factor > 0.5:
            k += cfg["hexp.mm.accel_k_boost"]
        k = max(cfg["hexp.k.min"], min(cfg["hexp.k.max"], k))

        # 4.5) 位置因子（BUG-1 修复 2026-08-13）：Donchian 分位提前到方向裁决之前计算。
        # 原在第 7/10 步才算 → 方向裁决完全拿不到位置信息，是"高多低空"的架构根因。
        atr = self._atr(period_data[primary]["highs"], period_data[primary]["lows"],
                        period_data[primary]["closes"], 14)
        _pos_pct = float(self._get_donchian_pct(period_data[primary], atr, cfg))

        # 5) HP-Score（方向=加权和符号，强度=幂加权和开方）
        # 体制感知：全 7 因子均取自 regime-aware factor_scheme（含 mm），归一化后用于 HP-Score。
        w_full = {k: factor_scheme[k] for k in ("adx", "er", "ma", "bbw", "hurst", "rsi", "mm")}
        wtot = max(sum(w_full.values()), 1e-9)
        w_full = {kk: vv / wtot for kk, vv in w_full.items()}
        fvec = {kk: pf[kk] for kk in wn}
        fvec["mm"] = f_mm
        dir_sum = sum(w_full[kk] * fvec[kk] for kk in w_full)
        # BUG-1 修复：位置因子 f_pos 参与方向裁决（仅方向，不进 pow_sum/hp 强度）。
        # 底部下跌趋势中 f_pos>0 对冲滞后因子净空 → 不再"低空"；顶部反之；中部 f_pos≈0 无影响。
        f_pos = 0.0
        if bool(cfg.get("hexp.pos_factor.enabled", True)):
            f_pos = (0.5 - _pos_pct) * 2.0
            dir_sum += float(cfg.get("hexp.pos_factor.weight", 0.15)) * f_pos
        pow_sum = sum(w_full[kk] * (abs(fvec[kk]) ** k) for kk in w_full)
        hp_strength = pow_sum ** (1.0 / k) if pow_sum > 0 else 0.0
        hp_100 = hp_strength * 100.0

        dmin = cfg["hexp.direction_min_score"]
        if abs(dir_sum) < 0.01 and max(abs(v) for v in fvec.values()) < dmin:
            direction = "NO_TRADE"
        elif dir_sum > 0:
            direction = "BUY"
        else:
            direction = "SELL"

        # 6) 多周期共振裁决（M5 主执行周期权重=0，不自我裁决）
        verdict = 0.0
        wsum_r = 0.0
        for p in periods:
            if p == primary:
                continue
            wp = cfg.get(f"hexp.mtf.weight_{p}", 0.0)
            if not isinstance(wp, (int, float)) or wp <= 0:
                continue
            st = period_states.get(p, _RANGE)
            pv = 1.0 if st == _TREND_UP else (-1.0 if st == _TREND_DOWN else 0.0)
            verdict += pv * float(wp)
            wsum_r += float(wp)
        if wsum_r > 0:
            verdict /= wsum_r
        # 共振加成/惩罚
        # 方案 B 对称降分（2026-08-10）：顺/逆风用同一把"主线偏置"折扣尺
        #  - 顺风：温和加成(系数 hexp.resonance.tailwind_bonus，默认0=完全对称，不虚涨)
        #  - 逆风：折扣(系数 hexp.resonance.penalty)，不再硬封成 NO_TRADE
        #  - 原 hexp_mtf_long_only / hexp_mtf_short_only 硬封已彻底移除
        #    （逆风单仅被 penalty 降分，分数够高仍能过 min_grade 闸门）
        sig_dir = 1 if direction == "BUY" else (-1 if direction == "SELL" else 0)
        _pending_pullback_mult = 1.0  # B' 初始化：默认不罚，避免未赋值分支 UnboundLocalError
        if sig_dir != 0 and abs(verdict) > 0.01:
            if (verdict > 0) == (sig_dir > 0):
                hp_100 *= (1.0 + abs(verdict) * cfg["hexp.resonance.tailwind_bonus"])
            else:
                hp_100 *= (1.0 - abs(verdict) * cfg["hexp.resonance.penalty"])
                # B' 延伸（2026-08-17）：逆风/回踩单额外下压综合评分 total，使 grade 更难过
                # min_grade 闸门；默认 pullback_penalty=1.0 等于不额外罚，向后兼容。
                # 仅降分不硬封 —— 若 total 仍够高（grade>=min_grade）仍会过闸下单。
                _pb = float(cfg.get("hexp.resonance.pullback_penalty", 1.0))
                if _pb < 1.0:
                    _pending_pullback_mult = _pb
        self._pending_pullback_mult = _pending_pullback_mult

        # 7) 6 维评分卡（atr/_pos_pct 已在 4.5 步计算）
        close_v = period_data[primary]["close"]
        sc = {
            "resonance": float(abs(verdict) * 100.0),
            "state": float(hp_100),
            "entry": float(self._entry_score(period_data[primary], atr, cfg)),
            # BUG-1 配套：position 维改为方向感知——极值追单不再因"反向空间大"白捡满分。
            "position": float(self._position_score(period_data[primary], atr, close_v, direction)),
            "vol": float(self._vol_score(period_data[primary], atr)),
            "session": float(self._session_score()),
        }
        sw = {
            "resonance": cfg["hexp.scorecard.weight_resonance"],
            "state": cfg["hexp.scorecard.weight_state"],
            "entry": cfg["hexp.scorecard.weight_entry"],
            "position": cfg["hexp.scorecard.weight_position"],
            "vol": cfg["hexp.scorecard.weight_vol"],
            "session": cfg["hexp.scorecard.weight_session"],
        }
        swsum = max(sum(sw.values()), 1e-9)
        total = sum(sc[kk] * sw[kk] for kk in sc) / swsum
        # B' 延伸（2026-08-17）：逆风/回踩单综合评分额外下压，使 grade 更难达 min_grade
        # 闸门。默认 pullback_penalty=1.0 不改变现有行为；设 <1.0 才生效。仅降分不硬封。
        _pb_mult = getattr(self, "_pending_pullback_mult", 1.0)
        if _pb_mult < 1.0:
            total *= _pb_mult

        # 8) 分级
        if total >= cfg["hexp.scorecard.a_threshold"] and hp_100 >= cfg["hexp.scorecard.s_hp_min"]:
            grade = "S"
        elif total >= cfg["hexp.scorecard.a_threshold"] or (
                hp_100 >= cfg["hexp.scorecard.s_hp_min"] and total >= cfg["hexp.scorecard.b_threshold"]):
            grade = "A"
        elif total >= cfg["hexp.scorecard.b_threshold"]:
            grade = "B"
        elif total >= cfg["hexp.scorecard.pass_threshold"]:
            grade = "C"
        else:
            grade = "RED"
        if hp_100 < cfg["hexp.scorecard.hp_floor"]:
            grade = "RED"

        # 减仓触发聚合：_TRANSITION（犹豫带）与 _REVERSAL（趋势态翻转窗口）
        # 二者互补：犹豫带是"方向没想好"，反转态是"方向刚掉头"。干净反转直接
        # UP→DOWN 不经犹豫带，只靠 transition 判据会完全漏掉 → 反转越果断手数越大。
        transition = any(s == _TRANSITION for s in period_states.values())
        reversal = bool(reversal_periods)

        # 9) 汇总输出（ScoreResult 契约 + hexp 元数据）
        # 精确信号闸门：仅 >= hexp.min_grade 的分级才允许产生交易方向。
        # 低于门槛的信号照常落库（grade/hp/评分卡元数据齐全）供观测与影子对照，
        # 但 direction=NO_TRADE 不进下单链路 —— 这是「少而准」的核心开关。
        _min_grade = str(cfg.get("hexp.min_grade") or "C").strip().upper()
        _min_rank = _GRADE_RANK.get(_min_grade, 1)
        if _min_rank < 1:
            _min_rank = 1  # RED 永不可交易，门槛下限锁在 C
        _grade_ok = _GRADE_RANK.get(grade, 0) >= _min_rank
        passed = _grade_ok and direction != "NO_TRADE"
        sr.pre_score = round(total / 100.0, 4)
        sr.threshold = round(cfg["hexp.scorecard.pass_threshold"] / 100.0, 4)
        sr.threshold_passed = bool(passed)
        sr.direction = direction if passed else "NO_TRADE"
        if not passed and not sr.fallback_reason:
            if grade == "RED":
                sr.fallback_reason = "hexp_grade_red"
            elif not _grade_ok:
                sr.fallback_reason = f"hexp_grade_below_min({grade}<{_min_grade})"
            else:
                sr.fallback_reason = "hexp_no_direction"
        # 10) 极值动量感知闸门（2026-08-13 升级·替换无条件硬封）+ 回踩支撑位诊断
        # A) 动量感知极值闸门：价格到 Donchian 极值区时，不再无条件封单 —— 仅当
        #    「微动量 f_mm 相对原方向回撤」(mm_aligned < mm_retreat_min) 才 NO_TRADE；
        #    mm 仍朝原方向 → 允许极值追单（按需求：仅动量回撤时停，可能扫损）。
        #    保留 pos_pct 写入 sr.position_in_range 供观测。
        # B) 回踩支撑位诊断：方向为 BUY/SELL 且现价已回踩到近期摆动低/高点支撑带内，
        #    标记 sr.extreme_support_pullback（非极值区本就由 7 因子重裁方向，此处仅标注）。
        _extreme_high = float(cfg.get("hexp.extreme.high_pct", 0.85))
        _extreme_low = float(cfg.get("hexp.extreme.low_pct", 0.15))
        # _pos_pct 已在 4.5 步计算（BUG-1），此处直接复用
        # 极值识别（2026-08-21 修复·方案B）：通道内分位 _pos_pct(∈[0,1]) 只在通道内判位，
        # 单边行情把 Donchian 通道大幅拉宽后，价格虽创极值仍落在被拉宽的通道区间内
        # (_pos_pct 封顶 1.0 无法区分"刚突破"vs"严重超买") → 极值闸门漏判 → 高位追单
        # 裸奔扫损。此处用 k 作 OR 补充，但【必须是双条件且 pos 需接近极值侧】：
        #   k 是剧烈行情指标(由 ADX/bbw 驱动)，当前高波动下 k 普遍 >1.8 → 若 k>阈值
        #   即触发极值会把通道中部(pos≈0.5~0.7)的正常回调误判为极值 → 批量拦截卡死
        #   (实锤: 01:58-59 连续 pos=0.57~0.64 被 hexp_extreme_guard 拦)。
        #   故 k 补充要求「k 高 且 pos 已接近极值侧(>0.7 / <0.3)」才视为真极值追单：
        #     BUY : k > k_extreme 且 pos > 0.7（真高位 + 剧烈偏离）
        #     SELL: k > k_extreme 且 pos < 0.3（真低位 + 剧烈偏离）
        #   通道中部(pos 0.3~0.7)即使 k 高也视为剧烈行情的正常波动，不触发极值闸门。
        #   _pos_pct 单独超阈值(>0.8/<0.2) 仍直接触发（RANGE 窄通道内的原有语义保留）。
        _k_extreme = float(cfg.get("hexp.extreme.k_extreme", 1.8))
        _k_pos_hi = float(cfg.get("hexp.extreme.k_pos_high", 0.7))
        _k_pos_lo = float(cfg.get("hexp.extreme.k_pos_low", 0.3))
        _in_extreme = (direction == "BUY" and (_pos_pct > _extreme_high or
                                               (k > _k_extreme and _pos_pct > _k_pos_hi))) or \
                      (direction == "SELL" and (_pos_pct < _extreme_low or
                                                (k > _k_extreme and _pos_pct < _k_pos_lo)))
        # ── 极值反转护栏（2026-08-18 增强）──
        # 触发需三条件同时成立，且仅拦「原趋势延续单」（顶拦 BUY / 底拦 SELL）：
        #   ① 价位处于极值区（_in_extreme 已判定）
        #   ② 动量减弱且反向：f_mm 符号与原方向反向（或趋零），且 |f_mm| 已低于 mm_retreat_min
        #   ③ 长影线：顶部长上影 / 底部长下影 超过 wick_min 阈值（接刀/反转前兆）
        # 三条件齐 → NO_TRADE 并收紧 SL（见 G3 段：sr.co_exec_sl_atr_mult 临时覆盖）。
        # momentum_reversed：原方向为 BUY 时要求 f_mm<0（动量转空），SELL 时要求 f_mm>0。
        # ── 极值护栏自动开/关（2026-08-19）──
        # auto_mode=auto 时按 M5 regime 自动推导 _rev_enabled：
        #   regime ∈ auto_on_regimes(RANGE/NEUTRAL 等震荡/极值) → 开硬封
        #   regime ∈ auto_off_regimes(TREND/PRE_TREND 等趋势确认) → 关硬封
        #   其余体制 → 回退人工开关 reversal_enabled
        # auto_mode=off（默认）→ 直接沿用 reversal_enabled（历史行为，零回归）
        _rev_enabled = bool(cfg.get("hexp.extreme.reversal_enabled", True))
        _auto_mode = str(cfg.get("hexp.extreme.auto_mode", "off")).strip().lower()
        if _auto_mode == "auto":
            _regime_val = str(getattr(regime_result, "regime", "") or "")
            _on_set = {s.strip().upper() for s in str(cfg.get("hexp.extreme.auto_on_regimes", "RANGE,NEUTRAL")).split(",") if s.strip()}
            _off_set = {s.strip().upper() for s in str(cfg.get("hexp.extreme.auto_off_regimes", "TREND,PRE_TREND")).split(",") if s.strip()}
            if _regime_val in _on_set:
                _rev_enabled = True
                logger.info("hexp %s %s | extreme auto-mode: regime=%s ∈ on-set → guard ON",
                            symbol, primary, _regime_val)
            elif _regime_val in _off_set:
                _rev_enabled = False
                logger.info("hexp %s %s | extreme auto-mode: regime=%s ∈ off-set → guard OFF",
                            symbol, primary, _regime_val)
        _wick_min = float(cfg.get("hexp.extreme.wick_min", 0.60))
        _mm_retreat_min = float(cfg.get("hexp.extreme.mm_retreat_min", 0.20))  # BUG-3: 0.05→0.20
        _dir_sign = 1.0 if direction == "BUY" else (-1.0 if direction == "SELL" else 0.0)
        # 微动量对齐度（与方向同号=仍朝原方向）：提前到极值块外计算，供动量枯竭保护共用
        _mm_aligned = f_mm * _dir_sign
        # 长影线比（用主周期最新收盘 bar 的 open/high/low/close）
        _pdata = period_data[primary]
        _o, _h, _l, _c = float(_pdata["opens"][-1]), float(_pdata["highs"][-1]), \
                         float(_pdata["lows"][-1]), float(_pdata["closes"][-1])
        _hl = (_h - _l) if (_h - _l) > 1e-9 else 1e-9
        _upper_wick = (_h - max(_o, _c)) / _hl
        _lower_wick = (min(_o, _c) - _l) / _hl
        sr.upper_wick_ratio = round(float(_upper_wick), 4)
        sr.lower_wick_ratio = round(float(_lower_wick), 4)
        if _in_extreme:
            _mm_retreat_enabled = bool(cfg.get("hexp.extreme.mm_retreat_enabled", True))
            # _mm_aligned 已在极值块外提前计算（供动量枯竭保护共用）
            _momentum_reversed = (_dir_sign > 0 and f_mm < 0.0) or (_dir_sign < 0 and f_mm > 0.0)
            # 顶部长上影 / 底部长下影
            _long_wick = (_dir_sign > 0 and _upper_wick >= _wick_min) or \
                         (_dir_sign < 0 and _lower_wick >= _wick_min)
            # 【2026-08-25 极值分层裁决】读 symbol 级保本标志 hcm:pos:be:sym:{symbol}:{dir}：
            # 该 symbol 已有同向保本持仓（风险已锁）→ 不硬封方向，标记 extreme_pending
            # 交由风控保本闸门最终裁决（放行+轻仓/拦截）；无保本 → 照常硬封（防接刀）。
            _sym_be_ok = False
            if direction in ("BUY", "SELL") and self._redis is not None:
                try:
                    _be_flag = await self._redis.get(f"hcm:pos:be:sym:{symbol}:{direction}")
                    _sym_be_ok = (_be_flag is not None and str(_be_flag).strip() == "1")
                except Exception:
                    _sym_be_ok = False
            if _rev_enabled and _mm_retreat_enabled and \
                    _mm_aligned < _mm_retreat_min and _momentum_reversed and _long_wick:
                if _sym_be_ok:
                    # 已有同向保本持仓：不硬封，标记交风控（轻仓追单）
                    sr.extreme_pending = True
                    logger.info(
                        "hexp %s %s | EXTREME PENDING(保本追单) dir=%s pos=%.2f mm=%.2f "
                        "(sym be=1 → risk BE-gate 裁决)",
                        symbol, primary, direction, _pos_pct, _mm_aligned)
                else:
                    _extreme_block = (f"hexp_extreme_reversal(top={_pos_pct:.2f} wick_u={_upper_wick:.2f} "
                                       f"wick_l={_lower_wick:.2f} mm={_mm_aligned:.2f} dir={direction})")
                    logger.info("hexp %s %s | BLOCK %s dir=%s (extreme + momentum reversed + long wick)",
                                symbol, primary, _extreme_block, direction)
                    direction = "NO_TRADE"
                    # 关键修复(2026-08-13)：step10 在 step9 之后执行，仅改局部 direction 不会
                    # 回写 sr.direction/threshold_passed → 闸门此前只打日志不真封单。
                    # 必须同步回写输出契约，BLOCK 才真正生效。
                    passed = False
                    sr.threshold_passed = False
                    sr.direction = "NO_TRADE"
                    sr.extreme_reversal_blocked = True
                    if not sr.fallback_reason:
                        sr.fallback_reason = _extreme_block
            elif _mm_retreat_enabled and _mm_aligned < _mm_retreat_min:
                if _sym_be_ok:
                    # 已有同向保本持仓：不硬封，标记交风控（轻仓追单）
                    sr.extreme_pending = True
                    logger.info(
                        "hexp %s %s | EXTREME PENDING(保本追单) dir=%s pos=%.2f mm=%.2f "
                        "(sym be=1 → risk BE-gate 裁决)",
                        symbol, primary, direction, _pos_pct, _mm_aligned)
                else:
                    # 旧语义兜底：极值区仅动量回撤（无长影线条件）仍拦原趋势延续单
                    _extreme_block = (f"hexp_extreme_guard(retreat mm={_mm_aligned:.2f} "
                                       f"pos={_pos_pct:.2f})")
                    logger.info("hexp %s %s | BLOCK %s dir=%s (extreme + mm retreat)",
                                symbol, primary, _extreme_block, direction)
                    direction = "NO_TRADE"
                    passed = False
                    sr.threshold_passed = False
                    sr.direction = "NO_TRADE"
                    if not sr.fallback_reason:
                        sr.fallback_reason = _extreme_block
            else:
                sr.extreme_chase = True
                logger.info(
                    "hexp %s %s | EXTREME CHASE allowed dir=%s mm_aligned=%.2f pos=%.2f "
                    "(mm still aligned, chase at extreme)",
                    symbol, primary, direction, _mm_aligned, _pos_pct)
        # A-2) 动量枯竭保护（2026-08-21）：极值盲区补充——不依赖 _in_extreme（k 未超阈、
        # pos 未到 0.85 的宽通道高位追单会被极值护栏漏判）。当 pos 高位 + 趋势质量差(er 低)
        # + 微动量枯竭(mm 近 0/反向) 三者齐 → 拦原趋势延续单（高位接刀/追顶）。
        #    BUY : pos>hi 且 er<er_drain 且 mm_aligned<mm_drain
        #    SELL: pos<(1-hi) 且 er<er_drain 且 mm_aligned<mm_drain
        # 真趋势延续 er 通常>0.22，不会误杀；仅拦动能枯竭的顶部追多/底部追空。
        _drain_enabled = bool(cfg.get("hexp.momentum_drain_enabled", True))
        if _drain_enabled and not _in_extreme and direction in ("BUY", "SELL") and passed:
            _drain_hi = float(cfg.get("hexp.momentum_drain_hi", 0.65))
            _drain_er = float(cfg.get("hexp.momentum_drain_er", 0.20))
            _drain_mm = float(cfg.get("hexp.momentum_drain_mm", 0.15))
            _er_now = float(pf.get("_er_raw", 0.0))
            _drained = False
            if direction == "BUY" and _pos_pct > _drain_hi and _er_now < _drain_er and _mm_aligned < _drain_mm:
                _drained = True
            elif direction == "SELL" and _pos_pct < (1.0 - _drain_hi) and _er_now < _drain_er and _mm_aligned < _drain_mm:
                _drained = True
            if _drained:
                _drain_block = (f"hexp_momentum_drain(er={_er_now:.3f} mm={_mm_aligned:.3f} "
                                f"pos={_pos_pct:.2f})")
                logger.info("hexp %s %s | BLOCK %s dir=%s (high pos + momentum drained)",
                            symbol, primary, _drain_block, direction)
                direction = "NO_TRADE"
                passed = False
                sr.threshold_passed = False
                sr.direction = "NO_TRADE"
                if not sr.fallback_reason:
                    sr.fallback_reason = _drain_block
        # A-3) 动量方向否决（2026-08-21）：动量明确反向时，无论位置/趋势结构如何都拦逆动量单。
        # 解决"滞后组(ma/adx 权重合计 49)锁死 dir_sum，近期微动量(mm 权重仅 11)转负仍出原
        # 方向单"的不敏捷问题（实锤: 388639530/534/536 等 mm=-0.01~-0.057 仍成交 BUY）。
        # 与 momentum_drain(近零枯竭+高位)互补：本护栏拦「动量明确反向」，不依赖 pos。
        _flip_enabled = bool(cfg.get("hexp.momentum_flip_enabled", True))
        _flip_mm = float(cfg.get("hexp.momentum_flip_mm", 0.04))
        # 【2026-08-26 高位分级加固】见 _DEFAULTS 注释。多头度(0~100)≥高位分界时，
        # 反向阈值收紧到 flip_ma_mm（微负即拦顶部追单）；正常位置用基础 flip_mm。
        # 拦截信号保留 mm 与 ma 明细，便于日志归因。
        _flip_ma_th = float(cfg.get("hexp.momentum_flip_ma_threshold", 90.0))
        _flip_ma_lo = float(cfg.get("hexp.momentum_flip_ma_low", 10.0))
        _flip_ma_mm = float(cfg.get("hexp.momentum_flip_ma_mm", 0.005))
        if _flip_enabled and not _in_extreme and direction in ("BUY", "SELL") and passed:
            _flip_block = None
            # 0~100 多头度：BUY 高=多头高位(顶部追多需拦)；SELL 低=空头低位(底部追空需拦)。
            _ma_deg = float(pf.get("_ma_raw", 50.0))
            _eff_th = _flip_mm  # 默认基础阈值（明确反向才拦，防误杀）
            if direction == "BUY" and _ma_deg >= _flip_ma_th:
                _eff_th = _flip_ma_mm  # 多头高位：微负即拦顶部追多
            elif direction == "SELL" and _ma_deg <= _flip_ma_lo:
                _eff_th = _flip_ma_mm  # 空头低位：微正即拦底部追空
            if direction == "BUY" and f_mm < -_eff_th:
                _flip_block = f"hexp_momentum_flip(BUY but mm={f_mm:.4f} ma={_ma_deg:.0f} th={_eff_th:.3f})"
            elif direction == "SELL" and f_mm > _eff_th:
                _flip_block = f"hexp_momentum_flip(SELL but mm={f_mm:.4f} ma={_ma_deg:.0f} th={_eff_th:.3f})"
            # ── 【2026-08-26 P0-1】高位微正枯竭加固 ──
            # momentum_flip 只拦"mm 反向(负)"，漏掉 ma 极高位 + mm 微正枯竭(0<mm<弱阈值)
            # 的顶部追多（实证 sig=388640598 BUY@4666.08 ma=100 pos=0.846 mm=+0.0124
            # regime=NEUTRAL 高位追多被止损）。此处拦「高位 + 动能未反转但已枯竭」：
            #   BUY : ma≥高位分界 且 0<mm<weak_mm        → 顶部微动量枯竭追多
            #   SELL: ma≤低位分界 且 0>mm>-weak_mm        → 底部微动量枯竭追空
            # 仅拦"高位+微正枯竭"；正常位置/动量明显同向(mm≥weak_mm)不拦，防误杀。
            _hi_weak_enabled = bool(cfg.get("hexp.momentum_hi_weak_enabled", True))
            if _hi_weak_enabled and _flip_block is None:
                _hi_weak_mm = float(cfg.get("hexp.momentum_hi_weak_mm", 0.02))
                if direction == "BUY" and _ma_deg >= _flip_ma_th and 0.0 < f_mm < _hi_weak_mm:
                    _flip_block = (f"hexp_momentum_hi_weak(BUY but mm={f_mm:.4f} ma={_ma_deg:.0f} "
                                   f"pos={_pos_pct:.2f} weak<{_hi_weak_mm:.3f})")
                elif direction == "SELL" and _ma_deg <= _flip_ma_lo and -_hi_weak_mm < f_mm < 0.0:
                    _flip_block = (f"hexp_momentum_hi_weak(SELL but mm={f_mm:.4f} ma={_ma_deg:.0f} "
                                   f"pos={_pos_pct:.2f} weak<{_hi_weak_mm:.3f})")
            if _flip_block:
                logger.info("hexp %s %s | BLOCK %s dir=%s (momentum against direction)",
                            symbol, primary, _flip_block, direction)
                direction = "NO_TRADE"
                passed = False
                sr.threshold_passed = False
                sr.direction = "NO_TRADE"
                if not sr.fallback_reason:
                    sr.fallback_reason = _flip_block
                # ── 反向单观测（2026-08-21，先观测不下单）──
                # 用户需求：高位动量反向时考虑做反向单。此处仅【记录观测候选】落库到
                # indicator_values._hexp.reverse_candidate，供后续 SQL 对照未来 K 线评估
                # "若做了反向单的胜率"，验证达标后再启用真下单。不发布 signal:stream，
                # 故不进风控/桥 → 零实盘影响。
                # 触发条件：momentum_flip 已判动量反向 + 处于高位/低位（顶部做空/底部做多）。
                _rc_enabled = bool(cfg.get("hexp.reverse_candidate_enabled", True))
                if _rc_enabled:
                    _rc_hi = float(cfg.get("hexp.reverse_candidate_hi", 0.7))
                    _rc_lo = float(cfg.get("hexp.reverse_candidate_lo", 0.3))
                    _rc_dir = None
                    _rc_pos_ok = False
                    if direction == "NO_TRADE" and _flip_block:
                        # 原 BUY 被拦 → 反向候选为 SELL（需价格在高位才成立）
                        _was_buy = "BUY" in _flip_block
                        _was_sell = "SELL" in _flip_block
                        if _was_buy and _pos_pct > _rc_hi:
                            _rc_dir = "SELL"
                            _rc_pos_ok = True
                        elif _was_sell and _pos_pct < _rc_lo:
                            _rc_dir = "BUY"
                            _rc_pos_ok = True
                        if _rc_pos_ok:
                            sr.reverse_candidate = {
                                "dir": _rc_dir,
                                "pos": round(float(_pos_pct), 4),
                                "er": round(float(pf.get("_er_raw", 0.0)), 4),
                                "mm": round(float(f_mm), 4),
                                "close": round(float(close_v), 3),
                                "verdict": round(float(verdict), 4),
                            }
                            logger.info(
                                "hexp %s %s | REVERSE CANDIDATE dir=%s (pos=%.2f er=%.3f mm=%.3f) "
                                "high-momentum-reverse — OBSERVE ONLY, no order",
                                symbol, primary, _rc_dir, _pos_pct,
                                float(pf.get("_er_raw", 0.0)), f_mm)
                        else:
                            sr.reverse_candidate = None
        # ── 【2026-08-26 P0-2】震荡市均值回归校验 ──
        # NEUTRAL/RANGE 市且 hurst<阈值（均值回归态，反持续）时，高位顺势追单易被
        # 均值回归反打（实证 sig=388640598 BUY@4666.08 pos=0.846 hurst=0.449 regime=NEUTRAL
        # 高位追多被止损）。震荡市应等回撤/极值反向，不在高低位顺势追单。
        #   BUY : regime∈{list} 且 hurst<hurst_max 且 pos>hi          → 拦高位追多
        #   SELL: regime∈{list} 且 hurst<hurst_max 且 pos<(1-hi)      → 拦低位追空
        # 仅拦"震荡市 + 均值回归态 + 极值区追单"；趋势市/非均值回归态/中位不拦，防误杀。
        _rh_enabled = bool(cfg.get("hexp.range_hurst_enabled", True))
        if _rh_enabled and direction in ("BUY", "SELL") and passed and not _in_extreme:
            _rh_max = float(cfg.get("hexp.range_hurst_max", 0.50))
            _rh_hi = float(cfg.get("hexp.range_hurst_hi", 0.80))
            _rh_regimes = {x.strip().upper() for x in
                           str(cfg.get("hexp.range_hurst_regimes", "NEUTRAL,RANGE")).split(",") if x.strip()}
            _hurst_now = float(pf.get("_hurst_raw", 0.5))
            _rval = str(getattr(regime_result, "regime", ""))
            _rh_block = None
            if _rval in _rh_regimes and _hurst_now < _rh_max:
                if direction == "BUY" and _pos_pct > _rh_hi:
                    _rh_block = (f"hexp_range_hurst(BUY pos={_pos_pct:.2f} hurst={_hurst_now:.3f} "
                                 f"regime={_rval} 均值回归高位追多)")
                elif direction == "SELL" and _pos_pct < (1.0 - _rh_hi):
                    _rh_block = (f"hexp_range_hurst(SELL pos={_pos_pct:.2f} hurst={_hurst_now:.3f} "
                                 f"regime={_rval} 均值回归低位追空)")
            if _rh_block:
                logger.info("hexp %s %s | BLOCK %s dir=%s (range mean-reversion chase)",
                            symbol, primary, _rh_block, direction)
                direction = "NO_TRADE"
                passed = False
                sr.threshold_passed = False
                sr.direction = "NO_TRADE"
                if not sr.fallback_reason:
                    sr.fallback_reason = _rh_block
        # B) 回踩支撑位诊断（独立于 A 的封单判定，仅作再评估标记/日志）
        if direction in ("BUY", "SELL"):
            _pivot = self._get_recent_pivot(period_data[primary], direction, cfg)
            _sup_atr = float(cfg.get("hexp.extreme.support_atr", 1.0))
            if _pivot is not None and atr > 0:
                if abs(close_v - _pivot) <= _sup_atr * atr:
                    sr.extreme_support_pullback = True
                    logger.info(
                        "hexp %s %s | SUPPORT PULLBACK dir=%s pivot=%.2f close=%.2f "
                        "(within %.1fATR, direction re-evaluated by 7 factors)",
                        symbol, primary, direction, _pivot, close_v, _sup_atr)

        sr.buy_score = max(0.0, dir_sum)
        sr.sell_score = max(0.0, -dir_sum)
        sr.position_in_range = round(_pos_pct, 4)  # 落库 Donchian 分位，支撑 SQL 敏捷识别

        # ── 三阶段趋势进度（2026-08-13）──
        # 把「预启动(蓄势)→启动(点火)→确立(跟随)」映射到 0-100 连续进度，供面板
        # 「趋势阶段进度条」渲染。分界点固定 33 / 66（前端同此刻度）。三分量均 0-100：
        #   squeeze   = 蓄势度：BBW 分位越低越"憋"（盘久必动前兆）
        #   ignite    = 点火度：主周期趋势态 + 微动量同向 + Donchian 极值突破
        #   establish = 确立度：ADX/ER/Hurst 三指标归一均值（趋势强度）
        _bbw_v = float(pf.get("_bbw_pct", 50.0))
        _adx_r = float(pf.get("_adx_raw", 20.0))
        _er_r = float(pf.get("_er_raw", 0.0))
        _hurst_r = float(pf.get("_hurst_raw", 0.5))
        _squeeze = max(0.0, min(100.0, 100.0 - _bbw_v))
        _adx_s = max(0.0, min(1.0, _adx_r / 40.0))
        _er_s = max(0.0, min(1.0, _er_r / 0.7))
        _hur_s = max(0.0, min(1.0, (_hurst_r - 0.45) / 0.25))
        _establish = (_adx_s + _er_s + _hur_s) / 3.0 * 100.0
        _pstate = period_states.get(primary, _RANGE)
        _in_trend = _pstate in (_TREND_UP, _TREND_DOWN)
        _tdir = 1.0 if dir_sum > 0 else (-1.0 if dir_sum < 0 else 0.0)
        _ignite = 0.0
        if _in_trend:
            _ignite = 50.0 + abs(f_mm) * 40.0
            # BUG-4 修复（2026-08-13）：删除"极值突破 +10"奖励——极值追单本就该被闸门拦/
            # 被方向感知位置分压低，不应再额外加分抬 grade（否则与 BUG-1/3 修复方向相反）。
            _ignite = min(100.0, _ignite)
        _est_th = float(cfg.get("hexp.phase.establish_threshold", 55.0))
        _ign_th = float(cfg.get("hexp.phase.ignite_threshold", 50.0))
        if _establish >= _est_th:
            _phase = "establish"
            _seg = max(0.0, min(1.0, (_establish - _est_th) / max(100.0 - _est_th, 1e-9)))
            _progress = 66.0 + _seg * 34.0
        elif _ignite >= _ign_th or _in_trend:
            _phase = "ignite"
            _seg = max(0.0, min(1.0, _ignite / 100.0))
            _progress = 33.0 + _seg * 33.0
        else:
            _phase = "squeeze"
            _seg = max(0.0, min(1.0, _squeeze / 100.0))
            _progress = _seg * 33.0
        trend_phase = {
            "progress": round(_progress, 1),
            "phase": _phase,
            "squeeze": round(_squeeze, 1),
            "ignite": round(_ignite, 1),
            "establish": round(_establish, 1),
        }

        # G3 执行增强（bridge 消费 ai_sl_mult/ai_tp_mult/lot）
        grade_lot = {"S": cfg["hexp.exec.grade_lot_s"], "A": cfg["hexp.exec.grade_lot_a"],
                     "B": cfg["hexp.exec.grade_lot_b"], "C": cfg["hexp.exec.grade_lot_c"]}.get(grade, 0.5)
        lot = cfg["hexp.exec.lot_mult"] * grade_lot
        # 减仓聚合：transition（犹豫带）与 reversal（反转态）独立识别，
        # 取两者系数中更谨慎者（min）一次性减仓，避免二者同时命中时重复打折（×0.25）。
        red_mult = 1.0
        if transition:
            red_mult = min(red_mult, float(cfg["hexp.exec.transition_lot_mult"]))
        if reversal:
            red_mult = min(red_mult, float(cfg["hexp.exec.reversal_lot_mult"]))
        # 方案2（2026-08-21）：极值区手数递减——高位接刀/抄底风险随 Donchian 分位递增，
        # 未被护栏封单的放行单也降仓（与 transition/reversal 用 min 聚合，不叠加打折）。
        if _in_extreme and passed:
            _extreme_lot_mult = float(cfg.get("hexp.exec.extreme_lot_mult", 0.5))
            red_mult = min(red_mult, _extreme_lot_mult)
            logger.info("hexp %s lot reduction extreme ×%.2f | pos_pct=%.2f dir=%s",
                        symbol, _extreme_lot_mult, _pos_pct, direction)
        # 方案2（2026-08-21）：行情波动率缩放手数——让「风控面板基础手数」随波动连续调整。
        # co_exec_lot_mult 经 suggested_lot_ratio 透传到风控 final_lot = risk.lot_base × 档位 × co_ai_mult，
        # 故在此把 ATR 相对常态的缩放编入 lot，波动大→基础手数降（防满仓接大波动），波动小→不超配。
        vol_scale = 1.0
        if bool(cfg.get("hexp.exec.vol_scale_enabled", True)) and atr and atr > 0:
            _atr_ref = float(cfg.get("hexp.exec.vol_scale_atr_ref", 7.0))
            _vol_min = float(cfg.get("hexp.exec.vol_scale_min", 0.5))
            _vol_max = float(cfg.get("hexp.exec.vol_scale_max", 1.0))
            # 缩放 = 参考ATR / 实际ATR：实际波动>常态 → 比<1 降仓；实际波动<常态 → 比>1 但封顶 vol_max。
            _raw = _atr_ref / atr
            vol_scale = max(_vol_min, min(_vol_max, _raw))
            if vol_scale < 1.0:
                logger.info("hexp %s vol-scale ×%.2f | atr=%.2f ref=%.2f (base lot scaled by market)",
                            symbol, vol_scale, atr, _atr_ref)
        if red_mult < 1.0 or vol_scale < 1.0:
            lot *= red_mult * vol_scale
            logger.info(
                "hexp %s lot mult ×%.2f (red=%.2f vol=%.2f) | transition=%s reversal=%s%s",
                symbol, red_mult * vol_scale, red_mult, vol_scale, transition, reversal,
                f" periods={','.join(reversal_periods)}" if reversal_periods else "")
        # G3a（2026-08-18）极值区 SL 收紧：处于极值区(_in_extreme)但未被护栏封单的单，
        # 用更紧的止损倍数 hexp.extreme.reversal_sl_atr_mult（默认0.5 < 常规 sl_atr_mult），
        # 降低「买在顶/卖在底」不被拦但扫损的幅度。仅在极值区且实际放行时生效。
        _sl_mult = float(cfg["hexp.exec.sl_atr_mult"])
        if _in_extreme and passed and _rev_enabled:
            _rev_sl = float(cfg.get("hexp.extreme.reversal_sl_atr_mult", 0.5))
            if _rev_sl < _sl_mult:
                _sl_mult = _rev_sl
                logger.info(
                    "hexp %s %s | SL tightened at extreme dir=%s sl_atr_mult=%.2f "
                    "(reversal_sl_atr_mult)", symbol, primary, direction, _sl_mult)
        sr.co_exec_sl_atr_mult = _sl_mult
        sr.co_exec_rr_min = float(cfg["hexp.exec.rr_min"])
        sr.co_exec_lot_mult = float(round(lot, 4))
        sr.co_band = f"hexp:{grade}"

        # hexp 元数据（观测/面板/落库）
        sr.grade = grade
        sr.hp_score = round(hp_100, 2)
        sr.k_value = round(k, 3)
        sr.mm_score = round(f_mm, 4)
        sr.factor_scores = {kk: round(float(fvec[kk]), 4) for kk in fvec}
        sr.trend_scores = {p: round(float(v), 2) for p, v in trend_scores.items()}
        sr.period_states = dict(period_states)
        sr.resonance_verdict = round(verdict, 4)
        sr.scorecard = {kk: round(v, 2) for kk, v in sc.items()}
        sr.scorecard_total = round(total, 2)
        # B: 落库持久化——把 dir_sum/hp_strength/trend_phase/factor_raws 挂到 sr，
        # 供 scheduler 落库到 signals.indicator_values._hexp（纯观测，零下单影响）。
        sr.dir_sum = round(float(dir_sum), 6)
        sr.hp_strength = round(float(hp_strength), 6)
        sr.trend_phase = trend_phase
        sr.factor_raws = {
            "adx": round(float(pf.get("_adx_raw", 0.0)), 2),
            "er": round(float(pf.get("_er_raw", 0.0)), 4),
            "ma": round(float(pf.get("_ma_raw", 50.0)), 2),
            # 【2026-08-25 命名一致性】与其他 6 因子统一用因子名 "bbw"（值为 0~100 带宽分位）。
            # 此前此处用 "bbw_pct"，与 factor_scores/快照(1534)的 "bbw" 不一致，下游按 7 因子名
            # 读 factor_raws.bbw 会拿到 None。现统一为 "bbw"。
            "bbw": round(float(pf.get("_bbw_pct", 50.0)), 2),
            "hurst": round(float(pf.get("_hurst_raw", 0.5)), 3),
            "rsi": round(float(pf.get("_rsi_raw", 50.0)), 2),
            "mm": round(float(f_mm), 4),
        }
        # ── 趋势抢跑候选观测（2026-08-24，顺势轻仓试探影子）──
        # 目标：识别"趋势启动初期"的顺势进场候选（与 reverse_candidate 同范式，只观测不下单）。
        # 触发条件（三条件齐）：
        #   ① phase=="ignite"（系统已产出的"点火"相位 = 启动信号）
        #   ② 微动量同向确认（BUY 要 mm>0 / SELL 要 mm<0）——启动方向明确，非假启动
        #   ③ 位置中低位（BUY 要 pos<0.7 / SELL 要 pos>0.3）——避开极值区，非高位追单
        # 与 reverse_candidate（极值高位动量反向做反向单）方向相反：本候选是【顺势】抢跑。
        # 产出 sr.trend_start_candidate，由 scheduler 落库到 signals.indicator_values._hexp，
        # 供 _reconcile_hexp_shadow 用未来 K 线评估"若轻仓顺势试探能否抓对趋势"（胜率/盈亏比），
        # 验证达标后再启用真试探。零实盘影响（不发布 signal:stream）。
        # 开关 hexp.trend_start_observe_enabled（默认 True）；阈值可热调。
        _trend_start = None
        _ts_observe = bool(cfg.get("hexp.trend_start_observe_enabled", True))
        if _ts_observe and _phase == "ignite" and direction in ("BUY", "SELL"):
            _ts_mm_ok = (direction == "BUY" and f_mm > 0) or (direction == "SELL" and f_mm < 0)
            _ts_hi = float(cfg.get("hexp.trend_start_pos_high", 0.7))
            _ts_lo = float(cfg.get("hexp.trend_start_pos_low", 0.3))
            _ts_pos_ok = (direction == "BUY" and _pos_pct < _ts_hi) or \
                         (direction == "SELL" and _pos_pct > _ts_lo)
            if _ts_mm_ok and _ts_pos_ok:
                _trend_start = {
                    "dir": direction, "phase": _phase,
                    "squeeze": round(_squeeze, 2), "ignite": round(_ignite, 2),
                    "pos": round(_pos_pct, 4), "mm": round(f_mm, 4),
                    "er": round(float(pf.get("_er_raw", 0.0)), 4),
                    "adx": round(float(pf.get("_adx_raw", 0.0)), 2),
                    "close": round(close_v, 3), "atr": round(atr, 4),
                    "verdict": round(verdict, 4), "grade": grade,
                }
                logger.info(
                    "hexp %s %s | TREND START CANDIDATE dir=%s phase=%s squeeze=%.1f "
                    "ignite=%.1f pos=%.2f mm=%.3f er=%.3f — OBSERVE ONLY, no order",
                    symbol, primary, direction, _phase, _squeeze, _ignite,
                    _pos_pct, f_mm, float(pf.get("_er_raw", 0.0)))
        sr.trend_start_candidate = _trend_start
        sr.used_periods = list(periods)
        sr.primary_period = primary
        sr.transition = transition
        sr.reversal = reversal
        sr.reversal_periods = list(reversal_periods)
        sr.lot_reduction = round(red_mult, 3)
        sr.close = close_v
        sr.atr = round(atr, 5)
        _rev_tag = f"{_REVERSAL}[{','.join(reversal_periods)}]" if reversal_periods else "-"
        sr.reason = (f"hexp hp={hp_100:.1f} k={k:.2f} grade={grade} "
                     f"verdict={verdict:+.2f} state={main_state} "
                     f"transition={transition} reversal={_rev_tag} lot×{red_mult:.2f}")

        logger.info(
            "hexp %s: dir=%s hp=%.1f k=%.2f grade=%s verdict=%+.2f total=%.1f passed=%s",
            symbol, sr.direction, hp_100, k, grade, verdict, total, passed)

        # 10) 实时快照发布（面板数据源）
        if live and self._redis is not None and getattr(self._redis, "is_initialized", False):
            try:
                import json as _json
                await self._redis.set(
                    f"hcm:live:hexp:{symbol.upper()}",
                    _json.dumps({
                        "direction": sr.direction, "grade": grade,
                        "hp_score": round(hp_100, 2), "k": round(k, 3),
                        "mm": round(f_mm, 4), "verdict": round(verdict, 4),
                        "scorecard_total": round(total, 2),
                        "scorecard": sr.scorecard,
                        "factor_scores": sr.factor_scores,
                        "factor_raws": {
                            "adx": round(float(pf.get("_adx_raw", 0.0)), 2),
                            "er": round(float(pf.get("_er_raw", 0.0)), 4),
                            "ma": round(float(pf.get("_ma_raw", 50.0)), 2),
                            "bbw": round(float(pf.get("_bbw_pct", 50.0)), 2),
                            "hurst": round(float(pf.get("_hurst_raw", 0.5)), 3),
                            "rsi": round(float(pf.get("_rsi_raw", 50.0)), 2),
                            "mm": round(float(f_mm), 4),
                        },
                        "trend_phase": trend_phase,
                        "period_states": period_states,
                        "trend_scores": sr.trend_scores,
                        "used_periods": list(periods),
                        "primary_period": primary,
                        # 减仓可观测（B-1）：面板可直接看到"为何这单手数变小"
                        "transition": bool(transition),
                        "reversal": bool(reversal),
                        "reversal_periods": list(reversal_periods),
                        "lot_reduction": round(red_mult, 3),
                        "lot_mult": sr.co_exec_lot_mult,
                        "close": close_v, "atr": round(atr, 5),
                        "passed": bool(passed),
                        "reason": sr.reason, "ts": time.time(),
                    }, default=str),
                    ex=15,
                )
            except Exception as exc:
                logger.debug("hexp live publish failed: %s", exc)

        # 11) min_grade 一致性校验（输出契约锁死）——防御性兜底：
        # 任何上游分支（极值追单 extreme_chase / 实时 live 路径 / 未来改动）若在非
        # 法路径把 direction 翻回 BUY/SELL 或 threshold_passed 置 True，此处强制回落，
        # 保证「评级低于 hexp.min_grade 的信号永不进入下单链路」。与 step9 的 min_grade
        # 闸门（1034-1053）双重保险，杜绝单点遗漏导致的「弱评级漏过」回归。
        if not _grade_ok:
            if sr.direction != "NO_TRADE" or sr.threshold_passed:
                logger.warning(
                    "hexp %s | min_grade override: grade=%s < min_grade=%s forced NO_TRADE "
                    "(was dir=%s passed=%s, reason=%s)",
                    symbol, grade, _min_grade, sr.direction, sr.threshold_passed, sr.fallback_reason)
            sr.threshold_passed = False
            sr.direction = "NO_TRADE"
            if not sr.fallback_reason:
                sr.fallback_reason = f"hexp_grade_below_min({grade}<{_min_grade})"

        return sr

    # ─────────────────────── 评分卡辅助 ───────────────────────
    def _entry_score(self, pdata: dict[str, Any], atr: float, cfg: dict[str, Any]) -> float:
        """入场技术分：趋势中贴近 EMA20 为优（回踩位），+ 实体占比。"""
        closes = pdata["closes"]
        if atr <= 0 or len(closes) < 25:
            return 50.0
        ema20 = self._ema(closes, cfg["hexp.ma.ema_fast"])[-1]
        dist_atr = abs(closes[-1] - ema20) / atr
        pos = max(0.0, 100.0 - dist_atr * 40.0)
        body = abs(closes[-1] - (pdata["closes"][-2] if len(closes) > 1 else closes[-1]))
        rng = float(pdata["highs"][-1] - pdata["lows"][-1])
        body_ratio = body / rng if rng > 0 else 0.0
        return max(0.0, min(100.0, pos * 0.6 + body_ratio * 100.0 * 0.4))

    def _position_score(self, pdata: dict[str, Any], atr: float, close_v: float,
                        direction: str = "") -> float:
        """位置优势分：距 Donchian(20) 反向边界的空间（ATR 单位，6ATR 满分）。

        BUG-1 修复（2026-08-13）：原实现用 max(hh-close, close-ll) 取较大空间 → 极值区
        反向空间天然大 → 顶部 BUY / 底部 SELL 反而拿满位置分（"高多低空"被评分奖励）。
        改为方向感知：BUY 奖励距上轨远(close-ll 大)、SELL 奖励距下轨远(hh-close 大)，
        极值追单时反向空间趋 0 → 位置分低 → grade 降 → 被 min_grade 闸门拦住。
        """
        if atr <= 0 or len(pdata["highs"]) < 21:
            return 50.0
        hh = float(np.max(pdata["highs"][-21:-1]))
        ll = float(np.min(pdata["lows"][-21:-1]))
        if direction == "BUY":
            room = (close_v - ll) / atr          # 距下轨远=上方空间大=低位做多有利
        elif direction == "SELL":
            room = (hh - close_v) / atr          # 距上轨远=下方空间大=高位做空有利
        else:                                    # NO_TRADE 兜底：维持旧中性口径
            room = max(hh - close_v, close_v - ll) / atr
        return max(0.0, min(100.0, room / 6.0 * 100.0))

    def _get_donchian_pct(self, pdata: dict[str, Any], atr: float, cfg: dict[str, Any]) -> float:
        """价格相对 Donchian(look) 通道的分位 ∈ [0,1]：0=贴下轨(低位), 1=贴上轨(高位)。

        用于极值硬闸门（2026-08-13）：高位(>0.85)禁止 BUY、低位(<0.15)禁止 SELL，
        根治「动量追逐在极值点追单」的频繁止损。与 _position_score 共用同一通道定义。
        """
        _look = int(cfg.get("hexp.extreme.donchian_look", 20))
        if atr <= 0 or len(pdata["highs"]) < _look + 1:
            return 0.5
        hh = float(np.max(pdata["highs"][-(_look + 1):-1]))
        ll = float(np.min(pdata["lows"][-(_look + 1):-1]))
        if hh - ll <= 1e-9:
            return 0.5
        return max(0.0, min(1.0, (float(pdata["close"]) - ll) / (hh - ll)))

    def _get_recent_pivot(self, pdata: dict[str, Any], direction: str,
                          cfg: dict[str, Any]) -> Optional[float]:
        """近期摆动枢轴（B 回踩支撑位诊断）：BUY 取回看窗口内最低 low（回踩支撑），
        SELL 取最高 high（回踩阻力）。窗口不足返回 None。

        仅用于观测标记：价格在极值回落后落回该枢轴 ±support_atr×ATR 带内，
        即视为「已回踩到支撑位」，方向由 7 因子重新裁定（反转/延续），不强制改方向。
        """
        _look = int(cfg.get("hexp.extreme.support_lookback", 20))
        if len(pdata["highs"]) < _look + 1:
            return None
        if direction == "BUY":
            return float(np.min(pdata["lows"][-(_look + 1):-1]))
        if direction == "SELL":
            return float(np.max(pdata["highs"][-(_look + 1):-1]))
        return None

    def _vol_score(self, pdata: dict[str, Any], atr: float) -> float:
        """波动环境分：ATR 分位 30-70% 最佳（同口径真实波幅序列）。"""
        highs, lows, closes = pdata["highs"], pdata["lows"], pdata["closes"]
        n = len(closes)
        if n < 120 or atr <= 0:
            return 60.0
        atrs = []
        for j in range(n - 100, n):
            if j < 16:
                continue
            a = self._atr(highs[:j + 1], lows[:j + 1], closes[:j + 1], 14)
            if a > 0:
                atrs.append(a)
        if not atrs:
            return 60.0
        pct = float(np.sum(np.array(atrs) <= atr) / len(atrs) * 100.0)
        if 30.0 <= pct <= 70.0:
            return 100.0
        return max(0.0, 100.0 - abs(pct - 50.0) * 2.0)

    @staticmethod
    def _session_score() -> float:
        """时段加成（GMT+8）：伦敦/纽约重叠 20-24 → 100；亚盘 08-15 → 50；其他 70。"""
        h = (time.gmtime().tm_hour + 8) % 24
        if 20 <= h < 24:
            return 100.0
        if 8 <= h < 15:
            return 50.0
        return 70.0

    # ─────────────────────── 指标兜底（独立于 IndicatorCalculator）───────────────────────
    @staticmethod
    def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
        n = len(closes)
        if n < period + 1:
            return 0.0
        trs = []
        for i in range(n - period, n):
            if i < 1:
                continue
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            trs.append(tr)
        return float(np.mean(trs)) if trs else 0.0

    @staticmethod
    def _adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14):
        """标准 Wilder ADX，返回 (adx, +DI, -DI)。数据不足返回 (20,25,25) 中性。"""
        n = len(closes)
        if n < period * 2 + 2:
            return 20.0, 25.0, 25.0
        plus_dm = np.zeros(n)
        minus_dm = np.zeros(n)
        tr = np.zeros(n)
        for i in range(1, n):
            up = highs[i] - highs[i - 1]
            dn = lows[i - 1] - lows[i]
            plus_dm[i] = up if (up > dn and up > 0) else 0.0
            minus_dm[i] = dn if (dn > up and dn > 0) else 0.0
            tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                        abs(lows[i] - closes[i - 1]))
        # Wilder 平滑
        def wilder(x: np.ndarray) -> np.ndarray:
            out = np.zeros_like(x)
            out[period] = np.sum(x[1:period + 1])
            for i in range(period + 1, n):
                out[i] = out[i - 1] - out[i - 1] / period + x[i]
            return out
        str_ = wilder(tr)
        sp = wilder(plus_dm)
        sm = wilder(minus_dm)
        with np.errstate(divide="ignore", invalid="ignore"):
            pdi = np.where(str_ > 0, 100.0 * sp / str_, 0.0)
            mdi = np.where(str_ > 0, 100.0 * sm / str_, 0.0)
            dx = np.where(pdi + mdi > 0, 100.0 * np.abs(pdi - mdi) / (pdi + mdi), 0.0)
        if n < period * 2 + 1:
            return 20.0, float(pdi[-1]), float(mdi[-1])
        adx = np.mean(dx[period + 1:period * 2 + 1])
        for i in range(period * 2 + 1, n):
            adx = (adx * (period - 1) + dx[i]) / period
        return float(adx), float(pdi[-1]), float(mdi[-1])
