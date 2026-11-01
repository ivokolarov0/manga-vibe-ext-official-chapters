"""Official chapters: the newest chapter out on the series' official sites, shown next to AniList's.

AniList's chapter count is often wrong (it counts extras, or lags behind). This plugin looks at the
official links the series already has (from AniList and MangaUpdates) and finds the newest regular
chapter there. It never changes AniList's number; it adds an "Official chapters" line to the series.

Where it looks, in this order (the first that gives a number wins):
1. Sites in the original language (Japanese, Korean, Chinese): they have every chapter that is out.
2. MangaUpdates: the original edition's count, e.g. "120 Chapters (Ongoing)".
3. English sites (MANGA Plus, Manga UP!, K MANGA, VIZ...): only the translated chapters.

How a page is read:
- Sites built on GigaViewer (Shonen Jump+, Comic Days, Magazine Pocket...) have an RSS feed per series.
- Manga UP! has its chapter list in the page's data.
- Any other page: its text goes to an Ollama model (optional, set in Settings), which picks the newest
  chapter. Its answer is only used if that number is actually on the page.

Only normal requests are made, with MangaVibe's own user agent. A site that refuses them is skipped.
"""
import html
import json
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx

from app.models import Link, Series
from app.plugins import Action, FieldDef, Plugin, SettingDef

FIELD = "official_chapters"
MU_API = "https://api.mangaupdates.com/v1/series/"
PAGE_TEXT = 15000   # characters of a page's text the model gets
AI_WAIT = 180       # seconds to wait for the model's answer

# Original-language sites that aren't on a .jp domain.
ORIGINAL_HOSTS = {
    "comic.pixiv.net", "piccoma.com", "comic-days.com", "comic-walker.com", "comic-fuz.com",
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
    "azuki.co", "global.bookwalker.jp", "bookwalker.com",
}
# Shops and publisher pages that list volumes, not chapters: not worth a request.
SKIP_HOSTS = {
    "yenpress.com", "kodansha.us", "sevenseasentertainment.com", "tokyopop.com", "darkhorse.com",
    "omoi.com", "kadokawa.co.jp", "shogakukan-comic.jp", "shueisha.co.jp", "kc.kodansha.co.jp",
    "kodansha.co.jp", "anilist.co", "myanimelist.net", "mangaupdates.com", "twitter.com", "x.com",
}

# "第12話", "12話", "Chapter 12", "Ch. 12.5", "Episode 12", "Ep 12", "#12"
CHAPTER_RE = re.compile(
    r"第\s*(\d+)\s*[話回]|(\d+)\s*話|\b(?:chapter|ch|episode|ep)\.?\s*(\d+)|#\s*(\d+)", re.I)
MU_CHAPTERS_RE = re.compile(r"(\d+)\s*(?:chapters?|chs?\b|話)", re.I)


def _host(url):
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def tier(url):
    """0 original language, 2 English, None not used. (MangaUpdates is tier 1.)"""
    host = _host(url)
    if not host or host in SKIP_HOSTS:
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
            out.append(int(next(g for g in m.groups() if g)))
    return out


def newest(texts):
    nums = [n for n in chapter_numbers(texts) if 0 < n < 10000]
    return max(nums) if nums else None


def mu_chapters(status_text):
    """'120 Chapters (Ongoing)' -> 120. MangaUpdates' status is about the original edition."""
    nums = [int(n) for n in MU_CHAPTERS_RE.findall(unicodedata.normalize("NFKC", status_text or ""))]
    return max(nums) if nums else None


def page_text(raw):
    """A page's visible text, short enough for a model."""
    raw = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


class OfficialChapters(Plugin):
    id = "official-chapters"
    name = "Official chapters"
    version = "0.1.0"
    description = "Finds the newest chapter on the series' official sites (Japanese first) and shows it next to AniList's count."
    repo = "ivokolarov0/manga-vibe-ext-official-chapters"
    permissions = [
        "Connects to the official sites in each series' links",
        "Connects to api.mangaupdates.com",
        "Sends page text to your Ollama server (only if one is set)",
    ]
    fields = [FieldDef(FIELD, "Official chapters")]
    actions = [Action("check", "Check official chapters")]
    global_actions = [Action("check_all", "Check all series")]
    settings_fields = [
        SettingDef("ollama_url", "Ollama server (optional)",
                   help="e.g. http://192.168.1.20:11434. Reads sites the plugin has no parser for. Leave empty to skip them."),
        SettingDef("model", "Model", default="gemma4", help="Any text model works, e.g. gemma4 or qwen3"),
    ]

    def __init__(self):
        super().__init__()
        self.progress = None  # "Checking 12/300" while Check all runs

    # ------------------------------------------------------------------ buttons
    def run_action(self, ctx, key, series):
        result = self.check(ctx, series)
        if result is None:
            return "No official chapter count found in this series' links"
        return f"Official: chapter {result['chapters']} ({result['source']})"

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
        """Find the count, save it on the series, and return {chapters, source, url} or None."""
        result = self.find(ctx, series)
        if result:
            text = f"{result['chapters']} ({result['source']})"
            if series.chapters_total and series.chapters_total != result["chapters"]:
                text += f" · AniList says {series.chapters_total}"
            result["checked"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            ctx.set_series_data(series, "last", result)
            ctx.set_field(series, FIELD, text)
        return result

    def find(self, ctx, series):
        links = ctx.db.query(Link).filter_by(series_id=series.id).all()
        by_tier = {0: [], 2: []}
        for link in links:
            t = tier(link.url)
            if t is not None:
                by_tier[t].append(link)
        title = series.title_english or series.title_romaji or series.folder_name
        for link in by_tier[0]:
            n = self.from_page(ctx, link.url, title)
            if n:
                return {"chapters": n, "source": link.site or _host(link.url), "url": link.url}
        n = self.from_mangaupdates(ctx, series)
        if n:
            return {"chapters": n, "source": "MangaUpdates", "url": None}
        for link in by_tier[2]:
            n = self.from_page(ctx, link.url, title)
            if n:
                return {"chapters": n, "source": link.site or _host(link.url), "url": link.url}
        return None

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
        return self.from_ai(ctx, title, page_text(raw))

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
        base = str(ctx.setting("ollama_url") or "").strip().rstrip("/")
        if not base or not text:
            return None
        if not re.match(r"^https?://", base):
            base = "http://" + base
        base = re.sub(r"/v1$", "", base)
        text = text[:PAGE_TEXT]
        prompt = (
            f"This is the text of an official page for the manga \"{title}\".\n"
            "What is the number of the newest regular chapter (episode) that has been released?\n"
            "Ignore extras, specials, bonus or side stories, previews, announcements, and volume numbers. "
            "If the page doesn't show chapter numbers, answer null.\n"
            'Answer only with JSON: {"last_chapter": <number or null>}\n\n'
            f"PAGE TEXT:\n{text}"
        )
        body = {"model": ctx.setting("model") or "gemma4", "stream": False, "think": False,
                "messages": [{"role": "user", "content": prompt}],
                "format": {"type": "object", "properties": {"last_chapter": {"type": ["integer", "null"]}},
                           "required": ["last_chapter"]},
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
        n = answer.get("last_chapter") if isinstance(answer, dict) else None
        if not isinstance(n, int) or n <= 0:
            return None
        # a made-up number is worse than none: it must be on the page
        if not re.search(rf"(?<!\d){n}(?!\d)", unicodedata.normalize("NFKC", text)):
            ctx.log.info("Official chapters: the model said %s for %s, but that's not on the page", n, title)
            return None
        return n
