"""AI Scorer — DeepSeek-based factor scoring engine.

Provides per-category scoring for macro, sentiment, and event data.
Returns structured scores (int 0-30/0-20), bias, and AI summary.

Supports stub mode when DeepSeek API is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRY_MAX = 2
DEFAULT_RETRY_DELAY = 1.0
DEFAULT_STUB_MODE = False

# Score ranges per factor type
SCORE_RANGES: dict[str, dict[str, Any]] = {
    "macro": {"max": 30, "label": "macro_risk_score"},
    "sentiment": {"max": 20, "label": "sentiment_risk_score"},
    "event": {"max": 10, "label": "event_risk_level"},
}


@dataclass
class ScoreResult:
    """AI scoring result for a factor category.

    Attributes:
        score: Numeric score (0 to max_range).
        max_score: Maximum possible score for this category.
        bias: Directional bias (bullish / bearish / neutral).
        summary: AI-generated summary text.
        raw_response: Raw JSON response for debugging.
        latency_ms: API call latency in milliseconds.
        success: Whether the call succeeded.
        stub: Whether stub mode was used.
        error: Error message if call failed.
    """

    score: int = 0
    max_score: int = 30
    bias: str = "neutral"         # bullish / bearish / neutral
    summary: str = ""
    raw_response: str = ""
    latency_ms: float = 0.0
    success: bool = False
    stub: bool = False
    error: str = ""


class AiScorer:
    """DeepSeek-based external factor scoring engine.

    Calls DeepSeek API with category-specific prompts, parses structured
    JSON responses to produce standardized scores.

    Supports stub mode for environments where DeepSeek is unavailable —
    generates reasonable default scores based on data heuristics.

    Example:
        scorer = AiScorer(api_key="sk-...")
        await scorer.initialize()
        result = await scorer.score_macro(
            category="metals",
            data={"dxy": 104.5, "real_yield_10y": 1.85, "vix": 18.2},
        )
        print(f"Score={result.score}/30, bias={result.bias}")
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        retry_max: int = DEFAULT_RETRY_MAX,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        stub_mode: bool = DEFAULT_STUB_MODE,
    ):
        """Initialize AiScorer.

        Args:
            api_key: DeepSeek API key.
            base_url: DeepSeek API base URL.
            model: Model name to use for scoring.
            timeout: API call timeout in seconds.
            retry_max: Max retry attempts per call.
            retry_delay: Base delay between retries in seconds.
            stub_mode: If True, use heuristic scoring without API call.
        """
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._retry_max = retry_max
        self._retry_delay = retry_delay
        self._stub_mode = stub_mode

        self._client: Optional[httpx.AsyncClient] = None
        self._stats: dict[str, int] = {
            "calls": 0,
            "successes": 0,
            "failures": 0,
            "stubs": 0,
        }

    # ── Lifecycle ───────────────────────────────

    async def initialize(self) -> None:
        """Create HTTP client if not in stub mode."""
        if self._stub_mode:
            logger.info("AiScorer initialized in STUB mode")
            return

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout + 5.0),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        logger.info("AiScorer initialized: model=%s, timeout=%.1fs", self._model, self._timeout)

    async def shutdown(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
            logger.info("AiScorer shutdown")

    # ── Public Scoring API ──────────────────────

    async def score_macro(
        self, category: str, data: dict[str, Any]
    ) -> ScoreResult:
        """Score macro environment data for a category.

        Args:
            category: Asset category (metals / crypto / forex).
            data: Macro data dict (e.g., dxy, real_yield_10y, vix, cpi, fed_funds).

        Returns:
            ScoreResult with score 0-30, bias, and summary.
        """
        if self._stub_mode:
            return self._stub_score_macro(category, data)

        prompt = self._build_macro_prompt(category, data)
        raw_response, latency, ok, err = await self._call_api(prompt, "macro", category)

        if ok and raw_response:
            return self._parse_score_response(raw_response, "macro", latency)
        else:
            # Fallback to stub on failure
            logger.warning("Macro scoring API failed for %s: %s — using stub", category, err)
            result = self._stub_score_macro(category, data)
            result.error = err
            result.latency_ms = latency
            result.success = False
            return result

    async def score_sentiment(
        self, category: str, data: dict[str, Any]
    ) -> ScoreResult:
        """Score market sentiment data for a category.

        Args:
            category: Asset category (metals / crypto / forex).
            data: Sentiment data dict (e.g., cot_long, cot_short, vix, fear_greed).

        Returns:
            ScoreResult with score 0-20, bias, and summary.
        """
        if self._stub_mode:
            return self._stub_score_sentiment(category, data)

        prompt = self._build_sentiment_prompt(category, data)
        raw_response, latency, ok, err = await self._call_api(prompt, "sentiment", category)

        if ok and raw_response:
            return self._parse_score_response(raw_response, "sentiment", latency)
        else:
            logger.warning("Sentiment scoring API failed for %s: %s — using stub", category, err)
            result = self._stub_score_sentiment(category, data)
            result.error = err
            result.latency_ms = latency
            result.success = False
            return result

    async def score_event(
        self, category: str, data: dict[str, Any]
    ) -> ScoreResult:
        """Score event risk for a category.

        Args:
            category: Asset category.
            data: Event data dict (e.g., event_name, importance, minutes_to_event).

        Returns:
            ScoreResult with score 0-10, bias, and summary.
        """
        if self._stub_mode:
            return self._stub_score_event(category, data)

        prompt = self._build_event_prompt(category, data)
        raw_response, latency, ok, err = await self._call_api(prompt, "event", category)

        if ok and raw_response:
            return self._parse_score_response(raw_response, "event", latency)
        else:
            logger.warning("Event scoring API failed for %s: %s — using stub", category, err)
            result = self._stub_score_event(category, data)
            result.error = err
            result.latency_ms = latency
            result.success = False
            return result

    # ── API Call ────────────────────────────────

    async def _call_api(
        self, prompt: str, factor_type: str, category: str
    ) -> tuple[Optional[str], float, bool, str]:
        """Call DeepSeek API with retry logic.

        Args:
            prompt: Prompt text for the API call.
            factor_type: Type of factor being scored (macro/sentiment/event).
            category: Asset category for logging.

        Returns:
            Tuple of (raw_response_text, latency_ms, success_bool, error_string).
        """
        self._stats["calls"] += 1
        t0 = time.time()

        if self._client is None:
            await self.initialize()

        for attempt in range(1, self._retry_max + 1):
            try:
                response = await asyncio.wait_for(
                    self._client.post(
                        f"{self._base_url}/v1/chat/completions",
                        json={
                            "model": self._model,
                            "messages": [
                                {
                                    "role": "system",
                                    "content": (
                                        "You are a financial market analyst. Respond ONLY with "
                                        "valid JSON, no markdown formatting, no code blocks."
                                    ),
                                },
                                {"role": "user", "content": prompt},
                            ],
                            "temperature": 0.2,
                            "max_tokens": 300,
                        },
                    ),
                    timeout=self._timeout,
                )

                latency = (time.time() - t0) * 1000

                if response.status_code == 200:
                    data = response.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    self._stats["successes"] += 1
                    logger.debug(
                        "AI scoring: type=%s category=%s latency=%.1fms",
                        factor_type, category, latency,
                    )
                    return content, latency, True, ""
                else:
                    logger.warning(
                        "DeepSeek HTTP %d for %s/%s (attempt %d): %s",
                        response.status_code, factor_type, category,
                        attempt, response.text[:200],
                    )

            except asyncio.TimeoutError:
                logger.warning(
                    "DeepSeek timeout for %s/%s (attempt %d)", factor_type, category, attempt
                )
            except Exception as exc:
                logger.error(
                    "DeepSeek error for %s/%s (attempt %d): %s", factor_type, category, attempt, exc
                )

            if attempt < self._retry_max:
                await asyncio.sleep(self._retry_delay * attempt)

        latency = (time.time() - t0) * 1000
        self._stats["failures"] += 1
        return None, latency, False, f"All {self._retry_max} attempts exhausted"

    # ── Response Parsing ────────────────────────

    def _parse_score_response(
        self, content: str, factor_type: str, latency_ms: float
    ) -> ScoreResult:
        """Parse DeepSeek JSON response into ScoreResult.

        Args:
            content: Raw response text from DeepSeek.
            factor_type: Type of factor (macro/sentiment/event).
            latency_ms: API call latency.

        Returns:
            ScoreResult with parsed values.
        """
        try:
            # Strip any markdown code blocks
            text = content.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                if len(lines) > 2 and lines[-1].strip() == "```":
                    text = "\n".join(lines[1:-1])
                else:
                    text = "\n".join(lines[1:])

            data = json.loads(text)

            score_range = SCORE_RANGES.get(factor_type, {"max": 30})
            max_score = score_range["max"]

            score = int(data.get("score", 0))
            score = max(0, min(max_score, score))  # Clamp to valid range

            return ScoreResult(
                score=score,
                max_score=max_score,
                bias=str(data.get("bias", "neutral")).lower(),
                summary=str(data.get("summary", "")),
                raw_response=content,
                latency_ms=round(latency_ms, 1),
                success=True,
                stub=False,
            )

        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            logger.warning("Score response parse error: %s — raw=%s", exc, content[:200])
            self._stats["failures"] += 1
            return ScoreResult(
                score=0,
                max_score=SCORE_RANGES.get(factor_type, {"max": 30})["max"],
                bias="neutral",
                summary=f"Parse error: {exc}",
                raw_response=content,
                latency_ms=round(latency_ms, 1),
                success=False,
                stub=False,
                error=f"parse_error: {exc}",
            )

    # ── Prompt Builders ─────────────────────────

    def _build_macro_prompt(self, category: str, data: dict[str, Any]) -> str:
        """Build macro scoring prompt.

        Args:
            category: Asset category.
            data: Macro data dictionary.

        Returns:
            Prompt string.
        """
        category_labels = {
            "metals": "贵金属",
            "crypto": "加密货币",
            "forex": "外汇",
        }
        label = category_labels.get(category, category)

        data_str = "\n".join(f"  {k}: {v}" for k, v in data.items())

        return f"""你是{label}市场宏观分析师。基于以下宏观数据对{label}市场进行风险评估赋分。

【宏观数据】
{data_str}

【赋分规则】
- 评分范围 0-30 分
- 0-10: 宏观环境有利，利好{label}（bullish）
- 11-20: 宏观环境中性，无明确方向（neutral）
- 21-30: 宏观环境不利，利空{label}（bearish）

【请输出JSON】
{{
  "score": <0-30 整数>,
  "bias": "bullish或bearish或neutral",
  "summary": "<一句话中文总结宏观环境对{label}的影响>"
}}"""

    def _build_sentiment_prompt(self, category: str, data: dict[str, Any]) -> str:
        """Build sentiment scoring prompt.

        Args:
            category: Asset category.
            data: Sentiment data dictionary.

        Returns:
            Prompt string.
        """
        category_labels = {
            "metals": "贵金属",
            "crypto": "加密货币",
            "forex": "外汇",
        }
        label = category_labels.get(category, category)

        data_str = "\n".join(f"  {k}: {v}" for k, v in data.items())

        return f"""你是{label}市场情绪分析师。基于以下情绪数据对{label}市场进行情绪风险评估赋分。

【情绪数据】
{data_str}

【赋分规则】
- 评分范围 0-20 分
- 0-7: 市场情绪偏多，利好{label}（bullish）
- 8-14: 市场情绪中性（neutral）
- 15-20: 市场情绪恐慌/极端，利空{label}（bearish）

【请输出JSON】
{{
  "score": <0-20 整数>,
  "bias": "bullish或bearish或neutral",
  "summary": "<一句话中文总结市场情绪对{label}的影响>"
}}"""

    def _build_event_prompt(self, category: str, data: dict[str, Any]) -> str:
        """Build event risk scoring prompt.

        Args:
            category: Asset category.
            data: Event data dictionary.

        Returns:
            Prompt string.
        """
        data_str = "\n".join(f"  {k}: {v}" for k, v in data.items())

        return f"""你是金融市场事件分析师。基于以下事件数据评估对{category}市场的风险等级。

【事件数据】
{data_str}

【赋分规则】
- 评分范围 0-10 分
- 0-3: 无重大事件，低风险（bullish/neutral）
- 4-7: 中等重要事件，中等风险（neutral/bearish）
- 8-10: 重大事件，高风险（bearish）

【请输出JSON】
{{
  "score": <0-10 整数>,
  "bias": "bullish或bearish或neutral",
  "summary": "<一句话中文总结事件风险>"
}}"""

    # ── Stub Scoring (API unavailable fallback) ─

    def _stub_score_macro(self, category: str, data: dict[str, Any]) -> ScoreResult:
        """Generate heuristic macro score without API call.

        Args:
            category: Asset category.
            data: Macro data dict.

        Returns:
            ScoreResult with heuristic values.
        """
        self._stats["stubs"] += 1
        score = 15  # Default neutral

        # Heuristic scoring based on DXY and real yields
        dxy = data.get("dxy", 100)
        if isinstance(dxy, (int, float)):
            if dxy > 105:
                score = 22  # Strong dollar → bearish for commodities
            elif dxy > 102:
                score = 18
            elif dxy < 95:
                score = 8   # Weak dollar → bullish for commodities
            elif dxy < 98:
                score = 12

        real_yield = data.get("real_yield_10y", 0)
        if isinstance(real_yield, (int, float)):
            if real_yield > 2.5:
                score = min(30, score + 5)
            elif real_yield < 1.0:
                score = max(0, score - 5)

        bias = "bearish" if score > 15 else ("bullish" if score < 10 else "neutral")

        return ScoreResult(
            score=score,
            max_score=30,
            bias=bias,
            summary=f"[STUB] Heuristic macro score for {category}: DXY={dxy}, real_yield={real_yield}",
            latency_ms=0.0,
            success=True,
            stub=True,
        )

    def _stub_score_sentiment(self, category: str, data: dict[str, Any]) -> ScoreResult:
        """Generate heuristic sentiment score without API call.

        Args:
            category: Asset category.
            data: Sentiment data dict.

        Returns:
            ScoreResult with heuristic values.
        """
        self._stats["stubs"] += 1
        score = 10  # Default neutral

        # Heuristic based on VIX / Fear & Greed
        vix = data.get("vix", 20)
        if isinstance(vix, (int, float)):
            if vix > 30:
                score = 18  # High fear
            elif vix > 25:
                score = 14
            elif vix < 15:
                score = 5   # Low fear / complacency
            elif vix < 18:
                score = 8

        fear_greed = data.get("fear_greed", 50)
        if isinstance(fear_greed, (int, float)):
            if fear_greed < 25:
                score = min(20, score + 4)  # Extreme fear
            elif fear_greed > 75:
                score = max(0, score - 3)   # Extreme greed → bullish short-term

        bias = "bearish" if score > 13 else ("bullish" if score < 7 else "neutral")

        return ScoreResult(
            score=score,
            max_score=20,
            bias=bias,
            summary=f"[STUB] Heuristic sentiment score for {category}: VIX={vix}",
            latency_ms=0.0,
            success=True,
            stub=True,
        )

    def _stub_score_event(self, category: str, data: dict[str, Any]) -> ScoreResult:
        """Generate heuristic event score without API call.

        Args:
            category: Asset category.
            data: Event data dict.

        Returns:
            ScoreResult with heuristic values.
        """
        self._stats["stubs"] += 1

        importance = data.get("importance", 1)
        if isinstance(importance, (int, float)):
            importance = int(importance)

        # Simple mapping: importance 1→3, 2→6, 3→9
        score = {1: 3, 2: 6, 3: 9}.get(importance, 3)

        bias = "bearish" if score >= 6 else "neutral"

        return ScoreResult(
            score=score,
            max_score=10,
            bias=bias,
            summary=f"[STUB] Heuristic event score: importance={importance}",
            latency_ms=0.0,
            success=True,
            stub=True,
        )

    # ── Properties ──────────────────────────────

    @property
    def stats(self) -> dict:
        """Get scorer statistics."""
        return dict(self._stats)

    @property
    def is_stub_mode(self) -> bool:
        """Check if stub mode is active."""
        return self._stub_mode

    @property
    def is_initialized(self) -> bool:
        """Check if scorer is initialized."""
        return self._stub_mode or self._client is not None
