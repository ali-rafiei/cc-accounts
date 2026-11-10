#!/bin/zsh
# Start the MCP gateway at login, then store a token for each service named:
#   mcp_gateway_setup.sh linear github
# Each token is asked for in a hidden-input dialog and saved in the Keychain
# as claude-mcp-<name>; the gateway reads it on every request.
set -e
dir="${0:A:h}"  # where install.sh put mcp_gateway.py
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
for name in "$@"; do
  token=$(osascript -e "text returned of (display dialog \"Paste your $name token\" default answer \"\" with hidden answer)")
  security add-generic-password -U -s "claude-mcp-$name" -a "$USER" -w "$token"
  echo "stored $name token"
done
