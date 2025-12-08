#!/bin/bash
# PostgreSQL backup script for AbstractFinance
# Run via cron: 0 3 * * * /srv/abstractfinance/scripts/backup_postgres.sh

set -e

# Configuration
BACKUP_DIR="/srv/abstractfinance/backups"
DB_CONTAINER="postgres"
DB_NAME="${DB_NAME:-abstractfinance}"
DB_USER="${DB_USER:-postgres}"
RETENTION_DAYS=30
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/abstractfinance_${TIMESTAMP}.sql.gz"

# Create backup directory if needed
mkdir -p "$BACKUP_DIR"

# Log start
echo "$(date): Starting PostgreSQL backup to ${BACKUP_FILE}"

# Perform backup (gzipped)
docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$BACKUP_FILE"

# Verify backup was created and has content
if [ -s "$BACKUP_FILE" ]; then
    BACKUP_SIZE=$(ls -lh "$BACKUP_FILE" | awk '{print $5}')
    echo "$(date): Backup successful - ${BACKUP_FILE} (${BACKUP_SIZE})"
else
    echo "$(date): ERROR - Backup file is empty or missing!"
    rm -f "$BACKUP_FILE"
    exit 1
fi

# Delete old backups (older than RETENTION_DAYS)
find "$BACKUP_DIR" -name "abstractfinance_*.sql.gz" -mtime +${RETENTION_DAYS} -delete
DELETED=$(find "$BACKUP_DIR" -name "abstractfinance_*.sql.gz" -mtime +${RETENTION_DAYS} 2>/dev/null | wc -l)
echo "$(date): Cleaned up ${DELETED} old backups (retention: ${RETENTION_DAYS} days)"

# List current backups
echo "$(date): Current backups:"
ls -lh "$BACKUP_DIR"/abstractfinance_*.sql.gz 2>/dev/null | tail -5

# Optional: Copy to standby server via WireGuard
if ping -c 1 10.0.0.2 &>/dev/null; then
    echo "$(date): Syncing backup to standby server..."
    rsync -avz "$BACKUP_FILE" root@10.0.0.2:/srv/abstractfinance/backups/ 2>/dev/null && \
        echo "$(date): Backup synced to standby" || \
        echo "$(date): WARNING - Failed to sync to standby"
fi

echo "$(date): Backup completed successfully"
