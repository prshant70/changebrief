"""Change-aware validation service."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from changebrief.core.analyzer.change_analyzer import ChangeSummary, analyze_changes
from changebrief.core.analyzer.confidence_scorer import ConfidenceSummary, compute_confidence
from changebrief.core.analyzer.impact_mapper import map_impact
from changebrief.core.analyzer.intent_classifier import IntentSummary, classify_intent
from changebrief.core.analyzer.risk_classifier import RiskSummary, classify_risk
from changebrief.core.cache.store import build_context_id, get_repo_id, read_cache, write_cache
from changebrief.core.context import AppContext
from changebrief.core.llm.validation_planner import (
    ValidationPlan,
    calibrate_with_confidence,
    generate_validation_plan,
    system_prompt_hash,
)
from changebrief.core.models.requests import ValidateRequest
from changebrief.core.tools.code_tools import CodeTools
from changebrief.core.validator import (
    resolve_git_sha,
    validate_git_branch,
    validate_path_exists,
)


class ValidationResult:
    """Bundle returned to the CLI: structured plan + the deterministic context."""

    def __init__(
        self,
        *,
        plan: ValidationPlan,
        intent: IntentSummary,
        confidence: ConfidenceSummary,
        change_summary: ChangeSummary,
    ) -> None:
        self.plan = plan
        self.intent = intent
        self.confidence = confidence
        self.change_summary = change_summary


class ValidationService:
    """Runs checks to catch regressions between two refs."""

    def __init__(self, ctx: AppContext) -> None:
        self.config = ctx.config
        self.logger = ctx.logger

    def run(self, request: ValidateRequest) -> ValidationResult:
        self._validate(request)
        return self._execute(request)

    # ------------------------------------------------------------------ helpers

    def _validate(self, request: ValidateRequest) -> None:
        repo: Optional[Path] = None
        if request.path:
            repo = validate_path_exists(request.path, kind="Repository path")
        base = validate_git_branch(request.base, repo=repo)
        feature = validate_git_branch(request.feature, repo=repo)
        self._resolved_base = base
        self._resolved_feature = feature
        self._repo = repo or Path(".").resolve()

    def _context_id(self) -> str:
        model = str(self.config.get("default_model") or "gpt-4o-mini").strip()
        return build_context_id(model=model, prompt_hash=system_prompt_hash())

    def _execute(self, request: ValidateRequest) -> ValidationResult:
        self.logger.info(
            "Analyzing changes between %s and %s",
            self._resolved_base,
            self._resolved_feature,
        )

        base_sha = resolve_git_sha(self._resolved_base, repo=self._repo)
        feature_sha = resolve_git_sha(self._resolved_feature, repo=self._repo)
        repo_id = get_repo_id(self._repo)
        context_id = self._context_id()

        def _read(key: str):
            if request.nocache:
                return None
            return read_cache(
                repo_id=repo_id,
                base_sha=base_sha,
                feature_sha=feature_sha,
                key=key,
                context_id=context_id,
            )

        def _write(key: str, value) -> None:
            if request.nocache:
                return
            write_cache(
                repo_id=repo_id,
                base_sha=base_sha,
                feature_sha=feature_sha,
                key=key,
                value=value,
                context_id=context_id,
            )

        # Change summary
        cached_change = _read("change_summary")
        if cached_change:
            self.logger.info(
                "Cache hit: change_summary (%s..%s)", base_sha[:8], feature_sha[:8]
            )
            change_summary = ChangeSummary(
                files=list(cached_change.get("files", [])),
                functions=list(cached_change.get("functions", [])),
                diff_text=str(cached_change.get("diff_text", "")),
            )
        else:
            self.logger.info(
                "Cache miss: change_summary (%s..%s)", base_sha[:8], feature_sha[:8]
            )
            change_summary = analyze_changes(
                self._resolved_base,
                self._resolved_feature,
                repo_path=str(self._repo),
            )
            _write(
                "change_summary",
                {
                    "files": change_summary.files,
                    "functions": change_summary.functions,
                    "diff_text": change_summary.diff_text,
                },
            )

        # Risk
        cached_risk = _read("risk_summary")
        if cached_risk:
            risk = RiskSummary(
                level=str(cached_risk.get("level", "low")),
                types=list(cached_risk.get("types", [])),
            )
        else:
            risk = classify_risk(change_summary)
            _write("risk_summary", {"level": risk.level, "types": risk.types})

        # Intent
        cached_intent = _read("intent_summary")
        if cached_intent:
            intent = IntentSummary(
                intent_score=float(cached_intent.get("intent_score", 0.5)),
                intent_label=str(cached_intent.get("intent_label", "uncertain")),
                signals=list(cached_intent.get("signals", [])),
            )
        else:
            intent = classify_intent(
                change_summary,
                repo_path=str(self._repo),
                feature_ref=self._resolved_feature,
            )
            _write(
                "intent_summary",
                {
                    "intent_score": intent.intent_score,
                    "intent_label": intent.intent_label,
                    "signals": intent.signals,
                },
            )

        # Confidence
        cached_conf = _read("confidence_summary")
        if cached_conf:
            confidence = ConfidenceSummary(
                score=float(cached_conf.get("score", 0.5)),
                level=str(cached_conf.get("level", "Medium")),
                reasons=list(cached_conf.get("reasons", [])),
            )
        else:
            confidence = compute_confidence(change_summary, intent)
            _write(
                "confidence_summary",
                {
                    "score": confidence.score,
                    "level": confidence.level,
                    "reasons": confidence.reasons,
                },
            )

        # Impact (LLM-assisted)
        tools = CodeTools(
            repo_path=self._repo,
            base=self._resolved_base,
            feature=self._resolved_feature,
            diff_text=change_summary.diff_text,
            changed_files=change_summary.files,
            config=dict(self.config),
        )
        cached_impact = _read("impact_summary")
        if cached_impact:
            impact_endpoints = list(cached_impact.get("endpoints", []))
            impact_mapping = dict(cached_impact.get("mapping", {}))
            impact = type(
                "ImpactSummaryObj",
                (),
                {"endpoints": impact_endpoints, "mapping": impact_mapping},
            )()
        else:
            impact = map_impact(change_summary, tools)
            _write(
                "impact_summary",
                {"endpoints": list(impact.endpoints), "mapping": dict(impact.mapping)},
            )

        # Validation plan (typed JSON, not a parsed string)
        cached_plan = _read("validation_plan")
        if cached_plan and isinstance(cached_plan, dict) and cached_plan.get("merge_risk"):
            plan = ValidationPlan.from_dict(cached_plan)
        else:
            plan = generate_validation_plan(
                change_summary,
                impact,
                risk,
                intent_summary=intent,
                confidence_summary=confidence,
                config=dict(self.config),
            )
            _write("validation_plan", plan.to_dict())

        # Confidence-aware calibration on the typed enum (no string parsing).
        plan = calibrate_with_confidence(plan, confidence)

        return ValidationResult(
            plan=plan,
            intent=intent,
            confidence=confidence,
            change_summary=change_summary,
        )
