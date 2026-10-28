#!/usr/bin/env bash
# Copy the QA commands, agent, skill and script to ~/.claude so they work in every project.
# Rerun after editing anything in this folder.
set -euo pipefail
cd "$(dirname "$0")"
D="$HOME/.claude"
mkdir -p "$D/commands" "$D/agents" "$D/skills/qa-requirements" "$D/scripts"
cp commands/qa-start.md commands/qa-end.md commands/qa-run.md "$D/commands/"
cp agents/qa-goal-extractor.md "$D/agents/"
cp skills/qa-requirements/SKILL.md "$D/skills/qa-requirements/"
cp scripts/qa.sh "$D/scripts/" && chmod +x "$D/scripts/qa.sh"
echo "installed to $D: /qa-start /qa-end /qa-run, agent qa-goal-extractor, skill qa-requirements"
