#!/usr/bin/env bash
# Lock/unlock the global skills tree with the FS-immutable bit (chattr +i).
#
# WHY: the broker runs as root with env_type=local (no container/chroot/seccomp),
# so any tenant can write the global skills body via execute_code open() /
# terminal echo using an absolute path — bypassing every application-layer
# guard (is_write_denied / _managed_skill_write_guard only gate structured
# tool APIs, not subprocess syscalls). chattr +i is enforced by the kernel:
# even root cannot write/modify/delete an immutable inode until it is cleared
# with chattr -i. This is the hard boundary the app-layer guards can't be.
#
# SCOPE: only skill *body* dirs (those containing a SKILL.md) are locked —
# both top-level skills and nested category skills (e.g. productivity/<skill>).
# Runtime-writable metadata/cache under the skills root is deliberately left
# writable so skill-hub indexing, the curator, and usage stats keep working:
#   .hub/ .archive/ .curator_backups/ .claude/ __pycache__  (dirs)
#   .curator_state .bundled_manifest .usage.json .usage.json.lock (files)
#   credential-bootstrap.py
#
# OPERATOR WORKFLOW:
#   scripts/skill_tree_immutable.sh unlock   # before editing global skills
#   <edit /root/.hermes/skills/...>
#   scripts/skill_tree_immutable.sh lock     # re-lock when done
#   scripts/skill_tree_immutable.sh status   # inspect
#
# Requires chattr (e2fsprogs) and CAP_LINUX_IMMUTABLE (runs as root).

set -euo pipefail

SKILLS_ROOT="${HERMES_SKILLS_ROOT:-/root/.hermes/skills}"

# Hidden/runtime entries that must stay writable (never locked).
SKIP_NAMES=(.hub .archive .curator_backups .claude __pycache__ .skipped
            .curator_state .bundled_manifest .usage.json .usage.json.lock
            credential-bootstrap.py)

usage() {
  cat <<EOF
Usage: $0 {lock|unlock|status|list} [skills_root]

  lock     Recursively set +i on every skill body dir under skills_root.
  unlock   Clear -i (operator editing the global tree).
  status   Show whether each skill body dir is currently immutable.
  list     Print the skill body dirs that would be (un)locked (dry-run).

Default skills_root: $SKILLS_ROOT
EOF
  exit "${1:-0}"
}

is_skip() {
  local name="$1"
  for s in "${SKIP_NAMES[@]}"; do
    [ "$name" = "$s" ] && return 0
  done
  # Any other dotfile/dir not explicitly allowed is skipped too.
  case "$name" in .*) return 0;; esac
  return 1
}

# Collect every dir that contains a SKILL.md (the skill bodies), excluding
# runtime entries. This is the precise set we lock.
collect_skill_dirs() {
  [ -d "$SKILLS_ROOT" ] || return 0
  # find skips hidden by default? No — find descends into hidden dirs. We
  # prune the SKIP_NAMES explicitly.
  find "$SKILLS_ROOT" -mindepth 1 \( \
        -name .hub -o -name .archive -o -name .curator_backups -o \
        -name .claude -o -name __pycache__ -o -name .skipped \) -prune -o \
    -name SKILL.md -print 2>/dev/null \
    | while read -r skillmd; do
        d="$(dirname "$skillmd")"
        # Skip if any path component is a hidden/runtime entry.
        skip=0
        rel="${d#$SKILLS_ROOT/}"
        IFS='/' read -ra parts <<<"$rel"
        for p in "${parts[@]}"; do
          if is_skip "$p"; then skip=1; break; fi
        done
        [ "$skip" -eq 0 ] && printf '%s\n' "$d"
      done
}

cmd_list() {
  echo "Skill body dirs that would be locked under $SKILLS_ROOT:"
  collect_skill_dirs | sort -u
}

apply() {  # $1 = +i or -i
  local bit="$1" count=0 failed=0
  [ -d "$SKILLS_ROOT" ] || { echo "skills_root not found: $SKILLS_ROOT" >&2; exit 1; }
  command -v chattr >/dev/null 2>&1 || { echo "chattr not found (install e2fsprogs)" >&2; exit 1; }

  # Collect then act, so the subshell pipe doesn't hide failures.
  mapfile -t dirs < <(collect_skill_dirs | sort -u)
  [ "${#dirs[@]}" -eq 0 ] && { echo "No skill body dirs found."; return 0; }

  for d in "${dirs[@]}"; do
    if chattr -R "$bit" "$d" >/dev/null 2>&1; then
      count=$((count+1))
    else
      failed=$((failed+1))
      echo "  WARN: failed on $d" >&2
    fi
  done
  echo "${bit}+immutable applied to $count skill body dir(s)"$([ "$failed" -gt 0 ] && echo ", $failed failed")"."
}

cmd_status() {
  [ -d "$SKILLS_ROOT" ] || { echo "skills_root not found: $SKILLS_ROOT" >&2; exit 1; }
  command -v lsattr >/dev/null 2>&1 || { echo "lsattr not found" >&2; exit 1; }
  mapfile -t dirs < <(collect_skill_dirs | sort -u)
  locked=0; writable=0
  for d in "${dirs[@]}"; do
    # lsattr on the dir; immutable bit = 'i' in the attr field.
    attr="$(lsattr -d "$d" 2>/dev/null | awk '{print $1}')"
    if printf '%s' "$attr" | grep -q 'i'; then
      echo "  LOCKED   $d"; locked=$((locked+1))
    else
      echo "  writable $d"; writable=$((writable+1))
    fi
  done
  echo "summary: $locked locked, $writable writable (of ${#dirs[@]} skill body dirs)"
}

case "${1:-}" in
  lock)   apply "+i" ;;
  unlock) apply "-i" ;;
  status) cmd_status ;;
  list)   cmd_list ;;
  ""|-h|--help) usage 0 ;;
  *) echo "unknown command: $1" >&2; usage 1 ;;
esac
