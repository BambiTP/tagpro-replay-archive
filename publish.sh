#!/bin/bash
# Regenerate the coverage site from tagpro.db and push if anything changed.
# Runs hourly from cron; safe to run by hand at any time.
#
# Commits only when the generated output actually differs, so a quiet hour
# costs nothing and the history stays meaningful. GitHub Pages rebuilds on
# push, so a successful commit here is all it takes for the live site to
# refresh.
set -uo pipefail

REPO=/home/metjr/tagpro-archive
export PATH=/usr/local/bin:/usr/bin:/bin

cd "$REPO" || exit 1

# A build that dies part-way must not commit a half-written site. Build into
# place, but bail out entirely if the generator fails.
if ! /usr/bin/python3 build.py > .publish.log 2>&1; then
    echo "$(date -Is) build failed:"
    tail -5 .publish.log
    exit 1
fi

if [ -z "$(git status --porcelain)" ]; then
    echo "$(date -Is) no change"
    exit 0
fi

SUMMARY=$(/usr/bin/python3 -c "
import json
c=json.load(open('data/coverage.json'))
t=lambda k: sum(r[k] for r in c)
print(f\"ids {t('ids'):,}/{t('est'):,} ({100*t('ids')/t('est'):.2f}%), \"
      f\"replays {t('replay'):,} ({100*t('replay')/t('ids'):.2f}%)\")
" 2>/dev/null || echo "coverage refresh")

git add -A
git -c commit.gpgsign=false commit -q -m "Coverage refresh: $SUMMARY" || exit 1

if git push -q origin main 2>/dev/null; then
    echo "$(date -Is) published: $SUMMARY"
else
    echo "$(date -Is) commit made but push failed - will retry next run"
    exit 1
fi
