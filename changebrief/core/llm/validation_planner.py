"""LLM-backed validation planner with structured output and safe fallback.

The model is asked to return JSON conforming to ``VALIDATION_PLAN_SCHEMA``.
This is enforced by OpenAI's ``response_format=json_schema`` so the consumer
side never has to parse emoji-prefixed prose for the merge decision.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List

from changebrief.core.analyzer.change_analyzer import ChangeSummary
from changebrief.core.analyzer.confidence_scorer import ConfidenceSummary
from changebrief.core.analyzer.impact_mapper import ImpactSummary
from changebrief.core.analyzer.intent_classifier import IntentSummary
from changebrief.core.analyzer.risk_classifier import RiskSummary
from changebrief.core.llm.guard import llm_disabled


SYSTEM_PROMPT = """\
You are a senior backend engineer reviewing a production code change.

Your goal is to produce a precise, evidence-based validation report.

CORE RESPONSIBILITIES:
1. Explain behavioral impact (what changed in system behavior).
2. Identify risks introduced by the change (NOT hypothetical failures).
3. Respect change intent: if INTENTIONAL, do NOT treat expected behavior as regression.
4. Recommend high-signal validations.
5. Provide a merge_risk decision: low | medium | high.

RISK DEFINITION (CRITICAL):
A risk MUST be directly supported by the code change.

DO:
- Identify changed components (methods, fields, logic).
- Describe how behavior or dependency has changed.
- Highlight new assumptions or dependencies.

DO NOT:
- Speculate about failures.
- Use 'if this fails then X happens'.
- Assume downstream systems break without evidence.

LANGUAGE:
Prefer 'introduces dependency on', 'changes behavior of', 'adds requirement
for', 'alters handling of'. Avoid 'if this fails', 'could lead to',
'might cause', 'would result in'.

OUTPUT:
You MUST return JSON that conforms to the supplied schema. No prose outside
the JSON object. No markdown.
"""

PROMPT_VERSION = "2"


VALIDATION_PLAN_SCHEMA: Dict[str, Any] = {
    "name": "validation_plan",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "behavioral_impact": {
                "type": "string",
                "description": "Concise description of what changed in system behavior.",
            },
            "risks": {
                "type": "array",
                "description": "Concrete risks anchored to the diff.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "level": {"type": "string", "enum": ["high", "medium", "low"]},
                        "change": {"type": "string"},
                        "impact": {"type": "string"},
                    },
                    "required": ["level", "change", "impact"],
                },
            },
            "validations": {
                "type": "array",
                "description": "Targeted scenarios to verify before merging (max 5).",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                        "scenario": {"type": "string"},
                        "expected": {"type": "string"},
                    },
                    "required": ["priority", "scenario", "expected"],
                },
            },
            "merge_risk": {"type": "string", "enum": ["low", "medium", "high"]},
        },
        "required": ["behavioral_impact", "risks", "validations", "merge_risk"],
    },
}


def system_prompt_hash() -> str:
    """Stable hash of the planner contract — used as a cache dimension."""
    payload = json.dumps(
        {
            "system": SYSTEM_PROMPT,
            "schema": VALIDATION_PLAN_SCHEMA,
            "prompt_version": PROMPT_VERSION,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


@dataclass
class RiskItem:
    level: str  # high | medium | low
    change: str
    impact: str


@dataclass
class ValidationItem:
    priority: str  # high | medium | low
    scenario: str
    expected: str


@dataclass
class ValidationPlan:
    behavioral_impact: str
    risks: List[RiskItem]
    validations: List[ValidationItem]
    merge_risk: str  # low | medium | high
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "behavioral_impact": self.behavioral_impact,
            "risks": [asdict(r) for r in self.risks],
            "validations": [asdict(v) for v in self.validations],
            "merge_risk": self.merge_risk,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ValidationPlan":
        risks = [
            RiskItem(
                level=_normalise_level(str(r.get("level", "low"))),
                change=str(r.get("change", "")).strip(),
                impact=str(r.get("impact", "")).strip(),
            )
            for r in (data.get("risks") or [])
            if isinstance(r, dict)
        ]
        validations = [
            ValidationItem(
                priority=_normalise_level(str(v.get("priority", "low"))),
                scenario=str(v.get("scenario", "")).strip(),
                expected=str(v.get("expected", "")).strip(),
            )
            for v in (data.get("validations") or [])
            if isinstance(v, dict)
        ]
        return cls(
            behavioral_impact=str(data.get("behavioral_impact", "")).strip(),
            risks=risks,
            validations=validations,
            merge_risk=_normalise_level(str(data.get("merge_risk", "medium"))),
            notes=[str(n) for n in (data.get("notes") or [])],
        )


_VALID_LEVELS = {"low", "medium", "high"}


def _normalise_level(value: str) -> str:
    v = (value or "").strip().lower()
    return v if v in _VALID_LEVELS else "medium"


def _fallback_plan(reason: str) -> ValidationPlan:
    """Conservative, schema-valid fallback used when the LLM is unavailable."""
    return ValidationPlan(
        behavioral_impact="Unable to generate detailed validation plan automatically.",
        risks=[],
        validations=[
            ValidationItem(
                priority="high",
                scenario="Manually verify impacted endpoints / call sites for the changed code.",
                expected="Behaviour matches the change author's intent on a representative input.",
            )
        ],
        merge_risk="medium",
        notes=[reason],
    )


def _build_user_prompt(
    change_summary: ChangeSummary,
    impact: ImpactSummary,
    risk: RiskSummary,
    intent: IntentSummary,
    confidence: ConfidenceSummary,
) -> str:
    files = "\n".join(f"- {f}" for f in (change_summary.files or [])[:50]) or "- (none)"
    diff_preview = (change_summary.diff_text or "")
    if len(diff_preview) > 16000:
        diff_preview = diff_preview[:16000] + "\n... [diff truncated for prompt budget]"
    return (
        "Changed files:\n"
        f"{files}\n\n"
        "Diff (unified, may be truncated):\n"
        "```\n"
        f"{diff_preview}\n"
        "```\n\n"
        f"Impacted endpoints: {impact.endpoints}\n"
        f"Risk types: {risk.types}\n"
        f"Intent: label={intent.intent_label} score={intent.intent_score:.2f} signals={intent.signals}\n"
        f"Confidence: level={confidence.level} score={confidence.score:.2f} reasons={confidence.reasons}\n\n"
        "Constraints:\n"
        "- Anchor every risk to a concrete change in the diff.\n"
        "- Avoid speculative language.\n"
        "- Provide between 1 and 5 validations.\n"
        "- Choose merge_risk in {low, medium, high} based on the evidence.\n"
        "- Return ONLY JSON conforming to the schema."
    )


def render_pretty(
    plan: ValidationPlan,
    *,
    intent: IntentSummary,
    confidence: ConfidenceSummary,
) -> str:
    """Pretty CLI output (the previous emoji format, but built from typed data)."""
    lines: list[str] = []

    intent_conf = (
        "High" if intent.intent_score >= 0.75
        else ("Medium" if intent.intent_score >= 0.5 else "Low")
    )
    lines.append("🧭 Change Intent:")
    lines.append(f"{intent.intent_label.capitalize()} ({intent_conf} Confidence)")
    lines.append("")

    lines.append("🎯 Analysis Confidence:")
    lines.append(confidence.level)
    for reason in (confidence.reasons or [])[:5]:
        lines.append(f"- {reason}")
    lines.append("")

    lines.append("🔍 Behavioral Impact:")
    lines.append(plan.behavioral_impact or "(no behavioural impact reported)")
    lines.append("")

    lines.append("💥 Change-Induced Risks:")
    by_level = {"high": [], "medium": [], "low": []}
    for r in plan.risks:
        by_level.setdefault(r.level, []).append(r)
    for level, glyph, header in [
        ("high", "🔥", "🔥 HIGH RISK:"),
        ("medium", "⚠️", "⚠️ MEDIUM RISK:"),
        ("low", "💡", "💡 LOW RISK:"),
    ]:
        items = by_level.get(level) or []
        if not items:
            continue
        lines.append("")
        lines.append(header)
        for item in items:
            lines.append(f"- Change: {item.change}")
            lines.append(f"  Impact: {item.impact}")
    if not plan.risks:
        lines.append("(no risks identified)")
    lines.append("")

    lines.append("🧪 Suggested Validations:")
    for idx, v in enumerate(plan.validations, start=1):
        glyph = {"high": "🔥", "medium": "⚠️", "low": "💡"}.get(v.priority, "•")
        lines.append("")
        lines.append(f"{glyph} {idx}. {v.scenario}")
        lines.append(f"   → Expect: {v.expected}")
    lines.append("")

    lines.append(f"🚨 Merge Risk: {plan.merge_risk.upper()}")

    if plan.notes:
        lines.append("")
        lines.append("Notes:")
        for note in plan.notes:
            lines.append(f"- {note}")

    return "\n".join(lines).rstrip() + "\n"


def render_markdown(
    plan: ValidationPlan,
    *,
    intent: IntentSummary,
    confidence: ConfidenceSummary,
) -> str:
    """Markdown rendering, suitable for PR comments / CI summaries."""
    md: list[str] = []
    md.append("## ChangeBrief — Pre-merge Validation")
    md.append("")
    md.append(
        f"- **Intent:** {intent.intent_label} (score {intent.intent_score:.2f})"
    )
    md.append(
        f"- **Analysis Confidence:** {confidence.level} (score {confidence.score:.2f})"
    )
    md.append(f"- **Merge Risk:** **{plan.merge_risk.upper()}**")
    md.append("")
    md.append("### Behavioral Impact")
    md.append(plan.behavioral_impact or "_(none reported)_")
    md.append("")
    md.append("### Risks")
    if not plan.risks:
        md.append("_(none identified)_")
    else:
        for r in plan.risks:
            md.append(f"- **{r.level.upper()}** — {r.change}: {r.impact}")
    md.append("")
    md.append("### Suggested Validations")
    if not plan.validations:
        md.append("_(none)_")
    else:
        for i, v in enumerate(plan.validations, 1):
            md.append(f"{i}. **[{v.priority.upper()}]** {v.scenario}")
            md.append(f"   - Expected: {v.expected}")
    if plan.notes:
        md.append("")
        md.append("### Notes")
        for n in plan.notes:
            md.append(f"- {n}")
    return "\n".join(md) + "\n"


def render_json(
    plan: ValidationPlan,
    *,
    intent: IntentSummary,
    confidence: ConfidenceSummary,
) -> str:
    """Machine-readable rendering for CI consumption."""
    payload = {
        "intent": {
            "label": intent.intent_label,
            "score": intent.intent_score,
            "signals": list(intent.signals or []),
        },
        "confidence": {
            "level": confidence.level,
            "score": confidence.score,
            "reasons": list(confidence.reasons or []),
        },
        "plan": plan.to_dict(),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def calibrate_with_confidence(
    plan: ValidationPlan,
    confidence: ConfidenceSummary,
) -> ValidationPlan:
    """Soften merge_risk when overall analysis confidence is Low."""
    if confidence.level != "Low":
        return plan
    if plan.merge_risk == "high":
        plan.merge_risk = "medium"
        plan.notes.append("merge_risk lowered from HIGH to MEDIUM due to low analysis confidence")
    elif plan.merge_risk == "medium":
        plan.merge_risk = "low"
        plan.notes.append("merge_risk lowered from MEDIUM to LOW due to low analysis confidence")
    return plan


def generate_validation_plan(
    change_summary: ChangeSummary,
    impact_summary: ImpactSummary,
    risk_summary: RiskSummary,
    intent_summary: IntentSummary,
    confidence_summary: ConfidenceSummary,
    *,
    config: dict,
) -> ValidationPlan:
    """Produce a structured plan; falls back safely when LLM is unavailable."""
    if llm_disabled():
        return _fallback_plan("LLM disabled (env: CHANGEBRIEF_DISABLE_LLM or running in pytest).")
    if not str(config.get("llm_api_key") or "").strip():
        return _fallback_plan("llm_api_key not configured; run `changebrief init`.")

    from changebrief.core.llm._openai_tools import run_with_tools

    user = _build_user_prompt(
        change_summary,
        impact_summary,
        risk_summary,
        intent_summary,
        confidence_summary,
    )

    try:
        text = run_with_tools(
            config=config,
            system=SYSTEM_PROMPT,
            user=user,
            tools=[],
            purpose="generate validation plan (structured JSON)",
            max_tool_rounds=1,
            temperature=0.1,
            response_format={"type": "json_schema", "json_schema": VALIDATION_PLAN_SCHEMA},
        )
    except Exception as exc:  # noqa: BLE001 — explicit defensive boundary for LLM
        return _fallback_plan(f"LLM call failed: {exc}")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _fallback_plan("LLM returned non-JSON response.")
    if not isinstance(data, dict):
        return _fallback_plan("LLM returned a non-object JSON payload.")

    plan = ValidationPlan.from_dict(data)
    if not plan.validations:
        plan.validations.append(
            ValidationItem(
                priority="high",
                scenario="Manually verify the changed code on a representative input.",
                expected="Behaviour matches the author's intent.",
            )
        )
    return plan
