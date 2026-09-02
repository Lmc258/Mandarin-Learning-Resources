#!/usr/bin/env python3
"""
anki-mb-words.py - build multi-character word cards from the terminal.

Companion to anki-mb-addon.py (single characters) and anki-mb-sentences.py
(whole sentences) - this one is for words made of two or more characters, so
they get their own deck instead of being mixed in with single hanzi.

Paste a Mandarin Blueprint-style word block:

    打电话

    Usage 1 - "to make a phone call"

    Living Link Suggestion(s):

        You know the song "Hit the Phone, Jack" right? Bonus points if you
        actually know what a phone jack is :)
        Use your finger to dial the phone keypad and MAKE A PHONE CALL.

    .

(a lone '.' ends the paste) and it becomes one 'MB Word' note with two
cards: word -> meaning and meaning -> word. Pinyin is looked up from
CC-CEDICT automatically; usage(s) and living link(s) are optional and can
also just be typed by hand if you don't have a block to paste.

Setup (once)
------------
  1. Install the AnkiConnect add-on in Anki (Tools > Add-ons > Get Add-ons,
     code 2055492159), restart Anki, and leave it running.
  2. The first time you run any command, you'll be asked where to save your
     files (settings, history, CC-CEDICT) - the same save directory
     anki-mb-addon.py/anki-mb-sentences.py use, if you've set one up there.
  3. python3 anki-mb-words.py
     Asks which deck word cards go to. 'MB Word' is created (or repaired,
     if you've since edited the template HTML here) automatically.

  If you already have multi-character words sitting in your single-character
  deck, or in an old bulk-imported deck, run this once to bring them over:
     python3 anki-mb-words.py migrate --dry-run   # preview first
     python3 anki-mb-words.py migrate             # then actually do it
  Old notes are never edited or deleted - only their *cards* are moved to a
  '<deck>::legacy-migrated' subdeck, so review history/scheduling survives
  untouched and undoing it is just dragging them back.

Daily use
---------
  python3 anki-mb-words.py

    > paste a word block, then a lone '.' to finish it
      (or just the word, then answer the prompts by hand)

No third-party packages. Python 3.8+.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import html
import json
import os
import re
import socket
import sys
import urllib.parse
import urllib.request

ANKI_CONNECT_URL = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".mb_anki")
APP_CONFIG_PATH = os.path.join(CACHE_DIR, "app_config.json")
CEDICT_CACHE = os.path.join(CACHE_DIR, "cedict.txt")
CEDICT_URL = "https://www.mdbg.net/chinese/export/cedict/cedict_1_0_ts_utf-8_mdbg.txt.gz"

WORD_MODEL = "MB Word"
NEW_WORDS_DECK = "Hanzi Movie Method Words"
LEGACY_CHAR_DECK = "Hanzi Movie Method"                          # this tool's single-char deck
LEGACY_IMPORT_DECK = "mandarin-blueprint-imports-word&characters"  # old bulk import

HANZI_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")

# 'Word' is the sort field (first field) - duplicate detection keys on the
# word text, same idea as 'Hanzi' in anki-mb-addon.py / 'Sentence' in
# anki-mb-sentences.py.
WORD_FIELDS = ["Word", "Pinyin", "Usages", "LivingLinks", "Examples", "Audio"]

WORD_CSS = """\
.card {
  font-family: arial;
  text-align: center;
  color: black;
  background-color: white;
}
.word { font-size: 44px; margin-bottom: 6px; }
.pinyin { font-size: 20px; color: #333; margin-top: 4px; margin-bottom: 10px; }
.usages { font-size: 17px; margin-top: 8px; text-align: left; display: inline-block; }
.usages .usage { margin: 4px 0; }
.living-links { font-size: 15px; color: #666; margin-top: 16px; text-align: left; display: inline-block; }
.living-links .heading {
  font-size: 12px; text-transform: uppercase; letter-spacing: .05em;
  color: #999; margin-bottom: 6px;
}
.living-links .link { margin: 5px 0; }
.examples { font-size: 13px; color: #888; margin-top: 14px; }

/* See anki-mb-addon.py's WORD_CSS for why this is needed: Anki's night mode
   only re-colours .card itself, not these custom classes, so their greys
   (tuned for a white background) would otherwise wash out in the dark. */
.card.night_mode { background-color: #2f2f31; color: #e8e8e8; }
.night_mode .pinyin { color: #e0e0e0; }
.night_mode .living-links { color: #bfbfbf; }
.night_mode .living-links .heading { color: #9e9e9e; }
.night_mode .examples { color: #c0c0c0; }
"""

# Audio is only on the Back of both directions, same reasoning as the
# sentence deck: hearing it read aloud on the Meaning->Word front would give
# away the very thing you're trying to recall.
WORD_TEMPLATES = {
    "Word to Meaning": {
        "Front": '<div class="word">{{Word}}</div>',
        "Back": '{{FrontSide}}<hr id=answer><div class="pinyin">{{Pinyin}}</div>'
                '<div class="usages">{{Usages}}</div>'
                '{{Audio}}'
                '{{#LivingLinks}}<div class="living-links"><div class="heading">living link</div>'
                '{{LivingLinks}}</div>{{/LivingLinks}}'
                '{{#Examples}}<div class="examples">{{Examples}}</div>{{/Examples}}',
    },
    "Meaning to Word": {
        "Front": '<div class="usages">{{Usages}}</div>'
                 '{{#LivingLinks}}<div class="living-links"><div class="heading">living link</div>'
                 '{{LivingLinks}}</div>{{/LivingLinks}}',
        "Back": '{{FrontSide}}<hr id=answer><div class="word">{{Word}}</div>'
                '<div class="pinyin">{{Pinyin}}</div>'
                '{{Audio}}'
                '{{#Examples}}<div class="examples">{{Examples}}</div>{{/Examples}}',
    },
}


# --------------------------------------------------------------------------
# AnkiConnect
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


def fetch_audio(text: str) -> bytes | None:
    """Google Translate TTS. Free, no key, but be gentle with it."""
    url = "https://translate.google.com/translate_tts?" + urllib.parse.urlencode(
        {"ie": "UTF-8", "q": text, "tl": "zh-CN", "client": "tw-ob"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"  ! audio failed: {exc}\n")
        return None


# --------------------------------------------------------------------------
# Pinyin (numbered -> marks)
# --------------------------------------------------------------------------

TONE_MARKS = {
    "a": "āáǎàa", "e": "ēéěèe", "i": "īíǐìi", "o": "ōóǒòo",
    "u": "ūúǔùu", "ü": "ǖǘǚǜü",
    "A": "ĀÁǍÀA", "E": "ĒÉĚÈE", "I": "ĪÍǏÌI", "O": "ŌÓǑÒO",
    "U": "ŪÚǓÙU", "Ü": "ǕǗǙǛÜ",
}


def syllable_to_marks(syl: str) -> str:
    """Convert one numbered CEDICT syllable ('hao3', 'nu:3') to tone marks."""
    syl = syl.replace("u:", "ü").replace("U:", "Ü").replace("v", "ü")
    m = re.match(r"^(.*?)([1-5])$", syl)
    if not m:
        return syl
    body, tone = m.group(1), int(m.group(2))
    if tone == 5:
        return body
    idx = -1
    for vowel in ("a", "o", "e", "A", "O", "E"):
        if vowel in body:
            idx = body.index(vowel)
            break
    if idx == -1:
        for pair in ("iu", "ui", "iü"):
            if pair in body.lower():
                idx = body.lower().index(pair) + 1
                break
    if idx == -1:
        for i, ch in enumerate(body):
            if ch.lower() in "aeiouü":
                idx = i
                break
    if idx == -1:
        return body
    ch = body[idx]
    table = TONE_MARKS.get(ch)
    if not table:
        return body
    return body[:idx] + table[tone - 1] + body[idx + 1:]


def numbered_to_marks(pinyin: str) -> str:
    return " ".join(syllable_to_marks(s) for s in pinyin.split())


# --------------------------------------------------------------------------
# CC-CEDICT (for pinyin lookups only - no scaffolding/meaning guessing here,
# usage/meaning is always yours to paste or type)
# --------------------------------------------------------------------------

def download(url, timeout=180, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "mb_anki/1.0"})
    real_getaddrinfo = socket.getaddrinfo

    def ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return real_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    last_exc = None
    socket.getaddrinfo = ipv4_getaddrinfo
    try:
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read()
            except (urllib.error.URLError, OSError) as exc:
                last_exc = exc
                if attempt < retries - 1:
                    sys.stderr.write(f"  (retrying after: {exc})\n")
    finally:
        socket.getaddrinfo = real_getaddrinfo
    raise SystemExit(f"Couldn't download {url} ({last_exc}). Check your connection and try again.")


def ensure_cedict(path: str | None) -> str:
    if path:
        return path
    if os.path.exists(CEDICT_CACHE):
        return CEDICT_CACHE
    os.makedirs(CACHE_DIR, exist_ok=True)
    sys.stderr.write("Downloading CC-CEDICT (one time, ~4 MB)...\n")
    raw = gzip.decompress(download(CEDICT_URL))
    with open(CEDICT_CACHE, "wb") as fh:
        fh.write(raw)
    return CEDICT_CACHE


CEDICT_LINE = re.compile(r"^(\S+)\s+(\S+)\s+\[([^\]]*)\]\s+/(.*)/\s*$")


def load_cedict(path: str):
    entries: dict[str, list[dict]] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            m = CEDICT_LINE.match(line.rstrip("\n"))
            if not m:
                continue
            trad, simp, pin, defs = m.groups()
            glosses = [d.strip() for d in defs.split("/") if d.strip()]
            glosses = [g for g in glosses
                       if not re.match(r"^(CL:|Taiwan pr\.|also pr\.|abbr\. for|\(Tw\)|Tai-lo pr\.)", g)
                       and "transcribe the Taiwanese" not in g]
            if not glosses:
                continue
            entries.setdefault(simp, []).append({"trad": trad, "pinyin": pin, "glosses": glosses})
    return entries


def lookup(entries, word: str):
    cands = entries.get(word)
    if not cands:
        return None
    def score(e):
        real = [g for g in e["glosses"]
                if not re.match(r"^(variant of|old variant|surname|see [A-Za-z])", g)]
        return (len(real), len(e["glosses"]))
    return max(cands, key=score)


# --------------------------------------------------------------------------
# Note type
# --------------------------------------------------------------------------

def ensure_word_model():
    """Create the 'MB Word' note type, or bring an existing one in line with
    the fields/templates/styling defined here. Never touches existing
    notes - only the model definition."""
    if WORD_MODEL not in invoke("modelNames"):
        invoke("createModel", modelName=WORD_MODEL, inOrderFields=WORD_FIELDS,
               css=WORD_CSS, cardTemplates=[
                   {"Name": name, **tpl} for name, tpl in WORD_TEMPLATES.items()
               ])
        print(f"created note type '{WORD_MODEL}' ({len(WORD_FIELDS)} fields, "
              f"{len(WORD_TEMPLATES)} card types)")
        return

    fields = invoke("modelFieldNames", modelName=WORD_MODEL)
    missing = [f for f in WORD_FIELDS if f not in fields]
    for f in missing:
        invoke("modelFieldAdd", modelName=WORD_MODEL, fieldName=f)

    current = invoke("modelTemplates", modelName=WORD_MODEL)
    added, changed, to_update = [], [], {}
    for name, tpl in WORD_TEMPLATES.items():
        if name not in current:
            invoke("modelTemplateAdd", modelName=WORD_MODEL, template={"Name": name, **tpl})
            added.append(name)
        elif current[name].get("Front") != tpl["Front"] or current[name].get("Back") != tpl["Back"]:
            to_update[name] = tpl
            changed.append(name)
    if to_update:
        invoke("updateModelTemplates", model={"name": WORD_MODEL, "templates": to_update})

    restyled = invoke("modelStyling", modelName=WORD_MODEL)["css"] != WORD_CSS
    if restyled:
        invoke("updateModelStyling", model={"name": WORD_MODEL, "css": WORD_CSS})

    bits = []
    if missing:
        bits.append(f"added missing field(s): {', '.join(missing)}")
    if added:
        bits.append(f"added card type(s): {', '.join(added)}")
    if changed:
        bits.append(f"updated card type(s): {', '.join(changed)}")
    if restyled:
        bits.append("updated styling")
    print(f"using note type '{WORD_MODEL}'" + (f" ({'; '.join(bits)})" if bits else ""))


# --------------------------------------------------------------------------
# Interactive shell
# --------------------------------------------------------------------------

class Cancel(Exception):
    """Abandon the card being built, keep the session."""


class Quit(Exception):
    """Leave the session."""


def _colour():
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return {k: "" for k in ("dim", "bold", "red", "green", "yellow", "off")}
    return {"dim": "\033[2m", "bold": "\033[1m", "red": "\033[31m",
            "green": "\033[32m", "yellow": "\033[33m", "off": "\033[0m"}


C = _colour()


def ask(text, default=None, cancel=True):
    try:
        val = input(text).strip()
    except EOFError:
        print()
        raise Quit
    except KeyboardInterrupt:
        print()
        raise Cancel
    if cancel and val.lower() in ("q", ":q", "quit"):
        raise Cancel
    return val or (default or "")


def yes(text, default=True):
    hint = "[Y/n]" if default else "[y/N]"
    val = ask(f"{text} {hint} ").lower()
    if not val:
        return default
    return val.startswith("y")


def choose(title, options, current=None):
    if current in options:
        return current
    print(f"\n{title}")
    for i, opt in enumerate(options, 1):
        print(f"  {i:>2}. {opt}")
    while True:
        val = ask("  number: ", cancel=False)
        if val.isdigit() and 1 <= int(val) <= len(options):
            return options[int(val) - 1]
        print(f"  {C['red']}Pick a number between 1 and {len(options)}.{C['off']}")


SHELL_HELP = """
  <word>      paste a word block: the word on the first line, then
              'Usage N - ...' lines and/or a 'Living Link Suggestion(s):'
              section, then a lone '.' on its own line to finish it
              (or just type the word and answer the prompts by hand)
  deck        change the target deck
  where       print the deck this session is using
  ?           this help
  q           quit

  At any prompt: enter accepts the default, q abandons the card, ctrl-d quits.
"""

USAGE_RE = re.compile(r"^usage\s*\d*\s*[-:]?\s*", re.IGNORECASE)
LINKS_HEADER_RE = re.compile(r"^living link suggestions?\s*:?\s*$", re.IGNORECASE)


def parse_block(lines):
    """Parse a pasted Mandarin Blueprint style word block: the word on its
    own first line, then 'Usage N - ...' line(s), then an optional
    'Living Link Suggestion(s):' header followed by one suggestion per line.
    Tolerant of the exact wording/spacing but not a strict grammar - a block
    that doesn't match the shape just falls back to treating stray lines as
    more usage text, so nothing pasted is silently dropped."""
    lines = [l.rstrip() for l in lines]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return None

    word = re.sub(r"\s+", "", lines[0].strip())
    usages, links = [], []
    section = None
    for line in lines[1:]:
        s = line.strip()
        if not s:
            continue
        if LINKS_HEADER_RE.match(s):
            section = "links"
            continue
        if USAGE_RE.match(s):
            usages.append(s)
            section = "usages"
            continue
        if section == "links":
            links.append(s)
        elif usages:
            usages[-1] += " " + s
        else:
            usages.append(s)
    return {"word": word, "usages": usages, "links": links}


def read_block():
    """Keep reading pasted lines until a lone '.' ends the block. ctrl-d
    quits the whole session, same as everywhere else in this shell."""
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            print()
            raise Quit
        except KeyboardInterrupt:
            print()
            raise Cancel
        if line.strip() == ".":
            break
        lines.append(line)
    return lines


def build_one(block, deck, entries):
    word = block["word"]
    if not HANZI_RE.search(word):
        print(f"  {C['red']}'{word}' doesn't look like Chinese.{C['off']}")
        raise Cancel

    entry = lookup(entries, word)
    default_pinyin = numbered_to_marks(entry["pinyin"]) if entry else ""
    pinyin = ask("  pinyin? ", default=default_pinyin)

    usages = list(block["usages"])
    if not usages:
        suggested = f'Usage 1 - "{"; ".join(entry["glosses"][:2])}"' if entry else ""
        one = ask("  usage? ", default=suggested)
        if one:
            usages = [one]
    else:
        print(f"  {C['dim']}usage(s):{C['off']} " + " | ".join(usages))

    links = list(block["links"])
    if not links:
        extra = ask("  living link (optional, blank to skip)? ")
        if extra:
            links = [extra]
    else:
        print(f"  {C['dim']}living link(s):{C['off']} {len(links)} pasted")

    examples = ask("  example sentence (optional)? ")

    fields = {
        "Word": word, "Pinyin": pinyin,
        "Usages": "".join(f'<div class="usage">{u}</div>' for u in usages),
        "LivingLinks": "".join(f'<div class="link">{l}</div>' for l in links),
        "Examples": examples, "Audio": "",
    }

    if not yes(f"  add to {deck}?"):
        raise Cancel

    data = fetch_audio(word)
    if data:
        fname = f"mb_word_{hashlib.md5(word.encode()).hexdigest()[:12]}.mp3"
        invoke("storeMediaFile", filename=fname, data=base64.b64encode(data).decode())
        fields["Audio"] = f"[sound:{fname}]"

    if deck not in invoke("deckNames"):
        invoke("createDeck", deck=deck)
    try:
        invoke("addNote", note={"deckName": deck, "modelName": WORD_MODEL,
                                "fields": fields, "tags": ["mb-word"],
                                "options": {"allowDuplicate": False}})
    except RuntimeError as exc:
        print(f"  {C['red']}Anki refused it:{C['off']} {exc}")
        return
    print(f"  {C['green']}added{C['off']} {C['dim']}(2 cards){C['off']}")


def cmd_shell(args):
    cfg = args.config
    try:
        import atexit
        import readline
        hist = os.path.join(cfg, ".words-history")
        try:
            readline.read_history_file(hist)
        except OSError:
            pass
        readline.set_history_length(2000)
        atexit.register(lambda: _write_history(readline, hist))
    except Exception:  # noqa: BLE001
        pass

    settings = load_settings(cfg)
    decks = sorted(invoke("deckNames"))
    current_deck = settings.get("words_deck", NEW_WORDS_DECK)
    deck = args.deck or choose("Which deck should word cards go to?",
                                sorted(set(decks) | {current_deck}), current_deck)
    save_settings(cfg, {"words_deck": deck})

    ensure_word_model()
    sys.stderr.write("loading CC-CEDICT (for pinyin lookups)...\n")
    entries = load_cedict(ensure_cedict(None))

    print(f"\n  {C['bold']}MB Words{C['off']}")
    print(f"  {C['dim']}deck{C['off']} {deck}")
    print(f"  {C['dim']}paste a word block, then a lone '.' to finish it{C['off']}")
    print(f"  {C['dim']}? for help, q to quit{C['off']}")

    while True:
        try:
            raw = ask(f"\n{C['bold']}>{C['off']} ", cancel=False)
            if not raw:
                continue
            low = raw.lower()
            if low in ("q", "quit", "exit"):
                raise Quit
            if low in ("?", "h", "help"):
                print(SHELL_HELP)
                continue
            if low == "deck":
                deck = choose("Which deck should word cards go to?", sorted(invoke("deckNames")))
                save_settings(cfg, {"words_deck": deck})
                continue
            if low == "where":
                print(f"  deck: {deck}")
                continue
            if not HANZI_RE.search(raw):
                print(f"  {C['red']}Not Chinese text.{C['off']} ? for help.")
                continue

            rest = read_block()
            block = parse_block([raw] + rest)
            build_one(block, deck, entries)

        except Cancel:
            print(f"  {C['dim']}dropped{C['off']}")
            continue
        except Quit:
            print(f"  {C['dim']}bye{C['off']}")
            return


def _write_history(readline, path):
    try:
        readline.write_history_file(path)
    except OSError:
        pass


# --------------------------------------------------------------------------
# One-off migration of existing multi-character words
# --------------------------------------------------------------------------

def _unescape(raw):
    """html.unescape() turns '&nbsp;' into a literal U+00A0, not a plain
    space - normalize it so migrated text doesn't carry invisible NBSPs."""
    return html.unescape(raw).replace("\xa0", " ")


def extract_cjk(raw):
    """Pull just the CJK characters out of a legacy field, ignoring
    whatever HTML/question-prompt text they're wrapped in - the old
    'CHINESE DECK' import uses an inconsistent mix of <h1>word</h1> and
    plain-text-then-question-prompt layouts across notes."""
    text = _unescape(re.sub(r"<[^>]+>", "", raw))
    return "".join(HANZI_RE.findall(text))


def parse_legacy_meanings(raw):
    """Turn a legacy CHINESE DECK 'Meanings' field (a loose HTML blob mixing
    'Usage N - ...', 'Keyword - ...', an '[No audio yet]' placeholder, and
    sometimes just a bare meaning with no prefix at all) into (usages,
    living_links) lists in the same shape build_one() produces."""
    # Split on *both* opening and closing div/p tags, not just closing ones -
    # the '[No audio yet]' placeholder is wrapped in its own <div ...>...
    # </div> with no tag boundary before it, so splitting on closing tags
    # alone glues it onto the usage text right before it.
    parts = re.split(r"<br\s*/?>|</?div[^>]*>|</?p[^>]*>", raw)
    lines = []
    for part in parts:
        text = _unescape(re.sub(r"<[^>]+>", "", part)).strip()
        text = re.sub(r"\[?no audio yet[^\]]*\]?", "", text, flags=re.IGNORECASE).strip()
        if not text:
            continue
        lines.append(text)

    usages, links = [], []
    for line in lines:
        if re.match(r"^keyword\b", line, re.IGNORECASE):
            links.append(re.sub(r"^keyword\s*-\s*", "", line, flags=re.IGNORECASE).strip())
        elif USAGE_RE.match(line):
            usages.append(line)
        else:
            usages.append(f'Usage {len(usages) + 1} - "{line}"')
    return usages, links


def parse_legacy_examples(raw):
    """'<div><strong>Sentence</strong></div><div>..</div><div><strong>
    English:</strong></div><div>..</div>' -> the two content lines joined."""
    if not raw:
        return ""
    text = _unescape(re.sub(r"<[^>]+>", "|", raw))
    parts = [p.strip() for p in text.split("|") if p.strip()]
    parts = [p for p in parts if p.lower() not in ("sentence", "english:")]
    return "<br>".join(parts)


def cmd_migrate(args):
    """One-off: pull existing multi-character word notes out of the legacy
    bulk-imported deck and this tool's own single-character deck, and
    re-create them as 'MB Word' notes in the words deck. Old notes are never
    edited or deleted - their *cards* are moved to a '<deck>::legacy-migrated'
    subdeck, so review history/scheduling survives untouched and undoing the
    migration is just moving them back."""
    ensure_word_model()
    entries = load_cedict(ensure_cedict(None))

    deck = args.deck or NEW_WORDS_DECK
    if deck not in invoke("deckNames") and not args.dry_run:
        invoke("createDeck", deck=deck)

    already_ids = invoke("findNotes", query=f'note:"{WORD_MODEL}"')
    already = ({n["fields"]["Word"]["value"] for n in invoke("notesInfo", notes=already_ids)}
               if already_ids else set())

    pools = []

    import_deck = args.legacy_deck or LEGACY_IMPORT_DECK
    if import_deck in invoke("deckNames"):
        ids = invoke("findNotes", query=f'deck:"{import_deck}" note:"CHINESE DECK"')
        for n in (invoke("notesInfo", notes=ids) if ids else []):
            f = n["fields"]
            word = extract_cjk(f["Word"]["value"])
            if len(word) <= 1:
                continue
            usages, links = parse_legacy_meanings(f["Meanings"]["value"])
            # Already valid HTML (may include an <img>) meant to be embedded
            # as-is - unlike the other fields, this one is not re-escaped.
            wiki = f.get("Wiktionary Entry", {}).get("value", "").strip()
            if wiki:
                links.append(wiki)
            pinyin = _unescape(re.sub(r"<[^>]+>", "", f["Pinyin"]["value"])).strip()
            examples = parse_legacy_examples(f.get("Example Sentences", {}).get("value", ""))
            audio = f.get("Audio of Word", {}).get("value", "").strip()
            pools.append({"word": word, "pinyin": pinyin, "usages": usages, "links": links,
                          "examples": examples, "audio": audio,
                          "note": n, "source_deck": import_deck})

    char_deck = args.char_deck or LEGACY_CHAR_DECK
    if char_deck in invoke("deckNames"):
        ids = invoke("findNotes", query=f'deck:"{char_deck}" note:"Hanzi Movie Method"')
        for n in (invoke("notesInfo", notes=ids) if ids else []):
            f = n["fields"]
            word = f["Hanzi"]["value"]
            if len(word) <= 1:
                continue
            glosses = [g.strip() for g in f["Meaning"]["value"].split(";") if g.strip()]
            usages = [f'Usage {i + 1} - "{g}"' for i, g in enumerate(glosses)]
            links = [f["Keyword"]["value"].strip()] if f["Keyword"]["value"].strip() else []
            pools.append({"word": word, "pinyin": f["Pinyin"]["value"], "usages": usages,
                          "links": links, "examples": f["Examples"]["value"],
                          "audio": f["Audio"]["value"], "note": n, "source_deck": char_deck})

    seen = set(already)
    to_create, skipped = [], 0
    for item in pools:
        if item["word"] in seen:
            skipped += 1
            continue
        seen.add(item["word"])
        to_create.append(item)

    print(f"found {len(pools)} multi-character word note(s) in legacy source(s) "
          f"({skipped} already migrated/duplicate, {len(to_create)} to create)\n")

    created = 0
    by_source_cards: dict[str, list[int]] = {}
    for item in to_create:
        fields = {
            "Word": item["word"], "Pinyin": item["pinyin"],
            "Usages": "".join(f'<div class="usage">{u}</div>' for u in item["usages"]),
            "LivingLinks": "".join(f'<div class="link">{l}</div>' for l in item["links"]),
            "Examples": item["examples"], "Audio": item["audio"],
        }
        verb = "would create" if args.dry_run else "create"
        print(f"{verb}  {item['word']:<6} {item['pinyin']:<16} "
              f"{len(item['usages'])} usage(s), {len(item['links'])} link(s)"
              f"{'  [has audio]' if item['audio'] else ''}")
        if args.dry_run:
            continue

        if not item["audio"]:
            data = fetch_audio(item["word"])
            if data:
                fname = f"mb_word_{hashlib.md5(item['word'].encode()).hexdigest()[:12]}.mp3"
                invoke("storeMediaFile", filename=fname, data=base64.b64encode(data).decode())
                fields["Audio"] = f"[sound:{fname}]"

        try:
            invoke("addNote", note={"deckName": deck, "modelName": WORD_MODEL,
                                    "fields": fields, "tags": ["mb-word", "migrated"],
                                    "options": {"allowDuplicate": False}})
        except RuntimeError as exc:
            print(f"  ! {item['word']} not added: {exc}")
            continue
        created += 1
        by_source_cards.setdefault(item["source_deck"], []).extend(item["note"]["cards"])

    print()
    verb = "would move" if args.dry_run else "moved"
    for source_deck, cards in by_source_cards.items():
        legacy_sub = f"{source_deck}::legacy-migrated"
        if not args.dry_run:
            if legacy_sub not in invoke("deckNames"):
                invoke("createDeck", deck=legacy_sub)
            invoke("changeDeck", cards=cards, deck=legacy_sub)
        print(f"{verb} {len(cards)} old card(s) from '{source_deck}' to '{legacy_sub}'")

    mverb = "would create" if args.dry_run else "created"
    count = len(to_create) if args.dry_run else created
    print(f"\n{mverb} {count} 'MB Word' note(s) in '{deck}'")


# --------------------------------------------------------------------------
# Config (same save directory / settings.json as anki-mb-addon.py)
# --------------------------------------------------------------------------

def load_app_config():
    try:
        with open(APP_CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def save_app_config(data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cur = load_app_config()
    cur.update(data)
    with open(APP_CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cur, fh, indent=1)


def prompt_save_dir(default):
    print(f"\n  {C['bold']}First run{C['off']} - where should this tool keep its files?")
    print(f"  {C['dim']}(settings/history/CC-CEDICT - shared with the other anki-mb-*.py tools){C['off']}")
    try:
        val = input(f"  save directory [{default}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        val = ""
    return os.path.expanduser(val) if val else default


def resolve_config_dir(explicit):
    if explicit:
        return explicit
    env = os.environ.get("MB_CONFIG")
    if env:
        return env
    saved = load_app_config().get("config_dir")
    if saved:
        return saved
    default = os.path.join(os.getcwd(), "mb_config")
    chosen = prompt_save_dir(default)
    os.makedirs(chosen, exist_ok=True)
    save_app_config({"config_dir": chosen})
    print(f"  {C['green']}saved{C['off']}\n")
    return chosen


def load_settings(cfg):
    try:
        with open(os.path.join(cfg, "settings.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def save_settings(cfg, data):
    os.makedirs(cfg, exist_ok=True)
    cur = load_settings(cfg)
    cur.update(data)
    with open(os.path.join(cfg, "settings.json"), "w", encoding="utf-8") as fh:
        json.dump(cur, fh, indent=1)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    sh = sub.add_parser("shell", help="paste a word block, one at a time (the usual way in)")
    sh.add_argument("--deck", help="deck to add word cards to")
    sh.add_argument("--config", default=None)

    mg = sub.add_parser("migrate", help="one-off: pull existing multi-character words out of "
                                         "the legacy import and your character deck")
    mg.add_argument("--deck", help="words deck to migrate into")
    mg.add_argument("--legacy-deck", help=f"legacy bulk-imported deck (default: {LEGACY_IMPORT_DECK})")
    mg.add_argument("--char-deck", help=f"your single-character deck (default: {LEGACY_CHAR_DECK})")
    mg.add_argument("--dry-run", action="store_true", help="preview without changing anything")
    mg.add_argument("--config", default=None)

    args = p.parse_args(sys.argv[1:] or ["shell"])
    args.config = resolve_config_dir(args.config)
    {"shell": cmd_shell, "migrate": cmd_migrate}[args.cmd](args)


if __name__ == "__main__":
    main()
