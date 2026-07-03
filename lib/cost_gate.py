"""Self-contained LLM spend policy gate.

Enforces a simple, caller-aware budget policy with a persistent JSON ledger
(file-locked so it is safe across concurrent processes on one host). This is
the same pattern real production callers use to keep an LLM-judge stage from
running away on cost: a per-run cap, a per-day cap, and a call-count cap, all
checked *before* the request goes out (`preflight`) and reconciled with actual
token usage afterward (`record_usage`).

There is no cross-process registry of "all callers on the stack" here — this
is a minimal, single-service version of the pattern. If you run several
services that should share one wallet, point them at the same `state_file`
and give each a distinct `caller_id`; the per-caller and global daily totals
in the ledger already support that.
"""
from __future__ import annotations

import fcntl
import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

# Illustrative pricing table (USD per million tokens). Update to match
# whatever models you actually call — this is not fetched from anywhere.
_DEFAULT_PRICING_USD_PER_MTOKEN = {
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
}


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    cost_capped: bool
    code: str
    reason: str
    projected_usd: float
    would_block: bool = False


class CostPolicyGate:
    """Budget gate with per-run, per-day, and call-count limits."""

    def __init__(
        self,
        caller_id: str,
        policy: dict[str, Any] | None = None,
        state_file: str | Path | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.caller_id = caller_id.strip() or "unknown-caller"
        self.policy = policy or {}
        self.log = logger or logging.getLogger("cost-gate")
        self.state_file = Path(state_file) if state_file else Path("state/cost_gate.json")

        self.enabled = _as_bool(self.policy.get("enabled", True), True)
        self.enforce = _as_bool(self.policy.get("enforce", True), True)
        self.provider = str(self.policy.get("provider", "anthropic")).strip() or "anthropic"

        self.global_daily_max_usd = _as_float(self.policy.get("global_daily_max_usd"), 0.0)
        self.caller_daily_max_usd = _as_float(self.policy.get("caller_daily_max_usd"), 0.0)
        self.caller_run_max_usd = _as_float(self.policy.get("caller_run_max_usd"), 0.0)
        self.caller_max_calls_per_run = _as_int(self.policy.get("caller_max_calls_per_run"), 0)

        self.pricing = dict(_DEFAULT_PRICING_USD_PER_MTOKEN)
        raw_pricing = self.policy.get("model_pricing_usd_per_mtoken", {})
        if isinstance(raw_pricing, dict):
            for model, rates in raw_pricing.items():
                if not isinstance(model, str) or not isinstance(rates, dict):
                    continue
                in_rate = _as_float(rates.get("input"), -1.0)
                out_rate = _as_float(rates.get("output"), -1.0)
                if in_rate >= 0 and out_rate >= 0:
                    self.pricing[model] = {"input": in_rate, "output": out_rate}

        self._run_usd = 0.0
        self._run_calls = 0

    def preflight(
        self,
        *,
        model: str,
        input_tokens_estimate: int,
        expected_output_tokens: int = 0,
        provider: str | None = None,
    ) -> GateDecision:
        if not self.enabled:
            return GateDecision(True, False, "disabled", "Cost gate disabled", 0.0)

        provider_name = (provider or self.provider).strip() or self.provider
        projected_usd = self.estimate_cost(
            tokens_in=max(input_tokens_estimate, 0),
            tokens_out=max(expected_output_tokens, 0),
            model=model,
            provider=provider_name,
        )

        block_code, block_reason = self._first_block_reason(projected_usd)
        if block_code is None:
            return GateDecision(True, False, "allow", "Within configured budget", projected_usd)

        if self.enforce:
            return GateDecision(False, True, block_code, block_reason, projected_usd)

        return GateDecision(
            True,
            False,
            "shadow_allow",
            f"Shadow mode allow; would block: {block_reason}",
            projected_usd,
            would_block=True,
        )

    def record_usage(
        self,
        *,
        model: str,
        tokens_in: int,
        tokens_out: int,
        provider: str | None = None,
    ) -> float:
        provider_name = (provider or self.provider).strip() or self.provider
        call_usd = self.estimate_cost(
            tokens_in=max(tokens_in, 0),
            tokens_out=max(tokens_out, 0),
            model=model,
            provider=provider_name,
        )

        self._run_calls += 1
        self._run_usd += call_usd

        if not self.enabled:
            return call_usd

        def _update(state: dict[str, Any]) -> dict[str, Any]:
            global_section = state.setdefault("global", {"usd": 0.0, "calls": 0})
            global_section["usd"] = float(global_section.get("usd", 0.0)) + call_usd
            global_section["calls"] = int(global_section.get("calls", 0)) + 1

            callers = state.setdefault("callers", {})
            caller = callers.setdefault(self.caller_id, {"usd": 0.0, "calls": 0})
            caller["usd"] = float(caller.get("usd", 0.0)) + call_usd
            caller["calls"] = int(caller.get("calls", 0)) + 1
            return state

        self._update_daily_state(_update)
        return call_usd

    def get_run_summary(self) -> dict[str, Any]:
        return {
            "caller_id": self.caller_id,
            "usd": round(self._run_usd, 6),
            "calls": self._run_calls,
            "caller_run_max_usd": self.caller_run_max_usd,
            "caller_max_calls_per_run": self.caller_max_calls_per_run,
        }

    def get_daily_summary(self) -> dict[str, Any]:
        state = self._read_daily_state()
        callers = state.get("callers", {})
        caller = callers.get(self.caller_id, {"usd": 0.0, "calls": 0})
        global_section = state.get("global", {"usd": 0.0, "calls": 0})

        return {
            "date": state.get("date", str(date.today())),
            "global_usd": float(global_section.get("usd", 0.0)),
            "global_calls": int(global_section.get("calls", 0)),
            "caller_usd": float(caller.get("usd", 0.0)),
            "caller_calls": int(caller.get("calls", 0)),
            "global_daily_max_usd": self.global_daily_max_usd,
            "caller_daily_max_usd": self.caller_daily_max_usd,
        }

    def estimate_cost(self, *, tokens_in: int, tokens_out: int, model: str, provider: str) -> float:
        rates = self._pricing_for_model(model)
        input_cost = (max(tokens_in, 0) / 1_000_000) * rates["input"]
        output_cost = (max(tokens_out, 0) / 1_000_000) * rates["output"]
        return input_cost + output_cost

    def _first_block_reason(self, projected_usd: float) -> tuple[str | None, str]:
        if self.caller_max_calls_per_run > 0 and self._run_calls >= self.caller_max_calls_per_run:
            return (
                "run_call_cap",
                f"Run call cap exceeded: {self._run_calls} calls used, cap {self.caller_max_calls_per_run}",
            )

        if self.caller_run_max_usd > 0 and (self._run_usd + projected_usd) > self.caller_run_max_usd:
            return (
                "run_spend_cap",
                f"Run spend cap exceeded: ${self._run_usd:.2f} accumulated, cap ${self.caller_run_max_usd:.2f}",
            )

        daily = self.get_daily_summary()
        if self.global_daily_max_usd > 0 and (daily["global_usd"] + projected_usd) > self.global_daily_max_usd:
            return (
                "global_daily_cap",
                f"Global daily cap exceeded: ${daily['global_usd']:.2f} accumulated, "
                f"cap ${self.global_daily_max_usd:.2f}",
            )

        if self.caller_daily_max_usd > 0 and (daily["caller_usd"] + projected_usd) > self.caller_daily_max_usd:
            return (
                "caller_daily_cap",
                f"Caller daily cap exceeded: ${daily['caller_usd']:.2f} accumulated, "
                f"cap ${self.caller_daily_max_usd:.2f}",
            )

        return None, ""

    def _pricing_for_model(self, model: str) -> dict[str, float]:
        if model in self.pricing:
            return self.pricing[model]
        return self.pricing.get("claude-sonnet-4-5", {"input": 3.0, "output": 15.0})

    def _read_daily_state(self) -> dict[str, Any]:
        today = str(date.today())
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        if not self.state_file.exists():
            return self._fresh_state(today)

        try:
            with self.state_file.open("r", encoding="utf-8") as fh:
                fcntl.flock(fh, fcntl.LOCK_SH)
                try:
                    raw = fh.read().strip()
                    data = json.loads(raw) if raw else {}
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
        except Exception:
            return self._fresh_state(today)

        if not isinstance(data, dict) or data.get("date") != today:
            return self._fresh_state(today)

        return data

    def _update_daily_state(self, update_fn: Any) -> None:
        today = str(date.today())
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            mode = "r+" if self.state_file.exists() else "w+"
            with self.state_file.open(mode, encoding="utf-8") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    fh.seek(0)
                    raw = fh.read().strip()
                    data = json.loads(raw) if raw else {}
                    if not isinstance(data, dict) or data.get("date") != today:
                        data = self._fresh_state(today)
                    updated = update_fn(data)
                    if not isinstance(updated, dict):
                        updated = data
                    updated.setdefault("date", today)
                    updated.setdefault("global", {"usd": 0.0, "calls": 0})
                    updated.setdefault("callers", {})

                    fh.seek(0)
                    fh.truncate()
                    json.dump(updated, fh, indent=2)
                    fh.write("\n")
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
        except Exception as exc:
            self.log.warning("Failed to update cost gate ledger: %s", exc)

    @staticmethod
    def _fresh_state(day: str) -> dict[str, Any]:
        return {"date": day, "global": {"usd": 0.0, "calls": 0}, "callers": {}}


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
