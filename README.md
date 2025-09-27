# Whisper Script Collection

A collection of bash scripts for meeting transcription using [Whisper.cpp](https://github.com/ggerganov/whisper.cpp). These scripts provide easy-to-use tools for recording meetings and transcribing audio files with high accuracy.

## Features

- **Real-time meeting recording** with automatic transcription
- **Batch audio transcription** for existing audio files
- **Multiple output formats**: TXT, SRT, VTT, JSON
- **Automatic audio normalization** for optimal transcription quality
- **Smart model selection** with fallback options
- **macOS optimized** with AVFoundation integration

## Scripts Overview

### 1. `meeting-assist-chunked.sh` - Live Meeting Recording & Transcription

Records a meeting in real-time and automatically transcribes it when recording stops.

**Features:**
- Continuous recording until manually stopped (Ctrl+C)
- Automatic transcription after recording ends
- Uses `small.en` model (falls back to `base.en` if needed)
- Outputs: audio file, TXT transcript, SRT subtitles
- Comprehensive logging and error handling

**Usage:**
```bash
./meeting-assist-chunked.sh
```

**Output Location:** `~/MeetingRecords/`

### 2. `transcribe-meeting.sh` - Batch Audio Transcription

Transcribes existing audio files with advanced preprocessing and multiple output formats.

**Features:**
- Interactive file path input
- Audio normalization to 16kHz mono WAV
- Multiple output formats (TXT, SRT, VTT, JSON)
- Automatic clipboard copying (macOS)
- Opens output folder when complete
- Duration estimation with ffprobe

**Usage:**
```bash
./transcribe-meeting.sh
```

**Output Location:** `~/MeetingRecords/Transcripts/`

## Prerequisites

### 1. Whisper.cpp Installation

First, install and build Whisper.cpp:

```bash
# Clone the repository
git clone https://github.com/ggerganov/whisper.cpp.git
cd whisper.cpp

# Build (macOS)
cmake -B build -DWHISPER_PORTAUDIO=OFF
cmake --build build -j

# Download models
bash ./models/download-ggml-model.sh small.en
bash ./models/download-ggml-model.sh base.en
```

### 2. Required Tools

- **FFmpeg**: For audio recording and processing
  ```bash
  brew install ffmpeg
  ```

- **System Requirements**: macOS (for AVFoundation support)

## Configuration

### Setting Whisper.cpp Path

Edit the `WHISPER_ROOT` variable in both scripts to match your installation:

```bash
# In meeting-assist-chunked.sh (line 22)
WHISPER_ROOT="${WHISPER_ROOT:-/Users/yourusername/whisper.cpp}"

# In transcribe-meeting.sh (line 14)
WHISPER_ROOT="/Users/yourusername/whisper.cpp"
```

### Audio Device Configuration

For recording scripts, you can modify the microphone device:

```bash
# In meeting-assist-chunked.sh (line 33)
MIC=":0"  # :0 is built-in Mac microphone
```

To see available devices:
```bash
ffmpeg -f avfoundation -list_devices true -i ""
```

## Usage Examples

### Recording a Live Meeting

1. Start recording:
   ```bash
   ./meeting-assist-chunked.sh
   ```

2. The script will:
   - List available audio devices
   - Start recording from your microphone
   - Display recording status

3. Stop recording:
   - Press `Ctrl+C` to stop recording
   - Transcription will begin automatically

4. Output files:
   - `meeting_YYYYMMDD_HHMMSS.wav` - Original audio
   - `meeting_YYYYMMDD_HHMMSS.txt` - Text transcript
   - `meeting_YYYYMMDD_HHMMSS.srt` - Subtitle file

### Transcribing Existing Audio

1. Run the transcription script:
   ```bash
   ./transcribe-meeting.sh
   ```

2. Enter the path to your audio file when prompted

3. The script will:
   - Normalize the audio to optimal format
   - Transcribe using the best available model
   - Generate multiple output formats
   - Copy transcript to clipboard (macOS)
   - Open the output folder

## Output Formats

| Format | Description | Use Case |
|--------|-------------|----------|
| `.txt` | Plain text transcript | Reading, editing, sharing |
| `.srt` | SubRip subtitle format | Video editing, streaming |
| `.vtt` | WebVTT subtitle format | Web players, HTML5 video |
| `.json` | Rich JSON with timestamps | Advanced processing, analysis |

## Model Information

The scripts use these Whisper models in order of preference:

1. **small.en** - Best balance of speed and accuracy for English
2. **base.en** - Faster but less accurate fallback

Models are automatically downloaded and cached in the `models/` directory.

## Troubleshooting

### Common Issues

**"whisper-cli not found"**
- Ensure Whisper.cpp is built: `cmake --build build -j`
- Check the `WHISPER_ROOT` path in scripts

**"No model found"**
- Download models: `bash ./models/download-ggml-model.sh small.en`
- Or: `bash ./models/download-ggml-model.sh base.en`

**"Recording file is missing or empty"**
- Check microphone permissions in System Preferences
- Verify the audio device ID with `ffmpeg -f avfoundation -list_devices true -i ""`

**Audio quality issues**
- Ensure good microphone positioning
- Check for background noise
- Use headphones to prevent feedback

### Performance Tips

- **CPU Usage**: The scripts automatically detect CPU cores for optimal threading
- **Long Meetings**: For meetings >30 minutes, consider using `small.en` for better accuracy
- **Storage**: Audio files can be large; ensure sufficient disk space

## File Structure

```
whisper-script/
├── README.md
├── meeting-assist-chunked.sh    # Live recording + transcription
└── transcribe-meeting.sh        # Batch transcription

~/MeetingRecords/                # Output directory
├── meeting_YYYYMMDD_HHMMSS.wav  # Recorded audio
├── meeting_YYYYMMDD_HHMMSS.txt  # Text transcript
├── meeting_YYYYMMDD_HHMMSS.srt  # Subtitle file
└── ffmpeg_YYYYMMDD_HHMMSS.log   # Recording log

~/MeetingRecords/Transcripts/    # Batch transcription output
├── filename_norm16k.wav         # Normalized audio
├── filename_final.txt           # Text transcript
├── filename_final.srt           # Subtitle file
├── filename_final.vtt           # WebVTT subtitle
└── filename_final.json          # Rich JSON data
```

## Contributing

Feel free to submit issues and enhancement requests! This project is designed to be simple and reliable for meeting transcription workflows.

## License

This project is open source. The scripts are provided as-is for personal and professional use.
