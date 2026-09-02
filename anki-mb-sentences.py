#!/usr/bin/env python3
"""
anki-mb-sentences.py - build whole-sentence Anki cards from the terminal.

Companion to anki-mb-addon.py (same "type it, it lands in Anki" idea, but for
sentences instead of characters). Paste a sentence, type its translation, and
it becomes one Anki note with two cards: sentence -> translation and
translation -> sentence. An optional note (your own memory hook) and an
optional list of new/unlearned words are attached to both cards - the tool
suggests the latter by diffing the sentence's characters against whatever's
already in your word deck, but never guesses the translation itself.

Setup (once)
------------
  1. Install the AnkiConnect add-on in Anki (Tools > Add-ons > Get Add-ons,
     code 2055492159), restart Anki, and leave it running.
  2. The first time you run any command, you'll be asked where to save your
     files (settings, history) - the same save directory anki-mb-addon.py
     uses, if you've already set one up there.
  3. python3 anki-mb-sentences.py
     Asks which deck sentence cards go to. 'MB Sentence' is created (or
     repaired, if you've since edited the template HTML here) automatically.

Daily use
---------
  python3 anki-mb-sentences.py

    > paste a sentence
      translation?
      note (optional)?
      new/unlearned words (optional, suggested from your word deck)?
      then a prompt to confirm.

No third-party packages. Python 3.8+.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request

ANKI_CONNECT_URL = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".mb_anki")
APP_CONFIG_PATH = os.path.join(CACHE_DIR, "app_config.json")
SENTENCE_MODEL = "MB Sentence"
DEFAULT_SENTENCE_DECK = "Hanzi Movie Method Sentences"
DEFAULT_WORD_DECK = "Hanzi Movie Method"

HANZI_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")

SENTENCE_FIELDS = ["Sentence", "Translation", "Notes", "NewWords", "Audio"]

SENTENCE_CSS = """\
.card {
  font-family: arial;
  text-align: center;
  color: black;
  background-color: white;
}
.text { font-size: 26px; margin-bottom: 10px; }
.notes { font-size: 15px; color: #888; font-style: italic; margin-top: 10px; }
.new-words { font-size: 14px; color: #666; margin-top: 10px; }
.new-words .label { color: #999; margin-right: 6px; }

/* See anki-mb-addon.py's WORD_CSS for why this is needed: Anki's night mode
   only re-colours .card itself, not these custom classes, so their greys
   (tuned for a white background) would otherwise wash out in the dark. */
.card.night_mode { background-color: #2f2f31; color: #e8e8e8; }
.night_mode .notes { color: #c9c9c9; }
.night_mode .new-words { color: #bfbfbf; }
.night_mode .new-words .label { color: #9e9e9e; }
"""

# 'Sentence' is the sort field (first field) - duplicate detection keys on
# the exact sentence text, same idea as 'Hanzi' in anki-mb-addon.py. Audio is
# only on the Back: hearing the sentence read aloud before you've attempted
# the Translation->Sentence card would give away the reading. NewWords is on
# the Front (and, via {{FrontSide}}, carries onto the Back too) so you have
# that context while attempting the card, not just after.
NEW_WORDS_HTML = ('{{#NewWords}}<div class="new-words"><span class="label">new</span>'
                   '{{NewWords}}</div>{{/NewWords}}')

SENTENCE_TEMPLATES = {
    "Sentence to Translation": {
        "Front": '<div class="text">{{Sentence}}</div>' + NEW_WORDS_HTML,
        "Back": '{{FrontSide}}<hr id=answer><div class="text">{{Translation}}</div>'
                '{{Audio}}'
                '{{#Notes}}<div class="notes">{{Notes}}</div>{{/Notes}}',
    },
    "Translation to Sentence": {
        "Front": '<div class="text">{{Translation}}</div>' + NEW_WORDS_HTML,
        "Back": '{{FrontSide}}<hr id=answer><div class="text">{{Sentence}}</div>'
                '{{Audio}}'
                '{{#Notes}}<div class="notes">{{Notes}}</div>{{/Notes}}',
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
# Note type
# --------------------------------------------------------------------------

def ensure_sentence_model():
    """Create the 'MB Sentence' note type, or bring an existing one in line
    with the fields/templates defined here. Never touches existing notes -
    only the model definition."""
    if SENTENCE_MODEL not in invoke("modelNames"):
        invoke("createModel", modelName=SENTENCE_MODEL, inOrderFields=SENTENCE_FIELDS,
               css=SENTENCE_CSS, cardTemplates=[
                   {"Name": name, **tpl} for name, tpl in SENTENCE_TEMPLATES.items()
               ])
        print(f"created note type '{SENTENCE_MODEL}' ({len(SENTENCE_FIELDS)} fields, "
              f"{len(SENTENCE_TEMPLATES)} card types)")
        return

    fields = invoke("modelFieldNames", modelName=SENTENCE_MODEL)
    missing = [f for f in SENTENCE_FIELDS if f not in fields]
    for f in missing:
        invoke("modelFieldAdd", modelName=SENTENCE_MODEL, fieldName=f)

    current = invoke("modelTemplates", modelName=SENTENCE_MODEL)
    added, changed, to_update = [], [], {}
    for name, tpl in SENTENCE_TEMPLATES.items():
        if name not in current:
            invoke("modelTemplateAdd", modelName=SENTENCE_MODEL, template={"Name": name, **tpl})
            added.append(name)
        elif current[name].get("Front") != tpl["Front"] or current[name].get("Back") != tpl["Back"]:
            to_update[name] = tpl
            changed.append(name)
    if to_update:
        invoke("updateModelTemplates", model={"name": SENTENCE_MODEL, "templates": to_update})

    restyled = invoke("modelStyling", modelName=SENTENCE_MODEL)["css"] != SENTENCE_CSS
    if restyled:
        invoke("updateModelStyling", model={"name": SENTENCE_MODEL, "css": SENTENCE_CSS})

    bits = []
    if missing:
        bits.append(f"added missing field(s): {', '.join(missing)}")
    if added:
        bits.append(f"added card type(s): {', '.join(added)}")
    if changed:
        bits.append(f"updated card type(s): {', '.join(changed)}")
    if restyled:
        bits.append("updated styling")
    print(f"using note type '{SENTENCE_MODEL}'" + (f" ({'; '.join(bits)})" if bits else ""))


def known_chars(deck):
    """Every hanzi already sitting in the given deck's notes, whatever note
    type they're under - used to guess which characters in a new sentence
    are still unlearned. Best-effort: an unrecognised/empty deck just means
    nothing is known yet, not an error."""
    if deck not in invoke("deckNames"):
        return set()
    ids = invoke("findNotes", query=f'deck:"{deck}"')
    known = set()
    if ids:
        for note in invoke("notesInfo", notes=ids):
            for f in note["fields"].values():
                known.update(HANZI_RE.findall(re.sub(r"<[^>]+>", "", f["value"])))
    return known


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


SHELL_HELP = """
  <sentence>  paste a sentence to build a card for
  deck        change the target deck
  where       print the deck and word-deck this session is using
  ?           this help
  q           quit

  At any prompt: enter accepts the default, q abandons the card, ctrl-d quits.
"""


def build_one(sentence, deck, known):
    unknown = list(dict.fromkeys(c for c in HANZI_RE.findall(sentence) if c not in known))
    if unknown:
        print(f"  {C['dim']}new/unlearned:{C['off']} {' '.join(unknown)}")

    translation = ask("  translation? ")
    if not translation:
        print(f"  {C['dim']}dropped (no translation){C['off']}")
        raise Cancel
    notes = ask("  note (optional, your own memory hook)? ")
    suggested = " ".join(unknown)
    new_words = ask("  new/unlearned words used (optional)? ", default=suggested)

    fields = {"Sentence": sentence, "Translation": translation,
              "Notes": notes, "NewWords": new_words, "Audio": ""}

    if not yes(f"  add to {deck}?"):
        raise Cancel

    data = fetch_audio(sentence)
    if data:
        fname = f"mb_sent_{hashlib.md5(sentence.encode()).hexdigest()[:12]}.mp3"
        invoke("storeMediaFile", filename=fname, data=base64.b64encode(data).decode())
        fields["Audio"] = f"[sound:{fname}]"

    if deck not in invoke("deckNames"):
        invoke("createDeck", deck=deck)
    try:
        invoke("addNote", note={"deckName": deck, "modelName": SENTENCE_MODEL,
                                "fields": fields, "tags": ["mb-sentence"],
                                "options": {"allowDuplicate": False}})
    except RuntimeError as exc:
        print(f"  {C['red']}Anki refused it:{C['off']} {exc}")
        return
    known.update(HANZI_RE.findall(sentence))
    print(f"  {C['green']}added{C['off']} {C['dim']}(2 cards){C['off']}")


def cmd_shell(args):
    cfg = args.config
    try:
        import atexit
        import readline
        hist = os.path.join(cfg, ".sentences-history")
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
    current_deck = settings.get("sentence_deck", DEFAULT_SENTENCE_DECK)
    word_deck = settings.get("deck", DEFAULT_WORD_DECK)
    # The remembered/default deck may not exist in Anki yet - choose() only
    # lists real decks, so it has to be added here or a brand-new deck name
    # could never be selected, first-run or not.
    deck = args.deck or choose("Which deck should sentences go to?",
                                sorted(set(decks) | {current_deck}), current_deck)
    save_settings(cfg, {"sentence_deck": deck})

    ensure_sentence_model()
    known = known_chars(word_deck)

    print(f"\n  {C['bold']}MB Sentences{C['off']}  {C['dim']}·  "
          f"{len(known)} known character(s) from '{word_deck}'{C['off']}")
    print(f"  {C['dim']}deck{C['off']} {deck}")
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
                deck = choose("Which deck should sentences go to?", sorted(invoke("deckNames")))
                save_settings(cfg, {"sentence_deck": deck})
                continue
            if low == "where":
                print(f"  deck: {deck}")
                print(f"  word deck (for new-word detection): {word_deck}")
                continue
            if not HANZI_RE.search(raw):
                print(f"  {C['red']}Not Chinese text.{C['off']} ? for help.")
                continue

            build_one(raw, deck, known)

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
    print(f"  {C['dim']}(settings and history - shared with anki-mb-addon.py if you use both){C['off']}")
    try:
        val = input(f"  save directory [{default}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        val = ""
    return os.path.expanduser(val) if val else default


def resolve_config_dir(explicit):
    """Where settings/history live. Priority: --config flag > MB_CONFIG env
    > saved choice from a previous run (shared with anki-mb-addon.py) > ask."""
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
    p.add_argument("--deck", help="deck to add sentence cards to")
    p.add_argument("--config", default=None, help="save directory (default: shared with anki-mb-addon.py)")
    args = p.parse_args()
    args.config = resolve_config_dir(args.config)
    cmd_shell(args)


if __name__ == "__main__":
    main()
