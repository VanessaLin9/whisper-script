import os, re, shutil, subprocess, sys
from subprocess import CalledProcessError
from pathlib import Path

# -------- util: 讀取 .env（僅 key=value，忽略註解/空行） --------
def load_env(env_path: Path) -> dict:
    env = {}
    if not env_path.exists():
        raise FileNotFoundError(f".env not found: {env_path}")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = os.path.expandvars(v.strip().strip('"').strip("'"))
    return env

# -------- folder helpers --------
def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)
    return p

def run_cmd(cmd, cwd=None, check=True):
    """小幫手：在終端執行指令並印出。"""
    print("$", " ".join(cmd))
    return subprocess.run(cmd, cwd=cwd, check=check)

def which(name: str):
    return shutil.which(name)

def on_macos():
    return sys.platform == "darwin"

def check_git():
    """檢查 git 是否安裝"""
    if not shutil.which("git"):
        raise RuntimeError("❌ 找不到 git，請先安裝 git")
    print("✅ git found")

def check_cmake():
    """檢查 cmake 是否安裝"""
    if not shutil.which("cmake"):
        raise RuntimeError("❌ 找不到 cmake，請先安裝 cmake (brew install cmake)")
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
    確保 whisper.cpp 已編譯
    假設 whisper.cpp 已手動下載到 whisper_root
    """
    print(f"\n🔍 檢查 whisper.cpp: {whisper_root}")
    
    # 1. 檢查目錄是否存在
    if not whisper_root.exists():
        raise FileNotFoundError(
            f"❌ whisper.cpp 目錄不存在: {whisper_root}\n"
            f"請手動 clone:\n"
            f"  git clone https://github.com/ggml-org/whisper.cpp.git {whisper_root}"
        )
    
    # 2. 檢查關鍵檔案
    cmake_file = whisper_root / "CMakeLists.txt"
    if not cmake_file.exists():
        raise FileNotFoundError(
            f"❌ whisper.cpp 目錄不完整（找不到 CMakeLists.txt）\n"
            f"請確認 {whisper_root} 是完整的 whisper.cpp repo"
        )
    
    print(f"✅ whisper.cpp 目錄完整")
    
    # 3. 檢查是否已編譯
    whisper_cli = whisper_root / "build" / "bin" / "whisper-cli"
    
    if not whisper_cli.exists():
        print(f"🔨 開始編譯 whisper.cpp...")
        
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
            
            print(f"✅ whisper.cpp 編譯完成")
        except CalledProcessError as e:
            print(f"❌ 編譯失敗: {e.stderr}")
            raise
    else:
        print(f"✅ whisper-cli 已編譯")
    
    return whisper_cli


def ensure_model(whisper_root: Path, model_name: str) -> Path:
    """
    確保單一模型已下載
    model_name 例如: "base.en" 或 "small.en"
    """
    models_dir = whisper_root / "models"
    model_file = models_dir / f"ggml-{model_name}.bin"
    
    print(f"\n🔍 檢查模型: {model_name}")
    
    # 檢查模型是否存在
    if model_file.exists() and model_file.stat().st_size > 0:
        print(f"✅ 模型已存在: {model_file.name}")
        return model_file
    
    # 模型不存在，使用官方腳本下載
    print(f"⬇️  下載模型: {model_name}")
    
    download_script = models_dir / "download-ggml-model.sh"
    
    if not download_script.exists():
        raise FileNotFoundError(f"❌ 找不到下載腳本: {download_script}")
    
    try:
        # sh ./models/download-ggml-model.sh base.en
        subprocess.run(
            ["sh", str(download_script), model_name],
            cwd=whisper_root,
            check=True,
            capture_output=False  # 讓使用者看到下載進度
        )
        
        # 再次檢查模型是否下載成功
        if model_file.exists() and model_file.stat().st_size > 0:
            print(f"✅ 模型下載完成: {model_file.name}")
            return model_file
        else:
            raise RuntimeError(f"❌ 模型下載後仍不存在: {model_file}")
            
    except CalledProcessError as e:
        raise RuntimeError(f"❌ 模型下載失敗: {model_name}\n{e}")


def init_whisper_environment(whisper_root: Path, models: list[str]) -> dict:
    """
    完整初始化 whisper 環境
    
    Args:
        whisper_root: whisper.cpp 的根目錄
        models: 要下載的模型列表，例如 ["base.en", "small.en"]
    
    Returns:
        dict: {
            "whisper_cli": Path,
            "models": {"base.en": Path, "small.en": Path}
        }
    """
    print("=" * 60)
    print("🚀 開始初始化 whisper 環境")
    print("=" * 60)
    
    # 1. 檢查必要工具
    check_git()
    check_cmake()
    
    # 2. 確保 whisper.cpp 存在並編譯
    whisper_cli = ensure_whisper_cpp(whisper_root)
    
    # 3. 下載所有需要的模型
    downloaded_models = {}
    for model_name in models:
        model_path = ensure_model(whisper_root, model_name)
        downloaded_models[model_name] = model_path
    
    print("\n" + "=" * 60)
    print("✅ 初始化完成！")
    print("=" * 60)
    print(f"whisper-cli: {whisper_cli}")
    for name, path in downloaded_models.items():
        print(f"模型 {name}: {path}")
    
    return {
        "whisper_cli": whisper_cli,
        "models": downloaded_models
    }

# -------- main --------
def main():
    # 偵測 repo 根目錄（此檔在 python_pipeline 下）
    repo_root = Path(__file__).resolve().parents[1]
    env_path  = repo_root / ".env"
    env = load_env(env_path)

    whisper_root_path = Path(env.get("WHISPER_ROOT", "")).expanduser()
    whisper_root = whisper_root_path.resolve()

    # 取其他變數
    records_dir = Path(os.path.expanduser(env.get("MEETING_RECORDS_DIR", f"{Path.home()}/MeetingRecords"))).resolve()
    transcripts_dir = Path(os.path.expanduser(env.get("TRANSCRIPTS_DIR", f"{Path.home()}/MeetingRecords/Transcripts"))).resolve()
    preferred = env.get("PREFERRED_MODEL", "small")
    default_language = env.get("DEFAULT_LANGUAGE", "en")
    
    # 動態組合模型名稱
    models_to_download = [
        f"{preferred}.{default_language}", 
        f"base.{default_language}"
    ]

    print("🔧 Init summary")
    print("  • Repo root        :", repo_root)
    print("  • .env             :", env_path)
    print("  • WHISPER_ROOT     :", whisper_root)
    print("  • MEETING_RECORDS  :", records_dir)
    print("  • TRANSCRIPTS_DIR  :", transcripts_dir)
    print("  • PREFERRED_MODEL  :", preferred)
    print("  • DEFAULT_LANGUAGE :", default_language)
    print()

    # 建資料夾
    ensure_dir(records_dir)
    ensure_dir(transcripts_dir)
    ensure_dir(whisper_root / "models")
    ensure_dir(repo_root / "logs")
    print("✅ folders ready")

    ensure_ffmpeg()

    try:
        result = init_whisper_environment(whisper_root, models_to_download)
        print("\n🎉 所有設定完成，可以開始使用了！")
    except Exception as e:
        print(f"\n❌ 初始化失敗: {e}")
        sys.exit(1)

    # 印出最終結果
    print("\n🎉 Ready to go!")
    print("  • whisper-cli     :", result["whisper_cli"])
    
    # 動態印出模型（避免硬編碼）
    for model_name, model_path in result["models"].items():
        print(f"  • model {model_name:8} :", model_path)
    
    print("  • recordings      :", records_dir)
    print("  • transcripts     :", transcripts_dir)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("❌ Init failed:", e)
        sys.exit(1)