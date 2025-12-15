# Portfolio Status Check

First, read the README.md to understand the project context and current paper trading status.

Then check the live portfolio state on the staging server:

```bash
ssh root@94.130.228.55 "cat /srv/abstractfinance/state/portfolio_state.json | python3 -m json.tool"
```

Summarize the portfolio with:
1. **Performance**: NAV, total return, daily P&L, max drawdown
2. **Positions**: Table of all holdings with quantities, costs, and current values
3. **Exposure**: Gross/net exposure, long/short breakdown
4. **NAV History**: Chart of recent performance

Compare current metrics to the targets in README.md (60-day burn-in progress, drawdown limits, etc.)
