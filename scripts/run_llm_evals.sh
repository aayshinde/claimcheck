#!/usr/bin/env bash
# Runs the LLM and hybrid evaluations on the same synthetic files as the offline demo (needs Ollama running).
# Each run uses its own database so results don't mix. Writes results/eval_llm.json and results/eval_hybrid.json.
set -euo pipefail
cd "$(dirname "$0")/.."
export LLM_BASE_URL=${LLM_BASE_URL:-http://localhost:11434/v1}
for m in hybrid llm; do
  DATABASE_URL="sqlite:///data/claimcheck_${m}.db" .venv/bin/python -m claimcheck.pipeline demo --keep-data --method "$m" --agent llm
done
