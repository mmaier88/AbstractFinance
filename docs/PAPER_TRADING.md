# Paper Trading Documentation

This document tracks the 60-day paper trading burn-in period required before deploying real capital.

## Overview

- **Inception Date**: December 3, 2025
- **Target End Date**: February 1, 2026 (60 trading days)
- **Initial Capital**: $10,000,000 (simulated)
- **IBKR Account Type**: Paper Trading (separate from live)

---

## Current Status

| Metric | Value | Target |
|--------|-------|--------|
| Days Elapsed | 5 | 60 |
| NAV | $10,843,730.90 | N/A |
| Total PnL | +$843,730.90 (+8.4%) | Positive |
| Max Drawdown | -4.21% | < 10% |
| Current Drawdown | -4.21% | < 10% |

### Position Summary

| Symbol | Quantity | Sleeve | Currency |
|--------|----------|--------|----------|
| ARCC | 62,798 | core_index_rv | USD |
| CSPX | 2,688 | core_index_rv | USD |
| IUHC | 1,710 | core_index_rv | USD |
| IUQA | 16,880 | core_index_rv | USD |
| IHYU | 1,556 | core_index_rv | USD |
| IUIT | 14,807 | core_index_rv | USD |
| IUKD | -4,056 | core_index_rv | GBP |
| FLOT | 20,000 | core_index_rv | USD |
| LQDE | 370 | core_index_rv | USD |

### Sleeve Weights (Updated v2.2)

| Sleeve | Target | Status |
|--------|--------|--------|
| core_index_rv | 20% | Active |
| sector_rv | 20% | Active |
| europe_vol_convex | 18% | Active (Primary Insurance) |
| credit_carry | 8% | NORMAL Regime Only |
| money_market | 34% | Active |

**v2.2 Changes:** Removed single_name and crisis_alpha. See `docs/PORTFOLIO_SIMPLIFICATION.md`.

---

## Daily Log

### Week 1 (Dec 3-8, 2025)

**Dec 3 (Day 1)**
- Paper trading initiated
- Initial positions established in core_index_rv sleeve
- All systems operational

**Dec 4 (Day 2)**
- NAV: $10,000,000
- Daily return: 0.00%
- Systems stable

**Dec 5 (Day 3)**
- NAV: $10,786,682.68
- Daily return: +1.34%
- Strong performance from US vs EU spread

**Dec 6-7 (Weekend)**
- Markets closed
- NAV: $11,320,013.99 (mark-to-market on Friday close)
- Cumulative return: +4.94%

**Dec 8 (Day 5)**
- NAV: $10,843,730.90
- Daily return: -0.02%
- Drawdown from Friday's peak: -4.21%
- System restart due to log file permissions (resolved)

---

## Success Criteria

Before going live, the following criteria must be met:

### Performance Criteria
- [ ] Complete 60 trading days
- [ ] Maximum drawdown < 10%
- [x] Positive total return (currently +8.4%)
- [ ] Sharpe ratio > 0.5 (annualized)
- [ ] No catastrophic single-day losses (> 5%)

### Operational Criteria
- [x] Daily scheduled runs execute reliably
- [x] Auto-reconnect handles IB Gateway disconnects
- [x] Positions reconcile with IBKR account
- [ ] Zero unhandled exceptions for 30+ consecutive days
- [x] Monitoring alerts function correctly

### Risk Management Criteria
- [x] Drawdown limits trigger correctly
- [ ] Regime detection responds to volatility spikes
- [ ] Emergency de-risk procedure tested
- [ ] Manual override commands work

---

## Known Issues & Resolutions

| Date | Issue | Resolution | Status |
|------|-------|------------|--------|
| Dec 8 | Log file permission denied | `chown -R 1000:1000 /srv/abstractfinance/state` | Resolved |

---

## Operational Checklist

### Daily (Automated)
- [x] 06:00 UTC: Daily run executes
- [x] Positions synced with IBKR
- [x] PnL calculated and logged
- [x] State persisted to JSON

### Weekly (Manual)
- [ ] Review weekly PnL vs benchmark
- [ ] Check for error patterns in logs
- [ ] Verify backup integrity
- [ ] Review Grafana dashboards

### Monthly (Manual)
- [ ] Full system health review
- [ ] Test failover to standby
- [ ] Review and update documentation
- [ ] Sharpe/Sortino ratio calculation

---

## How to Query Paper Trading Status

```bash
# SSH to server and check portfolio state
ssh root@94.130.228.55 "cat /srv/abstractfinance/state/portfolio_state.json | python3 -m json.tool"

# Check recent logs
ssh root@94.130.228.55 "tail -100 /srv/abstractfinance/state/logs/trading_$(date +%Y-%m-%d).log"

# Check container health
ssh root@94.130.228.55 "docker compose -f /srv/abstractfinance/docker-compose.yml ps"

# Check Grafana dashboards
# https://94.130.228.55:3000 (or via Caddy if enabled)
```

---

## Go-Live Decision

After 60 days, review:

1. **Performance**: Did we meet return/risk targets?
2. **Stability**: Were there any system failures?
3. **Operations**: Did all scheduled tasks run?
4. **Alerts**: Did monitoring catch issues?

If all criteria pass, proceed to `docs/PRODUCTION_HARDENING.md` final checklist.

---

## Appendix: Secrets Management Options

The system currently uses `.env` files for secrets. For production:

### Option 1: SOPS + Age (Recommended for solo traders)
- **SOPS**: Mozilla's "Secrets OPerationS" - encrypts files at rest
- **Age**: Modern encryption tool (replacement for PGP)
- Workflow: Encrypt `.env` -> commit encrypted version -> decrypt on deploy
- Pros: Simple, no external dependencies, git-friendly
- Cons: Manual key management

### Option 2: Doppler (Recommended for teams)
- SaaS secrets management platform
- Workflow: Store secrets in Doppler -> inject at runtime via CLI
- Pros: Web UI, audit logs, team access control
- Cons: External dependency, monthly cost (~$20/user)

### Option 3: HashiCorp Vault (Enterprise)
- Self-hosted secrets management
- Pros: Maximum control, advanced features
- Cons: Complex to operate, overkill for small operations

**Current Status**: Using `.env` with `chmod 600` (acceptable for paper trading).
