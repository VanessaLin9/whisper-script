#!/usr/bin/env bash
# Shared helpers for documented whisper-script shell workflows.

_COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${_COMMON_DIR}/../.." && pwd)"

load_project_env() {
    local env_file="${1:-${REPO_ROOT}/.env}"

    if [ ! -f "$env_file" ]; then
        echo "[!] Environment file not found: $env_file"
        echo "    Please copy .env.example to .env and configure your paths:"
        echo "    cp .env.example .env"
        exit 1
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        echo "[!] python3 is required to load .env safely"
        exit 1
    fi

    local dump_file
    dump_file="$(mktemp "${TMPDIR:-/tmp}/whisper-env.XXXXXX")"
    if ! python3 "${REPO_ROOT}/env_loader.py" "$env_file" --dump-shell >"$dump_file"; then
        rm -f "$dump_file"
        echo "[!] Failed to load environment file: $env_file"
        exit 1
    fi

    local key value
    while IFS= read -r -d '' key && IFS= read -r -d '' value; do
        printf -v "$key" '%s' "$value"
        export "$key"
    done <"$dump_file"
    rm -f "$dump_file"

    echo "[*] Loaded configuration from: $env_file"
}

require_var() {
    local name="$1"
    if [ -z "${!name:-}" ]; then
        echo "[!] ${name} not set in .env file"
        exit 1
    fi
}

apply_workflow_defaults() {
    MEETING_RECORDS_DIR="${MEETING_RECORDS_DIR:-$HOME/MeetingRecords}"
    TRANSCRIPTS_DIR="${TRANSCRIPTS_DIR:-$HOME/MeetingRecords/Transcripts}"
    MIC_DEVICE="${MIC_DEVICE:-:0}"
    DEFAULT_LANGUAGE="${DEFAULT_LANGUAGE:-zh}"
    PREFERRED_MODEL="${PREFERRED_MODEL:-small}"
    export MEETING_RECORDS_DIR TRANSCRIPTS_DIR MIC_DEVICE DEFAULT_LANGUAGE PREFERRED_MODEL
}

detect_threads() {
    if [ -n "${THREADS:-}" ]; then
        return 0
    fi
    if command -v sysctl >/dev/null 2>&1; then
        THREADS="$(sysctl -n hw.logicalcpu 2>/dev/null || echo 8)"
    else
        THREADS="8"
    fi
    export THREADS
}

whisper_cli_path() {
    local whisper_root="$1"
    echo "${whisper_root}/build/bin/whisper-cli"
}

configured_model_path() {
    local whisper_root="$1"
    local model_name="$2"
    echo "${whisper_root}/models/ggml-${model_name}.bin"
}

require_whisper_cli() {
    local whisper_root="$1"
    local cli
    cli="$(whisper_cli_path "$whisper_root")"

    if [ ! -x "$cli" ]; then
        echo "[!] whisper-cli not found at ${cli}"
        echo "    Please build whisper.cpp first:"
        echo "      cd ${whisper_root}"
        echo "      cmake -B build && cmake --build build -j --config Release"
        echo "    Or run: python3 setup.py install"
        exit 1
    fi
}

require_model_language_compatible() {
    local model_name="$1"
    local language="$2"

    if ! python3 "${REPO_ROOT}/env_loader.py" \
        --validate-model-language "$model_name" "$language"; then
        echo "[!] Invalid model/language combination."
        echo "    English-only models (*.en) are only allowed when DEFAULT_LANGUAGE=en."
        exit 1
    fi
}

require_configured_model() {
    local whisper_root="$1"
    local model_name="$2"
    local model_file
    model_file="$(configured_model_path "$whisper_root" "$model_name")"

    if [ ! -f "$model_file" ] || [ ! -s "$model_file" ]; then
        echo "[!] Preferred model not found: ${model_file}"
        echo "    Missing configured model(s). English-only files (*.en.bin) do not satisfy a multilingual setting."
        echo "    Download the configured multilingual model:"
        echo "      cd ${whisper_root}"
        echo "      bash ./models/download-ggml-model.sh ${model_name}"
        echo "    Or run: python3 setup.py install"
        exit 1
    fi
}

resolve_workflow_paths() {
    require_var WHISPER_ROOT
    apply_workflow_defaults
    detect_threads
    require_model_language_compatible "$PREFERRED_MODEL" "$DEFAULT_LANGUAGE"
    require_whisper_cli "$WHISPER_ROOT"
    require_configured_model "$WHISPER_ROOT" "$PREFERRED_MODEL"

    WHISPER_CLI="$(whisper_cli_path "$WHISPER_ROOT")"
    MODEL_FILE="$(configured_model_path "$WHISPER_ROOT" "$PREFERRED_MODEL")"
    export WHISPER_CLI MODEL_FILE
}
