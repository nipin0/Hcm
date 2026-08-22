# 变更说明：AI 分缩放 SL 宽度链动（2026-08-18）

## 一、需求背景（用户确认）
用户期望"AI 分的高低缩放 SL 宽度"，具体数值范围参考设计文档
`hcm-ai-quality-scorer-design.md` 的 ai_sl_coeff 区间 **0.8~1.5×ATR**。
- 方向：**分高→宽(1.5)，分低→窄(0.8)**（用户确认，线性 scale = min + (score/100)×(max-min)）
- **AI 断联/失效 → 回退现用的会话 SL**（不写显式 sl_price，桥用 close.<session> 重算）
- **DeepSeek 不直接干预订单 SL/TP**；DeepSeek 只负责辅助校准 LightGBM 模型（作为训练特征）
- 默认开启（用户确认，非灰度）

## 二、变更前基线（回归锚点）
- 旧实现：scheduler.py 用 **DeepSeek 的 `ai_sl_coeff`(0.8~1.5) 乘性缩放**会话 SL
  （`ai_sl_mult = 会话值 × coeff`），违背"DeepSeek 不直接干预订单 SL/TP"。
- AI 分(ai_score) 仅用于 quality_gate 裁决(VETO/升降级/手数档)，**从不缩放 SL 宽度**。
- 桥端仅 `signal_tower.ai_risk_enabled=true` 才消费 ai_sl_mult（默认 OFF），
  故调度器算的 ai_sl_mult 大多不落地，SL 实际由桥按会话系数重算。

## 三、变更内容
### 3.1 scheduler.py（signal-tower，bind mount :128）
1. **替换** P1a 的 DeepSeek ai_sl_coeff 块（原 2144-2166）为 **LightGBM ai_score → SL 缩放块**：
   - 判定 AI 有效：`ai_q.lm_score is not None` 且 `source != "none"`
     （lm_score=sidecar 发布的 ai_score 0-100，新鲜度已由 `_read_ai_quality` 校验）
   - `scale = min + (score/100)×(max-min)`，clamp 到 [min,max]
   - **AI 有效** → `ai_sl_mult = scale`（0.8~1.5，**替代**会话/co_source 值）
   - **AI 断联/失效**（lm_score=None/source=none/未耦合）→ 保持会话 ai_sl_mult，不写显式价
   - DeepSeek `ai_sl_coeff` **不再**作为开仓 SL 缩放因子（仅作训练特征）
2. **新增 AI 显式写价块**（extreme_chase 块之后）：AI 有效时用最终 ai_sl_mult(≤1.8 封顶)
   显式算 `sl_price/tp1`，桥对非零 sl_price 直接采用（与 extreme_chase 同机制，
   无需 ai_risk_enabled）。
3. **SignalData 构造**：`sl_price = _chase_sl_price if _chase_sl_price else _ai_sl_price`
   （extreme_chase 优先，其次 AI 缩放，都无→桥回退会话 SL）。

### 3.2 配置键（PG + Redis 双写 + PUB，可热调）
| 键 | 默认 | 说明 |
|---|---|---|
| `ai.lm.sl_scale_enabled` | True | 总开关（默认开启） |
| `ai.lm.sl_scale_min` | 0.8 | AI 分缩放 SL 下限（×ATR） |
| `ai.lm.sl_scale_max` | 1.5 | AI 分缩放 SL 上限（×ATR） |

### 3.3 web/api/ai_config.py（hcm-web，bind mount :325）
- AI_KEYS 白名单新增三键，面板/API 可读写（config_provider.set 双写 PG+Redis+PUB）。

### 3.4 前端 AiQualityConfig.tsx
- lm 组新增三字段（开关 + 上下限），待 vite build 生效。

### 3.5 迁移 deploy/migrations/0016_ai_sl_scale.sql
- seed 三键到 PG hcm_config.metadata（INSERT 3，已执行）。
- Redis hcm:config:v2 三键已 HSET + PUBLISH（5 订阅者）。

## 四、影响面
- 仅影响**开仓 SL/TP 定价**（AI 有效时 0.8~1.5×ATR，比会话 2.0×ATR 更窄/更宽取决于分）。
- 不改变 HEXP 方向/评分/手数/闸门裁决（quality_gate 不变）。
- 不改变持仓后的 trailing/breakeven/TP-relay（仍按会话系数走）。
- AI 断联/失效时**完全回退**旧会话 SL 行为，无回归风险。
- DeepSeek ai_sl_coeff 不再影响开仓 SL（旧功能被新设计替换）。

## 五、回归验证
- py_compile：scheduler.py + ai_config.py 均通过（C:\Python313）。
- 容器内 grep：scheduler.py 含 "LightGBM ai_score SL scale"=1，ai_config.py 含 "sl_scale_enabled"=1。
- 两容器重启后 healthy，signal-tower 启动日志正常，无 Traceback/UnboundLocalError/NameError。
- PG+Redis 三键值一致（enabled=True/min=0.8/max=1.5）。
- 当前行情 AI 快照 ai_score=27.78(coupled, ready)，HEXP 快照 NO_TRADE(grade=C)——
  无 BUY/SELL 故 AI SL scale 块暂不触发（属正常，功能就绪待信号）。

## 六、回滚锚点
- 配置回退：置 `ai.lm.sl_scale_enabled=False`（双写）即恢复会话 SL，秒级生效，无需改码。
- 代码回退：恢复 scheduler.py P1a 原 DeepSeek ai_sl_coeff 块 + SignalData 原 sl_price=_chase_sl_price。

## 七、遗留
- 前端 dist 待重建后 AiQualityConfig 面板显示新字段（配置已可经 API/Redis 调）。
- 桥端 `signal_tower.ai_risk_enabled` 仍默认 OFF；本功能用显式 sl_price 通道规避，
  无需开启该开关（保留既有 ai_risk_enabled 独立链路不动）。
