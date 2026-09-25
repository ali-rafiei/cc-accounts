# cc-accounts for Windows: the Git Bash commands.
#
# The same commands as profiles.ps1, for Git Bash (which is also the shell Claude Code's own
# Bash tool uses on Windows). Each is a thin wrapper around the Python scripts beside this file.
# For interactive `cc <profile>` sessions, Git Bash inside Windows Terminal works best; the
# older mintty window may not give Claude Code a proper console.

_cc_profiles_dir() {
  local dir="${CLAUDE_PROFILES:-$HOME/.claude-profiles}"
  printf '%s' "${dir%/}"
}

# Prefer the py launcher: on a fresh Windows install a bare `python` can be the Store stub.
_cc_python() {
  if command -v py >/dev/null 2>&1; then py -3 "$@"; else python "$@"; fi
}

cc() {
  if [ $# -eq 0 ]; then
    _cc_python "$(_cc_profiles_dir)/cc_run.py" --help
  else
    _cc_python "$(_cc_profiles_dir)/cc_run.py" run "$@"
  fi
}

cc-add() { _cc_python "$(_cc_profiles_dir)/cc_run.py" add "$@"; }

cc-login() { _cc_python "$(_cc_profiles_dir)/cc_run.py" login "$@"; }

cc-use() { _cc_python "$(_cc_profiles_dir)/cc_use.py" "$@"; }

ccusage-all() { _cc_python "$(_cc_profiles_dir)/usage_table.py" "$@"; }
