# Shared secret pattern (ERE) for the coordinator's pattern scans. Sourced by
# scripts/coord/sync-github.sh, scripts/coord/push-review.sh and
# .kiro/hooks/scripts/secret-scan.sh, so the three can never drift apart.
#
# Sakana PR #4 P1-2: any variable named <NAME>_API_KEY (or a bare API_KEY) assigned a
# value is a hit, not a host-specific list of key names, so a public copy needs no
# renaming. The name must end in API_KEY right before ':'/'=' (API_KEY_NAME is not a hit).
# Optional whitespace/quotes (single, triple, or multi-level JSON-escaped) and Python
# raw/bytes/unicode/f-string prefixes around quoted values are allowed; unquoted values
# require immediate '=' (no spaces or '(' within 6 chars). $(...) / $VAR expansions,
# empty strings, and values under 6 chars stay excluded; file does not match itself.
P='[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|sk-(ant-|proj-)?[A-Za-z0-9_-]{24,}|mesh_(at|rt)_[A-Za-z0-9_-]{20,}|fk-[A-Za-z0-9_-]{16,}|([A-Za-z0-9_]*_)?(API_KEY|BOT_TOKEN|WEBHOOK_KEY|PASSWORD|SECRET)(((\\*['\''"]){3}|\\*['\''"])?[[:space:]]*[:=][[:space:]]*([rRbBuUfF]|[rR][bBfF]|[bBfF][rR])?((\\*['\''"]){3}|\\*['\''"])[^$` '\''"\\][^` '\''"\\]{5,}|=[^$` '\''"\\][^` '\''"\\(]{5,})|BEGIN ([A-Z0-9 ]+ )?PRIVATE KEY'
