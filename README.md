# 🀄 Mandarin Learning Resources

Free tools, links, and resources for learning **Simplified Mandarin Chinese**.

---

## 🌐 All Levels

| Resource | Type | Description | Link |
|----------|------|-------------|------|
| Language Reactor | Browser Extension / App | Dual subtitles (Pinyin + translation) on videos and streaming | [languagereactor.com](https://www.languagereactor.com/settings) |
| Anki | Flashcard App (SRS) | Spaced repetition flashcards | [github.com/ankitects/anki](https://github.com/ankitects/anki) |
| Mandarin Frequency List 1–1000 | Word List | The 1,000 most common Mandarin words | [Wiktionary](https://en.wiktionary.org/wiki/Appendix:Mandarin_Frequency_lists/1-1000) |
| Pleco | Dictionary App | Characters, Pinyin, definitions, stroke order, and example sentences | [pleco.com](https://www.pleco.com) |
| Claude / ChatGPT | AI Tool | Translation, sentence correction, and practice prompts | [claude.ai](https://claude.ai) / [chatgpt.com](https://chatgpt.com) |

## 🟢 Beginner

| Resource | Type | Description | Link |
|----------|------|-------------|------|
| Mandarin Blueprint | Course | Structured course covering characters, pronunciation, and grammar | [courses.mandarinblueprint.com](https://courses.mandarinblueprint.com/library) |
| ChinesePod | Podcast | Dialogue-based lessons; see the "Newbie" and "Elementary" episodes | Search "ChinesePod" on your podcast app |
| Yoyo Chinese | YouTube Channel | Beginner grammar and pronunciation explanations | [youtube.com/@YoyoChinese](https://www.youtube.com/@YoyoChinese) |
| MandarinClick | YouTube Channel | Short stories for listening practice | [youtube.com/@MandarinClick](https://www.youtube.com/@MandarinClick) |
| Paul Noble – Learn Mandarin Chinese | Audio Course | Story-based audio course | Search "Paul Noble Mandarin Chinese" |
| Beginner Mandarin Lesson | Video | Beginner video lesson | [YouTube](https://youtu.be/9lJUTpSSN8c?si=LpfauBrIOO629O8D) |
| I speak 12 languages – copy my 30 min routine | Video | Polyglot's daily routine, including the self-talk audio method | [YouTube](https://youtu.be/_wYjXdt4a2I?si=cQngb9h54ylCF4KK) |

## 🛠️ Tools in This Repo

| Script | Description |
|--------|-------------|
| [`anki-mb-characters.py`](anki-mb-characters.py) | Builds Mandarin Blueprint character cards (actor, set, room, props) in Anki, with recognition and offline stroke-writing cards |
| [`anki-mb-words.py`](anki-mb-words.py) | Builds multi-character word cards from pasted Mandarin Blueprint word blocks, with auto-generated Pinyin (CC-CEDICT) and audio; `fill` backfills existing notes |
| [`anki-mb-sentences.py`](anki-mb-sentences.py) | Builds two-way sentence cards and suggests new words from your existing deck |
| [`add-english.py`](add-english.py) | Adds a spoken English translation before each Chinese sentence in an mp3, and writes a transcript |

The Anki scripts need the [AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on running. See each script's header for setup and usage.
