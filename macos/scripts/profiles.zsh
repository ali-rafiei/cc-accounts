# claude-multi-account: run Claude Code as several accounts on one machine.
#
# Each extra account is a profile: a directory under $CLAUDE_PROFILES (default
# ~/.claude-profiles) used as that account's CLAUDE_CONFIG_DIR, with its own login,
# settings and transcripts. Skills and plugins are the exception: `cc` links them from
# ~/.claude, so every profile sees yours. `cc-use` moves a profile's login into the
# default slot, so VS Code and plain `claude` run as it.
#
# The "default" account is the normal install (~/.claude.json at the home root) and takes
# NO CLAUDE_CONFIG_DIR. Pointing CLAUDE_CONFIG_DIR at ~/.claude would be wrong: Claude
# would treat it as a fresh config root and write a new .claude.json inside it.
#
# Every function resolves its own base path rather than trusting $CLAUDE_PROFILES to be
# exported. Tools that restore shell functions from a snapshot (Claude Code's Bash tool,
# for one) bring the functions across without the variable. Each also starts with
# `emulate -L zsh`, so options set in your own .zshrc (nounset, no_bare_glob_qual, ...)
# do not change how it runs.
export CLAUDE_PROFILES="${CLAUDE_PROFILES:-$HOME/.claude-profiles}"

# Run Claude Code as a named account: `cc work`, `cc work -p "hi"`, `cc default`
cc() {
  emulate -L zsh
  local base="$(_cc_base)" profile="$1"
  if [[ -z "$profile" ]]; then
    print "usage: cc <profile|default> [claude args...]"
    print "profiles: default $(_cc_profiles | tr '\n' ' ')"
    return 1
  fi
  shift
  if [[ "$profile" == default ]]; then
    # A shell started inside a profile's session inherits its CLAUDE_CONFIG_DIR.
    local CLAUDE_CONFIG_DIR; unset CLAUDE_CONFIG_DIR
    claude "$@"
  elif _cc_is_profile "$profile"; then
    # While cc-use has this login in the default slot, the profile's own copy may be
    # stale, and using it would strand the live one.
    if [[ "$(_cc_loaded)" == "$profile" ]]; then
      print "$profile is loaded into the default login by cc-use: run plain \`claude\`, or \`cc-use default\` first"
      return 1
    fi
    _cc_share "$base/$profile"
    CLAUDE_CONFIG_DIR="$base/$profile" claude "$@"
  else
    print "no such profile: $profile (create it with: cc-add $profile)"
    return 1
  fi
}

# Create a profile and log it in: `cc-add work`
cc-add() {
  emulate -L zsh
  local base="$(_cc_base)"
  # status, forget and -h/--help are cc-use's own commands, so it could never load such a profile.
  if [[ -z "$1" || "$1" == (default|bin|status|forget) || "$1" == [-._]* || "$1" == */* ]]; then
    print "usage: cc-add <profile>  (not 'default', 'bin', 'status' or 'forget', no /, and not starting with -, . or _)"
    return 1
  fi
  mkdir -p "$base/$1" && cc-login "$1"
}

# Log a profile in (or re-auth it): `cc-login work`
# On macOS the OAuth page is forced into a fresh private browser window (via the `open`
# shim in bin/, put on PATH for this call only), so it never reuses whichever account the
# browser is already signed into. CC_LOGIN_BROWSER picks the browser.
cc-login() {
  emulate -L zsh
  local base="$(_cc_base)"
  PATH="$base/bin:$PATH" cc "$1" auth login
}

# Put a profile's login in the default slot (macOS): `cc-use work`; `cc-use default`
# restores yours; bare `cc-use` shows which account is loaded.
cc-use() {
  emulate -L zsh
  python3 "$(_cc_base)/cc_use.py" "$@"
}

# Usage for every account, soonest reset first: `ccusage-all`
# Full per-account breakdown instead of the table: `ccusage-all --raw`
ccusage-all() {
  emulate -L zsh
  local base="$(_cc_base)"
  if [[ "$1" != --raw ]]; then
    python3 "$base/usage_table.py"
    return
  fi
  local loaded="$(_cc_loaded)" profile CLAUDE_CONFIG_DIR
  unset CLAUDE_CONFIG_DIR  # inherited inside a profile's session; the default takes none
  print -P "%B=== default ===%b"
  claude -p "/usage" < /dev/null 2>&1
  print
  for profile in ${(f)"$(_cc_profiles)"}; do
    [[ "$profile" == "$loaded" ]] && continue  # cc-use put it in the default slot, printed above
    print -P "%B=== $profile ===%b"
    CLAUDE_CONFIG_DIR="$base/$profile" claude -p "/usage" < /dev/null 2>&1
    print
  done
}

_cc_base() {
  emulate -L zsh
  local base="${CLAUDE_PROFILES:-$HOME/.claude-profiles}"
  print -r -- "${base%/}"
}

_cc_profiles() {
  emulate -L zsh
  local base="$(_cc_base)" dir
  for dir in "$base"/*(/N); do
    _cc_is_profile "${dir:t}" && print -r -- "${dir:t}"
  done
}

_cc_is_profile() {
  emulate -L zsh
  # No /: `work/` names the same directory as `work` but would slip past the .loaded check.
  # So would `WORK` or a decomposed `café`, which -d finds on macOS: the name must be the folder's own.
  local -a names=("$(_cc_base)"/*(/N:t))
  [[ -n "$1" && "$1" != bin && "$1" != default && "$1" != [._]* && "$1" != */* ]] && (( ${names[(Ie)$1]} ))
}

_cc_loaded() {
  emulate -L zsh
  local file="$(_cc_base)/.loaded"
  [[ -f "$file" ]] && print -r -- "$(<"$file")"
}

# Link your skills and plugins into a profile. A real directory already there is left alone.
# Which plugins are on lives in settings.json, not the plugins dir, so those two keys are
# mirrored from ~/.claude/settings.json into the profile's own settings on every launch.
_cc_share() {
  emulate -L zsh
  local item
  for item in skills plugins; do
    [[ -e "$1/$item" || -L "$1/$item" ]] || ln -s "$HOME/.claude/$item" "$1/$item"
  done
  python3 - "$1" <<'EOF'
import json, sys
from pathlib import Path
KEYS = ('enabledPlugins', 'extraKnownMarketplaces')
source_path = Path.home() / '.claude' / 'settings.json'
source = json.loads(source_path.read_text()) if source_path.exists() else {}
target_path = Path(sys.argv[1]) / 'settings.json'
target = json.loads(target_path.read_text()) if target_path.exists() else {}
merged = {**target, **{k: source[k] for k in KEYS if k in source}}
if merged != target:
    target_path.write_text(json.dumps(merged, indent=2) + '\n')
EOF
}
