#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Убираем SOCKS-прокси, который ломает Anthropic SDK
unset all_proxy ALL_PROXY

export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium-browser
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export PLAYWRIGHT_HEADLESS=1
export HH_HEADLESS=1

VENV=".venv/bin/python"

start_bot() {
    local name=$1; shift
    local pid_file="/tmp/${name}.pid"
    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        echo "⏭️  $name уже работает (PID $(cat "$pid_file"))"
        return
    fi
    setsid "$VENV" -u "$@" </dev/null >>"/tmp/${name}.log" 2>&1 &
    local pid=$!
    echo "$pid" > "$pid_file"
    echo "✅ $name запущен (PID $pid)"
}

start_bot hh_dashboard        dashboard.py
start_bot hh_resume_boost     resume_boost.py --watch
start_bot hh_auto_reply       auto_reply.py --watch

echo ""
echo "📊 Дашборд: http://127.0.0.1:8765"
