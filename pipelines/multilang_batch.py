#!/usr/bin/env python3
# batch_pipeline.py
# Main controller for multilingual meeting transcription pipeline
# Orchestrates: preprocess → transcribe → merge

from __future__ import annotations
import os
import sys
import json
import argparse
import subprocess
import shutil
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


class Colors:
    """Terminal colors for pretty output"""
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    END = '\033[0m'


def print_header(text: str):
    """Print a formatted header"""
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'='*60}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.BLUE}{text}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'='*60}{Colors.END}\n")


def print_step(step_num: int, text: str):
    """Print a step indicator"""
    print(f"{Colors.BOLD}[Step {step_num}]{Colors.END} {text}")


def print_success(text: str):
    """Print success message"""
    print(f"{Colors.GREEN}✓{Colors.END} {text}")


def print_error(text: str):
    """Print error message"""
    print(f"{Colors.RED}✗{Colors.END} {text}")


def print_warning(text: str):
    """Print warning message"""
    print(f"{Colors.YELLOW}⚠{Colors.END} {text}")


def find_audio_files(directory: Path) -> List[Path]:
    """Find all WAV files in directory"""
    audio_extensions = {'.wav', '.mp3', '.m4a', '.flac', '.aac', '.ogg'}
    files = []
    
    for ext in audio_extensions:
        files.extend(directory.glob(f'*{ext}'))
    
    return sorted(files, key=lambda x: x.name)


def run_preprocess(audio_file: Path, script_dir: Path) -> Optional[Path]:
    """
    Run preprocess_audio.py on the audio file
    Returns the output directory path if successful
    """
    preprocess_script = script_dir / "preprocess_audio.py"
    
    if not preprocess_script.exists():
        print_error(f"preprocess_audio.py not found at {preprocess_script}")
        return None
    
    # Output directory will be named after the audio file
    output_dir = audio_file.parent / audio_file.stem
    
    print(f"    Splitting audio into segments...")
    print(f"    Output: {output_dir}")
    
    try:
        result = subprocess.run(
            [sys.executable, str(preprocess_script), str(audio_file)],
            capture_output=True,
            text=True,
            check=True
        )
        
        # Show some output
        for line in result.stdout.split('\n'):
            if '[INFO]' in line or '[DONE]' in line or 'segments' in line.lower():
                print(f"    {line}")
        
        if output_dir.exists():
            return output_dir
        else:
            print_error(f"Output directory not created: {output_dir}")
            return None
            
    except subprocess.CalledProcessError as e:
        print_error(f"Preprocessing failed: {e}")
        if e.stderr:
            print(f"    Error: {e.stderr}")
        return None


def run_transcribe(segments_dir: Path, script_dir: Path) -> bool:
    """
    Run multi-lang.sh on the segments directory
    Returns True if successful
    """
    transcribe_script = script_dir / "multi-lang.sh"
    
    if not transcribe_script.exists():
        print_error(f"multi-lang.sh not found at {transcribe_script}")
        return False
    
    # Make sure script is executable
    transcribe_script.chmod(0o755)
    
    print(f"    Transcribing segments...")
    
    try:
        result = subprocess.run(
            [str(transcribe_script), str(segments_dir)],
            capture_output=True,
            text=True,
            check=True
        )
        
        # Show relevant output
        for line in result.stdout.split('\n'):
            if any(keyword in line for keyword in ['Transcribing:', 'Success', 'Failed', 'Complete']):
                print(f"    {line}")
        
        # Check if transcripts folder was created
        transcripts_dir = segments_dir / "transcripts"
        if transcripts_dir.exists():
            txt_files = list(transcripts_dir.glob("*.txt"))
            print_success(f"Generated {len(txt_files)} transcripts")
            return True
        else:
            print_error("Transcripts directory not created")
            return False
            
    except subprocess.CalledProcessError as e:
        print_error(f"Transcription failed: {e}")
        if e.stderr:
            print(f"    Error: {e.stderr}")
        return False


def merge_transcripts(segments_dir: Path) -> bool:
    """
    Merge all segment transcripts into one file
    Uses metadata.json to maintain correct order
    """
    transcripts_dir = segments_dir / "transcripts"
    metadata_file = segments_dir / "metadata.json"
    output_file = segments_dir / "merged_transcript.txt"
    
    print(f"    Merging transcripts...")
    
    # Load metadata to get correct order
    try:
        with metadata_file.open('r', encoding='utf-8') as f:
            metadata = json.load(f)
    except Exception as e:
        print_warning(f"Could not load metadata: {e}")
        print_warning("Will merge in alphabetical order")
        metadata = None
    
    # Collect transcript contents
    merged_parts = []
    
    if metadata and 'segments' in metadata:
        # Use metadata order
        segments = metadata['segments']
        print(f"    Found {len(segments)} segments in metadata")
        
        for seg in segments:
            txt_file = transcripts_dir / f"{Path(seg['filename']).stem}.txt"
            
            if txt_file.exists():
                try:
                    content = txt_file.read_text(encoding='utf-8').strip()
                    if content:
                        # Add separator with timing info
                        start_time = seg.get('start_time', 0)
                        end_time = seg.get('end_time', 0)
                        merged_parts.append(f"=== {seg['filename']} ({start_time:.1f}s - {end_time:.1f}s) ===")
                        merged_parts.append(content)
                        merged_parts.append("")  # blank line
                except Exception as e:
                    print_warning(f"Could not read {txt_file.name}: {e}")
            else:
                print_warning(f"Transcript not found: {txt_file.name}")
    else:
        # Fallback: use alphabetical order
        txt_files = sorted(transcripts_dir.glob("segment_*.txt"))
        print(f"    Found {len(txt_files)} transcript files")
        
        for txt_file in txt_files:
            try:
                content = txt_file.read_text(encoding='utf-8').strip()
                if content:
                    merged_parts.append(f"=== {txt_file.name} ===")
                    merged_parts.append(content)
                    merged_parts.append("")
            except Exception as e:
                print_warning(f"Could not read {txt_file.name}: {e}")
    
    # Write merged file
    if merged_parts:
        try:
            output_file.write_text('\n'.join(merged_parts), encoding='utf-8')
            print_success(f"Merged transcript saved: {output_file.name}")
            
            # Show file size
            size_kb = output_file.stat().st_size / 1024
            print(f"    File size: {size_kb:.1f} KB")
            return True
        except Exception as e:
            print_error(f"Could not write merged file: {e}")
            return False
    else:
        print_error("No transcript content to merge")
        return False


def cleanup_segments(segments_dir: Path) -> bool:
    """
    Remove segment WAV files to save space
    Keeps: transcripts/, metadata.json, merged_transcript.txt
    Removes: segment_*.wav
    """
    print(f"    Cleaning up segment audio files...")
    
    removed_count = 0
    removed_size = 0
    
    for wav_file in segments_dir.glob("segment_*.wav"):
        try:
            size = wav_file.stat().st_size
            wav_file.unlink()
            removed_count += 1
            removed_size += size
        except Exception as e:
            print_warning(f"Could not remove {wav_file.name}: {e}")
    
    if removed_count > 0:
        size_mb = removed_size / (1024 * 1024)
        print_success(f"Removed {removed_count} segment files ({size_mb:.1f} MB)")
        return True
    else:
        print_warning("No segment files to remove")
        return False


def process_audio_file(
    audio_file: Path,
    script_dir: Path,
    file_num: int,
    total_files: int
) -> Dict[str, any]:
    """
    Process a single audio file through the complete pipeline
    Returns a result dictionary
    """
    result = {
        'file': audio_file.name,
        'success': False,
        'steps_completed': [],
        'error': None,
        'start_time': time.time()
    }
    
    print_header(f"[{file_num}/{total_files}] Processing: {audio_file.name}")
    
    # Step 1: Preprocess
    print_step(1, "Preprocessing audio (splitting at silences)")
    segments_dir = run_preprocess(audio_file, script_dir)
    
    if not segments_dir:
        result['error'] = "Preprocessing failed"
        return result
    
    result['steps_completed'].append('preprocess')
    result['segments_dir'] = segments_dir
    
    # Step 2: Transcribe
    print_step(2, "Transcribing segments with whisper.cpp")
    if not run_transcribe(segments_dir, script_dir):
        result['error'] = "Transcription failed"
        return result
    
    result['steps_completed'].append('transcribe')
    
    # Step 3: Merge
    print_step(3, "Merging transcripts")
    if not merge_transcripts(segments_dir):
        result['error'] = "Merge failed"
        return result
    
    result['steps_completed'].append('merge')
    
    # Step 4: Cleanup
    print_step(4, "Cleaning up segment audio files")
    cleanup_segments(segments_dir)
    result['steps_completed'].append('cleanup')
    
    # Success!
    result['success'] = True
    result['duration'] = time.time() - result['start_time']
    
    print_success(f"Completed in {result['duration']:.1f}s")
    print(f"    Output: {segments_dir}/merged_transcript.txt")
    
    return result


def merge_all_transcripts(input_dir: Path, results: List[Dict]) -> Optional[Path]:
    """
    Merge all meeting transcripts into one master file
    Sorted by filename (which includes date/time)
    """
    print_header("Creating Master Transcript")
    
    # Collect all successful results with their merged transcripts
    transcript_data = []
    
    for result in results:
        if not result['success']:
            continue
        
        segments_dir = result.get('segments_dir')
        if not segments_dir:
            continue
        
        merged_file = segments_dir / "merged_transcript.txt"
        if not merged_file.exists():
            print_warning(f"Merged transcript not found for {result['file']}")
            continue
        
        try:
            content = merged_file.read_text(encoding='utf-8').strip()
            if content:
                # Extract meeting name (without extension)
                meeting_name = Path(result['file']).stem
                
                # Try to parse date/time from filename
                # Format: meeting_YYYYMMDD_HHMMSS
                date_str = "Unknown date"
                try:
                    parts = meeting_name.split('_')
                    if len(parts) >= 3:
                        date_part = parts[-2]  # YYYYMMDD
                        time_part = parts[-1]  # HHMMSS
                        
                        # Format as readable date
                        year = date_part[:4]
                        month = date_part[4:6]
                        day = date_part[6:8]
                        hour = time_part[:2]
                        minute = time_part[2:4]
                        second = time_part[4:6]
                        
                        date_str = f"{year}-{month}-{day} {hour}:{minute}:{second}"
                except Exception:
                    pass
                
                transcript_data.append({
                    'filename': result['file'],
                    'meeting_name': meeting_name,
                    'date_str': date_str,
                    'content': content
                })
        except Exception as e:
            print_warning(f"Could not read transcript for {result['file']}: {e}")
    
    if not transcript_data:
        print_error("No transcripts to merge")
        return None
    
    # Sort by filename (which should sort chronologically)
    transcript_data.sort(key=lambda x: x['filename'])
    
    print(f"Merging {len(transcript_data)} meeting transcript(s)...")
    
    # Build master transcript
    master_parts = []
    master_parts.append("=" * 70)
    master_parts.append("MASTER TRANSCRIPT - ALL MEETINGS")
    master_parts.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    master_parts.append(f"Total meetings: {len(transcript_data)}")
    master_parts.append("=" * 70)
    master_parts.append("")
    
    for i, data in enumerate(transcript_data, start=1):
        master_parts.append("")
        master_parts.append("=" * 70)
        master_parts.append(f"Meeting {i}/{len(transcript_data)}: {data['meeting_name']}")
        master_parts.append(f"Date: {data['date_str']}")
        master_parts.append(f"Source file: {data['filename']}")
        master_parts.append("=" * 70)
        master_parts.append("")
        master_parts.append(data['content'])
        master_parts.append("")
    
    # Write master file
    output_file = input_dir / "all_meetings_transcript.txt"
    
    try:
        output_file.write_text('\n'.join(master_parts), encoding='utf-8')
        print_success(f"Master transcript created: {output_file.name}")
        
        # Show file size
        size_kb = output_file.stat().st_size / 1024
        print(f"    File size: {size_kb:.1f} KB")
        print(f"    Location: {output_file}")
        
        return output_file
    except Exception as e:
        print_error(f"Could not write master transcript: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Batch process multilingual meeting recordings through complete pipeline"
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        required=True,
        help="Directory containing audio files to process"
    )
    parser.add_argument(
        "--file-pattern",
        default="*.wav",
        help="File pattern to match (default: *.wav)"
    )
    parser.add_argument(
        "--no-master",
        action="store_true",
        help="Skip creating master transcript file"
    )
    
    args = parser.parse_args()
    
    # Validate input directory
    if not args.input_dir.exists():
        print_error(f"Input directory not found: {args.input_dir}")
        sys.exit(1)
    
    # Get script directory
    script_dir = Path(__file__).parent.resolve()
    
    # Find audio files
    print_header("Scanning for audio files")
    audio_files = find_audio_files(args.input_dir)
    
    if not audio_files:
        print_error(f"No audio files found in {args.input_dir}")
        sys.exit(1)
    
    print(f"Found {len(audio_files)} audio file(s):")
    for f in audio_files:
        print(f"  • {f.name}")
    
    # Confirm
    print()
    response = input(f"Process all {len(audio_files)} files? [y/N]: ").strip().lower()
    if response not in ['y', 'yes']:
        print("Cancelled.")
        sys.exit(0)
    
    # Process all files
    start_time = time.time()
    results = []
    
    for i, audio_file in enumerate(audio_files, start=1):
        result = process_audio_file(audio_file, script_dir, i, len(audio_files))
        results.append(result)
    
    # Final summary
    total_duration = time.time() - start_time
    success_count = sum(1 for r in results if r['success'])
    failed_count = len(results) - success_count
    
    print_header("Pipeline Complete")
    print(f"Total files: {len(results)}")
    print(f"{Colors.GREEN}Success: {success_count}{Colors.END}")
    if failed_count > 0:
        print(f"{Colors.RED}Failed: {failed_count}{Colors.END}")
    print(f"Total time: {total_duration/60:.1f} minutes")
    
    # Show failed files if any
    if failed_count > 0:
        print(f"\n{Colors.RED}Failed files:{Colors.END}")
        for r in results:
            if not r['success']:
                print(f"  • {r['file']}: {r['error']}")
    
    # Show success files
    if success_count > 0:
        print(f"\n{Colors.GREEN}Completed files:{Colors.END}")
        for r in results:
            if r['success']:
                print(f"  • {r['file']} ({r['duration']:.1f}s)")
    
    # Create master transcript
    if not args.no_master and success_count > 0:
        master_file = merge_all_transcripts(args.input_dir, results)
        if master_file:
            print(f"\n{Colors.BOLD}📄 Master transcript available:{Colors.END}")
            print(f"    {master_file}")
    
    print(f"\n{Colors.GREEN}✓ All done!{Colors.END}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}[INTERRUPTED]{Colors.END} Pipeline cancelled.")
        sys.exit(130)