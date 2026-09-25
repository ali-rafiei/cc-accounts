#!/usr/bin/env bash
# Install the shell side of claude-multi-account.
#
#   ./install.sh               copy the scripts into $CLAUDE_PROFILES and source them from ~/.zshrc
#   ./install.sh --skills      also link the skills into ~/.claude/skills (skip if you use the plugin)
#   ./install.sh --uninstall   undo both and delete cc-use's stashed login; profiles are left alone
#
# A file already installed that differs from this copy is backed up beside itself first.
set -euo pipefail

# CDPATH unset: with it exported, `cd macos` prints the directory and repo gets it twice.
repo="$(CDPATH='' cd "$(dirname "$0")" && pwd)"
dest="${CLAUDE_PROFILES:-$HOME/.claude-profiles}"
# The source line written to ~/.zshrc runs from wherever a terminal opens, so it needs an
# absolute path: expand a quoted ~/ and anchor a relative path at the current directory.
case "$dest" in
  "~/"*) dest="$HOME/${dest#"~/"}" ;;
  /*) ;;
  *) dest="$PWD/$dest" ;;
esac
dest="${dest%/}"
zshrc="${ZDOTDIR:-$HOME}/.zshrc"
skills_dir="$HOME/.claude/skills"
scripts=(profiles.zsh cc_use.py usage_table.py bin/open)
skills=(cc-usage cc-use cc-run)
marker="# claude-multi-account"
if [[ "$dest" == "$HOME/.claude-profiles" ]]; then
  source_line="source \"\$HOME/.claude-profiles/profiles.zsh\"  $marker"
else
  # Single-quoted for zsh, so a ", $ or ` in the path stays text rather than running as code.
  quoted="'$(printf '%s' "$dest" | sed "s/'/'\\\\''/g")'"
  source_line="export CLAUDE_PROFILES=$quoted; source \"\$CLAUDE_PROFILES/profiles.zsh\"  $marker"
fi

main() {
  case "${1:-}" in
    "") install ;;
    --skills) install; link_skills ;;
    --uninstall) uninstall ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
}

install() {
  mkdir -p "$dest/bin"
  local file
  for file in "${scripts[@]}"; do
    copy_with_backup "$repo/scripts/$file" "$dest/$file"
  done
  chmod +x "$dest/cc_use.py" "$dest/usage_table.py" "$dest/bin/open"
  if ! already_sourced; then
    printf '\n%s\n' "$source_line" >> "$zshrc"
    echo "added to $zshrc: $source_line"
  fi
  [[ "${SHELL:-}" == */zsh ]] || echo "note: your login shell is ${SHELL:-unknown}; the cc commands need zsh."
  cat <<EOF

Installed into $dest. Open a new terminal (or: source "$dest/profiles.zsh"), then:
  cc-add <profile>     create an account profile and log it in
  cc <profile>         run Claude Code as it
  ccusage-all          usage across every account
  cc-use <profile>     make VS Code and plain \`claude\` run as it (macOS); cc-use default undoes
EOF
}

uninstall() {
  if [[ -f "$dest/.loaded" ]]; then
    echo "cc-use has '$(<"$dest/.loaded")' in your default login. Run \`cc-use default\` first." >&2
    exit 1
  fi
  if [[ -f "$dest/.home-account.json" && "$(uname)" == Darwin ]]; then
    # forget refuses whenever it can't prove the stash is a spare copy. Keeping it is the
    # safe side, so uninstall carries on and says how to drop it later.
    if ! python3 "$dest/cc_use.py" forget; then
      echo "kept cc-use's stashed copy of your login (reason above). It is harmless; to delete it later:"
      echo "  security delete-generic-password -s 'Claude Code-credentials-cc-use-home'; rm '$dest/.home-account.json'"
    fi
  fi
  local file skill
  for file in "${scripts[@]}"; do
    rm -f "$dest/$file"
  done
  if [[ -f "$zshrc" ]] && grep -qF "$marker" "$zshrc"; then
    # Rewrite in place rather than mv, so a symlinked ~/.zshrc stays a symlink.
    local kept
    kept="$(grep -vF "$marker" "$zshrc" || true)"
    printf '%s\n' "$kept" > "$zshrc"
    echo "removed the source line from $zshrc"
  fi
  local leftover
  leftover="$(hand_written_source_lines || true)"
  if [[ -n "$leftover" ]]; then
    echo "note: $zshrc still sources the profiles.zsh just removed; delete this by hand:"
    printf '  %s\n' "$leftover"
  fi
  for skill in "${skills[@]}"; do
    if [[ -L "$skills_dir/$skill" && "$(readlink "$skills_dir/$skill")" == "$repo/skills/$skill" ]]; then
      rm "$skills_dir/$skill"
    fi
  done
  echo "Uninstalled. Profiles in $dest, and the skills/plugins links inside them, were left in place."
}

link_skills() {
  mkdir -p "$skills_dir"
  local skill
  for skill in "${skills[@]}"; do
    if [[ -e "$skills_dir/$skill" || -L "$skills_dir/$skill" ]]; then
      echo "skipped skill $skill: $skills_dir/$skill already exists"
    else
      ln -s "$repo/skills/$skill" "$skills_dir/$skill"
      echo "linked skill $skill"
    fi
  done
}

# True when ~/.zshrc already sources profiles.zsh: our marked line, or one written by hand.
already_sourced() {
  [[ -f "$zshrc" ]] || return 1
  uncommented_lines | grep -qF "$marker" && return 0
  hand_written_source_lines > /dev/null
}

# Print the lines of ~/.zshrc that source profiles.zsh by its path; false when there are none.
hand_written_source_lines() {
  [[ -f "$zshrc" ]] || return 1
  local found=1
  uncommented_lines | grep -F "$dest/profiles.zsh" && found=0
  if [[ "$dest" == "$HOME/.claude-profiles" ]]; then
    uncommented_lines | grep -E '(\$HOME|\$\{HOME\}|~)/\.claude-profiles/profiles\.zsh' && found=0
  fi
  return $found
}

# ~/.zshrc without its comment lines: a commented-out source line sources nothing.
uncommented_lines() {
  grep -v '^[[:space:]]*#' "$zshrc" || true
}

copy_with_backup() {
  local src="$1" dst="$2"
  if [[ -f "$dst" ]] && ! cmp -s "$src" "$dst"; then
    local backup="$dst.bak-$(date +%Y%m%d%H%M%S)"
    cp -p "$dst" "$backup"
    echo "backed up $dst -> $backup"
  fi
  cp "$src" "$dst"
}

main "$@"
