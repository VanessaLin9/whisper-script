#!/usr/bin/env bash
# Thin compatibility wrapper; transcription runs in src.transcription.cli via Python.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
export MEETING_WORKFLOW_INVOKED_AS="$0"
export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:$PYTHONPATH}"
exec python3 "${REPO_ROOT}/src/workflow_cli.py" multi-lang "$@"
