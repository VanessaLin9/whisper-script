#!/usr/bin/env python3
# preprocess_audio.py
# Split audio files at silence points for better mixed-language transcription

from __future__ import annotations
import os
import sys
import json
import argparse
import subprocess
from pathlib import Path
from typing import List, Dict, Tuple
from dataclasses import dataclass, asdict

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


@dataclass
class SilenceSegment:
    """Represents a detected silence period"""
    start: float  # seconds
    end: float    # seconds
    duration: float  # seconds


@dataclass
class AudioSegment:
    """Represents a split audio segment"""
    filename: str
    start_time: float  # seconds in original file
    end_time: float    # seconds in original file
    duration: float    # seconds
    segment_index: int


def check_ffmpeg() -> bool:
    """Check if ffmpeg is available"""
    try:
        subprocess.run(["ffmpeg", "-version"], 
                      capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def detect_silences(
    audio_file: Path,
    noise_threshold_db: float = -45.0,
    min_silence_duration: float = 1.0
) -> List[SilenceSegment]:
    """
    Detect silence segments in audio file using ffmpeg silencedetect
    
    Args:
        audio_file: Path to input audio file
        noise_threshold_db: Anything quieter than this is silence (e.g., -40 to -50)
        min_silence_duration: Minimum silence duration in seconds to detect
    
    Returns:
        List of SilenceSegment objects
    """
    cmd = [
        "ffmpeg",
        "-i", str(audio_file),
        "-af", f"silencedetect=noise={noise_threshold_db}dB:d={min_silence_duration}",
        "-f", "null",
        "-"
    ]
    
    print(f"[INFO] Detecting silences (threshold: {noise_threshold_db}dB, min: {min_silence_duration}s)...")
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    # Parse ffmpeg stderr output
    silences = []
    silence_start = None
    
    for line in result.stderr.split('\n'):
        if 'silencedetect' in line:
            if 'silence_start' in line:
                # Extract start time
                parts = line.split('silence_start:')
                if len(parts) > 1:
                    silence_start = float(parts[1].strip().split()[0])
            
            elif 'silence_end' in line and silence_start is not None:
                # Extract end time and duration
                parts = line.split('silence_end:')
                if len(parts) > 1:
                    end_part = parts[1].strip().split('|')
                    silence_end = float(end_part[0].strip().split()[0])
                    
                    # Duration might be in the line
                    duration = silence_end - silence_start
                    
                    silences.append(SilenceSegment(
                        start=silence_start,
                        end=silence_end,
                        duration=duration
                    ))
                    silence_start = None
    
    print(f"[INFO] Found {len(silences)} silence periods")
    return silences


def get_audio_duration(audio_file: Path) -> float:
    """Get total duration of audio file in seconds"""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_file)
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(result.stdout.strip())


def calculate_split_points(
    silences: List[SilenceSegment],
    total_duration: float,
    min_segment_length: float = 30.0,
    max_segment_length: float = 120.0
) -> List[Tuple[float, float]]:
    """
    Calculate optimal split points based on silence detection
    
    Args:
        silences: List of detected silences
        total_duration: Total audio duration in seconds
        min_segment_length: Minimum segment length (too short = bad context)
        max_segment_length: Maximum segment length (too long = mixed languages)
    
    Returns:
        List of (start_time, end_time) tuples in seconds
    """
    if not silences:
        # No silences found, split by max_segment_length
        print("[WARN] No silences detected, splitting by time only")
        segments = []
        current = 0.0
        while current < total_duration:
            end = min(current + max_segment_length, total_duration)
            segments.append((current, end))
            current = end
        return segments
    
    segments = []
    current_start = 0.0
    
    for silence in silences:
        # Use middle of silence as split point
        split_point = (silence.start + silence.end) / 2.0
        segment_duration = split_point - current_start
        
        # Only split if we've accumulated enough content
        if segment_duration >= min_segment_length:
            segments.append((current_start, split_point))
            current_start = split_point
        
        # Force split if segment would be too long
        elif segment_duration >= max_segment_length:
            segments.append((current_start, split_point))
            current_start = split_point
    
    # Add final segment
    if current_start < total_duration:
        segments.append((current_start, total_duration))
    
    return segments


def calculate_segment_silence_ratio(
    audio_file: Path,
    start_time: float,
    duration: float,
    noise_threshold_db: float = -45.0
) -> float:
    """
    Calculate what percentage of a segment is silence
    Used to filter out segments that are almost entirely silent
    """
    cmd = [
        "ffmpeg",
        "-ss", str(start_time),
        "-t", str(duration),
        "-i", str(audio_file),
        "-af", f"silencedetect=noise={noise_threshold_db}dB:d=0.1",
        "-f", "null",
        "-"
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    total_silence = 0.0
    for line in result.stderr.split('\n'):
        if 'silencedetect' in line and 'silence_duration' in line:
            parts = line.split('silence_duration:')
            if len(parts) > 1:
                total_silence += float(parts[1].strip().split()[0])
    
    return total_silence / duration if duration > 0 else 0.0


def split_audio_segment(
    input_file: Path,
    output_file: Path,
    start_time: float,
    duration: float
) -> bool:
    """
    Extract a segment from audio file using ffmpeg
    
    Args:
        input_file: Source audio file
        output_file: Output segment file
        start_time: Start time in seconds
        duration: Duration in seconds
    
    Returns:
        True if successful
    """
    cmd = [
        "ffmpeg",
        "-i", str(input_file),
        "-ss", str(start_time),
        "-t", str(duration),
        "-c", "copy",  # Copy codec (fast, no re-encoding)
        "-y",  # Overwrite output
        str(output_file)
    ]
    
    try:
        subprocess.run(cmd, capture_output=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Failed to split segment: {e}")
        return False


def process_audio_file(
    input_file: Path,
    output_dir: Path,
    noise_threshold_db: float = -45.0,
    min_silence_duration: float = 1.0,
    min_segment_length: float = 30.0,
    max_segment_length: float = 120.0,
    silence_ratio_threshold: float = 0.9
) -> List[AudioSegment]:
    """
    Main processing function: detect silences, split audio, filter silent segments
    
    Args:
        input_file: Input audio file path
        output_dir: Directory for output segments
        noise_threshold_db: Silence detection threshold
        min_silence_duration: Minimum silence to detect
        min_segment_length: Minimum output segment length
        max_segment_length: Maximum output segment length
        silence_ratio_threshold: Skip segments with >this ratio of silence
    
    Returns:
        List of AudioSegment metadata
    """
    print(f"\n{'='*60}")
    print(f"Processing: {input_file.name}")
    print(f"{'='*60}")
    
    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Get total duration
    total_duration = get_audio_duration(input_file)
    print(f"[INFO] Total duration: {total_duration:.2f}s ({total_duration/60:.1f} min)")
    
    # Detect silences
    silences = detect_silences(
        input_file,
        noise_threshold_db=noise_threshold_db,
        min_silence_duration=min_silence_duration
    )
    
    # Calculate split points
    split_points = calculate_split_points(
        silences,
        total_duration,
        min_segment_length=min_segment_length,
        max_segment_length=max_segment_length
    )
    
    print(f"[INFO] Will create {len(split_points)} segments")
    
    # Process each segment
    segments_metadata = []
    segment_index = 1
    
    for start_time, end_time in split_points:
        duration = end_time - start_time
        
        # Check silence ratio
        print(f"[{segment_index}/{len(split_points)}] Checking segment {start_time:.1f}s - {end_time:.1f}s ({duration:.1f}s)...", end='')
        
        silence_ratio = calculate_segment_silence_ratio(
            input_file,
            start_time,
            duration,
            noise_threshold_db=noise_threshold_db
        )
        
        if silence_ratio > silence_ratio_threshold:
            print(f" SKIP (silence: {silence_ratio*100:.0f}%)")
            continue
        
        print(f" OK (silence: {silence_ratio*100:.0f}%)")
        
        # Generate output filename
        output_filename = f"segment_{segment_index:03d}.wav"
        output_path = output_dir / output_filename
        
        # Split segment
        if split_audio_segment(input_file, output_path, start_time, duration):
            segments_metadata.append(AudioSegment(
                filename=output_filename,
                start_time=start_time,
                end_time=end_time,
                duration=duration,
                segment_index=segment_index
            ))
            segment_index += 1
    
    print(f"[DONE] Created {len(segments_metadata)} segments (filtered {len(split_points) - len(segments_metadata)} silent segments)")
    
    return segments_metadata


def save_metadata(segments: List[AudioSegment], output_file: Path, source_file: Path):
    """Save segment metadata to JSON file"""
    metadata = {
        "source_file": source_file.name,
        "total_segments": len(segments),
        "segments": [asdict(seg) for seg in segments]
    }
    
    with output_file.open('w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
    
    print(f"[INFO] Metadata saved to: {output_file}")


def main():
    parser = argparse.ArgumentParser(
        description="Preprocess audio files: split at silences for mixed-language transcription"
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Input audio file (WAV recommended)"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory (default: same name as input file)"
    )
    parser.add_argument(
        "--noise-threshold",
        type=float,
        default=-45.0,
        help="Silence threshold in dB (default: -45.0, range: -30 to -60)"
    )
    parser.add_argument(
        "--min-silence",
        type=float,
        default=1.0,
        help="Minimum silence duration to detect in seconds (default: 1.0)"
    )
    parser.add_argument(
        "--min-segment",
        type=float,
        default=30.0,
        help="Minimum segment length in seconds (default: 30.0)"
    )
    parser.add_argument(
        "--max-segment",
        type=float,
        default=120.0,
        help="Maximum segment length in seconds (default: 120.0)"
    )
    parser.add_argument(
        "--silence-ratio-threshold",
        type=float,
        default=0.9,
        help="Skip segments with silence ratio above this (default: 0.9 = 90%%)"
    )
    
    args = parser.parse_args()
    
    # Validate input
    if not args.input.exists():
        print(f"[ERROR] Input file not found: {args.input}")
        sys.exit(1)
    
    # Check ffmpeg
    if not check_ffmpeg():
        print("[ERROR] ffmpeg not found. Please install ffmpeg first.")
        print("  macOS: brew install ffmpeg")
        sys.exit(1)
    
    # Determine output directory
    if args.output_dir:
        output_dir = args.output_dir
    else:
        # Create folder named after input file (without extension)
        output_dir = args.input.parent / args.input.stem
    
    # Process audio
    segments = process_audio_file(
        input_file=args.input,
        output_dir=output_dir,
        noise_threshold_db=args.noise_threshold,
        min_silence_duration=args.min_silence,
        min_segment_length=args.min_segment,
        max_segment_length=args.max_segment,
        silence_ratio_threshold=args.silence_ratio_threshold
    )
    
    # Save metadata
    if segments:
        metadata_file = output_dir / "metadata.json"
        save_metadata(segments, metadata_file, args.input)
        
        print(f"\n{'='*60}")
        print(f"✓ Processing complete!")
        print(f"Output: {output_dir}")
        print(f"Segments: {len(segments)}")
        print(f"{'='*60}")
    else:
        print("\n[WARN] No valid segments created. Audio might be entirely silent?")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INTERRUPTED] Bye.")
        sys.exit(130)