#!/usr/bin/env bash
# Claude Code PreToolUse hook — blockt Write/Edit/MultiEdit, wenn der Inhalt
# Secrets oder Geraete-Identifier enthaelt, die unter homeassistant-villa-gw/
# nicht ins Repo sollen.
#
# Aktiviert via .claude/settings.json (PreToolUse, matcher "Write|Edit|MultiEdit").
# Eingabe: JSON auf stdin. Ausgabe: JSON auf stdout (permissionDecision).

set -u

VILLA_PATH="/Users/sascha.nordquist/git/private/homeassistant/homeassistant-villa-gw"

input=$(cat)
tool_name=$(printf '%s' "$input" | jq -r '.tool_name // ""')

file_path=""
content=""

case "$tool_name" in
  Write)
    file_path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // ""')
    content=$(printf '%s'  "$input" | jq -r '.tool_input.content   // ""')
    ;;
  Edit)
    file_path=$(printf '%s' "$input" | jq -r '.tool_input.file_path  // ""')
    content=$(printf '%s'  "$input" | jq -r '.tool_input.new_string // ""')
    ;;
  MultiEdit)
    file_path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // ""')
    content=$(printf '%s'  "$input" | jq -r '[.tool_input.edits[]?.new_string] | join("\n")')
    ;;
  *)
    exit 0
    ;;
esac

case "$file_path" in
  "$VILLA_PATH"/*) ;;
  *) exit 0 ;;
esac

# Patterns mit Label (label::regex). egrep-kompatible POSIX-Regex.
PATTERNS=(
  'JWT::eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{20,}'
  'cloud_account.password JSON::"password"[[:space:]]*:[[:space:]]*"[^"]{4,}"'
  'SIP-User Pattern::s00c[0-9a-f]{12}'
  'Cloud-UID Pattern::u00c[0-9a-f]{12}'
  'Bekanntes Leak-PW (cloud)::bigwe3-gyTsus-xavqav'
  'Bekanntes Leak-PW (SIP)::oA7KcU'
  'Geraete-MAC A8B58E85356E::A8B58E85356E'
  'Sample-Geraete-MAC 304A261460E6::304A261460E6'
  'RTMP-Stream-Key::G0xF0mni-[A-Za-z0-9]{10,}'
  'RFC1918 IP (192.168)::(^|[^0-9])192\.168\.[0-9]{1,3}\.[0-9]{1,3}([^0-9]|$)'
  'RFC1918 IP (10.x)::(^|[^0-9])10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}([^0-9]|$)'
  'RFC1918 IP (172.16-31)::(^|[^0-9])172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}([^0-9]|$)'
  'Alibaba MQTT (47.254.135.101)::47\.254\.135\.101'
  'User-Email::sascha@nordquist\.de'
)

findings=""
for entry in "${PATTERNS[@]}"; do
  label="${entry%%::*}"
  regex="${entry#*::}"
  match=$(printf '%s' "$content" | grep -oE "$regex" | head -1 || true)
  if [ -n "$match" ]; then
    findings+=$'\n- '"$label"': `'"$match"'`'
  fi
done

if [ -n "$findings" ]; then
  reason=$'Secret-Scanner hat verdaechtige Inhalte fuer '"$file_path"' geblockt:'"$findings"$'\n\nRedigiere die Werte (REDACTED, AA:BB:CC:DD:EE:FF, RFC5737-Doc-IPs 203.0.113.x / 198.51.100.x / 192.0.2.x) oder erklaere dem User, warum es legitim ist.'
  jq -n --arg r "$reason" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $r
    }
  }'
fi

exit 0
