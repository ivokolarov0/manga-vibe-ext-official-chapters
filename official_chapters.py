"""Official chapters: the newest chapter out on the series' official sites, shown next to AniList's.

AniList's chapter count is often wrong (it counts extras, or lags behind). This plugin looks at the
official links the series already has (from AniList and MangaUpdates) and finds the newest regular
chapter there. It never changes AniList's number; it adds an "Official sources" list to the series:
each source and its number, the highest first, to compare.

Where it looks:
1. Every site in the original language (Japanese, Korean, Chinese): they have every chapter that is out.
2. MangaUpdates: the original edition's count, e.g. "120 Chapters (Ongoing)". Always asked too.
3. English sites (MANGA Plus, Manga UP!, K MANGA, VIZ...): only when 1 and 2 gave no number, since
   they have only the translated chapters.

How a page is read:
- Sites built on GigaViewer (Shonen Jump+, Comic Days, Magazine Pocket...) have an RSS feed per series.
- Manga UP! has its chapter list in the page's data.
- Any other page: the chapter labels in its text ("第12話", "Chapter 12", "Chapters 44-47": a range
  counts as its last number). Only a page without any goes to an Ollama model (optional, set in
  Settings), and the model's answer is only used if that number is actually on the page.

When no original-language site gives a number (MangaUpdates' count can be wrong), the plugin also
searches DuckDuckGo (its plain HTML page: free, no key); the Web search setting can make it always or never.
An official site's result title like "[第250話]ダンダダン | 少年ジャンプ＋" is read like a page;
otherwise the Ollama model (if set) reads the results, and its number is only used if it is in the
result the model names.

Only normal requests are made, with MangaVibe's own user agent. A site that refuses them is skipped.
"""
import html
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from app.models import Link, Series
from app.plugins import Action, FieldDef, Plugin, SettingDef

SOURCES = "official_sources"  # every source asked and its number, one per line, to compare
OLD_FIELD = "official_chapters"  # version 0.1 showed the highest number on a line of its own
MU_API = "https://api.mangaupdates.com/v1/series/"
WEB_SEARCH = "https://html.duckduckgo.com/html/"
PAGE_TEXT = 15000   # characters of a page's text the model gets
RESULT_TEXT = 1000  # characters of each search result the model gets (DuckDuckGo's snippets are shorter)
MAX_RESULTS = 15
MAX_WEB_SOURCES = 10  # sites from a web search listed under Official sources, to compare
MAL_CHAPTERS_RE = re.compile(r'<span class="dark_text">Chapters:</span>\s*(\d+)')
AI_WAIT = 180       # seconds to wait for the model's answer

# Original-language sites that aren't on a .jp domain.
ORIGINAL_HOSTS = {
    "comic.pixiv.net", "comic-days.com", "comic-walker.com", "comic-fuz.com",
    "magcomi.com", "comic-action.com", "kuragebunch.com", "comic-zenon.com", "comic-gardo.com",
    "comic-trail.com", "comic-earthstar.com", "comic-growl.com", "comicborder.com", "comic-ogyaaa.com",
    "shonenjumpplus.com", "pocket.shonenmagazine.com", "manga-park.com", "beltoon.jp",
    "ganganonline.com", "magazine.jp.square-enix.com", "pixiv.net", "sunday-webry.com", "mangaone.com",
    "series.naver.com", "comic.naver.com", "page.kakao.com", "webtoon.kakao.com",
    "manga.bilibili.com", "ac.qq.com",
}
# English sites. Checked before the .jp rule: MANGA Plus and Manga Mee are on .co.jp.
ENGLISH_HOSTS = {
    "mangaplus.shueisha.co.jp", "mangamillion.shueisha.co.jp", "global.manga-up.com",
    "kmanga.kodansha.com", "viz.com", "shonenjump.com", "comikey.com", "comics.inkr.com",
    "mangaplaza.com", "coolmic.me", "manta.net", "webtoons.com", "tappytoon.com", "tapas.io",
    "azuki.co", "global.bookwalker.jp", "bookwalker.com", "omoi.com",
}
# Shops and publisher pages that list volumes, not chapters: not worth a request.
SKIP_HOSTS = {
    "yenpress.com", "kodansha.us", "sevenseasentertainment.com", "tokyopop.com", "darkhorse.com",
    "kadokawa.co.jp", "shogakukan-comic.jp", "shueisha.co.jp", "kc.kodansha.co.jp",
    "kodansha.co.jp", "anilist.co", "myanimelist.net", "mangaupdates.com", "twitter.com", "x.com",
}
# Japanese e-book stores that cut each volume into small paid parts and number those: "全172話" on
# Mecha Comic is SIX HALF's 11 volumes, not its chapters; Piccoma's "39話" for あした死ぬには、 is
# about 23 chapters. Skipped as links and in search results. Subdomains count too (sp.handycomic.jp).
STORE_HOSTS = {
    "piccoma.com", "mechacomic.jp", "cmoa.jp", "booklive.jp", "ebookjapan.yahoo.co.jp", "renta.papy.co.jp",
    "manga.line.me", "handycomic.jp", "k-manga.jp", "book.dmm.com",
}

# "第12話", "12話", "Chapter 12", "Ch. 12.5", "Episode 12", "Ep 12", "#12", and ranges:
# "Chapters 44-47", "第1～4話" (a volume's worth on a store page). A range counts as its last number.
_TO = r"(?:\.\d+)?(?:\s*[-–~〜]\s*(\d+))?"
CHAPTER_RE = re.compile(
    rf"第\s*(\d+){_TO}\s*[話回]|(\d+){_TO}\s*話|\b(?:chapters?|chs?|episodes?|eps?)\.?\s*:?\s*\(?\s*(\d+){_TO}|#\s*(\d+)",
    re.I)
MU_CHAPTERS_RE = re.compile(r"(\d+)\s*(?:chapters?|chs?\b|話)", re.I)


def _host(url):
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_store(url):
    host = _host(url)
    return any(host == h or host.endswith("." + h) for h in STORE_HOSTS)


def tier(url):
    """0 original language, 2 English, None not used. (MangaUpdates is tier 1.)"""
    host = _host(url)
    if not host or host in SKIP_HOSTS or is_store(url):
        return None
    if host in ENGLISH_HOSTS:
        return 2
    if host in ORIGINAL_HOSTS or host.endswith(".jp") or host.endswith(".kr"):
        return 0
    return None


def chapter_numbers(texts):
    """Every regular chapter number in the texts (decimals like 44.5 count as 44)."""
    out = []
    for t in texts:
        t = unicodedata.normalize("NFKC", t)  # full-width digits: ２０１ -> 201
        for m in CHAPTER_RE.finditer(t):
            g = m.groups()
            for start, end in ((g[0], g[1]), (g[2], g[3]), (g[4], g[5]), (g[6], None)):
                if start:
                    out.append(int(end or start))
                    break
    return out


def newest(texts):
    nums = [n for n in chapter_numbers(texts) if 0 < n < 10000]
    return max(nums) if nums else None


def mu_chapters(status_text):
    """'120 Chapters (Ongoing)' -> 120. MangaUpdates' status is about the original edition."""
    nums = [int(n) for n in MU_CHAPTERS_RE.findall(unicodedata.normalize("NFKC", status_text or ""))]
    return max(nums) if nums else None


def sources_text(rows):
    """One line per source, the highest number first: "Omoi: 47", "MangaUpdates: no chapter number found"."""
    rows = sorted(rows, key=lambda r: -(r["chapters"] or 0))  # stable: equal numbers keep their order
    return "\n".join(f"{r['source']}: {r['chapters'] or 'no chapter number found'}" for r in rows) or None


def page_text(raw):
    """A page's visible text, short enough for a model."""
    raw = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


class OfficialChapters(Plugin):
    id = "official-chapters"
    name = "Official chapters"
    version = "0.6.0"
    description = "Finds the newest chapter on the series' official sites (Japanese first) and shows it next to AniList's count."
    repo = "ivokolarov0/manga-vibe-ext-official-chapters"
    permissions = [
        "Connects to the official sites in each series' links",
        "Connects to api.mangaupdates.com",
        "Sends page text to your Ollama server (only if one is set)",
        "Searches DuckDuckGo for a series' titles when its links give no number (can be turned off)",
        "Reads the series' MyAnimeList page when the web search finds it",
    ]
    fields = [FieldDef(SOURCES, "Official sources")]
    actions = [Action("check", "Check official chapters")]
    global_actions = [Action("check_all", "Check all series")]
    settings_fields = [
        SettingDef("ollama_url", "Ollama server (optional)",
                   help="e.g. http://192.168.1.20:11434. Reads sites the plugin has no parser for. Leave empty to skip them."),
        SettingDef("model", "Model", default="gemma4", help="Any text model works, e.g. gemma4 or qwen3"),
        SettingDef("web_search", "Web search", default="auto",
                   options=[("auto", "Unless an official site has a number"), ("always", "Always"), ("off", "Off")],
                   help="Search DuckDuckGo for the series, also when only MangaUpdates or English sites have a "
                        "number (MangaUpdates can be wrong). Every site it finds is listed, and the number most "
                        "of them and MangaUpdates agree on is named. Official sites' results are read "
                        "directly; other results need the Ollama server above."),
    ]

    def __init__(self):
        super().__init__()
        self.progress = None  # "Checking 12/300" while Check all runs

    # ------------------------------------------------------------------ buttons
    def run_action(self, ctx, key, series):
        result = self.check(ctx, series)
        if result is None:
            return "No official chapter count found in this series' links"
        others = len(ctx.get_series_data(series, "last")["sources"]) - 1
        return (f"Official: chapter {result['chapters']} ({result['source']})"
                + (f", {others} more source{'s' if others > 1 else ''} under Details" if others > 0 else ""))

    def run_global_action(self, ctx, key):
        if key != "check_all":
            return "Unknown action"
        if self.progress:
            return "Already running"
        self.progress = "Starting…"
        ctx.run_in_thread(self._check_all)  # its own thread: hours of work mustn't hold up other plugins
        return "Checking every series in the background. Progress shows here."

    def status(self, ctx):
        if self.progress:
            return self.progress
        last = ctx.settings.get("last_run")
        ai = "with Ollama" if ctx.setting("ollama_url") else "without Ollama (only sites it has a parser for)"
        mode = web_mode(ctx)
        if mode == "always":
            ai += ", and searches the web for every series"
        elif mode == "auto":
            ai += ", and searches the web when no official site has a number"
        return f"Last check of all series: {last}. Reads pages {ai}." if last else f"Reads pages {ai}."

    def _check_all(self, ctx):
        try:
            ids = [sid for (sid,) in ctx.db.query(Series.id).filter(Series.missing_since.is_(None))]
            found = 0
            for i, sid in enumerate(ids, 1):
                if ctx.should_stop():
                    return
                self.progress = f"Checking {i}/{len(ids)} · found {found}"
                s = ctx.db.get(Series, sid)
                try:
                    found += self.check(ctx, s) is not None
                except Exception:
                    ctx.log.exception("Official chapters: %s failed", s.folder_name)
                ctx.db.commit()
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            ctx.save_settings({**ctx.settings, "last_run": f"{stamp}, {found} of {len(ids)} found"})
            ctx.db.commit()
        finally:
            self.progress = None

    # ------------------------------------------------------------------ the check
    def check(self, ctx, series):
        """Ask every source, save the list on the series, and return the headline
        {chapters, source, url} or None. Websites are asked before anything is written."""
        best, rows = self.find(ctx, series)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ctx.set_series_data(series, "last", {"best": best, "sources": rows, "checked": stamp})
        ctx.set_field(series, SOURCES, sources_text(rows))
        ctx.set_field(series, OLD_FIELD, None)  # version 0.1's separate headline line
        return best

    def find(self, ctx, series):
        """(headline, every source asked). Every original-language site and MangaUpdates are always
        asked, so they can be compared. The web search runs when no original-language site has a
        number (or always, or never: the Web search setting); each site it finds is listed. English
        sites only when nothing else gave a number. The headline is the highest original-language
        site (a site can lag behind the others); else the number most web sources and MangaUpdates
        agree on (a tie: the highest); else the highest English one."""
        links = ctx.db.query(Link).filter_by(series_id=series.id).all()
        by_tier = {0: [], 2: []}
        for link in links:
            t = tier(link.url)
            if t is not None:
                by_tier[t].append(link)
        title = series.title_english or series.title_romaji or series.folder_name

        def ask(link):
            return {"source": link.site or _host(link.url), "url": link.url,
                    "chapters": self.from_page(ctx, link.url, title)}

        original = [ask(link) for link in by_tier[0]]
        rows = list(original)
        if series.mangaupdates_id:
            mu = {"source": "MangaUpdates", "url": None, "chapters": self.from_mangaupdates(ctx, series)}
            rows.append(mu)
        else:
            mu = None
        mode = web_mode(ctx)
        web = []
        if mode == "always" or (mode == "auto" and not any(r["chapters"] for r in original)):
            web = [{"source": f"{_host(url)} (web search)", "url": url, "chapters": n}
                   for n, url in self.from_web_search(ctx, series)]
            rows += web or [{"source": "Web search", "url": None, "chapters": None}]
        if any(r["chapters"] for r in original):
            # the highest official site (one can lag behind the others), or the web's agreed number
            best = max((r for r in original + [vote(web)] if r and r["chapters"]), key=lambda r: r["chapters"])
        else:
            # no official site: the number most web sources and MangaUpdates agree on
            best = vote(web + ([mu] if mu else []))
        if best is None:
            english = [ask(link) for link in by_tier[2]]
            rows += english
            best = max((r for r in english if r["chapters"]), key=lambda r: r["chapters"], default=None)
        return best, rows

    # ------------------------------------------------------------------ sources
    @staticmethod
    def _get(ctx, url):
        try:
            return ctx.http.request("GET", url, retries=1).text
        except ctx.http.ProviderError as e:
            ctx.log.info("Official chapters: %s skipped (%s)", url, e)
            return None

    def from_page(self, ctx, url, title):
        raw = self._get(ctx, url)
        if not raw:
            return None
        n = self.from_rss(ctx, url, raw) or self.from_next_data(raw)
        if n:
            return n
        text = page_text(raw)
        # chapter labels in the page's own text ("第12話", "Chapters 44-47") beat a model's reading
        return newest([text]) or self.from_ai(ctx, title, text)

    def from_rss(self, ctx, url, raw):
        """GigaViewer sites (Shonen Jump+, Comic Days...) link a feed of the series' chapters."""
        m = re.search(r"""["'](https?://[^"']+/rss/series/\d+|/rss/series/\d+)["']""", raw)
        if not m:
            return None
        feed = self._get(ctx, urljoin(url, m.group(1)))
        if not feed:
            return None
        return newest(html.unescape(t) for t in re.findall(r"<title>(.*?)</title>", feed, re.S))

    @staticmethod
    def from_next_data(raw):
        """Manga UP! (and other Next.js sites) put the chapter list in the page's __NEXT_DATA__."""
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
        except ValueError:
            return None
        chapters = (((data.get("props") or {}).get("pageProps") or {}).get("data") or {}).get("chapters")
        if not isinstance(chapters, list):
            return None
        return newest(str(c.get("mainName") or c.get("name") or "") for c in chapters if isinstance(c, dict))

    def from_mangaupdates(self, ctx, series):
        mu = series.mangaupdates_id
        if not mu:
            return None
        mu_id = mu if str(mu).isdigit() else str(int(str(mu), 36))  # the site's URLs use base 36
        try:
            data = ctx.http.get_json(MU_API + mu_id, retries=1)
        except ctx.http.ProviderError as e:
            ctx.log.info("Official chapters: MangaUpdates %s skipped (%s)", mu_id, e)
            return None
        return mu_chapters(data.get("status"))

    def from_ai(self, ctx, title, text):
        if not text:
            return None
        text = text[:PAGE_TEXT]
        prompt = (
            f"This is the text of an official page for the manga \"{title}\".\n"
            "What is the number of the newest regular chapter (episode) that has been released?\n"
            "Chapters you must pay for, or buy a volume to unlock, count too. A range like "
            "\"Chapters 44-47\" means the chapters go up to 47.\n"
            "Ignore extras, specials, bonus or side stories, previews, announcements, and volume numbers. "
            "If the page doesn't show chapter numbers, answer null.\n"
            'Answer only with JSON: {"last_chapter": <number or null>}\n\n'
            f"PAGE TEXT:\n{text}"
        )
        answer = self._ask_model(ctx, prompt, {"last_chapter": {"type": ["integer", "null"]}})
        n = answer.get("last_chapter") if answer else None
        if not isinstance(n, int) or n <= 0:
            return None
        if not on_page(n, text):  # a made-up number is worse than none
            ctx.log.info("Official chapters: the model said %s for %s, but that's not on the page", n, title)
            return None
        return n

    # ------------------------------------------------------------------ web search
    def from_web_search(self, ctx, series):
        """[(chapter, URL)], one per site, from a DuckDuckGo search: [] if nothing reliable was found.
        The model reads every result, and each number must be a chapter label in its own result.
        Official sites' result titles that name the series are read too ("[第250話]ダンダダン | 少年ジャンプ＋")."""
        titles = [t for t in (series.title_native, series.title_english, series.title_romaji) if t]
        if not titles:
            titles = [series.folder_name]
        queries = []
        if series.title_native:
            queries.append(f"{series.title_native} 漫画 最新話")  # "manga, newest chapter"
        # "how many chapters" finds databases (MyAnimeList, Wikipedia, Kenmei); "latest chapter" found
        # mostly reading sites
        queries.append(f'"{series.title_english or series.title_romaji or series.folder_name}" manga how many chapters')
        results, seen = [], set()
        for q in queries:
            for r in self._search(ctx, q):
                if r["url"] not in seen and not is_store(r["url"]):
                    seen.add(r["url"])
                    results.append(r)
        results = results[:MAX_RESULTS]
        if not results:
            return []

        found = self._read_results(ctx, titles, results) + self._from_mal(ctx, titles, results)
        names = [unicodedata.normalize("NFKC", t).casefold() for t in titles]
        for r in results:
            title = unicodedata.normalize("NFKC", r["title"]).casefold()
            # only the known official sites: other .jp pages include spoiler blogs ("第251話ネタバレ")
            n = newest([r["title"]]) if _host(r["url"]) in ORIGINAL_HOSTS and any(t in title for t in names) else None
            if n:
                found.append((n, r["url"]))
        by_site = {}  # one number per site, its highest (Jump+ shows chapter 219 and 250)
        for n, url in found:
            if n > by_site.get(_host(url), (0, None))[0]:
                by_site[_host(url)] = (n, url)
        return sorted(by_site.values(), reverse=True)[:MAX_WEB_SOURCES]

    def _from_mal(self, ctx, titles, results):
        """[(chapters, URL)] from the first MyAnimeList manga page in the results: its "Chapters: 21"
        (an ongoing series says "Unknown": nothing). The page's title must name the series."""
        r = next((r for r in results if re.match(r"https?://(www\.)?myanimelist\.net/manga/\d+", r["url"])), None)
        raw = self._get(ctx, r["url"]) if r else None
        if not raw:
            return []
        page_title = re.search(r"<title>(.*?)</title>", raw, re.S)
        if not page_title or not any(plain(t) and plain(t) in plain(html.unescape(page_title.group(1)))
                                     for t in titles):
            ctx.log.info("Official chapters: %s is about another series", r["url"])
            return []
        m = MAL_CHAPTERS_RE.search(raw)
        return [(int(m.group(1)), r["url"])] if m and int(m.group(1)) > 0 else []

    def _read_results(self, ctx, titles, results):
        """[(chapter, URL)] the model reads from the search results, one per result that shows this
        manga's chapter count. Each number must be a chapter label (第250話, Chapter 250) in the result
        the model names: a bare number could be an anime season or a date. If the model names a wrong
        result (it once gave the chapter as the result's number), another result with that label counts."""
        def labels(r):  # chapter labels in what the model gets of a result
            seen = f"{r['title']} {r['content'][:RESULT_TEXT]}"
            # a store's "配信話数 全172話" (parts on sale) isn't a chapter label
            return chapter_numbers([re.sub(r"配信話数\s*全?\s*\d+\s*話", " ", seen)])

        listing = "\n\n".join(f"RESULT {i}: {r['title']}\nURL: {r['url']}\n{r['content'][:RESULT_TEXT]}"
                              for i, r in enumerate(results, 1))
        names = " / ".join(f'"{t}"' for t in titles)
        prompt = (
            f"These are web search results about the manga (Japanese comic) {names}.\n"
            "For each result that shows it: what is the number of the newest regular chapter of this manga "
            "released so far in its original language (Japanese, Korean or Chinese), or its total number "
            "of chapters if it has ended?\n"
            "Only use results about this exact manga. Ignore results about its anime (episode counts), "
            "light novel, a different series with a similar name, volume counts, and English release counts "
            "if the original is further along. Ignore extras, specials and side stories. Ignore spoiler and "
            "prediction pages (ネタバレ, 予想, raw), which name chapters that aren't out yet. Ignore e-book "
            "stores' episode counts (配信話数, 全N話 on a store), which cut volumes into smaller paid parts.\n"
            "List every result that shows it, each with its own number; leave out the others. If no "
            "result shows it, answer with an empty list. Do not guess.\n"
            "Answer only with JSON: "
            '{"sources": [{"result_number": <which RESULT, from 1 to ' + str(len(results)) + ' (the number '
            'after the word RESULT, not the chapter number)>, "last_chapter": <the chapter number>}, ...]}\n\n'
            + listing
        )
        item = {"type": "object", "properties": {"result_number": {"type": "integer"},
                                                 "last_chapter": {"type": "integer"}},
                "required": ["result_number", "last_chapter"]}
        answer = self._ask_model(ctx, prompt, {"sources": {"type": "array", "items": item}})
        sources = answer.get("sources") if answer else None
        found = []
        for s in sources if isinstance(sources, list) else []:
            n, i = (s.get("last_chapter"), s.get("result_number")) if isinstance(s, dict) else (None, None)
            if not isinstance(n, int) or n <= 0:
                continue
            named = [results[i - 1]] if isinstance(i, int) and 1 <= i <= len(results) else []
            r = next((r for r in named + results if n in labels(r)), None)
            if r:
                found.append((n, r["url"]))
            else:
                ctx.log.info("Official chapters: the model said %s for %s, but no search result shows it",
                             n, titles[0])
        return found

    @staticmethod
    def _search(ctx, query):
        """DuckDuckGo's results: [{title, url, content}], or [] if it failed or refused."""
        try:
            raw = ctx.http.request("GET", WEB_SEARCH, retries=1, params={"q": query}).text
        except ctx.http.ProviderError as e:
            ctx.log.warning("Official chapters: web search failed: %s", e)
            return []
        results = search_results(raw)
        if not results and re.search(r"anomaly|challenge", raw, re.I):
            ctx.log.warning("Official chapters: DuckDuckGo asked to confirm a human is searching; skipped")
        return results

    # ------------------------------------------------------------------ the model
    @staticmethod
    def _ask_model(ctx, prompt, properties):
        """The model's JSON answer (a dict), or None if no Ollama server is set or it failed."""
        base = str(ctx.setting("ollama_url") or "").strip().rstrip("/")
        if not base:
            return None
        if not re.match(r"^https?://", base):
            base = "http://" + base
        base = re.sub(r"/v1$", "", base)
        body = {"model": ctx.setting("model") or "gemma4", "stream": False, "think": False,
                "messages": [{"role": "user", "content": prompt}],
                "format": {"type": "object", "properties": properties, "required": list(properties)},
                "options": {"temperature": 0}}
        try:
            with httpx.Client(timeout=httpx.Timeout(AI_WAIT, connect=15), transport=ctx.http.transport) as c:
                r = c.post(base + "/api/chat", json=body)
                if r.status_code == 400 and "think" in r.text:  # older Ollama
                    del body["think"]
                    r = c.post(base + "/api/chat", json=body)
                r.raise_for_status()
                answer = json.loads(r.json()["message"]["content"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            ctx.log.warning("Official chapters: Ollama failed (%s)", e)
            return None
        return answer if isinstance(answer, dict) else None


def plain(text):
    """Letters and digits only, for comparing titles: "Ashita Shinu ni wa," -> "ashitashinuniwa"."""
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def vote(rows):
    """The number most of the rows agree on (a tie: the highest), as a headline row; None if none has one."""
    rows = [r for r in rows if r and r["chapters"]]
    if not rows:
        return None
    counts = Counter(r["chapters"] for r in rows)
    n = max(counts, key=lambda c: (counts[c], c))
    first = next(r for r in rows if r["chapters"] == n)
    if len(rows) == 1:
        return first
    agree = (f"{counts[n]} of {len(rows)} sources agree" if counts[n] > 1
             else f"{len(rows)} sources disagree, the highest")
    return {"source": agree, "url": first["url"], "chapters": n}


def web_mode(ctx):
    """"auto", "always" or "off". Version 0.3.0 saved "on" (only when nothing had a number): now "auto"."""
    mode = ctx.setting("web_search")
    return mode if mode in ("always", "off") else "auto"


def search_results(raw):
    """The results on a DuckDuckGo HTML page, without ads: [{title, url, content}]."""
    out = []
    for block in re.split(r'<div class="result results_links', raw)[1:]:
        if "result--ad" in block[:200]:
            continue
        link = re.search(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        href = html.unescape(link.group(1))
        url = (parse_qs(urlparse(href).query).get("uddg") or [href])[0]  # DuckDuckGo's redirect link
        if not url.startswith("http") or _host(url).endswith("duckduckgo.com"):
            continue
        snippet = re.search(r'class="result__snippet"[^>]*>(.*?)</(?:a|div)>', block, re.S)
        out.append({"title": page_text(link.group(2)), "url": url,
                    "content": page_text(snippet.group(1)) if snippet else ""})
    return out


def on_page(n, text):
    """Is the number n written in the text (full-width digits count)?"""
    return bool(re.search(rf"(?<!\d){n}(?!\d)", unicodedata.normalize("NFKC", text)))
