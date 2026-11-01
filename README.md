# Official chapters for MangaVibe

A [MangaVibe](https://github.com/ivokolarov0/manga-vibe) plugin that finds the newest chapter on a
series' official sites and shows it next to AniList's count. AniList often gets this wrong: it counts
extras as chapters, or is behind. The plugin never changes AniList's number.

## Use

It only runs when you press a button:

- **Check official chapters** on a series page (under Plugin tools): checks that series.
- **Check all series** in Settings → Plugins → Official chapters: checks the whole library in the
  background. Progress shows on that page.

The result shows under the series' **Details**:

```
Official sources    Shonen Jump+: 201
                    MangaUpdates: 199
                    Piccoma: 198
                    ComicWalker: no chapter number found
```

The highest number comes first. AniList's own count stays under **Length**, to compare.

## Where it looks

It uses the links the series already has (from AniList and MangaUpdates):

1. **Every site in the original language** (Japanese, Korean, Chinese): they have every chapter that's out.
2. **MangaUpdates**: the original edition's count, e.g. "120 Chapters (Ongoing)". Always asked too.
3. **English sites** (MANGA Plus, Manga UP!, K MANGA, VIZ, Omoi...): only when 1 and 2 gave no
   number, since they have only the translated chapters.

**Official sources** lists every source asked and its number, so you can compare them (one site can
be behind the others). The button's message names the highest original-language number, else
MangaUpdates', else the highest English one.

How each page is read:

| Site | How |
|---|---|
| Shonen Jump+, Comic Days, Magazine Pocket and other GigaViewer sites | the series' RSS feed |
| Manga UP! | the chapter list in the page's data |
| any other site | the chapter labels in the page's text (`第12話`, `Chapter 12`, `Chapters 44-47`) |
| a page without any | its text goes to your Ollama model, if you set one (see below) |
| no link gave a number | a DuckDuckGo search, read by your Ollama model (see below) |

Extras, specials, previews and announcements without a number are left out. `Chapter 44.4`
counts as chapter 44, and a range like `Chapters 44-47` (a store's volume) as 47.

Shops that only list volumes (Yen Press, Kodansha US, Seven Seas...) and sites in other languages
(e.g. French) are skipped.

## Ollama (optional)

Without it, only the sites in the table above and MangaUpdates are read. With it, the plugin can read
any official page: set **Ollama server** (e.g. `http://192.168.1.20:11434`) and **Model** in the
plugin's settings. Any text model works.

The model's answer is only used if that number is actually on the page, so a made-up number is never
shown.

Some sites show their chapter list only after JavaScript runs, or refuse requests that don't come
from a browser (MANGA Plus does). The plugin doesn't try to get around that; it moves on to the
next link.

## Web search (optional)

When none of the series' links gives a number, the plugin searches DuckDuckGo (its plain HTML page:
free, no account or key). It searches for the Japanese title with 漫画 最新話 ("manga, newest
chapter") and for the English title with "manga latest chapter".

- **With an Ollama server set**, your model reads the results. It's told to ignore anime episodes,
  light novels, spoiler pages and series with similar names. Its number is only used if a result
  shows it as a chapter label (`第250話`, `Chapter 250`), so a made-up number or an anime season
  is never shown.
- **Without one** (or if the model has no answer), the plugin reads the titles of results from the
  official sites, e.g. `[第250話]ダンダダン | 少年ジャンプ＋`.

The result shows as `Web search: 250` under **Official sources**. The **Web search** setting turns
it off.

DuckDuckGo sometimes asks automated searches to prove they're human. The plugin doesn't try to get
around that: the search is skipped (a warning in the log) and the series gets no number from it.

## Install

Copy `official_chapters.py` to the `plugins` folder in MangaVibe's data folder, then press
**Settings → Reload plugins**.

## Tests

```
python -m pytest
```

with MangaVibe's requirements installed and a MangaVibe checkout next to this folder (or set
`MANGAVIBE_DIR`).
