"""
Backtest Comparison Script - Strategy Evolution Analysis.

Compares different strategy configurations to validate:
1. Original v1.0 (equity-heavy, VIX-based hedging)
2. Evolved v2.0 (EU vol convexity, Europe-first regime, trend filter)
3. Aggressive EU Vol (maximize insurance profile)

Usage:
    python -m src.research.backtest_compare
"""

import json
import logging
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Dict, Any, List
import numpy as np
import pandas as pd

from src.research.backtest import (
    BacktestConfig, BacktestRunner, BacktestResult, CostModelConfig
)

logger = logging.getLogger(__name__)


# Strategy configurations to compare
STRATEGY_CONFIGS = {
    "v1.0_original": {
        "description": "Original strategy - equity-heavy, VIX-based hedging",
        "sleeve_weights": {
            "core_index_rv": 0.35,        # Original high equity L/S
            "sector_rv": 0.25,            # Original sector
            "single_name": 0.10,
            "credit_carry": 0.15,
            "europe_vol_convex": 0.00,    # No EU vol convexity
            "crisis_alpha": 0.05,         # Small VIX-based hedging
            "cash_buffer": 0.10
        },
        "trend_filter_enabled": False,    # No trend filter
        "v2x_weight": 0.2,                # VIX-heavy regime
        "vix_weight": 0.5,
        "eurusd_trend_weight": 0.1,
        "drawdown_weight": 0.2,
    },

    "v2.0_evolved": {
        "description": "Current evolved strategy - EU vol convexity as primary insurance",
        "sleeve_weights": {
            "core_index_rv": 0.20,        # Reduced equity L/S
            "sector_rv": 0.20,            # Factor-neutral
            "single_name": 0.10,          # Trend-gated
            "credit_carry": 0.15,         # Regime-adaptive
            "europe_vol_convex": 0.15,    # PRIMARY insurance
            "crisis_alpha": 0.10,         # Europe-centric secondary
            "cash_buffer": 0.10
        },
        "trend_filter_enabled": True,     # Thesis monitoring
        "v2x_weight": 0.4,                # Europe-first regime
        "vix_weight": 0.3,
        "eurusd_trend_weight": 0.2,
        "drawdown_weight": 0.1,
    },

    "v2.1_aggressive_convex": {
        "description": "Aggressive EU convexity - maximize insurance profile",
        "sleeve_weights": {
            "core_index_rv": 0.15,        # Minimal equity L/S
            "sector_rv": 0.15,
            "single_name": 0.05,
            "credit_carry": 0.15,
            "europe_vol_convex": 0.20,    # Max EU vol convexity
            "crisis_alpha": 0.15,         # Higher hedging
            "cash_buffer": 0.15           # More cash buffer
        },
        "trend_filter_enabled": True,
        "v2x_weight": 0.5,                # Maximum Europe-first
        "vix_weight": 0.2,
        "eurusd_trend_weight": 0.2,
        "drawdown_weight": 0.1,
    },

    "v2.2_balanced": {
        "description": "Balanced approach - moderate convexity, solid returns",
        "sleeve_weights": {
            "core_index_rv": 0.20,
            "sector_rv": 0.20,
            "single_name": 0.10,
            "credit_carry": 0.15,
            "europe_vol_convex": 0.10,    # Moderate EU vol
            "crisis_alpha": 0.15,         # Higher secondary hedge
            "cash_buffer": 0.10
        },
        "trend_filter_enabled": True,
        "v2x_weight": 0.4,
        "vix_weight": 0.3,
        "eurusd_trend_weight": 0.2,
        "drawdown_weight": 0.1,
    },

    "v2.3_minimal_linear": {
        "description": "Minimal linear - convexity dominates",
        "sleeve_weights": {
            "core_index_rv": 0.10,        # Very small equity L/S
            "sector_rv": 0.10,
            "single_name": 0.05,
            "credit_carry": 0.20,         # Higher carry to offset bleed
            "europe_vol_convex": 0.25,    # Maximum convexity
            "crisis_alpha": 0.15,
            "cash_buffer": 0.15
        },
        "trend_filter_enabled": True,
        "v2x_weight": 0.5,
        "vix_weight": 0.2,
        "eurusd_trend_weight": 0.2,
        "drawdown_weight": 0.1,
    },
}


def create_config(strategy_name: str, base_config: Dict[str, Any]) -> BacktestConfig:
    """Create BacktestConfig from strategy parameters."""
    return BacktestConfig(
        start_date=date(2010, 1, 1),
        end_date=date.today(),
        initial_capital=1_000_000.0,
        sleeve_weights=base_config["sleeve_weights"],
        trend_filter_enabled=base_config.get("trend_filter_enabled", True),
        v2x_weight=base_config.get("v2x_weight", 0.4),
        vix_weight=base_config.get("vix_weight", 0.3),
        eurusd_trend_weight=base_config.get("eurusd_trend_weight", 0.2),
        drawdown_weight=base_config.get("drawdown_weight", 0.1),
        output_dir="state/research/comparison"
    )


def run_comparison() -> Dict[str, BacktestResult]:
    """Run all strategy configurations and return results."""
    results = {}

    for name, config_params in STRATEGY_CONFIGS.items():
        print(f"\n{'='*60}")
        print(f"Running: {name}")
        print(f"Description: {config_params['description']}")
        print(f"{'='*60}")

        config = create_config(name, config_params)
        runner = BacktestRunner(config)
        result = runner.run()

        # Save individual report
        Path("state/research/comparison").mkdir(parents=True, exist_ok=True)
        runner.save_report(result, f"{name}_report.json")

        results[name] = result

        print(f"\nResults for {name}:")
        print(f"  Total Return: {result.total_return:.1%}")
        print(f"  CAGR: {result.cagr:.1%}")
        print(f"  Sharpe: {result.sharpe_ratio:.2f}")
        print(f"  Sortino: {result.sortino_ratio:.2f}")
        print(f"  Max DD: {result.max_drawdown:.1%}")
        print(f"  Calmar: {result.calmar_ratio:.2f}")
        print(f"  Realized Vol: {result.realized_vol:.1%}")
        print(f"  Insurance Score: {result.insurance_score:.2%}")

        # Stress period performance
        print(f"\n  Stress Periods:")
        for sp in result.stress_periods:
            print(f"    {sp.name}: {sp.total_return:.1%} (DD: {sp.max_drawdown:.1%})")

    return results


def generate_comparison_report(results: Dict[str, BacktestResult]) -> str:
    """Generate comprehensive comparison report."""

    lines = [
        "# Strategy Evolution Backtest Comparison",
        "",
        f"**Generated:** {date.today()}",
        f"**Period:** January 2010 - December 2025",
        "",
        "---",
        "",
        "## Summary Metrics Comparison",
        "",
        "| Strategy | Total Return | CAGR | Sharpe | Sortino | Max DD | Calmar | Vol | Insurance |",
        "|----------|-------------|------|--------|---------|--------|--------|-----|-----------|",
    ]

    for name, result in results.items():
        lines.append(
            f"| {name} | {result.total_return:.0%} | {result.cagr:.1%} | "
            f"{result.sharpe_ratio:.2f} | {result.sortino_ratio:.2f} | "
            f"{result.max_drawdown:.1%} | {result.calmar_ratio:.2f} | "
            f"{result.realized_vol:.1%} | {result.insurance_score:+.1%} |"
        )

    lines.extend([
        "",
        "---",
        "",
        "## Stress Period Performance",
        "",
    ])

    # Get all stress periods
    stress_names = ["Euro Crisis 2011", "COVID 2020", "Rate Shock 2022"]

    lines.extend([
        "### Euro Crisis 2011 (Jul-Dec 2011)",
        "",
        "| Strategy | Return | Max DD | Hedge Payoff |",
        "|----------|--------|--------|--------------|",
    ])

    for name, result in results.items():
        sp = next((s for s in result.stress_periods if s.name == "Euro Crisis 2011"), None)
        if sp:
            lines.append(f"| {name} | {sp.total_return:.1%} | {sp.max_drawdown:.1%} | {sp.hedge_payoff:.1%} |")

    lines.extend([
        "",
        "### COVID Crash (Feb-Apr 2020)",
        "",
        "| Strategy | Return | Max DD | Hedge Payoff |",
        "|----------|--------|--------|--------------|",
    ])

    for name, result in results.items():
        sp = next((s for s in result.stress_periods if s.name == "COVID 2020"), None)
        if sp:
            lines.append(f"| {name} | {sp.total_return:.1%} | {sp.max_drawdown:.1%} | {sp.hedge_payoff:.1%} |")

    lines.extend([
        "",
        "### Rate Shock 2022 (Jan-Oct 2022)",
        "",
        "| Strategy | Return | Max DD | Hedge Payoff |",
        "|----------|--------|--------|--------------|",
    ])

    for name, result in results.items():
        sp = next((s for s in result.stress_periods if s.name == "Rate Shock 2022"), None)
        if sp:
            lines.append(f"| {name} | {sp.total_return:.1%} | {sp.max_drawdown:.1%} | {sp.hedge_payoff:.1%} |")

    lines.extend([
        "",
        "---",
        "",
        "## Key Findings",
        "",
        "### 1. EU Vol Convexity Impact",
        "",
        "Moving from VIX-based hedging (v1.0) to VSTOXX-based convexity (v2.0):",
        "",
    ])

    if "v1.0_original" in results and "v2.0_evolved" in results:
        v1 = results["v1.0_original"]
        v2 = results["v2.0_evolved"]

        lines.extend([
            f"- **Insurance Score:** {v1.insurance_score:+.1%} → {v2.insurance_score:+.1%} "
            f"({(v2.insurance_score - v1.insurance_score) / max(abs(v1.insurance_score), 0.01) * 100:+.0f}% improvement)",
            f"- **Sharpe Ratio:** {v1.sharpe_ratio:.2f} → {v2.sharpe_ratio:.2f}",
            f"- **Max Drawdown:** {v1.max_drawdown:.1%} → {v2.max_drawdown:.1%}",
            "",
        ])

    lines.extend([
        "### 2. Trend Filter Impact",
        "",
        "The trend filter reduces thesis bleed during EU outperformance by scaling down",
        "equity L/S when 60-day US vs EU momentum is negative.",
        "",
        "### 3. Optimal Configuration",
        "",
    ])

    # Find best insurance score
    best_insurance = max(results.items(), key=lambda x: x[1].insurance_score)
    best_sharpe = max(results.items(), key=lambda x: x[1].sharpe_ratio)
    best_calmar = max(results.items(), key=lambda x: x[1].calmar_ratio)

    lines.extend([
        f"- **Best Insurance Score:** {best_insurance[0]} ({best_insurance[1].insurance_score:+.1%})",
        f"- **Best Sharpe Ratio:** {best_sharpe[0]} ({best_sharpe[1].sharpe_ratio:.2f})",
        f"- **Best Calmar Ratio:** {best_calmar[0]} ({best_calmar[1].calmar_ratio:.2f})",
        "",
        "---",
        "",
        "## Configuration Details",
        "",
    ])

    for name, config_params in STRATEGY_CONFIGS.items():
        lines.extend([
            f"### {name}",
            "",
            f"*{config_params['description']}*",
            "",
            "```yaml",
            "sleeve_weights:",
        ])
        for sleeve, weight in config_params["sleeve_weights"].items():
            lines.append(f"  {sleeve}: {weight:.0%}")
        lines.extend([
            f"trend_filter: {config_params.get('trend_filter_enabled', True)}",
            f"v2x_weight: {config_params.get('v2x_weight', 0.4)}",
            f"vix_weight: {config_params.get('vix_weight', 0.3)}",
            "```",
            "",
        ])

    return "\n".join(lines)


def main():
    """Run comparison and generate report."""
    logging.basicConfig(level=logging.INFO)

    print("="*60)
    print("AbstractFinance Strategy Evolution Backtest Comparison")
    print("="*60)

    results = run_comparison()

    # Generate comparison report
    report = generate_comparison_report(results)

    # Save report
    output_path = Path("state/research/comparison/COMPARISON_REPORT.md")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report)

    print(f"\n{'='*60}")
    print("Comparison complete!")
    print(f"Report saved to: {output_path}")
    print(f"{'='*60}")

    # Print summary
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)

    print("\n{:<25} {:>10} {:>8} {:>8} {:>10}".format(
        "Strategy", "Return", "Sharpe", "Max DD", "Insurance"
    ))
    print("-"*65)

    for name, result in results.items():
        print("{:<25} {:>10.0%} {:>8.2f} {:>8.1%} {:>10.1%}".format(
            name,
            result.total_return,
            result.sharpe_ratio,
            result.max_drawdown,
            result.insurance_score
        ))


if __name__ == "__main__":
    main()
