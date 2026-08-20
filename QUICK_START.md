# Quick Start Guide

## Bot Commands

### Resume Boost Bot
```bash
# Watch mode (continuous)
/home/master/my-workspace/hh-auto-apply/.venv/bin/python -u /home/master/my-workspace/hh-auto-apply/resume_boost.py --watch

# Single run
/home/master/my-workspace/hh-auto-apply/.venv/bin/python -u /home/master/my-workspace/hh-auto-apply/resume_boost.py

# Dry run (no actual clicks)
/home/master/my-workspace/hh-auto-apply/.venv/bin/python -u /home/master/my-workspace/hh-auto-apply/resume_boost.py --dry-run
```

### Auto Apply Bot
```bash
# Watch mode (continuous)
/home/master/my-workspace/hh-auto-apply/.venv/bin/python -u /home/master/my-workspace/hh-auto-apply/auto_apply_template.py --watch

# Single run
/home/master/my-workspace/hh-auto-apply/.venv/bin/python -u /home/master/my-workspace/hh-auto-apply/auto_apply_template.py
```

### Dashboard
```bash
# Start dashboard
/home/master/my-workspace/hh-auto-apply/.venv/bin/python -u /home/master/my-workspace/hh-auto-apply/dashboard.py

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
- **Auto Apply**: `/home/master/my-workspace/hh-auto-apply/logs/auto_apply.log`
- **Dashboard**: `/home/master/my-workspace/hh-auto-apply/logs/dashboard.log`