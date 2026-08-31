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
    # 六因子基础权重（运行时归一）——NEUTRAL 方案基线（2026-08-26 矫正：
    # 原趋势组 adx/er/ma=70 过高、rsi/hurst 均值回归因子过低，导致 NEUTRAL 高位
    # 只追趋势多、不反向。降趋势组、升 rsi/hurst/mm，使 NEUTRAL 高位自然偏向反向。）
    "hexp.factor.adx_weight": 15.0,
    "hexp.factor.er_weight": 15.0,
    "hexp.factor.ma_weight": 15.0,
    "hexp.factor.bbw_weight": 17.0,
    "hexp.factor.hurst_weight": 13.0,
    "hexp.factor.rsi_weight": 13.0,
    "hexp.factor.mm_weight": 16.0,
    # 体制感知因子权重方案（BUG-3 修复）：三套 7 因子权重（adx/er/ma/bbw/hurst/rsi/mm），
    # 运行时按 M5 regime_result 体制 + 强度在方案间连续混合。缺省回退旧固定权重基线。
    "hexp.factor_weights_json": json.dumps({
        # 2026-08-27 结构根因矫正：降滞后组(adx/er/ma)权重、升微动量(mm)，并适度抬升
        # rsi(超买超卖反转感知)——根治"滞后组≈64% 锁死方向 → 趋势确立才给方向 → 高位追单"。
        #   trend   滞后组 64%→约50%、mm 13.5%→约24%
        #   neutral 滞后组 61%→约46%、mm 13%→约21%
        # range 方案滞后组本就≈27%(震荡市均值回归)，保持不变。
        "trend":   {"adx": 16.0, "er": 18.0, "ma": 16.0, "bbw": 3.0,  "hurst": 14.0, "rsi": 8.0, "mm": 24.0},
        "range":   {"adx": 9.0,  "er": 9.0,  "ma": 9.0,  "bbw": 26.0, "hurst": 9.0,  "rsi": 22.0, "mm": 16.0},
        "neutral": {"adx": 18.0, "er": 18.0, "ma": 16.0, "bbw": 12.0, "hurst": 12.0, "rsi": 10.0, "mm": 22.0},
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
    "hexp.mtf.flip_bars": 4,          # K：决定性反向持续 K 次 update 才翻向(原2→4，过滤下跌中继小反弹)
    "hexp.mtf.flip_slope_mult": 1.8,  # 斜率方向强度阈值(原1.0→1.8，反弹需更强才"决定性")
    # 微结构动量（幂律）
    "hexp.mm.alpha": 0.5,
    "hexp.mm.window": 20,
    "hexp.mm.period": "M1",
    "hexp.mm.scale": 0.002,
    "hexp.mm.accel_k_boost": 0.3,
    "hexp.mm.accel_threshold": 0.7,
    "hexp.mm.ema_alpha": 0.8,      # 护栏判定用 mm 的 EMA 平滑值(消除瞬时 mm 抖动卡点)
    # 共振矩阵
    "hexp.mtf.weight_D1": 0.25,
    "hexp.mtf.weight_H4": 0.35,
    "hexp.mtf.weight_H1": 0.25,
    "hexp.mtf.weight_M30": 0.15,
    # 2026-08-28 闸门清除：G1 MTF 多周期共识否决已移除（不再翻转/降级方向）。
    # verdict 仍用于共振调分与观测(sr.mtf_dir/flat_band)，仅取消"否决动作"。
    "hexp.mtf.flat_band": 0.15,              # |verdict|<此值视为无共识（观测用）
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
    "hexp.zone.grade_align_enabled": True,  # zone 方向对齐 → 对 grade 判定加权
    "hexp.zone.grade_bonus": 2.0,           # zone 对齐加权：分/strength 档 × 距离近度
    "hexp.zone.lowpos_bonus": 4.0,          # 低位(RSI超卖)+zone对齐 额外加分：修复低位启动不下单
    "hexp.extreme.rsi_chase_high": 72.0,    # RSI 超买阈值：强趋势高位禁追(不受通道拉宽稀释)
    "hexp.extreme.rsi_launch_low": 35.0,    # RSI 超卖阈值：低位启动可放大放行
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
    # 【2026-08-28 趋势单保护】transition 减仓仅看主周期：原 transition=any(周期==TRANSITION)
    # 只要任意一个周期(哪怕 D1 日线)在犹豫带就整体减半仓，误伤"主周期明确趋势"的顺势趋势单
    # （实锤 state=TREND_DOWN 趋势单因辅助周期 TRANSITION 被 lot×0.50）。开启后仅当主执行
    # 周期(primary)处于 TRANSITION(真正犹豫带)才触发 transition 减仓；辅助周期犹豫不影响。
    "hexp.exec.transition_primary_only": True,   # True→transition 减仓只看主周期(趋势单保护)
    # 极值区手数递减（2026-08-21 方案2）：处于 Donchian 极值区(_in_extreme)且未被护栏封单的
    # 放行单，按此折扣降仓（与 transition/reversal 用 min() 聚合，不叠加重复打折）。
    # 解决「高位动量减弱反转仍满档手数」——没拦住也减半仓。
    # 2026-08-28 闸门清除：D3 极值区降仓(extreme_lot_mult)、D4 波动率缩放(vol_scale_*) 已移除。
    # 方向判定
    "hexp.direction_min_score": 0.20,
    # 2026-08-27 方向迟滞死区：dir_sum 在 0 附近微动会跨阈值翻转 → direction 闪烁。
    # 维持上一根方向，仅当候选反向且 |dir_sum| 跨过此死区才翻向（NO_TRADE 不受阻）。
    "hexp.direction_hysteresis": 0.06,
    # 方向强制翻转阈值：|dir_sum| >= 此值（趋势级反向）或 MTF 周期共识反向 → 绕过死区立即翻，
    # 避免迟滞"死黏"首次方向（如下跌趋势被位置因子顶在小幅 → 永久 BUY 死标签）。
    "hexp.direction_hysteresis_strong": 0.20,
    # 位置因子在趋势态降权系数：趋势/反转态下 pos_factor 权重乘此值（默认 0.25），
    # 避免下跌趋势 pos_cycle 低位强推 BUY 与真实 SELL 反向（死标签根因）。
    "hexp.pos_factor.trend_scale": 0.25,
    # 位置因子（BUG-1 修复 2026-08-13）：Donchian 分位 → 均值回归方向因子参与方向裁决。
    # f_pos=(0.5-pos_pct)*2 ∈[-1,+1]：底部→正(托BUY)、顶部→负(压SELL)、中部≈0 无影响。
    # weight 为 dir_sum 的绝对权重（7 因子归一后和为 1）。2026-08-27 由 0.15→0.30：
    # 强化"底部托 BUY / 顶部压 SELL"，在方向上直接压制高位追单（结构根因矫正方案 2）。
    "hexp.pos_factor.enabled": True,
    # 2026-08-27 A1 顶权：由 0.30→0.42。趋势因子(ma/di/er)在高位仍给 +，是"高位追多"根因。
    # f_pos 顶部=-1、ma 顶部=+1，权重 0.30 时趋势项碾压位置项 → 高位仍 BUY。提到 0.42 让
    # 顶部 f_pos=-1 能压过 ma=+1（0.42*1 > 0.30*1+残余），方向上直接掐掉高位接刀。
    "hexp.pos_factor.weight": 0.42,
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
    # 【2026-08-28 闸门清除】G5b 高位微正枯竭加固(momentum_hi_weak_*) 已移除。
    # 【2026-08-28 趋势单保护】momentum_flip 纯动量(M1 f_mm_s)否决不辨趋势结构，会把
    # 趋势单在正常回撤/换手中的 M1 动量短暂反向(mm=-0.01~-0.18)误判为"动量反转"而拦掉
    # （实锤 388642131-163 连续 BUY 被拦、388642161 BUY mm=-0.4445 趋势单被拦）。
    # 趋势单推进中动量回撤是常态，不应等同"真反转"。优化：仅放行"顺势趋势单"
    # （主周期 period_states 处于 TREND_UP/DOWN 且方向与趋势同向；或 ADX≥trend_adx
    # 且 ma 方向与信号一致）时，改用更宽的反向阈值 flip_trend_mm（需剧烈反向才拦），
    # 放行趋势单正常回调；逆势单/弱趋势/震荡仍用基础 flip_mm（敏捷拦逆动量）。
    # 仅调阈值不动拦截路径，保留 momentum_flip 对"真反转"的防护。开关默认开，可热回退旧行为。
    "hexp.momentum_flip_trend_enabled": True,   # 趋势单保护总开关；False→退回纯动量否决(旧行为)
    "hexp.momentum_flip_trend_adx": 25.0,       # 辅助判据：主周期 ADX≥此视为强趋势(ADX>25 标准强趋势线)
    "hexp.momentum_flip_trend_mm": 0.15,        # 顺势趋势单放宽反向阈值：|mm|≥此(剧烈反向)才拦
    # 【2026-08-26 P0-2 震荡市均值回归校验】NEUTRAL/RANGE 市(hurst<0.5 均值回归态)中，
    # 高位(Donchian 分位接近极值)顺势追单易被均值回归反打（实证 sig=388640598 BUY@4666.08
    # pos=0.846 hurst=0.449 regime=NEUTRAL 高位追多被止损）。震荡市应等回撤/极值反向，
    # 不应在高低位顺势追单。本闸门：NEUTRAL/RANGE + hurst<hurst_max + pos 高位追高(BUY)/低位追低(SELL) → 拦。
    "hexp.range_hurst_enabled": True,          # 震荡市 hurst 均值回归校验总开关；False→关闭(向后兼容)
    "hexp.range_hurst_max": 0.50,              # hurst 均值回归阈值：<此视为均值回归态(反持续)
    "hexp.range_hurst_hi": 0.80,               # 高位分界：BUY pos>此 / SELL pos<(1-此) 视为极值区追单
    "hexp.range_hurst_regimes": "NEUTRAL,RANGE",  # 启用本校验的体制（逗号分隔）
    # 防抵消（方案1·位置调制权重）：NEUTRAL/RANGE 均值回归态消除 dir_sum 因子抵消。
    "hexp.anti_cancel.enabled": True,   # 总开关；False→关闭(向后兼容)
    "hexp.anti_cancel.curve": 1.0,      # 位置调制强度(指数)：越大越极端时越放大反向/压缩趋势
    "hexp.anti_cancel.ma_floor": 0.35,  # ma 权重保留底权(防趋势因子彻底失聪)
    # 反向单观测（2026-08-21，先观测不下单）：momentum_flip 判动量反向且处于高位/低位时，
    # 记录反向候选(dir/pos/er/mm/close/verdict) 落库到 indicator_values._hexp.reverse_candidate，
    # 供后续 SQL 对照未来 K 线评估"若做反向单的胜率"，验证后再启用真下单。零实盘影响。
    "hexp.reverse_candidate_enabled": True,   # 观测开关；False→不记录反向候选
    "hexp.reverse_candidate_hi": 0.7,          # BUY被拦→SELL候选 的高位分位阈值(pos>此)
    "hexp.reverse_candidate_lo": 0.3,          # SELL被拦→BUY候选 的低位分位阈值(pos<此)
    # 【2026-08-26 反向候选精准化】与极值护栏(momentum_reversed 三条件)同口径，
    # 反向候选除"动量翻转+高位"外，再要求「长影线确认」+「效率比枯竭确认」，
    # 避免把"回调中动量暂歇"误判成"极值位反转接刀"（假候选偏多）。以下两开关可热调。
    "hexp.reverse_candidate_wick_enabled": True,   # 长影线确认：顶部需长上影/底部需长下影才产候选
    "hexp.reverse_candidate_er_enabled": True,     # 效率比枯竭确认：er<阈值(同 drain_er)才产候选
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
               flip_slope_mult: float = 1.0,
               confirm_enter: Optional[float] = None) -> str:
        # 方案2（2026-08-26）：方向强确认快速通道。
        # 原迟滞状态机进 TREND 需 trend_score >= enter(默认60)，下跌/上涨初期
        # 强度分常落在 40~60 区间反复横跳 → 状态机持续迟滞、不跟趋势（"集体失聪"主因）。
        # 当 direction 已确定（ma 与 DI 同向，pdir!=0）且强度分达 enter 的 confirm_enter
        # 比例（默认 0.7 → 42 分）时，视为趋势确认，用更低门槛进入 TREND，
        # 使 M30/H4 等中周期在趋势早期即报 TREND_UP/DOWN，避免共振矩阵被 RANGE 稀释。
        # confirm_enter 可由配置 hexp.state.confirm_enter_ratio * enter 注入，热可调。
        # B-1 修复（2026-08-11）：每次 update 入口重置反转标记，仅反映本次调用是否发生动量翻转。
        self.reversed = False
        # ── 动量翻转（消除高周期迟滞滞后）──
        # 当前处于确定性趋势态，但近 flip_window 根该周期收盘斜率决定性反向
        # 持续 flip_bars 次 update → 立即翻向，绕过 enter/exit 迟滞阈值。
        if (flip_enabled and self.state in (_TREND_UP, _TREND_DOWN)
                and closes is not None and flip_bars >= 1 and flip_window >= 1):
            sdir = self._slope_dir(closes, flip_window, flip_slope_mult)
            # 主趋势保护：用更长周期(2×flip_window)看中长期方向。若想翻向的方向
            # 与中长期主趋势相反(如下跌主趋势想翻 BUY)，禁止逆势接刀，重置计数。
            ma_sdir = self._slope_dir(closes, flip_window * 2, flip_slope_mult)
            opposite = (self.state == _TREND_UP and sdir == -1) or \
                       (self.state == _TREND_DOWN and sdir == 1)
            if opposite:
                forced = _TREND_DOWN if self.state == _TREND_UP else _TREND_UP
                ma_conflict = (forced == _TREND_UP and ma_sdir == -1) or \
                              (forced == _TREND_DOWN and ma_sdir == 1)
                if ma_conflict:
                    # 中长期主趋势仍逆翻向目标 → 逆势接刀，禁止翻转(下跌中不出 BUY 接刀)
                    self._flip_count = 0
                else:
                    self._flip_count += 1
            else:
                self._flip_count = 0
            if self._flip_count >= flip_bars:
                logger.info(
                    "hexp MTF momentum flip %s: %s→%s slope_dir=%d ma_dir=%d flip_bars=%d mult=%.2f",
                    label, self.state, forced, sdir, ma_sdir, flip_bars, flip_slope_mult)
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
        # 方案2 快速通道：方向已确认(direction!=0) 且 强度分达 enter 的 confirm_enter 比例
        # （默认 0.7→42分）即视为趋势确认，用更低门槛进入 TREND，避免中周期趋势早期失聪。
        _ce = enter * 0.7 if confirm_enter is None else confirm_enter
        if direction != 0 and trend_score >= _ce:
            target = _TREND_UP if direction > 0 else _TREND_DOWN
        elif trend_score >= enter and direction != 0:
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
        # ── 2026-08-26 抗抖动：EMA 平滑 + grade 迟滞状态（每 symbol 独立）──
        # _ema_state: 最近一次平滑后的 hp_100 / total，做指数移动平均消抖。
        self._ema_state: dict[str, dict] = {}
        # _grade_hyst: 当前档位 + 待切换候选 + 连续确认计数（迟滞带内维持 prev）。
        self._grade_hyst: dict[str, dict] = {}
        # 2026-08-27 方向迟滞死区状态（每 symbol 独立）：消除 dir_sum 在 0 附近跨阈值
        # 翻转导致的 direction 闪烁（BUY/SELL/NO_TRADE 频繁跳）。
        self._dir_hyst_state: dict[str, str] = {}

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

    # ─────────────────────── 抗抖动：EMA 平滑 + grade 迟滞（2026-08-26）───────────────────────
    @staticmethod
    def _ema_smooth(state: dict, key: str, cur: float, alpha: float) -> float:
        """对单 symbol 的 hp_100 / total 做指数移动平均，消除单 K 线瞬时抖动。

        alpha∈(0,1]：越大越贴近当前值（响应快、平滑弱），越小越平滑（滞后大）。
        默认 0.35（见 hexp.score.ema_alpha）。首次无历史则用当前值初始化。
        """
        try:
            _cur = float(cur)
        except (TypeError, ValueError):
            _cur = 0.0
        _a = max(0.0, min(1.0, float(alpha)))
        _prev = state.get(key)
        if _prev is None:
            state[key] = _cur
            return _cur
        _sm = _a * _cur + (1.0 - _a) * float(_prev)
        state[key] = _sm
        return _sm

    @staticmethod
    def _grade_rank(g: str) -> int:
        return {"S": 4, "A": 3, "B": 2, "C": 1, "RED": 0}.get(g, 0)

    @staticmethod
    def _grade_by_bounds(total: float, hp: float, gap: float, enter: bool,
                         a_t: float, b_t: float, p_t: float, s_hp: float) -> str:
        """严格升档(enter=True, 用 +gap 高边界) 或 保底降档(enter=False, 用 -gap 低边界) 求档位。"""
        s = gap if enter else -gap
        if total >= a_t + s and hp >= s_hp + s:
            return "S"
        if total >= a_t + s or (hp >= s_hp + s and total >= b_t + s):
            return "A"
        if total >= b_t + s:
            return "B"
        if total >= p_t + s:
            return "C"
        return "RED"

    def _resolve_grade_hyst(self, symbol: str, total: float, hp: float,
                            gap: float, cbars: int) -> str:
        """grade 迟滞裁决：升档需超 enter 边界、降档需跌破 exit 边界，且连续 confirm_bars 根确认。

        迟滞带内（介于 enter 与 exit 之间）维持上一档，避免 RED↔A 瞬时跳变。
        """
        cfg = self._cfg
        a_t = float(cfg.get("hexp.scorecard.a_threshold", 75.0))
        b_t = float(cfg.get("hexp.scorecard.b_threshold", 52.0))
        p_t = float(cfg.get("hexp.scorecard.pass_threshold", 50.0))
        s_hp = float(cfg.get("hexp.scorecard.s_hp_min", 60.0))
        st = self._grade_hyst.setdefault(symbol, {"grade": "RED", "pending": None, "cnt": 0})
        _prev = st["grade"]
        # 升档候选（需 +gap 边界）、降档候选（跌破 -gap 边界）
        _enter = self._grade_by_bounds(total, hp, gap, True, a_t, b_t, p_t, s_hp)
        _exit = self._grade_by_bounds(total, hp, gap, False, a_t, b_t, p_t, s_hp)
        _prev_rank = self._grade_rank(_prev)
        _enter_rank = self._grade_rank(_enter)
        _exit_rank = self._grade_rank(_exit)
        # 升档：仅当 enter 档高于当前；降档：仅当 exit 档低于当前；否则维持
        if _enter_rank > _prev_rank:
            _target = _enter
        elif _exit_rank < _prev_rank:
            _target = _exit
        else:
            _target = _prev
        if _target == _prev:
            st["pending"] = None
            st["cnt"] = 0
            return _prev
        # 连续确认（抗抖动核心）：
        #  - 升档：保持"待升级的最高档"累计，抖动到较低档不重置（等回高档再续）
        #  - 降档：保持"待降级的最低档"累计，抖动到较高档不重置
        #  - 只有方向反转（由升转降或反之）才重置计数
        _tgt_rank = self._grade_rank(_target)
        _pend_rank = self._grade_rank(st["pending"]) if st["pending"] else _prev_rank
        _is_up = _tgt_rank > _prev_rank
        _pend_is_up = _pend_rank > _prev_rank
        if st["pending"] is None or _is_up != _pend_is_up:
            # 首设或方向反转：重置
            st["pending"] = _target
            st["cnt"] = 1
        else:
            # 同向：维持该方向累计（升档时取更高档、降档时取更低档）
            if _is_up:
                if _tgt_rank >= _pend_rank:
                    st["pending"] = _target
                    st["cnt"] += 1
                # 抖动到较低档：保持 pending 与 cnt 不变，等回高档续累计
            else:
                if _tgt_rank <= _pend_rank:
                    st["pending"] = _target
                    st["cnt"] += 1
                # 抖动到较高档：保持 pending 与 cnt 不变
        if st["cnt"] >= max(1, int(cbars)):
            st["grade"] = st["pending"]
            st["pending"] = None
            st["cnt"] = 0
        return st["grade"]

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
            # 2026-08-27 A3 趋势末端衰减：ma_raw>90（极高多头/空头位）时非线性衰减 f_ma，
            # 削掉"趋势加速末端"的满 +1 动力——这是高位开多/低位开空止损的直接驱动力。
            # ma_raw=90→衰减 1.0(不削)；ma_raw=100→衰减 0.4（f_ma 仅剩 40% 推力）。
            f_ma = (ma_raw - 50.0) / 50.0  # → [-1,+1]
            if ma_raw > 90.0:
                _decay = max(0.4, 1.0 - (ma_raw - 90.0) / 10.0 * 0.6)
                f_ma *= _decay

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
        zone_level: float = 0.0,
        zone_type: str = "",
        zone_strength: int = 0,
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
            # 【2026-08-26 方案3精准版】hurst/rsi 反向抑制按体制条件化：
            #   · 震荡市(NEUTRAL/RANGE)：保留带符号抑制，防均值回归市误判趋势（原行为）。
            #   · 趋势市(TREND/PRE_TREND)：放开抑制，hurst/rsi 改取 abs（纯强度）。
            #     早期趋势常伴随 hurst<0.5(均值回归态)与 rsi 极端，原带符号会把这些
            #     趋势早期特征当"反转前兆"减分，导致长周期(H1/H4) trend_score 被压低、
            #     趋势早期集体失聪（下跌40+仍判 RANGE）。趋势市放开后 H1/H4 能更真实反映趋势强度。
            #   由配置 hexp.state.trendscore_hurst_rsi_suppress 控制总开关（默认开启=震荡市抑制）。
            _suppress = bool(cfg.get("hexp.state.trendscore_hurst_rsi_suppress", True))
            # 2026-08-27 修复：TREND_FADE 是趋势态（仅强度衰减），原只认 TREND/PRE_TREND 会导致
            # 成熟下跌趋势（ADX 连续降被标 TREND_FADE）被当非趋势 → rsi/hurst 带符号负 → ts 被拉低
            # → 全周期 RANGE + direction 失真。纳入趋势态语义。
            _trend_regime = regime_tag in ("TREND", "PRE_TREND", "TREND_FADE")
            _hr_abs = _trend_regime and _suppress   # 趋势市+开关开 → 不抑制(取abs)
            _hurst_term = abs(fac["hurst"]) if _hr_abs else fac["hurst"]
            _rsi_term = abs(fac["rsi"]) if _hr_abs else fac["rsi"]
            ts = (
                wn["adx"] * abs(fac["adx"]) +
                wn["er"] * abs(fac["er"]) +
                wn["ma"] * abs(fac["ma"]) +
                wn["bbw"] * abs(fac["bbw"]) +
                wn["hurst"] * _hurst_term +   # 趋势市取abs(不抑制)；震荡市带符号(抑制反转前兆)
                wn["rsi"] * _rsi_term          # 同上
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
                    confirm_enter=float(cfg["hexp.state.enter_score"])
                        * float(cfg.get("hexp.state.confirm_enter_ratio", 0.7)),
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
        # 2026-08-27 周期价格位置：长 lookback 滚动极值分位 + ATR-z 偏离，抗趋势稀释
        # （替代单纯 Donchian 20 根分位在趋势中失准的问题）。用于位置因子与极值守卫。
        _pos_cycle, _pos_z = self._get_cycle_position(period_data[primary], atr, cfg)
        # 绝对超买/超卖度量（修复 2026-08-27 缺陷）：Donchian 通道分位 _pos_pct 在强趋势
        # (ADX>30) 下被拉宽的通道稀释，永远<0.7~0.85 → 所有"高位禁追"守卫漏判 → 高位接刀。
        # RSI(14) 不随通道拉宽稀释：强趋势高位持续>70(超买)、低位持续<30(超卖)。
        # 此处取真实 RSI 作位置语义度量，与 HP-Score 内 rsi 强度因子互不冲突。
        _rsi = float(getattr(m5_indicators, "rsi_14", 50.0) or 50.0)

        # 5) HP-Score（方向=加权和符号，强度=幂加权和开方）
        # 体制感知：全 7 因子均取自 regime-aware factor_scheme（含 mm），归一化后用于 HP-Score。
        w_full = {k: factor_scheme[k] for k in ("adx", "er", "ma", "bbw", "hurst", "rsi", "mm")}
        wtot = max(sum(w_full.values()), 1e-9)
        w_full = {kk: vv / wtot for kk, vv in w_full.items()}
        fvec = {kk: pf[kk] for kk in wn}
        fvec["mm"] = f_mm
        # 防抵消（方案1·位置调制权重）：NEUTRAL/RANGE 均值回归态下，用 Donchian 位置
        # 调制各因子在方向裁决(dir_sum)的权重——趋势因子 ma 越极端越降权、反向因子
        # rsi/hurst/mm 越极端越升权，消除"ma 推多 vs rsi/hurst 推空"的因子抵消
        # （2026-08-26 NEUTRAL 均值回归矫正引入的缺陷）。仅改 dir_sum 方向裁决，
        # 不动 w_full → HP-Score 强度(pow_sum)不受影响。
        # 趋势早发现保护：仅均值回归态(hurst<0.5)才调制；ma 保留底权防彻底失聪。
        w_dir = dict(w_full)
        if (cfg.get("hexp.anti_cancel.enabled", True)
                and regime_tag in ("NEUTRAL", "RANGE")
                and float(pf.get("_hurst_raw", 0.5)) < float(cfg.get("hexp.range_hurst_max", 0.50))):
            _ex = abs(_pos_pct - 0.5) * 2.0          # 0(中位)~1(极值)
            _curve = float(cfg.get("hexp.anti_cancel.curve", 1.0))
            _ma_floor = float(cfg.get("hexp.anti_cancel.ma_floor", 0.35))
            _trend_down = (1.0 - _ex) ** _curve       # 极值→0
            _rev_up = (1.0 + _ex) ** _curve           # 极值→2
            w_dir["ma"] = max(w_dir["ma"] * _trend_down, w_dir["ma"] * _ma_floor)
            w_dir["rsi"] *= _rev_up
            w_dir["hurst"] *= _rev_up
            w_dir["mm"] *= _rev_up
            _ws = sum(w_dir.values())
            if _ws > 0:
                w_dir = {kk: vv / _ws for kk, vv in w_dir.items()}
        dir_sum = sum(w_dir[kk] * fvec[kk] for kk in w_dir)
        # BUG-1 修复：位置因子 f_pos 参与方向裁决（仅方向，不进 pow_sum/hp 强度）。
        # 底部下跌趋势中 f_pos>0 对冲滞后因子净空 → 不再"低空"；顶部反之；中部 f_pos≈0 无影响。
        f_pos = 0.0
        if bool(cfg.get("hexp.pos_factor.enabled", True)):
            # 2026-08-27: 位置因子改用 pos_cycle（长 lookback 滚动极值分位），
            # 替代易被趋势拉宽稀释的 Donchian 20 根分位，使趋势中也能识别"已到高位/低位"。
            f_pos = (0.5 - _pos_cycle) * 2.0
            _pos_w = float(cfg.get("hexp.pos_factor.weight", 0.15))
            # 2026-08-27 修复死标签：趋势/反转态下位置因子降权（trend_scale 默认 0.25）。
            # 原 0.30 权重在下跌趋势中 pos_cycle 低位 → f_pos 强正 → 持续推 BUY，与真实
            # SELL 行情反向且把 dir_sum 压在死区内 → 迟滞死黏 BUY（"死标签"）。位置因子
            # 本意是 RANGE/NEUTRAL 均值回归抄底摸顶，趋势态不应强推反向方向。
            if regime_tag in ("TREND", "PRE_TREND", "TREND_FADE"):
                _pos_w *= float(cfg.get("hexp.pos_factor.trend_scale", 0.25))
            dir_sum += _pos_w * f_pos
        pow_sum = sum(w_full[kk] * (abs(fvec[kk]) ** k) for kk in w_full)
        hp_strength = pow_sum ** (1.0 / k) if pow_sum > 0 else 0.0
        hp_100 = hp_strength * 100.0

        dmin = cfg["hexp.direction_min_score"]
        # 候选方向（硬阈值）
        if abs(dir_sum) < 0.01 and max(abs(v) for v in fvec.values()) < dmin:
            cand = "NO_TRADE"
        elif dir_sum > 0:
            cand = "BUY"
        else:
            cand = "SELL"
        # 2026-08-27 方向迟滞死区 + 强制翻转通道：
        # 死区防抖：cand 与上一方向相反且 |dir_sum| 在死区内 → 维持上一方向（消除小幅噪声翻转）。
        # 强制翻转：cand 大幅反向（|dir_sum| >= strong_hyst）或 MTF 周期共识反向 → 绕过死区立即翻，
        #   否则会"死黏"首次方向（如下跌趋势被位置因子顶在小幅 → 永远 BUY 死标签）。
        # NO_TRADE 不受迟滞阻碍（弱信号可直接收口）。
        _dir_hyst = float(cfg.get("hexp.direction_hysteresis", 0.06))
        _dir_hyst_strong = float(cfg.get("hexp.direction_hysteresis_strong", 0.20))
        _prev_dir = self._dir_hyst_state.get(symbol, "NO_TRADE")
        if _prev_dir in ("BUY", "SELL") and cand in ("BUY", "SELL") and cand != _prev_dir:
            # 计算 MTF 周期共识方向（主周期不自我裁决；取非 RANGE 的相反 TREND 计数）
            _cons_n = 0
            _cons_opp = 0
            for _p, _st in period_states.items():
                if _p == primary:
                    continue
                if _st in ("TREND_UP", "TREND_DOWN"):
                    _cons_n += 1
                    if (_st == "TREND_UP" and _prev_dir == "SELL") or (_st == "TREND_DOWN" and _prev_dir == "BUY"):
                        _cons_opp += 1
            _consensus_opp = _cons_n > 0 and _cons_opp >= max(1, _cons_n // 2)
            _big_reverse = abs(dir_sum) >= _dir_hyst_strong
            if _big_reverse or _consensus_opp:
                pass  # 趋势级/共识级反向 → 翻向 cand
            elif abs(dir_sum) < _dir_hyst:
                cand = _prev_dir  # 死区内小幅噪声 → 维持防抖
            else:
                cand = _prev_dir  # 中间地带（hyst<=|ds|<strong 且无共识）→ 保守维持
        self._dir_hyst_state[symbol] = cand
        direction = cand

        # 6) 多周期共振裁决（M5 主执行周期权重=0，不自我裁决）
        # 2026-08-28：MTF 加权共识直接进方向裁决（不止于调分，见下方方向闸）。
        # 先算 verdict（含 M30/H1/H4/D1 按 weight_* 加权），供方向闸与评分段共用。
        verdict = 0.0
        wsum_r = 0.0
        n_eff = 0  # 有效（非RANGE）周期计数
        for p in periods:
            if p == primary:
                continue
            wp = cfg.get(f"hexp.mtf.weight_{p}", 0.0)
            if not isinstance(wp, (int, float)) or wp <= 0:
                continue
            st = period_states.get(p, _RANGE)
            if st == _TREND_UP:
                pv = 1.0
            elif st == _TREND_DOWN:
                pv = -1.0
            elif st == _RANGE:
                # 方案 A（2026-08-27）：区间震荡反向共识，消除"全 RANGE→resonance=0"塌缩。
                # 原 pv=0 跳过导致纯震荡市共振维恒为 0（权重 12% 系统性压低 total），
                # 与 extreme.auto_on_regimes(RANGE 开启反向硬封) / range_hurst 护栏 /
                # anti_cancel(RANGE 反向因子升权) 的"RANGE 可反向交易"语义自相矛盾。
                # 现按主周期位置/RSI 推导反向共识：高位→一致做空(-1)、低位→一致做多(+1)、
                # 中位→无共识(0)。与 extreme guard / range_hurst 口径统一；趋势市分支不变。
                if _pos_pct > float(cfg.get("hexp.mtf.range_hi", 0.7)) \
                        or _rsi > float(cfg.get("hexp.extreme.rsi_launch_high", 70.0)):
                    pv = -1.0
                elif _pos_pct < float(cfg.get("hexp.mtf.range_lo", 0.3)) \
                        or _rsi < float(cfg.get("hexp.extreme.rsi_launch_low", 35.0)):
                    pv = 1.0
                else:
                    pv = 0.0
            else:  # TRANSITION
                pv = 0.0
            # 方案1（2026-08-26）：RANGE 周期不计入共振分母。
            # 原实现 pv=0 时仍累加 wsum_r，导致已确认方向的周期被未定态(RANGE)
            # 周期稀释（如 D1=TREND_UP 贡献 0.15 被 4 个 RANGE 周期分母摊薄到 0.17），
            # 共振 verdict 趋近 0、resonance 维塌缩。现仅对 pv!=0 的周期累计分母，
            # RANGE 周期（方案 A 后仅中位无共识者）既不加分也不减分、更不稀释，
            # 使趋势信号不被噪声周期压制。
            if pv == 0:
                continue
            verdict += pv * float(wp)
            wsum_r += float(wp)
            n_eff += 1
        if wsum_r > 0:
            verdict /= wsum_r
        # 方案4（2026-08-26）：最小有效周期数约束 —— 防单周期拉满 verdict 假象。
        # 共振本意是"多周期共同确认"，但原逻辑仅 1 个非RANGE周期（如只剩 D1=TREND_UP、
        # 其余 M30/H1/H4 全 RANGE）即可把 verdict 拉到 ±1.00 满分，制造"强多/强空"假象，
        # 而小周期实际无方向（NO_TRADE）。引入置信折扣：有效周期数越少，verdict 越被压低。
        #   conf = min(1.0, n_eff / min_periods)
        #   n_eff=1 → ×0.5（单周期弱信号）；n_eff≥2 → ×1.0（满置信）
        # min_periods=1 时 conf 恒为 1.0，完全退化为原行为，可热调、向后兼容。
        _min_periods = float(cfg.get("hexp.resonance.min_periods", 2.0))
        if _min_periods > 0 and n_eff > 0:
            _conf = min(1.0, n_eff / _min_periods)
            verdict *= _conf

        # ── 2026-08-28：MTF 加权共识直接进方向裁决（不止于调分）──
        # verdict 现为含 M30/H1/H4/D1 按 weight_* 加权的多周期共识 ∈[-1,1]。
        # 主周期候选方向(direction)若与加权共识相反，按共识强度递减处置：
        #   |verdict|>=veto_reverse 且反向 → 翻向共识方向（多周期合力否决主周期）
        #   |verdict|>=veto_notrade 且反向 → 降级 NO_TRADE（中强反向，不贸然反向也不强推）
        #   |verdict|<flat_band → 无共识，不干涉主周期方向
        # 单一周期(如 M30)权重仅 0.15，无法独断；需 H1/H4/D1 多数同向才能越过阈值，
        # 即"多周期共振"本意。veto_enabled=False 时完全退回旧行为(verdict 只调分)。
        sr.mtf_verdict = float(verdict)
        _flat = float(cfg.get("hexp.mtf.flat_band", 0.15))
        sr.mtf_dir = "BUY" if verdict > _flat else ("SELL" if verdict < -_flat else "")
        # 【2026-08-28 闸门清除】G1 MTF 多周期共识否决已移除：主周期方向不再被
        # 多周期加权共识翻转/降级。verdict 仍保留观测(sr.mtf_dir)与共振调分，仅取消"否决"动作。
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
        # 2026-08-26 修复：HP-Score 物理上限封顶 [0,100]。
        # tailwind_bonus（顺风共振加成，124行默认0=不虚涨）若被热配成 >0，
        # hp_100 会突破 100 且污染 state 维（权重27%）致 total 虚高、面板显示 100+。
        # 此处统一 clamp，保证 hp_score 与 6维卡 state 维物理意义一致。
        hp_100 = min(100.0, max(0.0, float(hp_100)))

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

        # 8) 分级（2026-08-26：先 EMA 平滑强度，再 grade 迟滞，抗瞬时 RED↔A 跳变）
        # ── BUG 修复：仅对 hp_100（强度维）做 EMA，绝不对 total 整体做 EMA ──
        # 原实现对 total 整体 EMA，而 total 已含 state 维(=hp_100, 权重27%)，
        # 等于把 hp 平滑了两次、且把共振/入场/仓位/波动/时段等本不该平滑的维也平滑，
        # 导致落库分值与实时行情脱节、表现为"乱跳"。正确做法：平滑 hp_100 →
        # 回写 sc["state"] → 用平滑后的评分卡重算 total（仅强度维被平滑）。
        _ema_alpha = float(cfg.get("hexp.score.ema_alpha", 0.35))
        # EMA 平滑强度维（仅 hp_100，已修复双重平滑）。每 tick 推进是 EMA 的标准用法：
        # 用 alpha 控制平滑强度，tick 级噪声由 alpha 吸收，不存在"乱跳"（乱跳真因是
        # 双重平滑，已修）。切勿用时间 gate 限制推进频率——那会丢弃窗口内所有 tick 的
        # 实时值、只保留边界瞬时值，使指标在窗口内冻结、严重脱离实时行情。
        _es = self._ema_state.setdefault(symbol, {"hp": None, "mm": None})
        # B 修复（2026-08-27）：对 HP-Score 原始值做斜率限幅(rate limit)，削除
        # pow_sum^(1/k) 对 k/因子单 tick 跳变的非线性放缩（如 ADX/BBW 窗口边界效应、
        # _fetch 返回 K 线数波动）。限幅以"上一 tick 限幅后原始值"为基准，限幅后值
        # 再进 EMA——EMA 吸收常规噪声、斜率限幅拦截极端暴跌，双保险消除 hp_score 瞬跳
        # 与击穿 hp_floor→RED。仅夹相邻 tick 增量，不改变稳态分值（无跳变时原值通过）。
        _hp_delta_max = float(cfg.get("hexp.score.hp_delta_max", 15.0))
        _hp_prev_raw = _es.get("hp_raw_prev")
        if _hp_prev_raw is not None and _hp_delta_max > 0:
            hp_100 = min(max(hp_100, _hp_prev_raw - _hp_delta_max),
                         _hp_prev_raw + _hp_delta_max)
        _es["hp_raw_prev"] = hp_100
        _hp_s = self._ema_smooth(_es, "hp", hp_100, _ema_alpha)
        hp_100 = _hp_s
        sc["state"] = round(float(_hp_s), 4)
        # A/B 修复：护栏判定改用 mm 的 EMA 平滑值，而非裸瞬时 f_mm。
        # 方向裁决/强度仍用瞬时 f_mm（灵敏推方向），护栏用平滑值（稳健否方向），
        # 消除"同一 mm 既推 BUY 又否 BUY"的抖动卡点（冲突 A/B）。
        _mm_ema_alpha = float(cfg.get("hexp.mm.ema_alpha", 0.8))
        f_mm_s = self._ema_smooth(_es, "mm", f_mm, _mm_ema_alpha)
        # 基于平滑后的 state 维重算 total（保留其他维度的实时性，避免双重平滑）
        total = sum(sc[kk] * sw[kk] for kk in sc) / swsum
        _pb_mult = getattr(self, "_pending_pullback_mult", 1.0)
        if _pb_mult < 1.0:
            total *= _pb_mult
        # zone 结构位方向对齐 → 对 grade 判定加权：zone 方向与信号方向一致时给 total 加分，
        # 使贴近结构位的顺势信号更易达 min_grade。与 scoring 的 pre_score 微加成(+0.03 量级)
        # 互补，这里直接作用于 grade 升档（strength 档×距离近度×grade_bonus 分/档）。
        if (cfg.get("hexp.zone.grade_align_enabled", True)
                and zone_type and zone_level and zone_strength > 0
                and direction in ("BUY", "SELL")):
            _zdir = "SELL" if zone_type in ("PIVOT", "RESISTANCE") else "BUY"
            if _zdir == direction:
                _z_close = float(period_data[primary]["close"])
                _prox = max(0.0, 1.0 - min(1.0, abs(_z_close - zone_level) / (atr * 3.0)))
                _zone_bonus = float(cfg.get("hexp.zone.grade_bonus", 2.0)) * float(zone_strength) * _prox
                # 低位启动放大（2026-08-27 修复"低位分不够不下单"）：RSI 处于低位超卖区时，
                # 结构位对齐的顺势信号更可能是趋势启动而非追高，额外加分让其达 min_grade。
                _lowpos_bonus = float(cfg.get("hexp.zone.lowpos_bonus", 4.0))
                _rsi_launch_low = float(cfg.get("hexp.extreme.rsi_launch_low", 35.0))
                if _rsi < _rsi_launch_low:
                    _zone_bonus += _lowpos_bonus * float(zone_strength) * _prox
                total += _zone_bonus
        # ── grade 迟滞裁决（升档用 +gap 边界、降档用 -gap 边界，连续 confirm_bars 确认）──
        _gap = float(cfg.get("hexp.score.hyst_gap", 2.5))
        _cbars = int(cfg.get("hexp.score.hyst_confirm_bars", 2))
        grade = self._resolve_grade_hyst(symbol, total, hp_100, _gap, _cbars)
        # 2026-08-27 方案A：移除 hp_floor 对 grade/passed 的强制干预。
        # 原逻辑 hp_100<floor(默认30)→grade="RED"，经 step9(_grade_ok)间接拦单，
        # 属"强度单维(hp_score)绕过 6 维综合闸门"，与"放行严格回到 scorecard_total"冲突。
        # 现 hp_floor 仅作观测标注（is_hp_red 透传面板），不再改变 grade/passed；
        # 放行严格由 6 维综合 total 经 _resolve_grade_hyst 决定。
        _hp_floor = float(cfg.get("hexp.scorecard.hp_floor", 30.0))
        sr.is_hp_red = bool(hp_100 < _hp_floor)
        if sr.is_hp_red:
            logger.info(
                "HP below floor (observational only, not blocking): %s hp=%.2f < %.2f "
                "(grade kept at %s by 6-dim total)",
                symbol, hp_100, _hp_floor, grade,
            )

        # 减仓触发聚合：_TRANSITION（犹豫带）与 _REVERSAL（趋势态翻转窗口）
        # 二者互补：犹豫带是"方向没想好"，反转态是"方向刚掉头"。干净反转直接
        # UP→DOWN 不经犹豫带，只靠 transition 判据会完全漏掉 → 反转越果断手数越大。
        # 【2026-08-28 趋势单保护】transition 减仓语义收紧：观测标签 transition 保留
        # "任意周期在犹豫带"(any，供诊断)；但减仓判定 transition_primary 仅当主执行周期
        # (primary)本身处于 TRANSITION 才触发。原 any 判据会让"主周期明确趋势、仅辅助周期
        # (如 D1)在犹豫带"的顺势趋势单被误减半仓（实锤 state=TREND_DOWN 趋势单 lot×0.50）。
        transition = any(s == _TRANSITION for s in period_states.values())
        _trans_primary_only = bool(cfg.get("hexp.exec.transition_primary_only", True))
        transition_primary = (period_states.get(primary, _RANGE) == _TRANSITION)
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
        # 绝对超买/超卖度量（2026-08-27）：RSI 不随 Donchian 通道拉宽稀释，
        # 解除 _in_extreme 对 _pos_pct 的单一依赖 → 强趋势高位也能触发极值闸门。
        _rsi_chase_high = float(cfg.get("hexp.extreme.rsi_chase_high", 72.0))
        _rsi_launch_low = float(cfg.get("hexp.extreme.rsi_launch_low", 35.0))
        # 2026-08-27 周期价格位置守卫：pos_cycle(长lookback极值分位) + pos_z(偏离中枢ATR数)
        # 二者均不随 Donchian 20 根通道在趋势中被拉宽稀释，补强 _in_extreme 在趋势中的漏判。
        _z_extreme = float(cfg.get("hexp.cycle.z_extreme", 3.5))
        _in_extreme = (direction == "BUY" and (_pos_pct > _extreme_high or
                                              (k > _k_extreme and _pos_pct > _k_pos_hi) or
                                              _rsi > _rsi_chase_high or
                                              _pos_cycle > _extreme_high or
                                              _pos_z > _z_extreme)) or \
                      (direction == "SELL" and (_pos_pct < _extreme_low or
                                                (k > _k_extreme and _pos_pct < _k_pos_lo) or
                                                _rsi < _rsi_launch_low or
                                                _pos_cycle < _extreme_low or
                                                _pos_z < -_z_extreme))
        # 2026-08-27 周期位置守卫命中派生（供落库可观测）：仅由 pos_cycle/pos_z 触发，
        # 与 pos_pct/k/rsi 触发的极值护栏区分开统计。
        _cycle_blocked = (direction == "BUY" and (_pos_cycle > _extreme_high or _pos_z > _z_extreme)) or \
                         (direction == "SELL" and (_pos_cycle < _extreme_low or _pos_z < -_z_extreme))
        # ── 2026-08-27 周期价格位置硬守护（接刀/摸顶拦截，优先级最高）──
        # _cycle_blocked 仅由 pos_cycle/pos_z 极值触发（与 _in_extreme 的 pos_pct/k/rsi 解耦，
        # 不被趋势中 Donchian 拉宽稀释）。命中即代表「周期绝对低位开空 / 高位开多」。
        # 此前该标志只落库(sr.cycle_pos_blocked)不参与裁决 → 174 条/周 低位空、241 条/周 高位多漏拦。
        # 现升级为硬闸门：无保本持仓 → 直接 NO_TRADE；有保本持仓 → 放行交风控(不硬封，防卡死)。
        # 注：_sym_be_ok 在此提前读取（下方极值护栏段复用，避免重复 IO）。
        _sym_be_ok = False
        if direction in ("BUY", "SELL") and self._redis is not None:
            try:
                _be_flag = await self._redis.get(f"hcm:pos:be:sym:{symbol}:{direction}")
                _sym_be_ok = (_be_flag is not None and str(_be_flag).strip() == "1")
            except Exception:
                _sym_be_ok = False
        if _cycle_blocked:
            if _sym_be_ok:
                sr.extreme_pending = True
                logger.info(
                    "hexp %s %s | CYCLE_POS PENDING(保本追单) dir=%s "
                    "pos_cycle=%.2f pos_z=%.2f (sym be=1 → risk BE-gate 裁决)",
                    symbol, primary, direction, _pos_cycle, _pos_z)
            else:
                _cycle_block = (f"hexp_cycle_pos_guard(pos_cycle={_pos_cycle:.2f} "
                                f"pos_z={_pos_z:.2f} dir={direction})")
                logger.info("hexp %s %s | BLOCK %s dir=%s (cycle extreme position: "
                            "bottom-short/top-long guarded)", symbol, primary,
                            _cycle_block, direction)
                direction = "NO_TRADE"
                passed = False
                sr.threshold_passed = False
                sr.direction = "NO_TRADE"
                sr.cycle_pos_blocked = True
                if not sr.fallback_reason:
                    sr.fallback_reason = _cycle_block
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
        # 微动量对齐度（与方向同号=仍朝原方向）：用 mm 平滑值，避免单根 mm 脉冲误触发护栏
        _mm_aligned = f_mm_s * _dir_sign
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
            _momentum_reversed = (_dir_sign > 0 and f_mm_s < 0.0) or (_dir_sign < 0 and f_mm_s > 0.0)
            # 顶部长上影 / 底部长下影
            _long_wick = (_dir_sign > 0 and _upper_wick >= _wick_min) or \
                         (_dir_sign < 0 and _lower_wick >= _wick_min)
            # 【2026-08-25 极值分层裁决】symbol 级保本标志 hcm:pos:be:sym:{symbol}:{dir} 已在
            # 上方「周期价格位置硬守护」段提前读取(_sym_be_ok)，此处直接复用：
            # 该 symbol 已有同向保本持仓（风险已锁）→ 不硬封方向，标记 extreme_pending
            # 交由风控保本闸门最终裁决（放行+轻仓/拦截）；无保本 → 照常硬封（防接刀）。
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
            # 【2026-08-28 闸门清除】G3c 极值动量反向档已移除：极值区内不再因"mm 明确反向
            # (无长影线)"拦截原趋势延续单。仅保留 G3b 长影线档(顶部上影/底部下影 ≥ wick_min
            # 且 mm 反向才拦，强佐证防接刀)。未命中长影线档一律放行 extreme_chase(顺势追单)。
            if not (_rev_enabled and _mm_retreat_enabled and
                    _mm_aligned < _mm_retreat_min and _momentum_reversed and _long_wick):
                sr.extreme_chase = True
                logger.info(
                    "hexp %s %s | EXTREME CHASE allowed dir=%s mm_aligned=%.2f pos=%.2f "
                    "(extreme, no long-wick reversal → chase allowed)",
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
                sr.momentum_drain_blocked = True
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
            # 高位分级收紧：顶部/底部追单需严格拦截（微动量反向即拦）。命中此区时
            # 后续趋势单保护不得放宽（追单风险优先），故记录标记供下方跳过保护。
            _at_extreme_ma = False
            if direction == "BUY" and _ma_deg >= _flip_ma_th:
                _eff_th = _flip_ma_mm  # 多头高位：微负即拦顶部追多
                _at_extreme_ma = True
            elif direction == "SELL" and _ma_deg <= _flip_ma_lo:
                _eff_th = _flip_ma_mm  # 空头低位：微正即拦底部追空
                _at_extreme_ma = True
            # ── 【2026-08-28 趋势单保护】顺势趋势单不因 M1 正常回撤/换手误杀 ──
            # momentum_flip 纯动量(仅 M1 f_mm_s)否决不辨趋势结构，趋势单推进中 M1 动量短暂
            # 反向（洗盘/换手）会被误判为"动量反转"而拦掉（实锤 388642131-163 连续 BUY 被
            # mm=-0.01~-0.18 拦、388642161 BUY mm=-0.4445 趋势单被拦）。修复：当信号是
            # 顺势趋势单（主周期 TREND_UP/DOWN 且方向同向，或 ADX 强+ma 方向一致）时，
            # 改用更宽的 flip_trend_mm（需剧烈反向才拦），放行趋势单正常回调；
            # 逆势单/弱趋势/震荡仍用基础 flip_mm 敏捷拦逆动量。仅调阈值不动拦截路径。
            _trend_prot = bool(cfg.get("hexp.momentum_flip_trend_enabled", True))
            if _trend_prot:
                _trend_adx = float(cfg.get("hexp.momentum_flip_trend_adx", 25.0))
                _trend_mm = float(cfg.get("hexp.momentum_flip_trend_mm", 0.15))
                # 趋势单判据：仅放行「顺势趋势单」，绝不因 ADX 高而放宽逆势单。
                #  · 主判据（与 range_hurst._in_trend_dir 同口径）：主周期处于 TREND_UP/DOWN
                #    且方向与趋势同向 → 真趋势单，M1 短暂反向不视为反转。
                #  · 辅助判据（主周期状态机未及时切 TREND 但趋势已强的顺势场景）：
                #    ADX≥trend_adx 且 ma 多头度方向与信号一致（BUY 需 _ma_raw>50 多头占优、
                #    SELL 需 _ma_raw<50 空头占优）→ 强趋势顺势单。
                #  逆势单（BUY 而 TREND_DOWN / ma<50）即使 ADX 高也绝不放宽，保持敏捷拦截。
                _pstate = period_states.get(primary, _RANGE)
                _in_trend_dir = (_pstate in (_TREND_UP, _TREND_DOWN)) and (
                    (direction == "BUY" and _pstate == _TREND_UP) or
                    (direction == "SELL" and _pstate == _TREND_DOWN))
                _adx_now = float(pf.get("_adx_raw", 0.0))
                _ma_deg_ok = (direction == "BUY" and _ma_deg > 50.0) or \
                             (direction == "SELL" and _ma_deg < 50.0)
                _is_trend = _in_trend_dir or (_adx_now >= _trend_adx and _ma_deg_ok)
                # 高位分级收紧区(顶部/底部追单)优先：即使趋势单也绝不放宽，保持严格拦截
                if _is_trend and not _at_extreme_ma:
                    # 仅当趋势保护阈值更宽(更不敏感)时才覆盖，绝不收紧(高位分级收紧优先)
                    if _trend_mm > _eff_th:
                        _eff_th = _trend_mm
                    logger.info(
                        "hexp %s | trend-protect flip threshold: pstate=%s adx=%.1f "
                        "trend_single=%s → th %.3f (放行顺势趋势单正常回调)",
                        symbol, _pstate, _adx_now, _in_trend_dir, _eff_th,
                    )
            if direction == "BUY" and f_mm_s < -_eff_th:
                _flip_block = f"hexp_momentum_flip(BUY but mm={f_mm_s:.4f} ma={_ma_deg:.0f} th={_eff_th:.3f})"
            elif direction == "SELL" and f_mm_s > _eff_th:
                _flip_block = f"hexp_momentum_flip(SELL but mm={f_mm_s:.4f} ma={_ma_deg:.0f} th={_eff_th:.3f})"
            # 【2026-08-28 闸门清除】G5b 高位微正枯竭加固已移除（原"ma 极高位+mm 微正枯竭"
            # 拦顶部追多/追空）。_flip_block 现仅由 momentum_flip(动量明确反向)产生。
            if _flip_block:
                logger.info("hexp %s %s | BLOCK %s dir=%s (momentum against direction)",
                            symbol, primary, _flip_block, direction)
                direction = "NO_TRADE"
                passed = False
                sr.threshold_passed = False
                sr.direction = "NO_TRADE"
                sr.momentum_flip_blocked = True
                if not sr.fallback_reason:
                    sr.fallback_reason = _flip_block
                # ── 反向单（2026-08-26 由"纯观测"升级为"经风控后下单"）──
                # 用户需求：高位动量反转时真的下反向单。momentum_flip 已把原方向封成
                # NO_TRADE 并标记 _flip_block，此处产出反向候选（dir 与原方向相反的接刀单）：
                # 顶部 BUY 被拦→SELL 候选；底部 SELL 被拦→BUY 候选。候选经 scheduler 覆写
                # final_direction + 透传 reverse_order 标记，由风控 rule_chain 裁决（接刀护栏）
                # 后由桥执行。默认 hexp.reverse_order_enabled=False 保持纯观测（零实盘影响），
                # 打开开关才真下单（建议先开 1h 观察面板成交/接刀率再长期启用）。
                # 触发条件：momentum_flip 已判动量反向 + 处于高位/低位（顶部做空/底部做多）。
                _rc_enabled = bool(cfg.get("hexp.reverse_candidate_enabled", True))
                if _rc_enabled:
                    _rc_hi = float(cfg.get("hexp.reverse_candidate_hi", 0.7))
                    _rc_lo = float(cfg.get("hexp.reverse_candidate_lo", 0.3))
                    _rc_wick_en = bool(cfg.get("hexp.reverse_candidate_wick_enabled", True))
                    _rc_er_en = bool(cfg.get("hexp.reverse_candidate_er_enabled", True))
                    _rc_dir = None
                    _rc_pos_ok = False
                    _rc_extra_ok = True  # 长影线 + 效率比 双重确认（精准化，防假候选）
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
                        # ── 【2026-08-26 反向候选精准化】与极值护栏同口径的额外确认 ──
                        # 仅"位置达标"不够，须确认"真反转"而非"回调中动量暂歇"：
                        #   ① 长影线：顶部反转需长上影(_upper_wick≥_wick_min)、
                        #      底部反转需长下影(_lower_wick≥_wick_min) —— 抛压/买盘前兆；
                        #   ② 效率比枯竭：er<_drain_er（趋势质量差、动能真枯竭，非暂歇）。
                        # 两开关任一关闭则跳过对应确认（保持向后兼容）。
                        if _rc_pos_ok and (_rc_wick_en or _rc_er_en):
                            _wick_ok = (not _rc_wick_en) or (
                                (_was_buy and _upper_wick >= _wick_min) or
                                (_was_sell and _lower_wick >= _wick_min))
                            _er_ok = (not _rc_er_en) or (_er_now < _drain_er)
                            _rc_extra_ok = _wick_ok and _er_ok
                            if not _rc_extra_ok:
                                _reason = []
                                if _rc_wick_en and not _wick_ok:
                                    _reason.append(f"wick(u={_upper_wick:.2f}/l={_lower_wick:.2f}<{_wick_min:.2f})")
                                if _rc_er_en and not _er_ok:
                                    _reason.append(f"er={_er_now:.3f}>={_drain_er:.3f}")
                                logger.info(
                                    "hexp %s %s | REVERSE CANDIDATE suppressed (not confirmed: %s) "
                                    "dir=%s pos=%.2f",
                                    symbol, primary, " ".join(_reason), _rc_dir, _pos_pct)
                                _rc_pos_ok = False
                        if _rc_pos_ok:
                            # order_intent: 反向候选是否真的要下单。由开关
                            # hexp.reverse_order_enabled(默认 False=纯观测) 控制：
                            #   False → 仅记录候选供 SQL 回测，不进 scheduler 覆写方向；
                            #   True  → 标记 order_intent=True，scheduler 据此覆写
                            #            final_direction 并经风控接刀护栏裁决后真下单。
                            _rc_order = bool(cfg.get("hexp.reverse_order_enabled", False))
                            sr.reverse_candidate = {
                                "dir": _rc_dir,
                                "pos": round(float(_pos_pct), 4),
                                "er": round(float(pf.get("_er_raw", 0.0)), 4),
                                "mm": round(float(f_mm), 4),
                                "close": round(float(close_v), 3),
                                "verdict": round(float(verdict), 4),
                                "order_intent": _rc_order,
                                "wick_confirm": bool(_rc_wick_en and _wick_ok),
                                "er_confirm": bool(_rc_er_en and _er_ok),
                            }
                            logger.info(
                                "hexp %s %s | REVERSE CANDIDATE dir=%s (pos=%.2f er=%.3f mm=%.3f) "
                                "high-momentum-reverse — %s",
                                symbol, primary, _rc_dir, _pos_pct,
                                float(pf.get("_er_raw", 0.0)), f_mm,
                                "ORDER INTENT (reverse_order_enabled)" if _rc_order
                                else "OBSERVE ONLY, no order")
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
            # 【2026-08-26 冲突②修复】主周期明确趋势方向且信号同向时，豁免 range_hurst 拦截。
            # 原逻辑用单值 M5 regime 决定均值回归拦截，与多周期状态机（M30/H1/H4 常=RANGE）
            # 错配：M5=TREND_DOWN 顺跌追空时，若 _rval 解析为 RANGE/NEUTRAL 会把顺势单也拦。
            # 修复：主周期 period_states 处于 TREND_UP/TREND_DOWN 且方向与趋势同向 → 视为真趋势
            # 顺势追单，skip 均值回归拦截（均值回归只拦"震荡市里的顺势追极值"，不拦真趋势同向单）。
            _pstate = period_states.get(primary, _RANGE)
            _in_trend_dir = (_pstate in (_TREND_UP, _TREND_DOWN)) and (
                (direction == "BUY" and _pstate == _TREND_UP) or
                (direction == "SELL" and _pstate == _TREND_DOWN))
            _rh_block = None
            if _rval in _rh_regimes and _hurst_now < _rh_max and not _in_trend_dir:
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
                sr.range_hurst_blocked = True
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
        # 2026-08-27 周期价格位置（抗趋势稀释）：落库更长窗口的极值分位与 ATR-z 偏离，
        # 供面板与 SQL 识别"当前价格在周期里的位置"，区分趋势中段 vs 极值区。
        sr.position_cycle = round(float(_pos_cycle), 4)
        sr.position_z = round(float(_pos_z), 4)
        sr.cycle_pos_blocked = bool(_cycle_blocked)
        # 2026-08-27 C4 落库 ma_raw（0-100 多头度，>90=极高位），供复盘高位接刀归因。
        sr.ma_raw = round(float(pf.get("_ma_raw", 0.0) or 0.0), 2)

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
        # 【2026-08-28 趋势单保护】transition 减仓用 transition_primary(仅主周期犹豫)，
        # 避免"辅助周期 TRANSITION"误伤顺势趋势单；transition_primary_only=False 时回退
        # 旧行为(任意周期犹豫即减)。reversal 减仓逻辑不变。
        red_mult = 1.0
        _trans_trigger = transition_primary if _trans_primary_only else transition
        if _trans_trigger:
            red_mult = min(red_mult, float(cfg["hexp.exec.transition_lot_mult"]))
        if reversal:
            red_mult = min(red_mult, float(cfg["hexp.exec.reversal_lot_mult"]))
        # 【2026-08-28 闸门清除】D3 极值区降仓、D4 波动率缩放均已移除：
        #  - 极值区不再按 extreme_lot_mult 降仓（防高位接刀抄底，由护栏自行决定拦/放）；
        #  - ATR 波动率不再缩放手数（vol_scale 恒 1.0，基础手数不随行情波动缩放）。
        # 仅保留 transition/reversal 减仓(red_mult)。lot 只乘 red_mult。
        if red_mult < 1.0:
            lot *= red_mult
            logger.info(
                "hexp %s lot mult ×%.2f (red=%.2f) | transition=%s reversal=%s%s",
                symbol, red_mult, red_mult, transition, reversal,
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
        sr.mm_smoothed = round(float(f_mm_s), 4)  # EMA 平滑值，前端方向显示应优先用此
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
        # 【2026-08-31 重构】判定模式开关：
        #   squeeze_breakout = 新判定（默认，回测胜率 62~67%，单均净 +0.24~+0.37R）
        #   phase_ignite     = 旧判定（热回退；实测胜率仅 4%、单均 -0.804R，勿长期启用）
        _ts_mode = str(cfg.get("hexp.trend_start_mode", "squeeze_breakout")).strip().lower()
        if _ts_observe and _ts_mode == "squeeze_breakout":
            _trend_start = self._trend_start_squeeze_breakout(
                period_data=period_data, primary=primary, pf=pf, cfg=cfg,
                atr=atr, close_v=close_v, grade=grade, verdict=verdict)
            if _trend_start is not None:
                logger.info(
                    "hexp %s %s | TREND START CANDIDATE(squeeze_breakout) dir=%s bbw=%.1f<%.1f "
                    "don_hi=%.3f don_lo=%.3f h1_up=%s close=%.3f atr=%.4f "
                    "— OBSERVE ONLY, no order",
                    symbol, primary, _trend_start["dir"], _trend_start["bbw"],
                    _trend_start["bbw_max"], _trend_start["don_hi"],
                    _trend_start["don_lo"], _trend_start["h1_up"], close_v, atr)
        elif _ts_observe:
            # 旧判定（hexp.trend_start_mode=phase_ignite 时热回退）
            # 放宽触发（2026-08-27 上线）：相位 ignite→ignite+establish（趋势启动到早期确立，
            # 仍靠 pos 中低位避开高位）；pos 阈值 0.7/0.3→0.8/0.2；mm 容忍微负(默认-0.05)。
            _ts_phases = [p.strip().lower() for p in
                          str(cfg.get("hexp.trend_start_phases", "ignite,establish")).split(",") if p.strip()]
            _ts_mm_min = float(cfg.get("hexp.trend_start_mm_min", -0.05))
            _ts_hi = float(cfg.get("hexp.trend_start_pos_high", 0.8))
            _ts_lo = float(cfg.get("hexp.trend_start_pos_low", 0.2))
            if _phase in _ts_phases and direction in ("BUY", "SELL"):
                _ts_mm_ok = (direction == "BUY" and f_mm > _ts_mm_min) or \
                            (direction == "SELL" and f_mm < -_ts_mm_min)
                _ts_pos_ok = (direction == "BUY" and _pos_pct < _ts_hi) or \
                             (direction == "SELL" and _pos_pct > _ts_lo)
                if _ts_mm_ok and _ts_pos_ok:
                    _trend_start = {
                        "dir": direction, "mode": "phase_ignite", "phase": _phase,
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
        # ── 趋势启动顺势下单（2026-08-26 由"纯观测"升级为可配置下单）──
        # 目标：趋势启动初期（ignite 点火 + 微动量同向 + 位置中低位）即轻仓顺势进场，
        # 根治"滞后组(adx/er/ma≈68% 权重)确认趋势时已在高位才给方向"的追单止损。
        # 触发复用 trend_start_candidate 三条件（phase==ignite + mm 同向 + pos 中低位），
        # 仅当原信号因 grade 未达 min_grade 被闸门拦成 NO_TRADE（passed=False，但非 RED）
        # 时才覆写放行；不绕过极值/momentum_flip/range_hurst 等安全护栏——三条件的
        # pos 中低位 + mm 同向天然避开这些护栏。开关 hexp.trend_start_order_enabled
        # （默认 True=2026-08-27 上线；metadata 设 false 可热回退纯观测）。
        # 【2026-08-31 安全收紧·双层保险】
        #   ① 默认值 True→False：旧判定实测胜率仅 4%、单均 -0.804R，却在生产上真
        #      下单（绕过 grade 闸门），先止血；新配置部署时缺省也保持纯观测。
        #   ② 新判定(squeeze_breakout)处于"真实信号影子验证"阶段，代码层明确禁止
        #      真下单——其下单能力须待 hexp_shadow_eval 累积达标后另行评估开放。
        _ts_order_enabled = bool(cfg.get("hexp.trend_start_order_enabled", False))
        # 新判定(squeeze_breakout)默认不下单（真实信号影子验证期）。验证达标后无需改
        # 代码：配置 hexp.trend_start.new_mode_order_allowed=true 配合 order_enabled
        # 即可开放。这样"切入生产"是一次纯配置变更，可秒级回退。
        _ts_new_allowed = bool(cfg.get("hexp.trend_start.new_mode_order_allowed", False))
        _ts_order_blocked = (_ts_mode == "squeeze_breakout") and not _ts_new_allowed
        # 【2026-08-31 安全护栏显式豁免】本覆写位于全部五道护栏(1602~1917)之后，
        # 会无条件把 direction="NO_TRADE"/passed=False 覆写为放行。原设计依赖旧判定
        # "mm 同向 + pos 中低位"天然避开护栏（2123 行注释），但新判定 squeeze_breakout
        # 不看 mm/pos，该假设不成立 → 护栏会被静默绕过。现改为显式检查：
        # 凡被任一道安全护栏拦过的信号，一律不放行；只放行"因 grade 未达 min_grade
        # 被闸门拦成 NO_TRADE"的信号（这才是本功能的初衷）。
        # 【2026-08-31 修正】cycle_pos_blocked 不纳入趋势启动豁免 —— 量化回测
        # (ts_conflict.py) 显示 cycle_pos_guard 会拦掉 45% 的趋势启动信号，但被拦
        # 信号胜率(62.9%)与保留信号(61.5%)几乎相同，即该护栏对"已有 H1 共振+压缩
        # 突破"强确认的趋势启动单是误伤、不产生质量增益。保留其余护栏
        # (grade/momentum/reversal/hurst)作为真正的风险屏障。
        _ts_guard_blocked = bool(
            getattr(sr, "extreme_reversal_blocked", False)
            or getattr(sr, "momentum_drain_blocked", False)
            or getattr(sr, "momentum_flip_blocked", False)
            or getattr(sr, "range_hurst_blocked", False)
        )
        if _ts_guard_blocked and _ts_order_enabled and not _ts_order_blocked \
                and _trend_start is not None:
            logger.info(
                "hexp %s %s | TREND START ORDER SUPPRESSED dir=%s (安全护栏已拦截，"
                "趋势启动覆写不放行) grade=%s", symbol, primary,
                _trend_start.get("dir"), grade)
        if (_ts_order_enabled and not _ts_order_blocked and not _ts_guard_blocked
                and _trend_start is not None and not passed):
            _ts_dir = _trend_start["dir"]
            _ts_min_grade = str(cfg.get("hexp.trend_start_min_grade", "B")).strip().upper()
            _ts_min_rank = _GRADE_RANK.get(_ts_min_grade, 1)
            _ts_grade_rank = _GRADE_RANK.get(grade, 0)
            if _ts_dir in ("BUY", "SELL") and _ts_grade_rank >= _ts_min_rank and grade != "RED":
                # 手数：链动风控动态手数，禁用硬编码固定折减。
                # 实际倍率由风控 risk.lot_multiplier_{low,mid,high} 按 lot_tier 决定
                # （scheduler 会用 trend_start_lot_tier 覆盖 ai_lot_tier）；
                # lot_mult 默认 1.0 = 完全不干预，仅在确需微调时才配置。
                _ts_lot_mult = float(cfg.get("hexp.trend_start_order_lot_mult", 1.0))
                sr.trend_start_lot_tier = str(
                    cfg.get("hexp.trend_start.lot_tier", "low")).strip().lower()
                # 【2026-08-31 SL/TP 口径修正】此前的覆写只改手数，SL/TP 沿用
                # hexp.exec.sl_atr_mult=2.0 / rr_min=1.5。但回测最优为 SL=3.5/RR=1.5，
                # 且回测中 SL=2.0 在样本外明确亏损(-1.9~-2.9R)、SL=3.5 盈利(+8.1R)。
                # 不修正则真下单落在回测已知亏损的参数上，回测结论直接失效。
                # 新判定候选自带 sl_atr_mult/rr；旧判定无此字段时回退 hexp.exec 口径。
                _ts_sl_mult = _trend_start.get("sl_atr_mult")
                _ts_rr = _trend_start.get("rr")
                if _ts_sl_mult is None or _ts_rr is None:
                    _ts_sl_mult = float(cfg["hexp.exec.sl_atr_mult"])
                    _ts_rr = float(cfg["hexp.exec.rr_min"])
                sr.direction = _ts_dir
                sr.threshold_passed = True
                sr.pre_score = round(total / 100.0, 4)
                sr.co_exec_lot_mult = round(float(sr.co_exec_lot_mult) * _ts_lot_mult, 4)
                sr.co_exec_sl_atr_mult = round(float(_ts_sl_mult), 4)
                sr.co_exec_rr_min = round(float(_ts_rr), 4)
                # 锁定 SL：避免被 scheduler 的会话 SL 覆盖改写（欧美盘会话值 2.0
                # 会把 3.5 打回 2.0，而该参数在回测中样本外明确亏损）。
                sr.trend_start_sl_locked = True
                if not sr.fallback_reason:
                    sr.fallback_reason = (
                        f"hexp_trend_start_order({_ts_dir},{_ts_mode},pos={_pos_pct:.2f},"
                        f"mm={f_mm:.3f},grade={grade},sl={_ts_sl_mult},rr={_ts_rr})"
                    )
                logger.info(
                    "hexp %s %s | TREND START ORDER %s mode=%s grade=%s lot×%.2f sl=%.2f rr=%.2f "
                    "(绕过 grade 闸门，已显式豁免全部安全护栏)",
                    symbol, primary, _ts_dir, _ts_mode, grade, _ts_lot_mult,
                    float(_ts_sl_mult), float(_ts_rr))
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
                        # 2026-08-27 假设方向预演：暴露 dir_sum 分解 + 上次方向，供预演端点诊断死标签/漏翻。
                        "dir_sum": round(float(dir_sum), 4) if "dir_sum" in dir() else None,
                        "dir_sum_factors": {
                            kk: round(float(fvec.get(kk, 0.0)), 4)
                            for kk in ("adx", "er", "ma", "bbw", "hurst", "rsi", "mm")
                        } if "fvec" in dir() else {},
                        "dir_pos_factor": round(float(f_pos), 4) if "f_pos" in dir() else None,
                        "prev_direction": self._dir_hyst_state.get(symbol, "NO_TRADE"),
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

    def _get_cycle_position(self, pdata: dict[str, Any], atr: float, cfg: dict[str, Any]) -> "tuple[float, float]":
        """周期价格位置（抗趋势稀释，2026-08-27）。

        返回值 (pos_cycle, pos_z)：
          pos_cycle = 价格相对[长 lookback]滚动高/低的百分位[0,1]。
                      用更长的回顾窗口（默认 60 根，配置 hexp.cycle.look），
                      不被 20 根 Donchian 通道在趋势中被拉宽稀释 → 趋势中也能识别"已到高位/低位"。
          pos_z     = (close - SMA(long)) / ATR(long)，偏离长周期中枢多少个 ATR。
                      趋势中价格持续偏离中枢时该值显著 >0/<0，是"远离中枢多远"的尺度。
        二者联合用于发信号前判断周期价格位置，抑制高点追多 / 低点追空。
        """
        _look = int(cfg.get("hexp.cycle.look", 60))
        closes = pdata.get("closes")
        if atr <= 0 or closes is None or len(closes) < _look + 1:
            return 0.5, 0.0
        hh = float(np.max(pdata["highs"][-(_look + 1):-1]))
        ll = float(np.min(pdata["lows"][-(_look + 1):-1]))
        _pos_cycle = 0.5
        if hh - ll > 1e-9:
            _pos_cycle = max(0.0, min(1.0, (float(pdata["close"]) - ll) / (hh - ll)))
        _sma = float(np.mean(closes[-_look:]))
        _atr_long = self._atr(pdata["highs"], pdata["lows"], closes, _look)
        _pos_z = 0.0
        if _atr_long > 1e-9:
            _pos_z = (float(pdata["close"]) - _sma) / _atr_long
        return _pos_cycle, _pos_z

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

    # ─────────────── 趋势启动候选·新判定（2026-08-31 重构）───────────────
    @staticmethod
    def _ema(arr: np.ndarray, span: int) -> np.ndarray:
        """EMA（adjust=False，与 pandas ewm(span=span, adjust=False).mean() 等价）。

        递推：y[0]=x[0]；y[t]=α·x[t]+(1-α)·y[t-1]，α=2/(span+1)。
        引擎不依赖 pandas，故本地实现（回测侧用 pandas 同口径验证一致）。
        """
        x = np.asarray(arr, dtype=float)
        if x.size == 0 or span < 1:
            return x
        alpha = 2.0 / (span + 1.0)
        out = np.empty(x.size, dtype=float)
        out[0] = x[0]
        for i in range(1, x.size):
            out[i] = alpha * x[i] + (1.0 - alpha) * out[i - 1]
        return out

    def _trend_start_squeeze_breakout(
        self,
        period_data: dict[str, dict[str, Any]],
        primary: str,
        pf: dict[str, float],
        cfg: dict[str, Any],
        atr: float,
        close_v: float,
        grade: str,
        verdict: float,
    ) -> dict[str, Any] | None:
        """趋势启动候选·新判定：波动率压缩 → Donchian 突破 + H1 多周期共振。

        回测依据（XAUUSD M5，2026-06-16~08-31，16918 根；成本 0.045R/次已扣）：
            样本内 64 次  胜率 61.8%  净 +15.6R（单均 +0.244R）
            样本外 29 次  胜率 66.7%  净 +10.7R（单均 +0.369R）
        参数稳健性：40 组 SL×HOLD×RR 中 30 组样本内外净 R 同正，HOLD 90/150 结果
        相同，属平原而非孤峰。旧判定(phase=ignite+mm+pos)实测胜率 4%、单均
        -0.804R（28 样本），根因是相位由滞后组(adx/er/ma≈50%权重)驱动，确认时
        趋势已走完；本判定改用"状态跃变"事件，不依赖滞后评分。

        三条件（缺一不可）：
          ① 压缩：bbw 带宽处近 120 根最低 bbw_max% 分位（pf["_bbw_pct"]）
          ② 突破：收盘突破 Donchian(don_look) 上下轨；切片 [-(n+1):-1] 天然
             排除当前未收棒，与回测 shift(1) 口径一致，无前视
          ③ 共振：最近【已完成】H1 棒收盘 > H1 EMA(h1_ema) 只做多，< 只做空；
             用倒数第二根避开当前未收 H1 棒漂移，与回测 shift(1) 对齐

        返回候选中携带 sl_atr_mult/rr/hold_bars，供 scheduler._reconcile_hexp_shadow
        按回测口径评估（勿与 hexp.exec.sl_atr_mult=2.0 混用，两者口径不同）。
        """
        if atr <= 0 or close_v <= 0:
            return None
        pdata = period_data.get(primary)
        if not pdata:
            return None
        _bbw_max = float(cfg.get("hexp.trend_start.bbw_max", 20.0))
        _don = int(cfg.get("hexp.trend_start.don_look", 10))
        _h1_ema = int(cfg.get("hexp.trend_start.h1_ema", 50))
        _sl_mult = float(cfg.get("hexp.trend_start.sl_atr_mult", 3.5))
        _rr = float(cfg.get("hexp.trend_start.rr", 1.5))
        _hold = int(cfg.get("hexp.trend_start.hold_bars", 90))

        # ① 压缩（先判：绝大多数 bar 在此被淘汰，开销最低）
        _bbw = float(pf.get("_bbw_pct", 100.0))
        if not np.isfinite(_bbw) or _bbw >= _bbw_max:
            return None

        # ② 突破
        highs, lows = pdata.get("highs"), pdata.get("lows")
        if highs is None or lows is None or len(highs) < _don + 2:
            return None
        don_hi = float(np.max(highs[-(_don + 1):-1]))
        don_lo = float(np.min(lows[-(_don + 1):-1]))
        if close_v > don_hi:
            _dir = "BUY"
        elif close_v < don_lo:
            _dir = "SELL"
        else:
            return None

        # ③ H1 多周期共振
        _h1 = period_data.get("H1")
        if not _h1:
            return None
        _h1c = _h1.get("closes")
        if _h1c is None or len(_h1c) < _h1_ema + 2:
            return None
        _h1_ema_arr = self._ema(np.asarray(_h1c, dtype=float), _h1_ema)
        _h1_up = float(_h1c[-2]) > float(_h1_ema_arr[-2])
        if (_dir == "BUY" and not _h1_up) or (_dir == "SELL" and _h1_up):
            return None

        return {
            "dir": _dir, "mode": "squeeze_breakout",
            "bbw": round(_bbw, 2), "bbw_max": round(_bbw_max, 2),
            "don_hi": round(don_hi, 3), "don_lo": round(don_lo, 3),
            "h1_up": bool(_h1_up),
            "close": round(close_v, 3), "atr": round(atr, 4),
            # 影子评估参数（回测最优值），勿与 hexp.exec.* 口径混用
            "sl_atr_mult": _sl_mult, "rr": _rr, "hold_bars": _hold,
            "verdict": round(verdict, 4), "grade": grade,
        }

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
