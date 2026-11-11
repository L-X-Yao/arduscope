#!/bin/bash
# Git commit-msg hook: enforce ArduScope commit message format.
# Install: cp tools/commit-msg-hook.sh .git/hooks/commit-msg && chmod +x .git/hooks/commit-msg
#
# Format: <type>(<scope>): <subject>
#   or:   <type>: <subject>
#
# type must be one of: feat fix refactor docs test chore i18n
# subject: lowercase start, no period, <= 72 chars first line
# No Co-Authored-By or Signed-off-by trailers allowed.

MSG_FILE="$1"
FIRST_LINE=$(head -1 "$MSG_FILE")

# Allowed types
TYPES="feat|fix|refactor|docs|test|chore|i18n"

# Check format: type(scope): subject  OR  type: subject
if ! echo "$FIRST_LINE" | grep -qE "^($TYPES)(\([a-z0-9_]+\))?: .+"; then
    echo "ERROR: commit message must match: <type>(<scope>): <subject>"
    echo "  Allowed types: feat, fix, refactor, docs, test, chore, i18n"
    echo "  Got: $FIRST_LINE"
    exit 1
fi

# Check subject starts lowercase (after the colon+space)
SUBJECT=$(echo "$FIRST_LINE" | sed 's/^[^:]*: //')
if echo "$SUBJECT" | grep -qE "^[A-Z]"; then
    echo "ERROR: subject must start lowercase"
    echo "  Got: $SUBJECT"
    exit 1
fi

# Check no trailing period
if echo "$FIRST_LINE" | grep -qE "\.$"; then
    echo "ERROR: subject must not end with a period"
    exit 1
fi

# Check first line length
if [ ${#FIRST_LINE} -gt 72 ]; then
    echo "ERROR: first line must be <= 72 characters (got ${#FIRST_LINE})"
    exit 1
fi

# Block AI attribution trailers
if grep -qiE "^(Co-Authored-By|Signed-off-by):" "$MSG_FILE"; then
    echo "ERROR: no Co-Authored-By or Signed-off-by trailers allowed"
    exit 1
fi

exit 0
