#!/usr/bin/env bash
# Offline characterization tests for thin transcribe-english.sh wrapper.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAKE_BIN="${ROOT}/tests/fake_bin"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/whisper-transcribe-wrapper.XXXXXX")"
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
    cat >"$dest" <<EOF
WHISPER_ROOT=${whisper_root}
MEETING_RECORDS_DIR=${records_dir}
TRANSCRIPTS_DIR=${records_dir}/Transcripts
MIC_DEVICE=:0
DEFAULT_LANGUAGE="zh"
PREFERRED_MODEL="small"
THREADS=2
EOF
}

clone_project() {
    local dest="$1"
    mkdir -p "${dest}/scripts/lib" "${dest}/src/transcription"
    cp "${ROOT}/env_loader.py" "${dest}/"
    cp "${ROOT}/scripts/lib/common.sh" "${dest}/scripts/lib/"
    cp "${ROOT}/scripts/organize_recording.py" "${dest}/scripts/"
    cp "${ROOT}/scripts/transcribe-english.sh" "${dest}/scripts/"
    cp "${ROOT}/src/transcription/"*.py "${dest}/src/transcription/"
    chmod +x "${dest}/scripts/"*.sh "${dest}/scripts/lib/common.sh"
}

# Use a standard timestamp prefix so organize_recording.py does not prompt.
SOURCE_NAME="2026-07-17_1500_demo.wav"

export PATH="${FAKE_BIN}:${PATH}"
hash -r
cd "$ROOT"

echo "== transcribe-english.sh: success path via core =="
OK="${TMP_ROOT}/ok"
mkdir -p "${OK}/inbox"
make_whisper_root "${OK}/whisper.cpp"
clone_project "${OK}/project"
write_env "${OK}/project/.env" "${OK}/whisper.cpp" "${OK}/records"
printf 'audio' >"${OK}/inbox/${SOURCE_NAME}"

run_transcribe() {
  # Captures stdout+stderr; UI helpers are stubbed via FAKE_BIN.
  PATH="${FAKE_BIN}:${PATH}" "$@" 2>&1
}

set +e
out="$(
  run_transcribe "${OK}/project/scripts/transcribe-english.sh" <<EOF
${OK}/inbox/${SOURCE_NAME}
EOF
)"
status=$?
set -e

assert_eq "success exit status" "0" "$status"
assert_contains "success message" "Transcription completed successfully" "$out"
meeting_dir="${OK}/records/2026-07-17_1500"
assert_file "keeps organized source copy" "${meeting_dir}/${SOURCE_NAME}"
assert_file "normalized wav" "${meeting_dir}/2026-07-17_1500_demo_norm16k.wav"
assert_file "txt artifact" "${meeting_dir}/2026-07-17_1500_demo_transcription.txt"
assert_file "srt artifact" "${meeting_dir}/2026-07-17_1500_demo_transcription.srt"
assert_file "vtt artifact" "${meeting_dir}/2026-07-17_1500_demo_transcription.vtt"
assert_file "json artifact" "${meeting_dir}/2026-07-17_1500_demo_transcription.json"
assert_file "original inbox file preserved" "${OK}/inbox/${SOURCE_NAME}"
assert_contains "delegates to python core" "src.transcription.cli" "$(grep -n 'src.transcription.cli' "${OK}/project/scripts/transcribe-english.sh" || true)"
if ! grep -q 'ffmpeg -y -i' "${OK}/project/scripts/transcribe-english.sh"; then
    echo "  PASS: shell no longer runs ffmpeg normalize directly"
    PASS=$((PASS + 1))
else
    echo "  FAIL: shell still contains direct ffmpeg normalize"
    FAIL=$((FAIL + 1))
fi
# Shell may pass --whisper-cli / $WHISPER_CLI into the core, but must not exec it.
if ! grep -E '^[[:space:]]*"?\$\{?WHISPER_CLI\}?"?' "${OK}/project/scripts/transcribe-english.sh" >/dev/null; then
    echo "  PASS: shell does not exec whisper-cli directly"
    PASS=$((PASS + 1))
else
    echo "  FAIL: shell still appears to invoke whisper-cli directly"
    FAIL=$((FAIL + 1))
fi

echo
echo "== transcribe-english.sh: output conflict =="
CONFLICT="${TMP_ROOT}/conflict"
# Feed an already-organized path so unique_destination keeps the same stem,
# then seed a stale transcript that the core must refuse to overwrite.
mkdir -p "${CONFLICT}/records/2026-07-17_1500"
make_whisper_root "${CONFLICT}/whisper.cpp"
clone_project "${CONFLICT}/project"
write_env "${CONFLICT}/project/.env" "${CONFLICT}/whisper.cpp" "${CONFLICT}/records"
printf 'audio' >"${CONFLICT}/records/2026-07-17_1500/${SOURCE_NAME}"
echo "old" >"${CONFLICT}/records/2026-07-17_1500/2026-07-17_1500_demo_transcription.txt"

set +e
out="$(
  run_transcribe "${CONFLICT}/project/scripts/transcribe-english.sh" <<EOF
${CONFLICT}/records/2026-07-17_1500/${SOURCE_NAME}
EOF
)"
status=$?
set -e
assert_eq "conflict exit status" "1" "$status"
assert_contains "conflict reports stage" "stage=check_outputs" "$out"
assert_eq "stale transcript unchanged" "old" "$(cat "${CONFLICT}/records/2026-07-17_1500/2026-07-17_1500_demo_transcription.txt")"
assert_file "source preserved on conflict" "${CONFLICT}/records/2026-07-17_1500/${SOURCE_NAME}"

echo
echo "== transcribe-english.sh: normalize failure =="
NORM_FAIL="${TMP_ROOT}/norm_fail"
mkdir -p "${NORM_FAIL}/inbox"
make_whisper_root "${NORM_FAIL}/whisper.cpp"
clone_project "${NORM_FAIL}/project"
write_env "${NORM_FAIL}/project/.env" "${NORM_FAIL}/whisper.cpp" "${NORM_FAIL}/records"
printf 'audio' >"${NORM_FAIL}/inbox/${SOURCE_NAME}"

set +e
out="$(
  FAIL_FFMPEG=1 run_transcribe "${NORM_FAIL}/project/scripts/transcribe-english.sh" <<EOF
${NORM_FAIL}/inbox/${SOURCE_NAME}
EOF
)"
status=$?
set -e
assert_eq "normalize failure exit status" "1" "$status"
assert_contains "normalize failure stage" "stage=normalize" "$out"
assert_file "source preserved on normalize failure" "${NORM_FAIL}/inbox/${SOURCE_NAME}"

echo
echo "== transcribe-english.sh: whisper failure =="
WHISPER_FAIL="${TMP_ROOT}/whisper_fail"
mkdir -p "${WHISPER_FAIL}/inbox"
make_whisper_root "${WHISPER_FAIL}/whisper.cpp"
clone_project "${WHISPER_FAIL}/project"
write_env "${WHISPER_FAIL}/project/.env" "${WHISPER_FAIL}/whisper.cpp" "${WHISPER_FAIL}/records"
printf 'audio' >"${WHISPER_FAIL}/inbox/${SOURCE_NAME}"

set +e
out="$(
  FAIL_WHISPER=1 run_transcribe "${WHISPER_FAIL}/project/scripts/transcribe-english.sh" <<EOF
${WHISPER_FAIL}/inbox/${SOURCE_NAME}
EOF
)"
status=$?
set -e
assert_eq "whisper failure exit status" "1" "$status"
assert_contains "whisper failure stage" "stage=transcribe" "$out"
assert_file "source preserved on whisper failure" "${WHISPER_FAIL}/inbox/${SOURCE_NAME}"
if [ ! -f "${WHISPER_FAIL}/records/2026-07-17_1500/2026-07-17_1500_demo_transcription.txt" ]; then
    echo "  PASS: no success transcript left after whisper failure"
    PASS=$((PASS + 1))
else
    echo "  FAIL: stale/success transcript present after whisper failure"
    FAIL=$((FAIL + 1))
fi

echo
echo "========================================"
echo "Passed: ${PASS}"
echo "Failed: ${FAIL}"
echo "========================================"
if [ "$FAIL" -ne 0 ]; then
    exit 1
fi
