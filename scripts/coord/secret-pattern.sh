# Shared secret pattern (ERE) for the coordinator's pattern scans. Sourced by
# scripts/coord/sync-github.sh, scripts/coord/push-review.sh and
# .kiro/hooks/scripts/secret-scan.sh, so the three can never drift apart.
#
# Sakana PR #4 P1-2: any variable named <NAME>_API_KEY (or a bare API_KEY) assigned a
# value is a hit, not a host-specific list of key names, so a public copy needs no
# renaming. The name must end in API_KEY right before the '=' (API_KEY_NAME is not a hit). The value must be at
# least 6 characters with no backtick, space, double quote or backslash, as before.
# Written so that this file does not match itself.
P='[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|brd-customer-[a-z0-9_-]+|sk-(ant-|proj-)?[A-Za-z0-9_-]{24,}|([A-Za-z0-9_]*_)?API_KEY=[^` "\\]{6,}|(BRIGHTDATA_[A-Z_]+|HERMES_TELEGRAM_BOT_TOKEN|WECHAT_ROUTINE_WEBHOOK_KEY)=[^` "\\]{6,}|BEGIN (OPENSSH|EC|DSA) PRIVATE KEY'
