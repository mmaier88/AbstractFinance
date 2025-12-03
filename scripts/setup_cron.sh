#!/bin/bash
# Setup cron job for daily trading run

# Get the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Create the run script
cat > "$PROJECT_DIR/scripts/run_daily.sh" << 'EOF'
#!/bin/bash
set -e

# Load environment
cd "$(dirname "$0")/.."
source venv/bin/activate 2>/dev/null || true

# Set environment variables
export PYTHONPATH="$PWD"

# Run the scheduler
python -m src.scheduler >> logs/cron_$(date +%Y%m%d).log 2>&1
EOF

chmod +x "$PROJECT_DIR/scripts/run_daily.sh"

# Add cron job (run at 6:00 AM UTC, Monday-Friday)
CRON_JOB="0 6 * * 1-5 $PROJECT_DIR/scripts/run_daily.sh"

# Check if cron job already exists
(crontab -l 2>/dev/null | grep -v "run_daily.sh"; echo "$CRON_JOB") | crontab -

echo "Cron job installed:"
echo "$CRON_JOB"
echo ""
echo "Current crontab:"
crontab -l
