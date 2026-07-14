import argparse
import os, re, shlex, shutil, subprocess, sys
from subprocess import CalledProcessError
from pathlib import Path

# -------- util: Load .env (key=value only, ignore comments/empty lines) --------
def load_env(env_path: Path) -> dict:
    env = {}
    if not env_path.exists():
        raise FileNotFoundError(f".env not found: {env_path}")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        try:
            parsed = shlex.split(v, comments=True, posix=True)
        except ValueError as exc:
            raise ValueError(f"Invalid .env value for {k.strip()}: {exc}") from exc
        env[k.strip()] = os.path.expandvars(" ".join(parsed))
    return env

# -------- folder helpers --------
def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)
    return p

def run_cmd(cmd, cwd=None, check=True):
    """Helper: Execute command in terminal and print."""
    print("$", " ".join(cmd))
    return subprocess.run(cmd, cwd=cwd, check=check)

def which(name: str):
    return shutil.which(name)

def on_macos():
    return sys.platform == "darwin"

def check_git():
    """Check if git is installed"""
    if not shutil.which("git"):
        raise RuntimeError("❌ git not found, please install git first")
    print("✅ git found")

def check_cmake():
    """Check if cmake is installed"""
    if not shutil.which("cmake"):
        raise RuntimeError("❌ cmake not found, please install cmake (brew install cmake)")
    print("✅ cmake found")

# -------- ffmpeg --------
def ensure_ffmpeg():
    if which("ffmpeg"):
        print("✅ ffmpeg found:", which("ffmpeg"))
        return
    if on_macos():
        print("⚠️ ffmpeg not found; installing via Homebrew...")
        run_cmd(["brew", "install", "ffmpeg"], check=False)
        if which("ffmpeg"):
            print("✅ ffmpeg installed:", which("ffmpeg"))
        else:
            print("❌ ffmpeg still not found. Please install it manually.")
    else:
        print("⚠️ Please install ffmpeg for your OS (apt/yum/choco, etc.)")

# -------- whisper.cpp --------
def ensure_whisper_cpp(whisper_root: Path) -> Path:
    """
    Ensure whisper.cpp is compiled
    Assumes whisper.cpp is manually downloaded to whisper_root
    """
    print(f"\n🔍 Checking whisper.cpp: {whisper_root}")
    
    # 1. Check if directory exists
    if not whisper_root.exists():
        raise FileNotFoundError(
            f"❌ whisper.cpp directory not found: {whisper_root}\n"
            f"Please manually clone:\n"
            f"  git clone https://github.com/ggml-org/whisper.cpp.git {whisper_root}"
        )
    
    # 2. Check key files
    cmake_file = whisper_root / "CMakeLists.txt"
    if not cmake_file.exists():
        raise FileNotFoundError(
            f"❌ whisper.cpp directory incomplete (CMakeLists.txt not found)\n"
            f"Please ensure {whisper_root} is a complete whisper.cpp repo"
        )
    
    print(f"✅ whisper.cpp directory complete")
    
    # 3. Check if already compiled
    whisper_cli = whisper_root / "build" / "bin" / "whisper-cli"
    
    if not whisper_cli.exists():
        print(f"🔨 Starting whisper.cpp compilation...")
        
        try:
            # cmake -B build
            print("   → cmake -B build")
            subprocess.run(
                ["cmake", "-B", "build"],
                cwd=whisper_root,
                check=True,
                capture_output=True,
                text=True
            )
            
            # cmake --build build -j --config Release
            print("   → cmake --build build -j --config Release")
            subprocess.run(
                ["cmake", "--build", "build", "-j", "--config", "Release"],
                cwd=whisper_root,
                check=True,
                capture_output=True,
                text=True
            )
            
            print(f"✅ whisper.cpp compilation complete")
        except CalledProcessError as e:
            print(f"❌ Compilation failed: {e.stderr}")
            raise
    else:
        print(f"✅ whisper-cli already compiled")
    
    return whisper_cli


def ensure_model(whisper_root: Path, model_name: str) -> Path:
    """
    Ensure a single model is downloaded
    model_name example: "base" or "small" for multilingual transcription
    """
    models_dir = whisper_root / "models"
    model_file = models_dir / f"ggml-{model_name}.bin"
    
    print(f"\n🔍 Checking model: {model_name}")
    
    # Check if model exists
    if model_file.exists() and model_file.stat().st_size > 0:
        print(f"✅ Model already exists: {model_file.name}")
        return model_file
    
    # Model doesn't exist, download using official script
    print(f"⬇️  Downloading model: {model_name}")
    
    download_script = models_dir / "download-ggml-model.sh"
    
    if not download_script.exists():
        raise FileNotFoundError(f"❌ Download script not found: {download_script}")
    
    try:
        # sh ./models/download-ggml-model.sh base
        subprocess.run(
            ["sh", str(download_script), model_name],
            cwd=whisper_root,
            check=True,
            capture_output=False  # Let user see download progress
        )
        
        # Check again if model download was successful
        if model_file.exists() and model_file.stat().st_size > 0:
            print(f"✅ Model download complete: {model_file.name}")
            return model_file
        else:
            raise RuntimeError(f"❌ Model still not found after download: {model_file}")
            
    except CalledProcessError as e:
        raise RuntimeError(f"❌ Model download failed: {model_name}\n{e}")


def init_whisper_environment(whisper_root: Path, models: list[str]) -> dict:
    """
    Complete initialization of whisper environment
    
    Args:
        whisper_root: Root directory of whisper.cpp
        models: List of models to download, e.g. ["base", "small"]
    
    Returns:
        dict: {
            "whisper_cli": Path,
            "models": {"base": Path, "small": Path}
        }
    """
    print("=" * 60)
    print("🚀 Starting whisper environment initialization")
    print("=" * 60)
    
    # 1. Check required tools
    check_git()
    check_cmake()
    
    # 2. Ensure whisper.cpp exists and is compiled
    whisper_cli = ensure_whisper_cpp(whisper_root)
    
    # 3. Download all required models
    downloaded_models = {}
    for model_name in models:
        model_path = ensure_model(whisper_root, model_name)
        downloaded_models[model_name] = model_path
    
    print("\n" + "=" * 60)
    print("✅ Initialization complete!")
    print("=" * 60)
    print(f"whisper-cli: {whisper_cli}")
    for name, path in downloaded_models.items():
        print(f"Model {name}: {path}")
    
    return {
        "whisper_cli": whisper_cli,
        "models": downloaded_models
    }

# -------- main --------
def check_environment(whisper_root: Path, models: list[str], directories: list[Path]) -> bool:
    """Report environment readiness without installing or modifying anything."""
    checks = [
        ("git", which("git")),
        ("cmake", which("cmake")),
        ("ffmpeg", which("ffmpeg")),
        ("whisper.cpp", whisper_root if (whisper_root / "CMakeLists.txt").is_file() else None),
        (
            "whisper-cli",
            whisper_root / "build" / "bin" / "whisper-cli"
            if (whisper_root / "build" / "bin" / "whisper-cli").is_file()
            else None,
        ),
    ]

    for model_name in models:
        model_path = whisper_root / "models" / f"ggml-{model_name}.bin"
        checks.append((f"model {model_name}", model_path if model_path.is_file() and model_path.stat().st_size > 0 else None))

    print("\nEnvironment check")
    print("=" * 60)
    ready = True
    for label, value in checks:
        if value:
            print(f"✅ {label}: {value}")
        else:
            print(f"❌ {label}: not found")
            ready = False

    for directory in directories:
        if directory.is_dir():
            print(f"✅ directory: {directory}")
        else:
            print(f"⚠️  directory will be created during install: {directory}")

    print("=" * 60)
    print("✅ Environment is ready" if ready else "❌ Environment is not ready; run: python3 setup.py install")
    return ready


def main():
    parser = argparse.ArgumentParser(description="Set up and validate the Whisper Script environment")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("check", "install"),
        default="check",
        help="check only reports status; install may create directories, build whisper.cpp, and download models",
    )
    args = parser.parse_args()

    # setup.py lives at the repository root.
    repo_root = Path(__file__).resolve().parent
    env_path  = repo_root / ".env"
    env = load_env(env_path)

    whisper_root_path = Path(env.get("WHISPER_ROOT", "")).expanduser()
    whisper_root = whisper_root_path.resolve()

    # Get other variables
    records_dir = Path(os.path.expanduser(env.get("MEETING_RECORDS_DIR", f"{Path.home()}/MeetingRecords"))).resolve()
    transcripts_dir = Path(os.path.expanduser(env.get("TRANSCRIPTS_DIR", f"{Path.home()}/MeetingRecords/Transcripts"))).resolve()
    preferred = env.get("PREFERRED_MODEL", "small")
    default_language = env.get("DEFAULT_LANGUAGE", "zh")
    
    # Model names and spoken language are independent. Models without the
    # ".en" suffix are multilingual and can transcribe Chinese with English.
    models_to_download = [preferred]

    print("🔧 Init summary")
    print("  • Repo root        :", repo_root)
    print("  • .env             :", env_path)
    print("  • WHISPER_ROOT     :", whisper_root)
    print("  • MEETING_RECORDS  :", records_dir)
    print("  • TRANSCRIPTS_DIR  :", transcripts_dir)
    print("  • PREFERRED_MODEL  :", preferred)
    print("  • DEFAULT_LANGUAGE :", default_language)
    print()

    if args.command == "check":
        ready = check_environment(
            whisper_root,
            models_to_download,
            [records_dir, transcripts_dir, repo_root / "logs"],
        )
        return 0 if ready else 1

    # Create directories
    ensure_dir(records_dir)
    ensure_dir(transcripts_dir)
    ensure_dir(whisper_root / "models")
    ensure_dir(repo_root / "logs")
    print("✅ Folders ready")

    ensure_ffmpeg()

    try:
        result = init_whisper_environment(whisper_root, models_to_download)
        print("\n🎉 All setup complete, ready to use!")
    except Exception as e:
        print(f"\n❌ Initialization failed: {e}")
        sys.exit(1)

    # Print final results
    print("\n🎉 Ready to go!")
    print("  • whisper-cli     :", result["whisper_cli"])
    
    # Dynamically print models (avoid hardcoding)
    for model_name, model_path in result["models"].items():
        print(f"  • model {model_name:8} :", model_path)
    
    print("  • recordings      :", records_dir)
    print("  • transcripts     :", transcripts_dir)
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("❌ Init failed:", e)
        sys.exit(1)
