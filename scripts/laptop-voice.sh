#!/usr/bin/env bash
# Friday's voice on the Windows laptop (WSL2, RTX 3050, qwentts.cpp, ~0.1 s to first audio).
#   laptop-voice.sh start    start the TTS server on the laptop (no-op if running)
#   laptop-voice.sh tunnel   forward 127.0.0.1:8091 on this Mac to it (foreground; FridayEars owns it)
#   laptop-voice.sh stop     stop the server and any tunnel
#   laptop-voice.sh status   health through the tunnel
# The audio is generated on the laptop and played on the Mac by fridayd.
set -uo pipefail
HOST="${FRIDAY_VOICE_HOST:-wsl-box}"
PORT="${FRIDAY_VOICE_PORT:-8091}"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=10 -o ServerAliveCountMax=2 -o LogLevel=ERROR)
SERVER='~/friday-tts/faster/.venv/bin/python ~/friday-tts/scripts/friday_tts_server.py --talker Q8_0 --codec BF16 --host 127.0.0.1 --port '"$PORT"

case "${1:-status}" in
  start)
    # Ask the port, not pgrep: this ssh command line contains the server's own command, so any process
    # match finds itself and the server never starts.
    "${SSH[@]}" "$HOST" "curl -s -m 2 http://127.0.0.1:$PORT/health >/dev/null || { mkdir -p ~/friday-tts/logs; setsid nohup $SERVER > ~/friday-tts/logs/server.log 2>&1 < /dev/null & }; echo ok" ;;
  tunnel)
    exec "${SSH[@]}" -N -o ExitOnForwardFailure=yes -L "$PORT:127.0.0.1:$PORT" "$HOST" ;;
  stop)
    pkill -f -- "-L $PORT:127.0.0.1:$PORT" 2>/dev/null
    "${SSH[@]}" "$HOST" "pkill -f '[f]riday_tts_server.py'; echo stopped" ;;
  status)
    curl -s -m 2 "http://127.0.0.1:$PORT/health" || { echo "laptop voice not reachable"; exit 1; } ;;
  *) echo "usage: $0 start|tunnel|stop|status"; exit 2 ;;
esac
