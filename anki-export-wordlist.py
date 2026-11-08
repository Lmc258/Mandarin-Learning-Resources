#!/usr/bin/env python3
"""
anki-export-wordlist.py - export everything you've studied in Anki to a
plain-text word list (e.g. to upload to an AI tutor each week).

Reads the three decks the other anki-mb-*.py tools write to:

  Characters  'Hanzi Movie Method' notes   (anki-mb-characters.py)
  Words       'MB Word' notes              (anki-mb-words.py)
  Sentences   'MB Sentence' notes          (anki-mb-sentences.py)

Only notes with at least one studied card are exported: new and suspended
cards are skipped. HTML and [sound:...] tags are stripped and duplicates
dropped. Lines look like:

  你好 (nǐ hǎo) - hello

(sentence notes have no Pinyin field, so sentence lines are just
"Hanzi - English").

PRIVACY: the list is always written OUTSIDE this repo, to
~/Documents/mandarin-wordlist.txt (overwritten each run), because the repo
is public. .gitignore also ignores mandarin-wordlist*.txt as a safety net.

Usage
-----
  python3 anki-export-wordlist.py

Needs Anki running with the AnkiConnect add-on. Deck names come from the
shared settings.json the other tools save (if reachable), otherwise the
same defaults they use. No third-party packages. Python 3.8+.
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.request

ANKI_CONNECT_URL = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".mb_anki")
APP_CONFIG_PATH = os.path.join(CACHE_DIR, "app_config.json")

# Note types and default decks, as defined in the anki-mb-*.py scripts.
CHAR_MODEL = "Hanzi Movie Method"
WORD_MODEL = "MB Word"
SENTENCE_MODEL = "MB Sentence"
DEFAULT_CHAR_DECK = "Hanzi Movie Method"
DEFAULT_WORDS_DECK = "Hanzi Movie Method Words"
DEFAULT_SENTENCE_DECK = "Hanzi Movie Method Sentences"

OUTPUT_PATH = os.path.join(os.path.expanduser("~"), "Documents", "mandarin-wordlist.txt")

HANZI_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
SOUND_RE = re.compile(r"\[sound:[^\]]*\]")
TAG_RE = re.compile(r"<[^>]+>")
BREAK_RE = re.compile(r"<\s*(br|/div|/p|/li|/h\d)\s*/?>", re.IGNORECASE)
KEYWORD_RE = re.compile(r"^\s*Keyword:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
CROSSREF_RE = re.compile(r"^(old |archaic |)(variant of|see |also written|used in|abbr\. for|surname)",
                         re.IGNORECASE)
USAGE_RE =re.compile(r'Usage\s*\d*\s*[-:]?\s*"([^"]+)"', re.IGNORECASE)


# --------------------------------------------------------------------------
# AnkiConnect (same as the anki-mb-*.py scripts)
# --------------------------------------------------------------------------

def invoke(action, **params):
    payload = json.dumps({"action": action, "version": 6, "params": params}).encode("utf-8")
    req = urllib.request.Request(
        ANKI_CONNECT_URL, payload, {"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.load(resp)
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"Could not reach AnkiConnect at {ANKI_CONNECT_URL} ({exc}).\n"
            "Is Anki running with the AnkiConnect add-on installed?"
        )
    if body.get("error"):
        raise RuntimeError(f"AnkiConnect error on '{action}': {body['error']}")
    return body["result"]


# --------------------------------------------------------------------------
# Settings (read-only; never prompts)
# --------------------------------------------------------------------------

def load_settings():
    """The deck choices the other tools saved, or {} if there's no saved
    config directory yet (or it isn't reachable right now)."""
    cfg = os.environ.get("MB_CONFIG")
    if not cfg:
        try:
            with open(APP_CONFIG_PATH, encoding="utf-8") as fh:
                cfg = json.load(fh).get("config_dir")
        except Exception:  # noqa: BLE001
            cfg = None
    if not cfg:
        return {}
    try:
        with open(os.path.join(cfg, "settings.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------
# Cleaning
# --------------------------------------------------------------------------

def clean(raw: str) -> str:
    """Field HTML -> one line of plain text (line breaks become newlines
    first so callers can still split on them)."""
    text = SOUND_RE.sub("", raw or "")
    text = BREAK_RE.sub("\n", text)
    text = TAG_RE.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def one_line(text: str) -> str:
    return " ".join(text.split())


def char_meaning(fields) -> str:
    """Short English for a character: its Keyword if it has one, otherwise
    the first few CC-CEDICT senses from Meaning."""
    keyword = clean(fields.get("Keyword", ""))
    meaning = clean(fields.get("Meaning", ""))
    if not keyword:
        m = KEYWORD_RE.search(meaning)
        if m:
            keyword = m.group(1)
    if keyword:
        return one_line(keyword)
    meaning = KEYWORD_RE.sub("", meaning)
    senses = []
    for s in re.split(r"[;\n]", meaning):
        # Drop CEDICT asides like "(Taiwan pr. [han4])" and inline "[nin2]".
        s = re.sub(r"\s*\([^()]*\bpr\.[^()]*\)", "", s)
        s = one_line(re.sub(r"\[[^\]]*\]", "", s))
        if s and not CROSSREF_RE.match(s):
            senses.append(s)
    return "; ".join(senses[:3])


def word_meaning(fields) -> str:
    usages = fields.get("Usages", "")
    found = USAGE_RE.findall(html.unescape(TAG_RE.sub(" ", usages)))
    return "; ".join(one_line(u) for u in found) or one_line(clean(usages))


def fmt(hanzi, pinyin, english):
    line = hanzi
    if pinyin:
        line += f" ({pinyin})"
    if english:
        line += f" - {english}"
    return line


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def studied_notes(model, deck):
    """Notes of this type in this deck (incl. subdecks, except the
    '::legacy-migrated' ones anki-mb-words.py parks old cards in) with at
    least one card that is neither new nor suspended. Oldest first."""
    query = (f'note:"{model}" deck:"{deck}" -deck:"{deck}::*legacy-migrated*" '
             "-is:new -is:suspended")
    ids = sorted(invoke("findNotes", query=query))
    if not ids:
        return []
    notes = invoke("notesInfo", notes=ids)
    return [{k: v["value"] for k, v in n["fields"].items()} for n in notes]


def main():
    settings = load_settings()
    char_deck = settings.get("deck", DEFAULT_CHAR_DECK)
    words_deck = settings.get("words_deck", DEFAULT_WORDS_DECK)
    sentence_deck = settings.get("sentence_deck", DEFAULT_SENTENCE_DECK)

    chars, words, sentences = {}, {}, {}   # hanzi -> line (dicts dedupe, keep order)

    for f in studied_notes(CHAR_MODEL, char_deck):
        hanzi = one_line(clean(f.get("Hanzi", "")))
        if not HANZI_RE.search(hanzi):
            continue
        # A few multi-character words live in the character deck; list them
        # as words.
        target = chars if len(HANZI_RE.findall(hanzi)) == 1 else words
        target.setdefault(hanzi, fmt(hanzi, one_line(clean(f.get("Pinyin", ""))),
                                     char_meaning(f)))

    for f in studied_notes(WORD_MODEL, words_deck):
        hanzi = one_line(clean(f.get("Word", "")))
        if not HANZI_RE.search(hanzi):
            continue
        words.setdefault(hanzi, fmt(hanzi, one_line(clean(f.get("Pinyin", ""))),
                                    word_meaning(f)))

    for f in studied_notes(SENTENCE_MODEL, sentence_deck):
        hanzi = one_line(clean(f.get("Sentence", "")))
        if not HANZI_RE.search(hanzi):
            continue
        sentences.setdefault(hanzi, fmt(hanzi, "", one_line(clean(f.get("Translation", "")))))

    sections = [("Characters", chars), ("Words", words), ("Sentences", sentences)]
    out = []
    for title, items in sections:
        out.append(f"{title} ({len(items)})")
        out.extend(items.values())
        out.append("")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    tmp = OUTPUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    os.replace(tmp, OUTPUT_PATH)

    print(f"Wrote {OUTPUT_PATH}")
    for title, items in sections:
        print(f"  {title:<11}{len(items)}")


if __name__ == "__main__":
    main()
