# 1Password Connect - Future Phase

This document outlines when and how to add 1Password Connect for higher-scale deployments.

## Current Setup (Service Accounts)

We currently use **Service Account tokens** for:
- GitHub Actions CI/CD
- Hetzner server bootstrapping
- Direct `op` CLI calls

This is appropriate for:
- Small team (1-5 people)
- Few servers (1-3)
- Low secret read frequency (< 100/hour)

---

## When to Add 1Password Connect

Consider adding Connect when:

1. **Scaling Infrastructure**
   - Moving to Kubernetes
   - Multiple microservices needing secrets
   - Many servers (> 5)

2. **High Read Volume**
   - > 100 secret reads per hour
   - Secrets needed at container startup
   - Rate limiting becomes an issue

3. **Enhanced Security Requirements**
   - Need local secret caching
   - Audit logging requirements
   - Compliance requirements (SOC2, etc.)

---

## Connect Architecture

```
┌─────────────────────┐
│   1Password Cloud   │
└─────────┬───────────┘
          │
          │ Sync
          │
┌─────────▼───────────┐
│  1Password Connect  │  (Self-hosted)
│  Server + Sync      │
└─────────┬───────────┘
          │
          │ REST API
          │
┌─────────▼───────────┐
│   Applications      │
│   - Trading Engine  │
│   - CI/CD Pipeline  │
│   - Kubernetes      │
└─────────────────────┘
```

---

## Implementation Plan (When Ready)

### 1. Infrastructure Changes

Add to `infra/connect/`:
```
infra/
  connect/
    docker-compose.yml      # Connect server
    credentials.json        # Connect credentials
    1password-credentials   # Encrypted credentials file
```

### 2. Docker Compose Addition

```yaml
# infra/connect/docker-compose.yml
services:
  op-connect-api:
    image: 1password/connect-api:latest
    ports:
      - "8080:8080"
    volumes:
      - ./1password-credentials.json:/home/opuser/.op/1password-credentials.json:ro
      - op-connect-data:/home/opuser/.op/data
    environment:
      - OP_HTTP_PORT=8080

  op-connect-sync:
    image: 1password/connect-sync:latest
    volumes:
      - ./1password-credentials.json:/home/opuser/.op/1password-credentials.json:ro
      - op-connect-data:/home/opuser/.op/data
    environment:
      - OP_HTTP_PORT=8081

volumes:
  op-connect-data:
```

### 3. Script Changes

Update `fetch_secret.sh` to use Connect API:
```bash
# Instead of: op read "op://..."
# Use: curl -H "Authorization: Bearer $OP_CONNECT_TOKEN" \
#           "$OP_CONNECT_URL/v1/vaults/$VAULT/items/$ITEM"
```

### 4. Kubernetes Integration

For K8s, use the 1Password Operator:
```yaml
# External Secrets or 1Password Operator
apiVersion: onepassword.com/v1
kind: OnePasswordItem
metadata:
  name: trading-secrets
spec:
  itemPath: "vaults/AF - Trading Infra - Prod/items/abstractfinance.prod.env"
```

---

## Cost Considerations

- 1Password Connect is included in Business tier
- Self-hosted, no additional licensing
- Infrastructure cost: ~$5-10/month for a small server

---

## Timeline

| Phase | Trigger | Action |
|-------|---------|--------|
| Current | Now | Service Accounts + `op` CLI |
| Phase 2 | > 5 servers OR K8s | Add 1Password Connect |
| Phase 3 | Multi-region | Connect per region |

---

## Resources

- [1Password Connect Documentation](https://developer.1password.com/docs/connect/)
- [1Password Kubernetes Operator](https://developer.1password.com/docs/k8s/k8s-operator/)
- [Connect API Reference](https://developer.1password.com/docs/connect/connect-api-reference/)
