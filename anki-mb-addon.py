#!/usr/bin/env python3
"""
mb_anki.py - build Mandarin Blueprint cards in Anki from the terminal.

Type a character; it works out the actor, set, room and props and sends a
finished note to Anki. The only thing you write is the movie.

Your props, actors and sets are notes in an Anki deck of your own (the
"component deck") - that deck is the source of truth, not a CSV. Anything
missing is offered as a prompt and saved there as you go.

Setup (once)
------------
  1. Install the AnkiConnect add-on in Anki (Tools > Add-ons > Get Add-ons,
     code 2055492159), restart Anki, and leave it running.
  2. The first time you run any command, you'll be asked where to save your
     files (hmm.ini, settings, history). Your answer is remembered; change it
     later with: python3 mb_anki.py savedir <path>
  3. python3 mb_anki.py hmm-init
     Creates the 'MB Component' note type in Anki (if missing) and writes
     hmm.ini (tone rooms + syllable-split corrections) to your save directory.
  4. python3 mb_anki.py shell
     The first run asks which deck holds your props/actors/sets.

Daily use
---------
  python3 mb_anki.py                 # same as 'shell'

    > only
      character  pinyin  ·  meaning
      cast / location / room / props, then a prompt for the script.

  Anything missing from your component deck is offered as a prompt: type a
  value to save it as a new note there, press enter to skip it, or q to
  abandon the card.

Other commands
--------------
  inspect        list your decks, note types and field names
  hmm-init       create the 'MB Component' note type and hmm.ini
  hmm-check      preview the scaffolding for a whole word list, changing nothing
  add FILE       bulk-import a word list without prompting
  migrate-props  one-off: push old actors.csv/sets.csv/props.csv into the component deck
  syllables      print the full computed initial/final split (--diff for overrides only)

No third-party packages. Python 3.8+.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import csv
import gzip
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

ANKI_CONNECT_URL = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")
CEDICT_URL = "https://www.mdbg.net/chinese/export/cedict/cedict_1_0_ts_utf-8_mdbg.txt.gz"
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".mb_anki")
CEDICT_CACHE = os.path.join(CACHE_DIR, "cedict.txt")
IDS_URL = "https://raw.githubusercontent.com/cjkvi/cjkvi-ids/master/ids.txt"
APP_CONFIG_PATH = os.path.join(CACHE_DIR, "app_config.json")
COMPONENT_MODEL = "MB Component"
DEFAULT_COMPONENT_DECK = "mandarin-blueprint-imports-actors-sets-props"

# Field-name synonyms used to auto-map our data onto your note type.
# First match wins, case-insensitive. Override with --map.
FIELD_SYNONYMS = {
    "hanzi": ["hanzi", "character", "characters", "chinese", "word", "simplified", "front"],
    "pinyin": ["pinyin", "reading", "pronunciation", "romanisation", "romanization"],
    "meaning": ["meaning", "english", "definition", "translation", "keyword", "back"],
    "audio": ["audio", "sound", "pronunciation audio", "tts"],
    "examples": ["examples", "example words", "words", "vocab", "example"],
    "mnemonic": ["mnemonic", "movie", "story", "scene", "notes", "memory"],
    "actor": ["actor", "actors"],
    "set": ["set", "sets", "location", "place"],
    "room": ["room", "tone location", "tone room", "rooms"],
    "props": ["props", "prop", "components", "component", "radicals"],
    "initial": ["initial", "initials"],
    "final": ["final", "finals"],
    "tone": ["tone", "tone number"],
}

# Field-name synonyms for the component deck (props/actors/sets note type).
COMPONENT_FIELD_SYNONYMS = {
    "kind": ["kind", "type"],
    "key": ["key", "id"],
    "value": ["value", "name", "meaning"],
}

TONE_MARKS = {
    "a": "āáǎàa", "e": "ēéěèe", "i": "īíǐìi", "o": "ōóǒòo",
    "u": "ūúǔùu", "ü": "ǖǘǚǜü",
    "A": "ĀÁǍÀA", "E": "ĒÉĚÈE", "I": "ĪÍǏÌI", "O": "ŌÓǑÒO",
    "U": "ŪÚǓÙU", "Ü": "ǕǗǙǛÜ",
}

HANZI_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


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


# --------------------------------------------------------------------------
# Pinyin
# --------------------------------------------------------------------------

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
# CC-CEDICT
# --------------------------------------------------------------------------

def download(url, timeout=180, retries=3):
    """GET a URL, retrying on timeouts/connection hiccups before giving up
    with a plain sentence instead of a raw traceback."""
    req = urllib.request.Request(url, headers={"User-Agent": "mb_anki/1.0"})
    last_exc = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError) as exc:
            last_exc = exc
            if attempt < retries - 1:
                sys.stderr.write(f"  (retrying after: {exc})\n")
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
    """Return (entries_by_simplified, index_of_words_containing_char)."""
    entries: dict[str, list[dict]] = {}
    contains: dict[str, list[str]] = {}
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
                       if not re.match(r"^(CL:|Taiwan pr\.|also pr\.|abbr\. for)", g)]
            if not glosses:
                continue
            entries.setdefault(simp, []).append(
                {"trad": trad, "pinyin": pin, "glosses": glosses}
            )
            if len(simp) > 1:
                for ch in set(simp):
                    contains.setdefault(ch, []).append(simp)
    return entries, contains


def lookup(entries, word: str):
    """Best entry for a word: the one with the most glosses, ignoring
    'variant of' / 'surname' style stubs where a real definition exists."""
    cands = entries.get(word)
    if not cands:
        return None
    def score(e):
        real = [g for g in e["glosses"]
                if not re.match(r"^(variant of|old variant|surname|see [A-Za-z])", g)]
        return (len(real), len(e["glosses"]))
    return max(cands, key=score)


# ==========================================================================
# HANZI MOVIE METHOD SCAFFOLDING
# ==========================================================================

INITIALS = ["zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l",
            "g", "k", "h", "j", "q", "x", "r", "z", "c", "s"]
FAKE_I = {"zh", "ch", "sh", "r", "z", "c", "s"}   # zhi/chi/shi/ri/zi/ci/si
NULL = "0"

# Remainder after the actor is stripped -> MB set key.
FINAL_NORM = {"": NULL, "u": "ou", "i": "ei", "n": "en", "ng": "eng"}

DEFAULT_ROOMS = {
    1: "Outside the entrance",
    2: "Kitchen",
    3: "Bedroom / living room",
    4: "Bathroom",
    5: "On the roof",
}

SYL_RE = re.compile(r"^([a-zA-ZüÜ:]+?)([1-5])?$")


def split_syllable(syl: str):
    """One numbered CEDICT syllable -> (actor_key, set_key, tone).

    The actor is the initial *plus* any medial glide (di-, ju-, xi-, wu-...),
    which is what gives MB its ~55 actors. The set is what's left over.
    """
    m = SYL_RE.match(syl.strip())
    if not m:
        return None
    base = m.group(1).lower().replace("u:", "ü").replace("v", "ü")
    tone = int(m.group(2) or 5)

    cons = next((i for i in INITIALS if base.startswith(i)), "")
    rest = base[len(cons):]

    if not cons:
        if base.startswith("y"):
            rest = base[1:]
            if rest.startswith("u"):          # yu, yue, yuan, yun -> ü
                actor, rest = "yu", rest[1:]
            else:
                actor = "yi"
                rest = rest[1:] if rest.startswith("i") else rest
        elif base.startswith("w"):
            actor, rest = "wu", base[1:]
            rest = rest[1:] if rest.startswith("u") else rest
        else:
            actor, rest = NULL, base          # a, e, ai, ao, an, ou, er...
    elif rest == "i" and cons in FAKE_I:
        actor, rest = cons, ""                # the "fake i" - stays male
    elif rest[:1] in ("i", "u", "ü"):
        actor, rest = cons + rest[0], rest[1:]
    else:
        actor = cons

    return actor, FINAL_NORM.get(rest, rest) or NULL, tone


def split_word(pinyin: str):
    out = []
    for syl in pinyin.split():
        r = split_syllable(syl)
        if r:
            out.append(r)
    return out


# ---------------------------------------------------------------- config

def load_csv_map(path, key_col=0, val_col=1):
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.reader(fh):
            if not row or row[0].startswith("#") or len(row) <= val_col:
                continue
            if row[key_col].strip().lower() in ("key", "tone", "component", "syllable"):
                continue
            k, v = row[key_col].strip(), row[val_col].strip()
            if k and v:
                out[k] = v
    return out


def load_hmm_ini(cfg_dir):
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    path = os.path.join(cfg_dir, "hmm.ini")
    if os.path.exists(path):
        cp.read(path, encoding="utf-8")
    return cp


def write_hmm_ini(path, rooms, overrides):
    lines = ["# Which room in the set each tone happens in.", "[rooms]"]
    for tone, room in sorted(rooms.items()):
        lines.append(f"{tone} = {room}")
    lines += [
        "",
        "# Corrections to the computed initial/final split, as: syllable = actor, set",
        "# Only syllables where the built-in rule is wrong need to be listed here.",
        "# Anything absent falls back to split_syllable().",
        "[syllables]",
    ]
    for syl, (actor, set_) in sorted(overrides.items()):
        lines.append(f"{syl} = {actor}, {set_}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def resolve_component_deck(cfg_dir, explicit=None):
    if explicit:
        return explicit
    return load_settings(cfg_dir).get("component_deck", DEFAULT_COMPONENT_DECK)


def ensure_component_model():
    if COMPONENT_MODEL in invoke("modelNames"):
        return
    invoke("createModel", modelName=COMPONENT_MODEL,
           inOrderFields=["Kind", "Key", "Value"],
           css=".card { font-family: arial; font-size: 20px; text-align: center; }",
           cardTemplates=[{
               "Name": "Card 1",
               "Front": "{{Kind}}: {{Key}}",
               "Back": "{{FrontSide}}<hr id=answer>{{Value}}",
           }])


class ComponentStore:
    """Fronts the component deck: your props/actors/sets live there as notes,
    one per (kind, key). Loaded once per session; add() refreshes in place
    so nothing re-queries Anki on every keystroke."""

    def __init__(self, deck):
        self.deck = deck
        if deck not in invoke("deckNames"):
            raise SystemExit(
                f"Component deck '{deck}' not found in Anki.\n"
                f"Change it with: python3 mb_anki.py shell   (then 'component-deck')"
            )
        try:
            fields = invoke("modelFieldNames", modelName=COMPONENT_MODEL)
        except RuntimeError as exc:
            raise SystemExit(
                f"Note type '{COMPONENT_MODEL}' not found in Anki ({exc}).\n"
                f"Run: python3 mb_anki.py hmm-init"
            )
        self.mapping = auto_map(fields, {}, COMPONENT_FIELD_SYNONYMS)
        missing = [f for f in ("kind", "key", "value") if f not in self.mapping]
        if missing:
            raise SystemExit(
                f"Note type '{COMPONENT_MODEL}' has no field that looks like "
                f"{', '.join(missing)}. Fields: {fields}"
            )
        self._data = {"actor": {}, "set": {}, "prop": {}}
        self._note_ids = {}
        self._load()

    def _load(self):
        self._data = {"actor": {}, "set": {}, "prop": {}}
        self._note_ids = {}
        ids = invoke("findNotes", query=f'deck:"{self.deck}" note:"{COMPONENT_MODEL}"')
        if not ids:
            return
        for note in invoke("notesInfo", notes=ids):
            kind = note["fields"].get(self.mapping["kind"], {}).get("value", "").strip().lower()
            key = note["fields"].get(self.mapping["key"], {}).get("value", "").strip()
            value = note["fields"].get(self.mapping["value"], {}).get("value", "").strip()
            if kind not in self._data or not key:
                continue
            self._data[kind][key] = value
            self._note_ids[(kind, key)] = note["noteId"]

    def actors(self):
        return self._data["actor"]

    def sets(self):
        return self._data["set"]

    def props(self):
        return self._data["prop"]

    def actor(self, key):
        return self._data["actor"].get(key)

    def set(self, key):
        return self._data["set"].get(key)

    def prop(self, component):
        return self._data["prop"].get(component)

    def has(self, kind, key):
        return key in self._data.get(kind, {})

    def add(self, kind, key, value):
        if self.has(kind, key):
            raise ValueError(f"{kind} '{key}' already exists in the component deck")
        fields = {self.mapping["kind"]: kind, self.mapping["key"]: key,
                  self.mapping["value"]: value}
        invoke("addNote", note={"deckName": self.deck, "modelName": COMPONENT_MODEL,
                                "fields": fields, "tags": [kind.capitalize()],
                                "options": {"allowDuplicate": False}})
        self._data[kind][key] = value


class HMM:
    """Your personal actor / set / room / prop assignments."""

    def __init__(self, cfg_dir, store):
        self.dir = cfg_dir
        self.store = store
        self.actors = store.actors()
        self.sets = store.sets()
        self.props = store.props()
        cp = load_hmm_ini(cfg_dir)
        if cp.has_section("rooms") and cp.items("rooms"):
            self.rooms = {int(k): v for k, v in cp.items("rooms") if k.isdigit()}
        else:
            self.rooms = dict(DEFAULT_ROOMS)
        self.overrides = {}
        if cp.has_section("syllables"):
            for syl, val in cp.items("syllables"):
                parts = [p.strip() for p in val.split(",", 1)]
                if len(parts) == 2:
                    self.overrides[syl.strip()] = (parts[0], parts[1])
        self.ids = load_ids(os.path.join(cfg_dir, "ids.txt"))
        self.unknown_props = set()
        self.unknown_actors = set()
        self.unknown_sets = set()

    def split(self, syl):
        m = SYL_RE.match(syl.strip())
        base = m.group(1).lower() if m else syl
        tone = int(m.group(2)) if m and m.group(2) else 5
        if base in self.overrides:
            a, s = self.overrides[base]
            return a, s, tone
        return split_syllable(syl)

    def scene(self, pinyin):
        """-> dict of actor / set / room strings for the first syllable."""
        parts = [self.split(s) for s in pinyin.split()]
        parts = [p for p in parts if p]
        if not parts:
            return {}
        actor_k, set_k, tone = parts[0]
        if actor_k not in self.actors:
            self.unknown_actors.add(actor_k)
        if set_k not in self.sets:
            self.unknown_sets.add(set_k)
        return {
            "actor": self.actors.get(actor_k, f"[{actor_k}-?]"),
            "set": self.sets.get(set_k, f"[{'null' if set_k == NULL else '-' + set_k}-?]"),
            "room": self.rooms.get(tone, ""),
            "initial": actor_k if actor_k != NULL else "Ø",
            "final": set_k if set_k != NULL else "Ø",
            "tone": str(tone),
        }

    def prop_list(self, word):
        """Decompose to the granularity of props you've actually learned."""
        comps = []
        for ch in word:
            comps.extend(decompose(ch, self.ids, set(self.props), set()))
        out = []
        for c in comps:
            if c in self.props:
                out.append(f"{c} = {self.props[c]}")
            else:
                self.unknown_props.add(c)
                out.append(f"{c} = ?")
        return out


# ---------------------------------------------------------------- IDS

IDC = "⿰⿱⿲⿳⿴⿵⿶⿷⿸⿹⿺⿻⿼⿽⿾⿿"


def load_ids(path):
    ids = {}
    if not os.path.exists(path):
        return ids
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 3:
                continue
            char = cols[1]
            for col in cols[2:]:
                col = re.sub(r"\[[^\]]*\]", "", col).strip()
                parts = [c for c in col if c not in IDC and HANZI_RE.match(c)]
                if parts and not (len(parts) == 1 and parts[0] == char):
                    ids.setdefault(char, parts)
                    break
    return ids


def decompose(char, ids, stop_at, seen, depth=0):
    if char in stop_at or depth > 5 or char in seen:
        return [char]
    parts = ids.get(char)
    if not parts:
        return [char]
    seen = seen | {char}
    out = []
    for p in parts:
        out.extend(decompose(p, ids, stop_at, seen, depth + 1))
    return out


def ensure_ids(cfg_dir):
    path = os.path.join(cfg_dir, "ids.txt")
    if os.path.exists(path):
        return path
    sys.stderr.write("Downloading character decomposition data (one time, ~10 MB)...\n")
    data = download(IDS_URL)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


# --------------------------------------------------------------------------
# Field building
# --------------------------------------------------------------------------

def build_examples(word, entries, contains, known: set[str], limit=5):
    """Example words containing the character, preferring ones built only from
    characters you've already learned."""
    if len(word) > 1:
        return []
    cands = contains.get(word, [])
    scored = []
    for cand in cands:
        if cand == word:
            continue
        entry = lookup(entries, cand)
        if not entry:
            continue
        all_known = all(ch in known or ch == word for ch in cand)
        scored.append((0 if all_known else 1, len(cand), cand, entry))
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    out = []
    for _, _, cand, entry in scored[:limit]:
        out.append(
            f"{cand} ({numbered_to_marks(entry['pinyin'])}) "
            f"{'; '.join(entry['glosses'][:2])}"
        )
    return out


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
        sys.stderr.write(f"  ! audio failed for {text}: {exc}\n")
        return None


# --------------------------------------------------------------------------
# Mapping our data onto the user's note type
# --------------------------------------------------------------------------

def auto_map(note_fields: list[str], overrides: dict[str, str],
             synonyms: dict[str, list[str]] = FIELD_SYNONYMS) -> dict[str, str]:
    lowered = {f.lower(): f for f in note_fields}
    mapping = {}
    for key, syns in synonyms.items():
        if key in overrides:
            target = overrides[key]
            if target not in note_fields:
                raise SystemExit(
                    f"Field '{target}' is not in note type. Available: {note_fields}"
                )
            mapping[key] = target
            continue
        for syn in syns:
            if syn in lowered:
                mapping[key] = lowered[syn]
                break
    return mapping


def parse_input(path: str):
    items = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "\t" in line:
                word, mnemonic = line.split("\t", 1)
            elif "|" in line:
                word, mnemonic = line.split("|", 1)
            else:
                word, mnemonic = line, ""
            word = word.strip()
            if word and HANZI_RE.search(word):
                items.append((word, mnemonic.strip()))
            elif word:
                sys.stderr.write(f"  ! skipping non-Chinese line: {word}\n")
    return items


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def all_syllables(entries):
    """Every syllable that actually occurs in Chinese, with its computed split."""
    seen = {}
    for cands in entries.values():
        for e in cands:
            for syl in e["pinyin"].split():
                m = SYL_RE.match(syl)
                if not m:
                    continue
                base = m.group(1).lower().replace("u:", "ü").replace("v", "ü")
                if base in seen:
                    continue
                r = split_syllable(syl)
                if r:
                    seen[base] = r
    return seen


def cmd_hmm_init(args):
    os.makedirs(args.config, exist_ok=True)
    ensure_ids(args.config)
    ensure_component_model()
    entries, _ = load_cedict(ensure_cedict(args.cedict))
    seen = all_syllables(entries)

    ini_path = os.path.join(args.config, "hmm.ini")
    if os.path.exists(ini_path) and not args.force:
        print(f"  kept   hmm.ini (exists; --force to overwrite)")
    else:
        rooms = dict(DEFAULT_ROOMS)
        migrated_rooms = 0
        legacy_rooms = os.path.join(args.config, "rooms.csv")
        if os.path.exists(legacy_rooms):
            old_rooms = load_csv_map(legacy_rooms)
            old_rooms = {int(k): v for k, v in old_rooms.items() if k.isdigit()}
            if old_rooms:
                rooms = old_rooms
                migrated_rooms = len(old_rooms)

        overrides = {}
        migrated_syl = 0
        legacy_syllables = os.path.join(args.config, "syllables.csv")
        if os.path.exists(legacy_syllables):
            with open(legacy_syllables, encoding="utf-8-sig", newline="") as fh:
                for row in csv.reader(fh):
                    if len(row) < 3 or not row[0].strip() or row[0].startswith("#"):
                        continue
                    if row[0].strip() == "syllable":
                        continue
                    syl, a, s_ = row[0].strip(), row[1].strip(), row[2].strip()
                    if not a or not s_:
                        continue
                    computed = seen.get(syl)
                    if computed and (a, s_) == (computed[0], computed[1]):
                        continue          # agrees with split_syllable() - no override needed
                    overrides[syl] = (a, s_)
                    migrated_syl += 1

        write_hmm_ini(ini_path, rooms, overrides)
        print(f"  wrote  hmm.ini")
        if migrated_rooms or migrated_syl:
            print(f"  migrated {migrated_rooms} room(s) and {migrated_syl} syllable "
                  f"override(s) from rooms.csv / syllables.csv into hmm.ini")
            print(f"  (old files left on disk - delete them yourself once you're happy)")

    print(f"""
Config is in {args.config}/ . Next:

  1. Your props, actors and sets live in Anki now, in the component deck
     (note type '{COMPONENT_MODEL}'). Run 'shell' once to pick which deck
     that is; anything missing is offered as a prompt as you go.
  2. hmm.ini has your tone rooms and any syllable-split corrections.
     Spot-check the full computed table ({len(seen)} syllables) against your
     MB pinyin chart with:  python3 mb_anki.py syllables
     Only rows that disagree with it need adding to hmm.ini's [syllables].

Then:  python mb_anki.py hmm-check words.txt""")


# ==========================================================================
# INTERACTIVE SHELL
# ==========================================================================

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
    """Read a line. 'q' abandons the card; ctrl-d ends the session."""
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


def show_card(word, entry, scene, props, examples, duplicate):
    print()
    print(f"  {C['bold']}{word}{C['off']}  {numbered_to_marks(entry['pinyin'])}"
          f"  {C['dim']}·{C['off']}  {'; '.join(entry['glosses'][:4])}")
    if duplicate:
        print(f"  {C['yellow']}already in this note type{C['off']}")
    print()

    def line(label, value, key, missing):
        mark = f"{C['red']}?{C['off']}" if missing else value
        print(f"  {C['dim']}{pad(label, 10)}{C['off']}{pad(mark, 32)}"
              f"{C['dim']}{key}{C['off']}")

    line("cast", scene.get("actor", ""), scene.get("initial", ""),
         scene.get("actor", "").endswith("-?]"))
    line("location", scene.get("set", ""), scene.get("final", ""),
         scene.get("set", "").endswith("-?]"))
    line("room", scene.get("room", ""), f"tone {scene.get('tone','')}",
         not scene.get("room"))
    for i, p in enumerate(props):
        val = p["prop"] or f"{C['red']}?{C['off']}"
        print(f"  {C['dim']}{pad('props' if i == 0 else '', 10)}{C['off']}"
              f"{pad(p['component'], 4)}{val}")
    if examples:
        for i, ex in enumerate(examples[:4]):
            print(f"  {C['dim']}{pad('also in' if i == 0 else '', 10)}{ex}{C['off']}")
    print()


def fill_gaps(word, hmm, scene, props):
    """Offer to name anything that has no entry yet. 'q' abandons the card."""
    gaps = []
    if hmm.unknown_actors:
        for k in sorted(hmm.unknown_actors):
            gaps.append(("actor", k, f"  who plays {C['bold']}{k}{C['off']}? "))
    if hmm.unknown_sets:
        for k in sorted(hmm.unknown_sets):
            label = "the null final" if k == NULL else f"-{k}"
            gaps.append(("set", k, f"  which place is {C['bold']}{label}{C['off']}? "))
    for p in props:
        if not p["prop"]:
            gaps.append(("prop", p["component"],
                         f"  prop for {C['bold']}{p['component']}{C['off']}? "))
    if not gaps:
        return False

    print(f"  {C['yellow']}{len(gaps)} thing(s) not in your component deck yet.{C['off']}")
    if not yes("  fill them in now?"):
        return False
    print(f"  {C['dim']}enter = skip this one, q = abandon this card{C['off']}")

    changed = False
    for kind, key, prompt in gaps:
        val = ask(prompt)
        if not val:
            continue
        if hmm.store.has(kind, key):
            print(f"  {C['dim']}{key} is already in the component deck, skipping{C['off']}")
            continue
        hmm.store.add(kind, key, val)
        print(f"  {C['green']}saved{C['off']} {C['dim']}{key} -> component deck{C['off']}")
        changed = True
    return changed


SHELL_HELP = """
  <character>     look it up and build a card
  <word>          several characters are treated as one word if it's in the
                  dictionary, otherwise queued one character at a time
  deck            change the target deck
  note            change the note type
  component-deck  change which deck holds your props/actors/sets
  audio           toggle downloading pronunciation audio
  where           print the paths of your config and component deck
  savedir         show the save directory (change it with: mb_anki.py savedir <path>)
  ?               this help
  q               quit

  At any prompt: enter accepts the default, q abandons the card, ctrl-d quits.
"""


def cmd_shell(args):
    cfg = args.config
    try:
        import atexit
        import readline
        hist = os.path.join(cfg, ".history")
        try:
            readline.read_history_file(hist)
        except OSError:
            pass
        readline.set_history_length(2000)
        atexit.register(lambda: _write_history(readline, hist))
    except Exception:  # noqa: BLE001
        pass

    entries, contains = load_cedict(ensure_cedict(args.cedict))
    settings = load_settings(cfg)

    decks = sorted(invoke("deckNames"))
    models = sorted(invoke("modelNames"))
    deck = args.deck or choose("Which deck?", decks, settings.get("deck"))
    note_type = args.note_type or choose("Which note type?", models, settings.get("note_type"))
    component_deck = args.component_deck or choose(
        "Which deck holds your props/actors/sets?", decks,
        settings.get("component_deck", DEFAULT_COMPONENT_DECK))
    audio = args.audio or settings.get("audio", False)
    save_settings(cfg, {"deck": deck, "note_type": note_type,
                         "component_deck": component_deck, "audio": audio})

    mapping, note_fields = None, None
    try:
        note_fields = invoke("modelFieldNames", modelName=note_type)
        mapping = auto_map(note_fields, {})
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Couldn't read '{note_type}': {exc}")
    if "hanzi" not in mapping:
        raise SystemExit(f"No field on '{note_type}' looks like the character field.\n"
                         f"Fields: {note_fields}")

    store = ComponentStore(component_deck)
    hmm = HMM(cfg, store)
    print(f"\n  {C['bold']}Hanzi Movie Method{C['off']}  {C['dim']}·  "
          f"{len(hmm.actors)} actors, {len(hmm.sets)} sets, {len(hmm.props)} props{C['off']}")
    print(f"  {C['dim']}deck{C['off']} {deck}   {C['dim']}note{C['off']} {note_type}"
          f"   {C['dim']}component deck{C['off']} {component_deck}"
          f"   {C['dim']}audio{C['off']} {'on' if audio else 'off'}")
    print(f"  {C['dim']}? for help, q to quit{C['off']}")

    known, existing = collection_state(note_type, mapping["hanzi"])
    queue = []

    while True:
        try:
            if queue:
                word = queue.pop(0)
                print(f"\n{C['dim']}> {word}  ({len(queue)} more queued){C['off']}")
            else:
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
                    deck = choose("Which deck?", sorted(invoke("deckNames")))
                    save_settings(cfg, {"deck": deck})
                    continue
                if low == "note":
                    note_type = choose("Which note type?", sorted(invoke("modelNames")))
                    note_fields = invoke("modelFieldNames", modelName=note_type)
                    mapping = auto_map(note_fields, {})
                    known, existing = collection_state(note_type, mapping["hanzi"])
                    save_settings(cfg, {"note_type": note_type})
                    continue
                if low == "component-deck":
                    component_deck = choose("Which deck holds your props/actors/sets?",
                                             sorted(invoke("deckNames")))
                    store = ComponentStore(component_deck)
                    hmm = HMM(cfg, store)
                    save_settings(cfg, {"component_deck": component_deck})
                    continue
                if low == "audio":
                    audio = not audio
                    save_settings(cfg, {"audio": audio})
                    print(f"  audio {'on' if audio else 'off'}")
                    continue
                if low == "where":
                    print(f"  {os.path.abspath(os.path.join(cfg, 'hmm.ini'))}")
                    print(f"  component deck: {component_deck}")
                    continue
                if low == "savedir":
                    print(f"  {os.path.abspath(cfg)}")
                    print(f"  {C['dim']}change with: python3 mb_anki.py savedir <path>{C['off']}")
                    continue
                if not HANZI_RE.search(raw):
                    print(f"  {C['red']}Not Chinese text.{C['off']} ? for help.")
                    continue
                word = raw
                if len(word) > 1 and not lookup(entries, word):
                    chars = [c for c in word if HANZI_RE.match(c)]
                    print(f"  {C['dim']}not a dictionary word - queuing "
                          f"{len(chars)} characters{C['off']}")
                    queue = chars[1:]
                    word = chars[0]

            build_one(word, entries, contains, known, existing, hmm,
                      deck, note_type, mapping, audio)

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


def collection_state(note_type, hanzi_field):
    ids = invoke("findNotes", query=f'note:"{note_type}"')
    known, words = set(), set()
    if ids:
        for note in invoke("notesInfo", notes=ids):
            val = re.sub(r"<[^>]+>", "", note["fields"].get(hanzi_field, {}).get("value", ""))
            if val.strip():
                words.add(val.strip())
                known.update(HANZI_RE.findall(val))
    return known, words


def build_one(word, entries, contains, known, existing, hmm,
              deck, note_type, mapping, audio):
    entry = lookup(entries, word)
    if not entry:
        print(f"  {C['red']}{word} isn't in CC-CEDICT.{C['off']} Add this one in Anki by hand.")
        return

    def compute():
        # hmm.actors/sets/props are live references into the ComponentStore's
        # cache, so this re-reads nothing from Anki - just recomputes the
        # scene/props against whatever is in that cache right now.
        hmm.unknown_actors, hmm.unknown_sets, hmm.unknown_props = set(), set(), set()
        scene = hmm.scene(entry["pinyin"])
        props, seen = [], set()
        for ch in word:
            for comp in decompose(ch, hmm.ids, set(hmm.props), set()):
                if comp not in seen:
                    seen.add(comp)
                    props.append({"component": comp, "prop": hmm.props.get(comp, "")})
        return scene, props

    scene, props = compute()
    examples = build_examples(word, entries, contains, known)
    show_card(word, entry, scene, props, examples, word in existing)

    # Asked once. Anything skipped here stays skipped for this card.
    if fill_gaps(word, hmm, scene, props):
        scene, props = compute()
        show_card(word, entry, scene, props, examples, word in existing)

    movie = ask(f"  {C['dim']}script{C['off']} ")

    fields = {mapping["hanzi"]: word}
    pairs = [("pinyin", numbered_to_marks(entry["pinyin"])),
             ("meaning", "; ".join(entry["glosses"][:4])),
             ("mnemonic", movie),
             ("actor", scene.get("actor")), ("set", scene.get("set")),
             ("room", scene.get("room")), ("initial", scene.get("initial")),
             ("final", scene.get("final")), ("tone", scene.get("tone"))]
    for key, val in pairs:
        if key in mapping and val:
            fields[mapping[key]] = val
    if "props" in mapping and props:
        fields[mapping["props"]] = "<br>".join(
            f"{p['component']} = {p['prop'] or '?'}" for p in props)
    if "examples" in mapping and examples:
        fields[mapping["examples"]] = "<br>".join(examples)

    if not yes(f"  add to {deck}?"):
        raise Cancel

    if audio and "audio" in mapping:
        data = fetch_audio(word)
        if data:
            fname = f"mb_{hashlib.md5(word.encode()).hexdigest()[:12]}.mp3"
            invoke("storeMediaFile", filename=fname,
                   data=base64.b64encode(data).decode())
            fields[mapping["audio"]] = f"[sound:{fname}]"

    if deck not in invoke("deckNames"):
        invoke("createDeck", deck=deck)
    try:
        invoke("addNote", note={"deckName": deck, "modelName": note_type,
                                "fields": fields, "tags": ["mb-auto"],
                                "options": {"allowDuplicate": False}})
    except RuntimeError as exc:
        print(f"  {C['red']}Anki refused it:{C['off']} {exc}")
        return
    existing.add(word)
    known.update(HANZI_RE.findall(word))
    print(f"  {C['green']}added{C['off']} {C['dim']}{len(fields)} fields{C['off']}")


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
    print(f"\n  {C['bold']}First run{C['off']} - where should mb_anki keep its files?")
    print(f"  {C['dim']}(your actor/set/room/prop CSVs, settings, and history){C['off']}")
    try:
        val = input(f"  save directory [{default}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        val = ""
    return os.path.expanduser(val) if val else default


def resolve_config_dir(explicit):
    """Where the CSVs/settings live. Priority: --config flag > MB_CONFIG env
    > saved choice from a previous run > ask (and remember the answer)."""
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
    print(f"  {C['green']}saved{C['off']} {C['dim']}- change anytime with: "
          f"python3 mb_anki.py savedir <path>{C['off']}\n")
    return chosen


def cmd_savedir(args):
    current = load_app_config().get("config_dir")
    if args.path:
        new_dir = os.path.expanduser(args.path)
        os.makedirs(new_dir, exist_ok=True)
        save_app_config({"config_dir": new_dir})
        print(f"Save directory set to {new_dir}")
        if current and current != new_dir:
            print(f"(previous: {current} - existing files are not moved automatically)")
    else:
        print(current or "(not set yet - it will be asked for on next run)")


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


ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def pad(text, width):
    """Left-justify by visible width: CJK glyphs are two cells, colour codes are none."""
    plain = ANSI_RE.sub("", str(text))
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in plain)
    return str(text) + " " * max(1, width - w)


def cmd_hmm_check(args):
    component_deck = resolve_component_deck(args.config, args.component_deck)
    hmm = HMM(args.config, ComponentStore(component_deck))
    entries, _ = load_cedict(ensure_cedict(args.cedict))
    print(pad("char", 6) + pad("pinyin", 12) + pad("actor", 22)
          + pad("set", 26) + pad("room", 24) + "props")
    print("-" * 118)
    for word, _mn in parse_input(args.input):
        entry = lookup(entries, word)
        if not entry:
            print(pad(word, 6) + "NOT IN DICTIONARY")
            continue
        sc = hmm.scene(entry["pinyin"])
        props = ", ".join(hmm.prop_list(word))
        print(pad(word, 6) + pad(numbered_to_marks(entry["pinyin"]), 12)
              + pad(sc["actor"], 22) + pad(sc["set"], 26)
              + pad(sc["room"], 24) + props)
    report_gaps(hmm)


def report_gaps(hmm):
    if hmm.unknown_actors:
        print(f"\n! No actor set for: {', '.join(sorted(hmm.unknown_actors))}"
              f"  -> add to the component deck ({hmm.store.deck})")
    if hmm.unknown_sets:
        print(f"! No set for: {', '.join(sorted(hmm.unknown_sets))}"
              f"  -> add to the component deck ({hmm.store.deck})")
    if hmm.unknown_props:
        print(f"! No prop for: {' '.join(sorted(hmm.unknown_props))}"
              f"  -> add to the component deck ({hmm.store.deck})")


def cmd_inspect(_args):
    print("Decks:")
    for d in sorted(invoke("deckNames")):
        print(f"  {d}")
    print("\nNote types and fields:")
    for model in sorted(invoke("modelNames")):
        fields = invoke("modelFieldNames", modelName=model)
        print(f"  {model}")
        print(f"      {', '.join(fields)}")


def cmd_add(args):
    overrides = {}
    for pair in args.map or []:
        if "=" not in pair:
            raise SystemExit(f"--map expects key=Field, got '{pair}'")
        k, v = pair.split("=", 1)
        overrides[k.strip()] = v.strip()

    note_fields = invoke("modelFieldNames", modelName=args.note_type)
    mapping = auto_map(note_fields, overrides)
    if "hanzi" not in mapping:
        raise SystemExit(
            f"Couldn't work out which field holds the character.\n"
            f"Fields available: {note_fields}\n"
            f"Pass e.g. --map hanzi='{note_fields[0]}'"
        )
    print("Field mapping:")
    for k in FIELD_SYNONYMS:
        print(f"  {k:9} -> {mapping.get(k, '(unused)')}")
    unused = [f for f in note_fields if f not in mapping.values()]
    if unused:
        print(f"  (left untouched: {', '.join(unused)})")
    print()

    if args.deck not in invoke("deckNames"):
        if args.dry_run:
            print(f"(deck '{args.deck}' would be created)")
        else:
            invoke("createDeck", deck=args.deck)

    # Everything you already have, used for dedupe and for the example filter.
    existing_ids = invoke("findNotes", query=f'note:"{args.note_type}"')
    known: set[str] = set()
    existing_words: set[str] = set()
    if existing_ids:
        for note in invoke("notesInfo", notes=existing_ids):
            val = note["fields"].get(mapping["hanzi"], {}).get("value", "")
            val = re.sub(r"<[^>]+>", "", val).strip()
            if val:
                existing_words.add(val)
                known.update(HANZI_RE.findall(val))

    hmm = None
    if not args.no_hmm:
        component_deck = resolve_component_deck(args.config, args.component_deck)
        hmm = HMM(args.config, ComponentStore(component_deck))
        if not hmm.actors and not hmm.sets:
            sys.stderr.write(
                f"Note: the component deck ({component_deck}) has no "
                f"actors/sets in it yet.\n\n")
    entries, contains = load_cedict(ensure_cedict(args.cedict))
    items = parse_input(args.input)
    print(f"{len(items)} item(s) in {args.input}; {len(existing_words)} already in collection.\n")

    added = skipped = missing = 0
    for word, mnemonic in items:
        if word in existing_words and not args.allow_duplicates:
            print(f"= {word}  (already in collection, skipped)")
            skipped += 1
            continue

        entry = lookup(entries, word)
        if not entry:
            print(f"? {word}  NOT IN DICTIONARY - add this one by hand")
            missing += 1
            continue

        pinyin = numbered_to_marks(entry["pinyin"])
        meaning = "; ".join(entry["glosses"][: args.max_glosses])
        examples = build_examples(word, entries, contains, known, args.examples)

        fields = {mapping["hanzi"]: word}
        if "pinyin" in mapping:
            fields[mapping["pinyin"]] = pinyin
        if "meaning" in mapping:
            fields[mapping["meaning"]] = meaning
        if "examples" in mapping and examples:
            fields[mapping["examples"]] = "<br>".join(examples)
        if "mnemonic" in mapping and mnemonic:
            fields[mapping["mnemonic"]] = mnemonic

        scene = {}
        if hmm:
            scene = hmm.scene(entry["pinyin"])
            props = hmm.prop_list(word)
            for key in ("actor", "set", "room", "initial", "final", "tone"):
                if key in mapping and scene.get(key):
                    fields[mapping[key]] = scene[key]
            if "props" in mapping and props:
                fields[mapping["props"]] = "<br>".join(props)

        print(f"+ {word}  {pinyin}  -  {meaning[:60]}")
        if scene:
            print(f"      {scene['actor']} / {scene['set']} / {scene['room']}")
            print(f"      props: {', '.join(props) if props else '-'}")
        for ex in examples:
            print(f"      {ex[:70]}")

        if args.audio and "audio" in mapping:
            if args.dry_run:
                print("      [audio would be downloaded]")
            else:
                data = fetch_audio(word)
                if data:
                    fname = f"mb_{hashlib.md5(word.encode()).hexdigest()[:12]}.mp3"
                    invoke("storeMediaFile", filename=fname,
                           data=base64.b64encode(data).decode())
                    fields[mapping["audio"]] = f"[sound:{fname}]"
                time.sleep(args.delay)

        if not args.dry_run:
            invoke("addNote", note={
                "deckName": args.deck,
                "modelName": args.note_type,
                "fields": fields,
                "tags": ["mb-auto"] + (args.tag or []),
                "options": {"allowDuplicate": args.allow_duplicates},
            })
        added += 1
        known.update(HANZI_RE.findall(word))
        existing_words.add(word)

    if hmm:
        report_gaps(hmm)
    verb = "would add" if args.dry_run else "added"
    print(f"\n{verb} {added}, skipped {skipped}, not found {missing}")
    if args.sync and not args.dry_run and added:
        invoke("sync")


def cmd_migrate_props(args):
    """One-off: push actors.csv/sets.csv/props.csv into the component deck."""
    component_deck = resolve_component_deck(args.config, args.component_deck)
    ensure_component_model()
    store = ComponentStore(component_deck)

    created = skipped = 0
    for kind, fname in (("actor", "actors.csv"), ("set", "sets.csv"), ("prop", "props.csv")):
        data = load_csv_map(os.path.join(args.config, fname))
        for key, value in sorted(data.items()):
            if store.has(kind, key):
                skipped += 1
                continue
            verb = "would add" if args.dry_run else "add"
            print(f"{verb}  {pad(kind, 6)}{pad(key, 8)}{value}")
            if not args.dry_run:
                store.add(kind, key, value)
            created += 1

    verb = "would create" if args.dry_run else "created"
    print(f"\n{verb} {created}, already present {skipped}")


def cmd_syllables(args):
    entries, _ = load_cedict(ensure_cedict(args.cedict))
    cp = load_hmm_ini(args.config)
    overrides = {}
    if cp.has_section("syllables"):
        for syl, val in cp.items("syllables"):
            parts = [p.strip() for p in val.split(",", 1)]
            if len(parts) == 2:
                overrides[syl.strip()] = (parts[0], parts[1])

    computed = all_syllables(entries)
    rows = []
    for base, (a, s_, _t) in computed.items():
        if base in overrides:
            oa, os_ = overrides[base]
            rows.append((base, oa, os_, True))
        else:
            rows.append((base, a, s_, False))

    if args.diff:
        rows = [r for r in rows if r[3]]
        if not rows:
            print("no overrides in hmm.ini")
            return

    def rank(row):
        base, a, _s, _o = row
        return (0 if a == NULL else 1, len(a), a, base)

    print(pad("syllable", 12) + pad("actor", 8) + pad("set", 8) + "override")
    print("-" * 40)
    for base, a, s_, is_override in sorted(rows, key=rank):
        print(pad(base, 12) + pad(a, 8) + pad(s_, 8) + ("yes" if is_override else ""))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("inspect", help="list decks, note types and field names")

    i = sub.add_parser("hmm-init", help="create the component note type and hmm.ini")
    i.add_argument("--config", default=None)
    i.add_argument("--cedict")
    i.add_argument("--force", action="store_true", help="overwrite an existing hmm.ini")

    c = sub.add_parser("hmm-check", help="preview the scaffolding for a word list")
    c.add_argument("input")
    c.add_argument("--config", default=None)
    c.add_argument("--cedict")
    c.add_argument("--component-deck", help="deck holding your props/actors/sets")

    sh = sub.add_parser("shell", help="type characters in, one at a time (the usual way in)")
    sh.add_argument("--deck")
    sh.add_argument("--note-type")
    sh.add_argument("--component-deck", help="deck holding your props/actors/sets")
    sh.add_argument("--audio", action="store_true")
    sh.add_argument("--config", default=None)
    sh.add_argument("--cedict")

    a = sub.add_parser("add", help="create notes from a word list")
    a.add_argument("input", help="text file, one hanzi/word per line")
    a.add_argument("--deck", required=True)
    a.add_argument("--note-type", required=True)
    a.add_argument("--component-deck", help="deck holding your props/actors/sets")
    a.add_argument("--map", nargs="*", metavar="key=Field",
                   help="override auto field mapping, e.g. hanzi=Character meaning=Keyword")
    a.add_argument("--audio", action="store_true", help="download TTS audio")
    a.add_argument("--examples", type=int, default=5, help="max example words (default 5)")
    a.add_argument("--max-glosses", type=int, default=4)
    a.add_argument("--cedict", help="path to a local cedict txt instead of downloading")
    a.add_argument("--tag", nargs="*")
    a.add_argument("--allow-duplicates", action="store_true")
    a.add_argument("--delay", type=float, default=0.4, help="seconds between audio fetches")
    a.add_argument("--sync", action="store_true", help="sync to AnkiWeb when done")
    a.add_argument("--config", default=None)
    a.add_argument("--no-hmm", action="store_true", help="skip actor/set/prop filling")
    a.add_argument("--dry-run", action="store_true")

    sd = sub.add_parser("savedir", help="show or change where mb_anki keeps its files")
    sd.add_argument("path", nargs="?", help="new save directory (omit to just show current)")

    mp = sub.add_parser("migrate-props",
                        help="one-off: push actors/sets/props CSVs into the component deck")
    mp.add_argument("--config", default=None)
    mp.add_argument("--component-deck", help="deck holding your props/actors/sets")
    mp.add_argument("--dry-run", action="store_true")

    sy = sub.add_parser("syllables", help="print the computed initial/final split")
    sy.add_argument("--config", default=None)
    sy.add_argument("--cedict")
    sy.add_argument("--diff", action="store_true", help="only rows overridden in hmm.ini")

    args = p.parse_args(sys.argv[1:] or ["shell"])
    if hasattr(args, "config"):
        args.config = resolve_config_dir(args.config)
    {"inspect": cmd_inspect, "hmm-init": cmd_hmm_init, "hmm-check": cmd_hmm_check,
     "shell": cmd_shell, "add": cmd_add, "savedir": cmd_savedir,
     "migrate-props": cmd_migrate_props,
     "syllables": cmd_syllables}[args.cmd](args)


if __name__ == "__main__":
    main()
