#!/bin/bash
# Throwaway launcher: detach the transition monitor and report that it is alive.
pgrep -f '/tmp/tmon.sh' | xargs -r kill 2>/dev/null
sleep 1
setsid nohup bash /tmp/tmon.sh "${1:-1800}" >/dev/null 2>&1 &
sleep 3
echo "monitor pid: $(pgrep -f '/tmp/tmon.sh' | head -1)"
echo "monitor lines: $(wc -l /tmp/transitions.log | awk '{print $1}')"
date +%H:%M:%S
