#!/usr/bin/env bash
# Placeholder: kept for backwards compatibility. The original full-sandbox plan
# included a kind cluster + CronJob apply for end-to-end ML pipeline validation,
# but the current goal is just to validate infrastructure/pulumi/ against
# LocalStack. Real kind-based CronJob validation is tracked separately.
#
# See /home/poridhian/.puku-cli/plans/graceful-purring-bengio.md for the
# full-sandbox design (not yet executed).

echo "scripts/start-kind.sh: deprecated for the current 'Pulumi vs LocalStack' goal."
echo "See CLAUDE.md § Local Sandbox for the original end-to-end design."
exit 0