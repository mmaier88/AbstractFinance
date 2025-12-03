#!/bin/bash
# Daily trading run wrapper script

set -e

# Get script directory and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Change to project directory
cd "$PROJECT_DIR"

# Create logs directory if needed
mkdir -p logs

# Log file for this run
LOG_FILE="logs/daily_run_$(date +%Y%m%d_%H%M%S).log"

echo "=== AbstractFinance Daily Run ===" | tee "$LOG_FILE"
echo "Start time: $(date)" | tee -a "$LOG_FILE"
echo "Project dir: $PROJECT_DIR" | tee -a "$LOG_FILE"

# Activate virtual environment if exists
if [ -d "venv" ]; then
    source venv/bin/activate
    echo "Virtual environment activated" | tee -a "$LOG_FILE"
fi

# Set Python path
export PYTHONPATH="$PROJECT_DIR"

# Run the scheduler
echo "Running scheduler..." | tee -a "$LOG_FILE"
python -m src.scheduler 2>&1 | tee -a "$LOG_FILE"

EXIT_CODE=$?

echo "" | tee -a "$LOG_FILE"
echo "End time: $(date)" | tee -a "$LOG_FILE"
echo "Exit code: $EXIT_CODE" | tee -a "$LOG_FILE"

exit $EXIT_CODE
