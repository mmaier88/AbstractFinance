#!/bin/bash
# PostgreSQL Streaming Replication Setup Script
# Sets up primary and standby for AbstractFinance
#
# Run this script on the PRIMARY server first, then on STANDBY
#
# Usage:
#   ./setup_pg_replication.sh primary   # Run on primary (94.130.228.55)
#   ./setup_pg_replication.sh standby   # Run on standby (46.224.46.117)

set -e

PRIMARY_IP="94.130.228.55"
STANDBY_IP="46.224.46.117"
WIREGUARD_PRIMARY="10.0.0.1"
WIREGUARD_STANDBY="10.0.0.2"
REPLICATION_USER="replicator"
REPLICATION_PASSWORD="${PG_REPLICATION_PASSWORD:-changeme_in_production}"
DB_PASSWORD="${DB_PASSWORD:-postgres}"
POSTGRES_VERSION="14"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

setup_primary() {
    log_info "Setting up PRIMARY PostgreSQL server for replication..."

    cd /srv/abstractfinance

    # Create replication user and configure PostgreSQL
    log_info "Creating replication user and configuring PostgreSQL..."

    docker exec postgres psql -U postgres -c "
        -- Create replication user if not exists
        DO \$\$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '${REPLICATION_USER}') THEN
                CREATE USER ${REPLICATION_USER} WITH REPLICATION ENCRYPTED PASSWORD '${REPLICATION_PASSWORD}';
            END IF;
        END
        \$\$;
    "

    # Create custom postgresql.conf for replication
    cat > /srv/abstractfinance/infra/postgresql-primary.conf << 'EOF'
# Replication settings for PRIMARY
wal_level = replica
max_wal_senders = 3
wal_keep_size = 256MB
hot_standby = on

# Performance tuning
shared_buffers = 256MB
effective_cache_size = 768MB
maintenance_work_mem = 64MB
checkpoint_completion_target = 0.9
wal_buffers = 16MB

# Logging
log_destination = 'stderr'
logging_collector = on
log_directory = 'log'
log_filename = 'postgresql-%Y-%m-%d.log'
log_min_messages = warning
log_min_error_statement = error
EOF

    # Create pg_hba.conf for replication
    cat > /srv/abstractfinance/infra/pg_hba-primary.conf << EOF
# PostgreSQL Client Authentication Configuration File
# TYPE  DATABASE        USER            ADDRESS                 METHOD

# Local connections
local   all             all                                     trust
host    all             all             127.0.0.1/32            scram-sha-256
host    all             all             ::1/128                 scram-sha-256

# Docker network
host    all             all             172.16.0.0/12           scram-sha-256
host    all             all             192.168.0.0/16          scram-sha-256

# Replication from standby (via WireGuard)
host    replication     ${REPLICATION_USER}     ${WIREGUARD_STANDBY}/32    scram-sha-256

# Replication from standby (via public IP)
host    replication     ${REPLICATION_USER}     ${STANDBY_IP}/32           scram-sha-256
EOF

    log_info "Configuration files created in /srv/abstractfinance/infra/"
    log_info "To apply these settings, update docker-compose.yml to mount these configs"

    # Print instructions
    cat << 'EOF'

=== PRIMARY SETUP COMPLETE ===

Next steps:
1. Update docker-compose.yml to mount postgresql-primary.conf:
   volumes:
     - ./infra/postgresql-primary.conf:/etc/postgresql/postgresql.conf:ro
     - ./infra/pg_hba-primary.conf:/etc/postgresql/pg_hba.conf:ro

2. Add command to postgres service:
   command: postgres -c 'config_file=/etc/postgresql/postgresql.conf' -c 'hba_file=/etc/postgresql/pg_hba.conf'

3. Restart PostgreSQL:
   docker compose restart postgres

4. Run this script on the STANDBY server:
   ./setup_pg_replication.sh standby

EOF
}

setup_standby() {
    log_info "Setting up STANDBY PostgreSQL server for replication..."

    cd /srv/abstractfinance

    # Stop PostgreSQL if running
    log_info "Stopping PostgreSQL on standby..."
    docker compose stop postgres 2>/dev/null || true

    # Create standby signal file and recovery configuration
    mkdir -p /srv/abstractfinance/infra

    cat > /srv/abstractfinance/infra/postgresql-standby.conf << 'EOF'
# Replication settings for STANDBY
primary_conninfo = 'host=${WIREGUARD_PRIMARY} port=5432 user=${REPLICATION_USER} password=${REPLICATION_PASSWORD}'
primary_slot_name = 'standby_slot'
hot_standby = on
wal_level = replica

# Performance tuning
shared_buffers = 256MB
effective_cache_size = 768MB
maintenance_work_mem = 64MB

# Logging
log_destination = 'stderr'
logging_collector = on
log_directory = 'log'
log_filename = 'postgresql-%Y-%m-%d.log'
EOF

    # Create standby signal file for PostgreSQL 12+
    cat > /srv/abstractfinance/infra/standby.signal << 'EOF'
# This file indicates this server is a standby
# PostgreSQL will start in recovery mode
EOF

    log_info "Standby configuration files created"

    # Print instructions
    cat << EOF

=== STANDBY SETUP INSTRUCTIONS ===

PostgreSQL streaming replication requires a base backup from primary.
Run these commands manually:

1. On PRIMARY, create a replication slot:
   docker exec postgres psql -U postgres -c "SELECT pg_create_physical_replication_slot('standby_slot');"

2. On STANDBY, stop postgres and remove old data:
   docker compose stop postgres
   docker volume rm abstractfinance_pgdata 2>/dev/null || true

3. On STANDBY, take a base backup from primary:
   docker run --rm -v abstractfinance_pgdata:/var/lib/postgresql/data \\
     postgres:${POSTGRES_VERSION}-alpine \\
     pg_basebackup -h ${WIREGUARD_PRIMARY} -U ${REPLICATION_USER} \\
     -D /var/lib/postgresql/data -P -R -X stream

4. Create standby signal:
   docker run --rm -v abstractfinance_pgdata:/var/lib/postgresql/data \\
     alpine touch /var/lib/postgresql/data/standby.signal

5. Start standby:
   docker compose up -d postgres

6. Verify replication on PRIMARY:
   docker exec postgres psql -U postgres -c "SELECT * FROM pg_stat_replication;"

NOTE: For this paper trading setup, consider using periodic pg_dump backups
(already configured in scripts/backup_postgres.sh) instead of streaming
replication, as it's simpler and sufficient for disaster recovery.

EOF
}

# Simplified alternative: just use pg_dump backups
setup_backup_sync() {
    log_info "Setting up simplified backup-based replication..."

    # This uses the existing backup script and adds rsync to standby
    cat << 'EOF'

=== SIMPLIFIED BACKUP-BASED REPLICATION ===

For paper trading with <$1M AUM, streaming replication is overkill.
Instead, use periodic pg_dump backups with rsync to standby.

This is already configured:
1. scripts/backup_postgres.sh runs daily at 3 AM via cron
2. Backups are synced to standby via WireGuard (10.0.0.2)

To restore on standby during failover:
  BACKUP=$(ls -t /srv/abstractfinance/backups/*.sql.gz | head -1)
  gunzip -c $BACKUP | docker exec -i postgres psql -U postgres -d abstractfinance

This provides:
- RPO (Recovery Point Objective): Up to 24 hours of data loss
- RTO (Recovery Time Objective): ~5 minutes manual failover
- Simplicity: No complex replication to maintain

For live trading with >$1M AUM, implement full streaming replication.

EOF
}

# Main script
case "${1:-}" in
    primary)
        setup_primary
        ;;
    standby)
        setup_standby
        ;;
    backup)
        setup_backup_sync
        ;;
    *)
        echo "AbstractFinance PostgreSQL Replication Setup"
        echo ""
        echo "Usage: $0 {primary|standby|backup}"
        echo ""
        echo "  primary  - Configure primary server for replication"
        echo "  standby  - Configure standby server as replica"
        echo "  backup   - Show simplified backup-based replication (recommended for paper trading)"
        echo ""
        echo "For paper trading, the 'backup' option is recommended."
        echo "Full streaming replication is better suited for live trading with larger AUM."
        exit 1
        ;;
esac
