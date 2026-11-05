#!/bin/bash
set -euo pipefail
camofox_dir="$HOME/Riju/camofox-browser"
plist="$HOME/Library/LaunchAgents/com.riju.camofox.plist"
npm_path="$(command -v npm || true)"
if [[ ! -f "$camofox_dir/package.json" || -z "$npm_path" ]]; then
  echo "Clone camofox-browser to $camofox_dir and install Node/npm first." >&2
  exit 1
fi
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Riju/logs"
RIJU_NPM_PATH="$npm_path" RIJU_CAMOFOX_DIR="$camofox_dir" RIJU_PLIST="$plist" python3 - <<'PY'
import os, plistlib
home=os.path.expanduser('~')
config={
  'Label':'com.riju.camofox',
  'ProgramArguments':[os.environ['RIJU_NPM_PATH'],'start'],
  'WorkingDirectory':os.environ['RIJU_CAMOFOX_DIR'],
  'RunAtLoad':True,
  'KeepAlive':True,
  'EnvironmentVariables':{
    'HOME':home,
    'PATH':os.path.dirname(os.environ['RIJU_NPM_PATH'])+':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin',
    'CAMOFOX_BIND_HOST':'127.0.0.1',
    'CAMOFOX_CRASH_REPORT_ENABLED':'false',
    'CAMOFOX_LOCALE':'en-AU',
    'CAMOFOX_TIMEZONE':'Australia/Melbourne',
  },
  'StandardOutPath':home+'/Riju/logs/camofox.out.log',
  'StandardErrorPath':home+'/Riju/logs/camofox.err.log',
}
with open(os.environ['RIJU_PLIST'],'wb') as f: plistlib.dump(config,f)
PY
launchctl bootout "gui/$(id -u)/com.riju.camofox" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "Camofox LaunchAgent installed. Health: curl http://127.0.0.1:9377/health"
