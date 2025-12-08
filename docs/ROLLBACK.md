# Deployment Rollback Procedure

This document describes how to roll back a failed deployment of AbstractFinance.

## Quick Rollback Commands

### Rollback to Previous Docker Image

```bash
# SSH to server
ssh root@94.130.228.55

# Stop current containers
cd /srv/abstractfinance
docker compose down trading-engine

# Find previous image
docker images abstractfinance-trading-engine --format "{{.ID}} {{.CreatedAt}}" | head -5

# Tag the previous image as latest (replace IMAGE_ID)
docker tag IMAGE_ID abstractfinance-trading-engine:latest

# Restart
docker compose up -d trading-engine
```

### Rollback Git Commit

```bash
# SSH to server
ssh root@94.130.228.55
cd /srv/abstractfinance

# View recent commits
git log --oneline -10

# Revert to specific commit
git checkout COMMIT_HASH

# Rebuild and restart
docker compose build trading-engine
docker compose up -d trading-engine
```

## Rollback Scenarios

### 1. Failed CI/CD Deployment

If GitHub Actions deployment failed mid-way:

```bash
# Check deployment status
ssh root@94.130.228.55 "docker compose -f /srv/abstractfinance/docker-compose.yml ps"

# If containers are unhealthy, view logs
ssh root@94.130.228.55 "docker compose -f /srv/abstractfinance/docker-compose.yml logs trading-engine --tail 100"

# Reset to last known good state
ssh root@94.130.228.55 "cd /srv/abstractfinance && git fetch origin && git reset --hard origin/main~1"

# Rebuild and restart
ssh root@94.130.228.55 "cd /srv/abstractfinance && docker compose build trading-engine && docker compose up -d"
```

### 2. Trading Engine Crash After Deployment

If the engine starts but crashes during operation:

```bash
# Check for Python errors
ssh root@94.130.228.55 "docker compose -f /srv/abstractfinance/docker-compose.yml logs trading-engine 2>&1 | grep -i 'error\|exception\|traceback' | tail -20"

# Rollback to previous commit
ssh root@94.130.228.55 "cd /srv/abstractfinance && git checkout HEAD~1 && docker compose build trading-engine && docker compose up -d trading-engine"
```

### 3. Configuration Error (.env or config/)

If configuration changes caused issues:

```bash
# Restore .env from backup
ssh root@94.130.228.55 "cp /srv/abstractfinance/.env.backup /srv/abstractfinance/.env"

# Or restore specific config file
ssh root@94.130.228.55 "git checkout HEAD~1 -- config/instruments.yaml"

# Restart (no rebuild needed for config changes)
ssh root@94.130.228.55 "docker compose -f /srv/abstractfinance/docker-compose.yml restart trading-engine"
```

### 4. Database Schema Migration Issue

If a database migration failed:

```bash
# Restore from latest backup
ssh root@94.130.228.55 "
  # Find latest backup
  BACKUP=\$(ls -t /srv/abstractfinance/backups/*.sql.gz | head -1)
  echo \"Restoring from: \$BACKUP\"

  # Stop engine to prevent writes
  cd /srv/abstractfinance
  docker compose stop trading-engine

  # Restore backup
  gunzip -c \$BACKUP | docker exec -i postgres psql -U postgres -d abstractfinance

  # Restart engine
  docker compose start trading-engine
"
```

### 5. IB Gateway Connection Issues After Update

If IBGA container fails to connect:

```bash
# Restart just the gateway
ssh root@94.130.228.55 "docker compose -f /srv/abstractfinance/docker-compose.yml restart ibgateway"

# Wait 3 minutes for TOTP auth
sleep 180

# Check gateway logs
ssh root@94.130.228.55 "docker logs ibgateway 2>&1 | tail -30"

# If still failing, check VNC to see login screen
# Connect to http://94.130.228.55:6080 for noVNC
```

## Pre-Deployment Checklist

Before deploying, ensure you have:

1. **Backup current state**:
   ```bash
   ssh root@94.130.228.55 "
     cp /srv/abstractfinance/.env /srv/abstractfinance/.env.backup
     docker compose -f /srv/abstractfinance/docker-compose.yml exec -T postgres pg_dump -U postgres abstractfinance > /srv/abstractfinance/backups/pre_deploy_\$(date +%Y%m%d_%H%M%S).sql
   "
   ```

2. **Note current commit**:
   ```bash
   ssh root@94.130.228.55 "cd /srv/abstractfinance && git rev-parse HEAD"
   ```

3. **Check current container status**:
   ```bash
   ssh root@94.130.228.55 "docker compose -f /srv/abstractfinance/docker-compose.yml ps"
   ```

## Emergency Stop Procedures

### Stop All Trading Immediately

```bash
# Cancel orders and flatten positions
ssh root@94.130.228.55 "docker compose -f /srv/abstractfinance/docker-compose.yml exec -T trading-engine python -c \"
from src.execution_ibkr import IBClient
client = IBClient(host='ibgateway', port=4000)
client.connect()
client.cancel_all_orders()
print('All orders cancelled')
client.disconnect()
\""
```

### Full System Stop

```bash
ssh root@94.130.228.55 "cd /srv/abstractfinance && docker compose down"
```

### Quick Disable (Scheduler Only)

```bash
# Stop just the trading engine, keep monitoring
ssh root@94.130.228.55 "docker compose -f /srv/abstractfinance/docker-compose.yml stop trading-engine"
```

## Verification After Rollback

After any rollback, verify:

1. **Container Health**:
   ```bash
   ssh root@94.130.228.55 "docker compose -f /srv/abstractfinance/docker-compose.yml ps"
   ```

2. **Health Endpoint**:
   ```bash
   curl http://94.130.228.55:8080/health
   ```

3. **IB Connection**:
   ```bash
   ssh root@94.130.228.55 "docker compose -f /srv/abstractfinance/docker-compose.yml logs trading-engine 2>&1 | grep -i 'connected\|gateway' | tail -10"
   ```

4. **No Error Logs**:
   ```bash
   ssh root@94.130.228.55 "docker compose -f /srv/abstractfinance/docker-compose.yml logs trading-engine --since 5m 2>&1 | grep -i 'error' | head -10"
   ```

5. **Metrics Available**:
   ```bash
   curl -s http://94.130.228.55:8000/metrics | grep abstractfinance | head -10
   ```

## Contact for Help

If rollback fails:
1. Check Grafana dashboard: http://94.130.228.55:3000
2. Check Alertmanager: http://94.130.228.55:9093
3. Review trading-engine logs in detail
4. Consider failover to standby server (see FAILOVER.md)
