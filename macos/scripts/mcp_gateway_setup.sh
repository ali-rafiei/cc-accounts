#!/bin/zsh
# Start the MCP gateway at login, then store a token for each service named:
#   mcp_gateway_setup.sh linear github
# Each token is asked for in a hidden-input dialog and saved in the Keychain
# as claude-mcp-<name>; the gateway reads it on every request.
#
# It also registers the gateway's OAuth routes (forgefirst for Alta, fellow) as user-scope
# servers in every profile, so a profile session uses the default login's sign-in. The
# default login keeps its own servers and is never changed. Rerun after adding a profile.
set -e
dir="${0:A:h}"  # where install.sh put mcp_gateway.py
oauth_routes=($(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import mcp_gateway; print(*mcp_gateway.OAUTH_UPSTREAMS)' "$dir"))
plist="$HOME/Library/LaunchAgents/com.user.claude-mcp-gateway.plist"
cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.user.claude-mcp-gateway</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/python3</string><string>$dir/mcp_gateway.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$dir/mcp_gateway.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$UID" "$plist" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$plist"
echo "gateway running on 127.0.0.1:8765"
for profile in "$dir"/*(N/); do
  [[ "${profile:t}" == (bin|_*) ]] && continue  # not profiles: the scripts' bin, __pycache__
  for route in "${oauth_routes[@]}"; do
    # Replaces a same-named server cc copied from the default login (fellow, say).
    CLAUDE_CONFIG_DIR="$profile" claude mcp remove --scope user "$route" >/dev/null 2>&1 || true
    CLAUDE_CONFIG_DIR="$profile" claude mcp add --scope user --transport http "$route" "http://127.0.0.1:8765/$route" >/dev/null
  done
  echo "${profile:t}: ${oauth_routes[*]} go through the gateway"
done
for name in "$@"; do
  token=$(osascript -e "text returned of (display dialog \"Paste your $name token\" default answer \"\" with hidden answer)")
  security add-generic-password -U -s "claude-mcp-$name" -a "$USER" -w "$token"
  echo "stored $name token"
done
