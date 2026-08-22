"""hcm-risk-engine core modules.

Risk Engine is the signal validation layer in the HCM v2 event-driven pipeline:
- Stream Consumer: XREADGROUP on signal:stream via Redis consumer group
- Rule Chain: sequential rule evaluation from ConfigProviderV3
- Decision Engine: PASS / REJECT / DEGRADE output, publishing to signal:risk_passed
"""

__all__ = [
    "stream_consumer",
    "rule_chain",
    "decision",
]
