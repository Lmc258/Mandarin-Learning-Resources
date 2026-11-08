#!/usr/bin/env python3
"""
Add an English translation (spoken) before each Chinese sentence in an mp3.

Result per sentence:  [English] -> short pause -> [Chinese] -> original pause

Setup (once):
    brew install ffmpeg        # or: sudo apt install ffmpeg
    pip install pydub faster-whisper edge-tts

Usage:
    python add_english.py input.mp3 [more.mp3 ...]
    python add_english.py *.mp3 --model large-v3
    python add_english.py input.mp3 --dry-run     # only count sentences

Outputs next to each input:
    <name>_with_english.mp3
    <name>_transcript.txt   (Chinese + English, one pair per line, to proof-read)
"""
import argparse
import asyncio
import os
import tempfile

import edge_tts
from faster_whisper import WhisperModel
from pydub import AudioSegment
from pydub.silence import detect_nonsilent


def split_sentences(audio, min_silence_ms, thresh_db_below_avg, pad_ms):
    """Return [(chunk, pause_after_ms), ...] using silence detection."""
    thresh = audio.dBFS - thresh_db_below_avg
    spans = detect_nonsilent(audio, min_silence_len=min_silence_ms,
                             silence_thresh=thresh, seek_step=10)
    out = []
    for i, (s, e) in enumerate(spans):
        s2, e2 = max(0, s - pad_ms), min(len(audio), e + pad_ms)
        next_start = spans[i + 1][0] if i + 1 < len(spans) else len(audio)
        out.append((audio[s2:e2], max(0, next_start - e)))
    return out


def transcribe(model, chunk, task):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
    chunk.export(path, format="wav")
    try:
        segs, _ = model.transcribe(path, language="zh", task=task,
                                   beam_size=5, vad_filter=False)
        return " ".join(s.text.strip() for s in segs).strip()
    finally:
        os.remove(path)


async def tts(text, voice, rate, path):
    await edge_tts.Communicate(text, voice, rate=rate).save(path)


def speak(text, voice, rate):
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        path = f.name
    asyncio.run(tts(text, voice, rate, path))
    seg = AudioSegment.from_mp3(path)
    os.remove(path)
    return seg


def process(path, args, model):
    print(f"\n=== {os.path.basename(path)}")
    audio = AudioSegment.from_file(path)
    items = split_sentences(audio, args.min_silence, args.thresh, args.pad)
    print(f"Found {len(items)} sentences")
    if args.dry_run:
        return

    base = os.path.splitext(path)[0]
    result = AudioSegment.silent(500)
    lines = []
    gap = AudioSegment.silent(args.gap_after_english)

    for n, (chunk, pause) in enumerate(items, 1):
        zh = transcribe(model, chunk, "transcribe")
        en = transcribe(model, chunk, "translate")
        print(f"{n:3d}. {zh}\n     {en}")
        lines.append(f"{n}\t{zh}\t{en}")

        if en:
            result += speak(en, args.voice, args.rate) + gap
        result += chunk + AudioSegment.silent(max(pause, args.min_pause))

    result.export(base + "_with_english.mp3", format="mp3", bitrate="128k")
    with open(base + "_transcript.txt", "w", encoding="utf-8") as f:
        f.write("#\tChinese\tEnglish\n" + "\n".join(lines) + "\n")
    print(f"Saved {base}_with_english.mp3")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("files", nargs="+")
    p.add_argument("--model", default="medium",
                   help="tiny/base/small/medium/large-v3 (bigger = more accurate, slower)")
    p.add_argument("--voice", default="en-US-AriaNeural")
    p.add_argument("--rate", default="+0%", help='speech speed, e.g. "-10%%"')
    p.add_argument("--min-silence", type=int, default=500,
                   help="ms of silence that counts as a pause between sentences")
    p.add_argument("--thresh", type=float, default=16,
                   help="dB below average loudness treated as silence")
    p.add_argument("--pad", type=int, default=120, help="ms padding kept around each sentence")
    p.add_argument("--gap-after-english", type=int, default=600, help="ms between English and Chinese")
    p.add_argument("--min-pause", type=int, default=1000, help="minimum ms pause after Chinese")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    model = None if args.dry_run else WhisperModel(args.model, compute_type="int8")
    for f in args.files:
        process(f, args, model)


if __name__ == "__main__":
    main()
