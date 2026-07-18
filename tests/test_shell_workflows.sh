#!/usr/bin/env bash
# Offline regression tests for documented shell workflows.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAKE_BIN="${ROOT}/tests/fake_bin"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/whisper-script-tests.XXXXXX")"
PASS=0
FAIL=0

cleanup() {
    rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

assert_eq() {
    local label="$1"
    local expected="$2"
    local actual="$3"
    if [ "$expected" = "$actual" ]; then
        echo "  PASS: ${label}"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: ${label}"
        echo "        expected: ${expected}"
        echo "        actual:   ${actual}"
        FAIL=$((FAIL + 1))
    fi
}

assert_file() {
    local label="$1"
    local path="$2"
    if [ -f "$path" ]; then
        echo "  PASS: ${label}"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: ${label} (missing ${path})"
        FAIL=$((FAIL + 1))
    fi
}

assert_contains() {
    local label="$1"
    local needle="$2"
    local haystack="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        echo "  PASS: ${label}"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: ${label}"
        echo "        missing: ${needle}"
        echo "        in: ${haystack}"
        FAIL=$((FAIL + 1))
    fi
}

make_whisper_root() {
    local dest="$1"
    mkdir -p "${dest}/build/bin" "${dest}/models"
    ln -sf "${FAKE_BIN}/whisper-cli" "${dest}/build/bin/whisper-cli"
    echo "model" >"${dest}/models/ggml-small.bin"
}

write_env() {
    local dest="$1"
    local whisper_root="$2"
    local records_dir="$3"
    local transcripts_dir="$4"
    cat >"$dest" <<EOF
WHISPER_ROOT=${whisper_root}
MEETING_RECORDS_DIR=${records_dir}
TRANSCRIPTS_DIR=${transcripts_dir}
MIC_DEVICE=:0
DEFAULT_LANGUAGE="zh"
PREFERRED_MODEL="small"
THREADS=2
EOF
}

clone_project() {
    local dest="$1"
    mkdir -p "${dest}/scripts/lib"
    cp "${ROOT}/env_loader.py" "${dest}/"
    cp "${ROOT}/scripts/lib/common.sh" "${dest}/scripts/lib/"
    cp "${ROOT}/scripts/multi-lang.sh" "${dest}/scripts/"
    cp "${ROOT}/scripts/record-meeting.sh" "${dest}/scripts/"
    cp "${ROOT}/scripts/transcribe-english.sh" "${dest}/scripts/"
    chmod +x "${dest}/scripts/"*.sh "${dest}/scripts/lib/common.sh"
}

export PATH="${FAKE_BIN}:${PATH}"
hash -r
cd "$ROOT"

echo "== env_loader.py =="
PYTHONPATH="$ROOT" python3 tests/test_env_loader.py -v

echo
echo "== shell: quoted .env values reach Whisper as bare tokens =="
CASE="${TMP_ROOT}/quoted"
mkdir -p "$CASE"
make_whisper_root "${CASE}/whisper.cpp"
write_env "${CASE}/.env" "${CASE}/whisper.cpp" "${CASE}/records" "${CASE}/transcripts"
# shellcheck source=../scripts/lib/common.sh
source "${ROOT}/scripts/lib/common.sh"
load_project_env "${CASE}/.env"
apply_workflow_defaults
assert_eq "DEFAULT_LANGUAGE strips quotes" "zh" "$DEFAULT_LANGUAGE"
assert_eq "PREFERRED_MODEL strips comments/quotes" "small" "$PREFERRED_MODEL"

echo
echo "== shell: missing .env / whisper-cli / model =="
MISSING="${TMP_ROOT}/missing"
mkdir -p "$MISSING"
set +e
out="$(
  source "${ROOT}/scripts/lib/common.sh"
  load_project_env "${MISSING}/.env" 2>&1
)"
status=$?
set -e
assert_eq "missing .env exits non-zero" "1" "$status"
assert_contains "missing .env message" "Environment file not found" "$out"

make_whisper_root "${MISSING}/whisper.cpp"
rm -f "${MISSING}/whisper.cpp/build/bin/whisper-cli"
write_env "${MISSING}/.env" "${MISSING}/whisper.cpp" "${MISSING}/records" "${MISSING}/transcripts"
set +e
out="$(
  source "${ROOT}/scripts/lib/common.sh"
  load_project_env "${MISSING}/.env"
  require_whisper_cli "$WHISPER_ROOT" 2>&1
)"
status=$?
set -e
assert_eq "missing whisper-cli exits non-zero" "1" "$status"
assert_contains "missing whisper-cli message" "whisper-cli not found" "$out"

ln -sf "${FAKE_BIN}/whisper-cli" "${MISSING}/whisper.cpp/build/bin/whisper-cli"
rm -f "${MISSING}/whisper.cpp/models/ggml-small.bin"
echo "english-only" >"${MISSING}/whisper.cpp/models/ggml-small.en.bin"
set +e
out="$(
  source "${ROOT}/scripts/lib/common.sh"
  load_project_env "${MISSING}/.env"
  require_configured_model "$WHISPER_ROOT" "$PREFERRED_MODEL" 2>&1
)"
status=$?
set -e
assert_eq "missing multilingual model exits non-zero" "1" "$status"
assert_contains "does not accept small.en" "English-only" "$out"
assert_contains "download hint includes preferred model" "download-ggml-model.sh small" "$out"

echo
echo "== multi-lang.sh: all segments succeed =="
OK_CASE="${TMP_ROOT}/batch_ok"
mkdir -p "${OK_CASE}/segments"
make_whisper_root "${OK_CASE}/whisper.cpp"
clone_project "${OK_CASE}/project"
write_env "${OK_CASE}/project/.env" "${OK_CASE}/whisper.cpp" "${OK_CASE}/records" "${OK_CASE}/transcripts"
printf 'a' >"${OK_CASE}/segments/segment_001.wav"
printf 'b' >"${OK_CASE}/segments/segment_002.wav"

set +e
out="$(PATH="${FAKE_BIN}:${PATH}" "${OK_CASE}/project/scripts/multi-lang.sh" "${OK_CASE}/segments" 2>&1)"
status=$?
set -e
assert_eq "batch success exit status" "0" "$status"
assert_file "segment_001 transcript" "${OK_CASE}/segments/transcripts/segment_001.txt"
assert_file "segment_002 transcript" "${OK_CASE}/segments/transcripts/segment_002.txt"
if [ ! -f "${OK_CASE}/segments/failed_segments.log" ]; then
    echo "  PASS: failure log removed on full success"
    PASS=$((PASS + 1))
else
    echo "  FAIL: failure log should be removed on full success"
    FAIL=$((FAIL + 1))
fi

echo
echo "== multi-lang.sh: partial failure keeps audio and fails the run =="
PARTIAL="${TMP_ROOT}/batch_partial"
mkdir -p "${PARTIAL}/segments"
make_whisper_root "${PARTIAL}/whisper.cpp"
clone_project "${PARTIAL}/project"
write_env "${PARTIAL}/project/.env" "${PARTIAL}/whisper.cpp" "${PARTIAL}/records" "${PARTIAL}/transcripts"
printf 'a' >"${PARTIAL}/segments/segment_001.wav"
printf 'b' >"${PARTIAL}/segments/segment_002.wav"
printf 'c' >"${PARTIAL}/segments/segment_003.wav"
echo "stale_segment" >"${PARTIAL}/segments/failed_segments.log"

set +e
out="$(
  PATH="${FAKE_BIN}:${PATH}" \
  FAIL_SEGMENTS="segment_002.wav" \
  "${PARTIAL}/project/scripts/multi-lang.sh" "${PARTIAL}/segments" 2>&1
)"
status=$?
set -e
assert_eq "partial failure exit status" "1" "$status"
assert_file "kept failed segment audio" "${PARTIAL}/segments/segment_002.wav"
assert_file "success transcript remains" "${PARTIAL}/segments/transcripts/segment_001.txt"
assert_file "later segment still processed" "${PARTIAL}/segments/transcripts/segment_003.txt"
assert_file "fresh failure log written" "${PARTIAL}/segments/failed_segments.log"
fail_log="$(cat "${PARTIAL}/segments/failed_segments.log")"
assert_contains "failure log lists failed segment" "segment_002" "$fail_log"
if [[ "$fail_log" != *stale_segment* ]]; then
    echo "  PASS: stale failure log replaced"
    PASS=$((PASS + 1))
else
    echo "  FAIL: stale failure log was not replaced"
    FAIL=$((FAIL + 1))
fi
assert_contains "summary reports failed count" "Failed: 1" "$out"

echo
echo "== record-meeting.sh: ffmpeg failure does not start whisper =="
REC="${TMP_ROOT}/record_fail"
make_whisper_root "${REC}/whisper.cpp"
clone_project "${REC}/project"
write_env "${REC}/project/.env" "${REC}/whisper.cpp" "${REC}/records" "${REC}/transcripts"

set +e
out="$(
  PATH="${FAKE_BIN}:${PATH}" \
  FAIL_FFMPEG=1 \
  "${REC}/project/scripts/record-meeting.sh" 2>&1
)"
status=$?
set -e
assert_eq "recording failure exit status" "1" "$status"
assert_contains "recording failure message" "FFmpeg recording failed" "$out"
assert_contains "does not start transcription" "Transcription was not started" "$out"
transcript_count="$(find "${REC}/records" -name 'meeting_*.txt' 2>/dev/null | wc -l | tr -d ' ')"
assert_eq "no transcript created after ffmpeg failure" "0" "$transcript_count"

echo
echo "== record-meeting.sh: intentional interrupt still transcribes valid audio =="
REC_OK="${TMP_ROOT}/record_ok"
make_whisper_root "${REC_OK}/whisper.cpp"
clone_project "${REC_OK}/project"
write_env "${REC_OK}/project/.env" "${REC_OK}/whisper.cpp" "${REC_OK}/records" "${REC_OK}/transcripts"

# Background shell jobs ignore SIGINT; drive the script from Python so the
# interrupt trap runs the same way a foreground Ctrl+C would.
set +e
out="$(
  PATH="${FAKE_BIN}:${PATH}" \
  FFMPEG_SLEEP_FOREVER=1 \
  REC_OK_DIR="${REC_OK}" \
  python3 - <<'PY'
import os, signal, subprocess, sys, time
from pathlib import Path

rec = Path(os.environ["REC_OK_DIR"])
script = rec / "project" / "scripts" / "record-meeting.sh"
records = rec / "records"
env = os.environ.copy()
proc = subprocess.Popen(
    [str(script)],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    env=env,
)

wav_ready = False
for _ in range(50):
    if proc.poll() is not None:
        break
    if any(records.glob("meeting_*.wav")):
        wav_ready = True
        break
    time.sleep(0.1)

if not wav_ready and proc.poll() is None:
    proc.send_signal(signal.SIGINT)
else:
    proc.send_signal(signal.SIGINT)

try:
    out, _ = proc.communicate(timeout=10)
except subprocess.TimeoutExpired:
    proc.kill()
    out, _ = proc.communicate()
    print(out or "")
    print("TIMEOUT", file=sys.stderr)
    sys.exit(1)

print(out or "")
print(f"EXIT:{proc.returncode}")
sys.exit(0 if proc.returncode == 0 else 1)
PY
)"
status=$?
set -e
assert_eq "interrupt path exit status" "0" "$status"
assert_contains "interrupt path reports completion" "Transcription complete" "$out"
transcript_count="$(find "${REC_OK}/records" -name 'meeting_*.txt' 2>/dev/null | wc -l | tr -d ' ')"
assert_eq "interrupt path produces transcript" "1" "$transcript_count"

echo
echo "== syntax / compile checks =="
set +e
bash -n "${ROOT}/scripts/record-meeting.sh"
assert_eq "bash -n record-meeting.sh" "0" "$?"
bash -n "${ROOT}/scripts/transcribe-english.sh"
assert_eq "bash -n transcribe-english.sh" "0" "$?"
bash -n "${ROOT}/scripts/multi-lang.sh"
assert_eq "bash -n multi-lang.sh" "0" "$?"
bash -n "${ROOT}/scripts/lib/common.sh"
assert_eq "bash -n common.sh" "0" "$?"
python3 -m py_compile "${ROOT}/env_loader.py" "${ROOT}/setup.py"
assert_eq "python compile env_loader/setup" "0" "$?"
set -e

echo
echo "========================================"
echo "Passed: ${PASS}"
echo "Failed: ${FAIL}"
echo "========================================"
if [ "$FAIL" -ne 0 ]; then
    exit 1
fi
