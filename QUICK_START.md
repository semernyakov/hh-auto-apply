# Quick Start Guide

## Bot Commands

### Resume Boost Bot
```bash
# Watch mode (continuous)
.venv/bin/python -u resume_boost.py --watch

# Single run
.venv/bin/python -u resume_boost.py

# Dry run (no actual clicks)
.venv/bin/python -u resume_boost.py --dry-run
```

### Auto Apply Bot
```bash
# Watch mode (continuous)
.venv/bin/python -u auto_apply_template.py --watch

# Single run
.venv/bin/python -u auto_apply_template.py
```

### Dashboard
```bash
# Start dashboard
.venv/bin/python -u dashboard.py

# Dashboard will be available at http://127.0.0.1:8765
```

## Process Management

### Check running bots
```bash
ps aux | grep -E "(resume_boost|auto_apply|dashboard)" | grep -v grep
```

### Stop bots
```bash
# Find PID first
ps aux | grep -E "(resume_boost|auto_apply|dashboard)" | grep -v grep

# Kill by PID
kill <PID>
```

## Logs
- **Resume Boost**: `/tmp/hh_resume_boost.log`
- **Auto Apply**: `logs/auto_apply.log`
- **Dashboard**: `logs/dashboard.log`