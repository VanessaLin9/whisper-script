#!/usr/bin/env python3
# clean_transcript.py
# Clean up transcripts: remove markers, hallucinations, and excessive fillers

from __future__ import annotations
import re
import sys
import argparse
from pathlib import Path
from typing import List


def remove_metadata_lines(text: str) -> str:
    """
    Remove transcript metadata and markers
    - === segment_xxx.wav (time) ===
    - ======== lines
    - Meeting X/Y: ...
    - Date: ...
    - Source file: ...
    """
    lines = text.split('\n')
    cleaned = []
    
    for line in lines:
        # Skip separator lines
        if re.match(r'^=+$', line.strip()):
            continue
        
        # Skip segment markers
        if re.match(r'^===.*\.wav.*===\s*$', line.strip()):
            continue
        
        # Skip meeting metadata
        if re.match(r'^(Meeting|Date|Source file|Generated|Total):', line.strip()):
            continue
        
        # Skip master transcript header
        if 'MASTER TRANSCRIPT' in line or 'ALL MEETINGS' in line:
            continue
        
        cleaned.append(line)
    
    return '\n'.join(cleaned)


def remove_inaudible_markers(text: str) -> str:
    """
    Remove markers for inaudible content
    - [INAUDIBLE]
    - (murmullos)
    - [Música]
    - (background noise)
    etc.
    """
    # Remove [INAUDIBLE]
    text = re.sub(r'\[INAUDIBLE\]', '', text, flags=re.IGNORECASE)
    
    # Remove (murmullos), (music), [Música], etc.
    text = re.sub(r'\[Música\]', '', text)
    text = re.sub(r'\(murmullos\)', '', text)
    text = re.sub(r'\(music\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\(background noise\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[background noise\]', '', text, flags=re.IGNORECASE)
    
    return text


def remove_spam_lines(text: str) -> str:
    """
    Remove spam lines (YouTube subscribe messages, etc.)
    """
    lines = text.split('\n')
    cleaned = []
    
    spam_keywords = [
        'subscribe',
        'đăng ký',
        'theo dõi',
        'kênh',
        'video hấp dẫn',
        'ghiền mì gõ',
        'like and subscribe',
        'hit the bell',
    ]
    
    for line in lines:
        line_lower = line.lower()
        if any(keyword in line_lower for keyword in spam_keywords):
            continue
        cleaned.append(line)
    
    return '\n'.join(cleaned)


def remove_repetitive_hallucinations(text: str) -> str:
    """
    Remove obvious hallucinations where the same phrase repeats many times
    Example: "I don't know if you can see, but I don't know if you can see, but..."
    """
    lines = text.split('\n')
    cleaned = []
    
    for line in lines:
        # Skip if line is too long and contains repetitive patterns
        if len(line) > 200:
            # Check for phrases that repeat 3+ times
            words = line.split()
            
            # Look for patterns like "phrase, but phrase, but phrase"
            # Split by common connectors
            segments = re.split(r',\s*but\s+|,\s*and\s+|\.\s+', line)
            
            if len(segments) > 3:
                # Check if segments are very similar
                first_seg = segments[0].strip().lower()
                similar_count = sum(1 for seg in segments if seg.strip().lower() == first_seg)
                
                # If >50% are identical, it's probably a hallucination
                if similar_count > len(segments) / 2:
                    continue
        
        cleaned.append(line)
    
    return '\n'.join(cleaned)


def remove_excessive_fillers(text: str) -> str:
    """
    Remove obvious filler words (moderate approach)
    Only remove very obvious ones: um..., uh..., well...
    Keep natural speech patterns
    """
    # Remove standalone um, uh at start of lines
    text = re.sub(r'^\s*(um|uh|er|ah)\.\.+\s*', '', text, flags=re.IGNORECASE | re.MULTILINE)
    
    # Remove um... uh... in middle of sentences (with ellipsis)
    text = re.sub(r'\s+(um|uh|er|ah)\.\.+\s+', ' ', text, flags=re.IGNORECASE)
    
    # Remove repeated single letters (I I I, a a a)
    text = re.sub(r'\b([a-zA-Z])\s+\1\s+\1\b', r'\1', text)
    
    return text


def normalize_whitespace(text: str) -> str:
    """
    Clean up excessive whitespace and blank lines
    """
    # Remove lines that are only whitespace
    lines = [line.rstrip() for line in text.split('\n')]
    
    # Remove consecutive blank lines (keep max 1)
    cleaned = []
    prev_blank = False
    
    for line in lines:
        is_blank = len(line.strip()) == 0
        
        if is_blank:
            if not prev_blank:
                cleaned.append('')
            prev_blank = True
        else:
            cleaned.append(line)
            prev_blank = False
    
    # Join and clean up spaces
    text = '\n'.join(cleaned)
    
    # Remove multiple spaces
    text = re.sub(r' +', ' ', text)
    
    # Clean up spaces before punctuation
    text = re.sub(r' +([.,!?;:])', r'\1', text)
    
    return text.strip()


def add_sentence_breaks(text: str) -> str:
    """
    Add line breaks after sentences (. ? !)
    Keep dialogue format (one line per utterance)
    """
    lines = text.split('\n')
    result = []
    
    for line in lines:
        line = line.strip()
        if not line:
            result.append('')
            continue
        
        # Split by sentence endings, but keep the punctuation
        sentences = re.split(r'([.!?]+\s+)', line)
        
        # Rejoin sentences, adding line breaks
        current = ''
        for i, part in enumerate(sentences):
            if not part.strip():
                continue
            
            current += part
            
            # If this is punctuation + space, add line break after
            if re.match(r'[.!?]+\s+', part):
                if current.strip():
                    result.append(current.strip())
                current = ''
        
        # Add any remaining text
        if current.strip():
            result.append(current.strip())
    
    return '\n'.join(result)


def clean_transcript(input_text: str, aggressive: bool = False) -> str:
    """
    Main cleaning pipeline
    
    Args:
        input_text: Raw transcript text
        aggressive: If True, more aggressive cleaning (not recommended)
    
    Returns:
        Cleaned transcript
    """
    text = input_text
    
    # Step 1: Remove metadata and markers
    text = remove_metadata_lines(text)
    
    # Step 2: Remove inaudible markers
    text = remove_inaudible_markers(text)
    
    # Step 3: Remove spam lines
    text = remove_spam_lines(text)
    
    # Step 4: Remove hallucinations
    text = remove_repetitive_hallucinations(text)
    
    # Step 5: Remove excessive fillers (moderate)
    text = remove_excessive_fillers(text)
    
    # Step 6: Normalize whitespace
    text = normalize_whitespace(text)
    
    # Step 7: Add sentence breaks
    text = add_sentence_breaks(text)
    
    # Step 8: Final whitespace cleanup
    text = normalize_whitespace(text)
    
    return text


def process_file(input_file: Path, output_file: Path = None, aggressive: bool = False) -> bool:
    """
    Process a transcript file
    
    Args:
        input_file: Input transcript file
        output_file: Output file (if None, will add _cleaned suffix)
        aggressive: Use aggressive cleaning
    
    Returns:
        True if successful
    """
    try:
        # Read input
        input_text = input_file.read_text(encoding='utf-8')
        
        # Clean
        cleaned_text = clean_transcript(input_text, aggressive=aggressive)
        
        # Determine output path
        if output_file is None:
            output_file = input_file.parent / f"{input_file.stem}_cleaned{input_file.suffix}"
        
        # Write output
        output_file.write_text(cleaned_text, encoding='utf-8')
        
        # Show stats
        input_lines = len([l for l in input_text.split('\n') if l.strip()])
        output_lines = len([l for l in cleaned_text.split('\n') if l.strip()])
        input_size = len(input_text)
        output_size = len(cleaned_text)
        
        print(f"✓ Cleaned: {input_file.name}")
        print(f"  Input:  {input_lines} lines, {input_size:,} characters")
        print(f"  Output: {output_lines} lines, {output_size:,} characters")
        print(f"  Removed: {input_lines - output_lines} lines, {input_size - output_size:,} characters")
        print(f"  Saved to: {output_file}")
        
        return True
        
    except Exception as e:
        print(f"✗ Error processing {input_file.name}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Clean up meeting transcripts (remove markers, hallucinations, fillers)"
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Input transcript file (.txt)"
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="Output file (default: input_cleaned.txt)"
    )
    parser.add_argument(
        "--aggressive",
        action="store_true",
        help="Use aggressive cleaning (removes more, may lose content)"
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Preview cleaning without saving (shows first 1000 chars)"
    )
    
    args = parser.parse_args()
    
    # Validate input
    if not args.input.exists():
        print(f"✗ Error: File not found: {args.input}")
        sys.exit(1)
    
    if not args.input.suffix.lower() in ['.txt', '.text']:
        print(f"⚠ Warning: Expected .txt file, got {args.input.suffix}")
    
    # Preview mode
    if args.preview:
        input_text = args.input.read_text(encoding='utf-8')
        cleaned_text = clean_transcript(input_text, aggressive=args.aggressive)
        
        print("=" * 60)
        print("PREVIEW (first 1000 characters)")
        print("=" * 60)
        print(cleaned_text[:1000])
        if len(cleaned_text) > 1000:
            print("\n... (truncated)")
        print("=" * 60)
        print(f"\nTo save: python {sys.argv[0]} {args.input} -o output.txt")
        return
    
    # Process file
    success = process_file(args.input, args.output, aggressive=args.aggressive)
    
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INTERRUPTED] Cancelled.")
        sys.exit(130)