#!/bin/sh
# Refuse to commit a real Schoology id.
#
# This repo is PUBLIC and the account it reads is a real student's, so ids from
# it must never land in a fixture, a comment or a docstring. They are easy to
# paste in by accident: a comment explaining a trap reads better with the id
# that produced it, and a fixture is quickest to write by copying a response.
#
# Everything Schoology numbers -- sections, assignments, users, attachments,
# grading periods -- is a dense 7+ digit integer. Synthetic ids in this repo are
# written in an obviously fake shape instead: a leading digit followed by zeros
# and a small tail (1000000001, 2000000011, 6000000003). Anything else that long
# is presumed real.
#
#   scripts/check_no_real_ids.sh            # check what is staged
#   scripts/check_no_real_ids.sh --worktree # check the working tree
set -eu

if [ "${1:-}" = "--worktree" ]; then
    subject=$(git ls-files | grep -v '^dumps/' | xargs cat 2>/dev/null || true)
else
    # ADDED lines only. A diff carries the old text too, so scanning the whole
    # thing reports every id a commit is *removing* -- which is what a cleanup
    # commit consists of, and the check would block exactly the change that
    # fixes it.
    subject=$(git diff --cached | grep '^+' | grep -v '^+++')
fi

# A synthetic id is <digit><zeros><short tail>. A real one is not.
suspects=$(printf '%s\n' "$subject" \
    | grep -oE '[0-9]{7,}' \
    | sort -u \
    | grep -vE '^[1-9]0{4,}[0-9]{0,4}$' \
    | grep -vE '^17[0-9]{8}$' \
    || true)

if [ -n "$suspects" ]; then
    echo "Possible real Schoology ids -- do not commit these:"
    printf '  %s\n' $suspects
    echo "If one is a deliberate synthetic value, give it the obviously-fake"
    echo "shape used elsewhere (1000000001, 2000000011) so it reads as invented."
    exit 1
fi
echo "clean: no real-looking ids"
