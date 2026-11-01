"""Tests with fake websites. Run: python -m pytest (with MangaVibe's requirements installed)."""
import json
import shutil
from pathlib import Path

import httpx
import pytest

from conftest import TMP

PID = "official-chapters"
PLUGIN = Path(__file__).parent / "official_chapters.py"

JUMP_PAGE = '<html><link rel="alternate" href="https://shonenjumpplus.com/rss/series/123"></html>'
JUMP_RSS = """<rss><channel><title>少年ジャンプ＋（2.5次元の誘惑）</title>
<item><title>[第２０１話]2.5次元の誘惑</title></item>
<item><title>[最終話予告]2.5次元の誘惑</title></item>
<item><title>[第199話・第200話]2.5次元の誘惑</title></item></channel></rss>"""
MANGA_UP = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": {"data": {
    "chapters": [{"mainName": "Chapter 44.4"}, {"mainName": "Chapter 44.3"}, {"mainName": "Special Extra"},
                 {"mainName": "Chapter 1"}]}}}}) + "</script>"


class FakeWeb:
    def __init__(self):
        self.pages = {}       # url -> (status, text)
        self.asked = []       # urls requested, in order
        self.ai_answer = None  # what Ollama says
        self.ai_result = None  # which search result the model points to
        self.ai_sources = None  # or every source it lists: [{"result_number", "last_chapter"}]
        self.search_results = []  # what DuckDuckGo finds: [(title, url, snippet)]
        self.searches = []        # what was searched for
        self.prompts = []         # what the model was asked

    def __call__(self, request: httpx.Request):
        url = str(request.url)
        self.asked.append(url)
        if url.endswith("/api/chat"):
            self.prompts.append(json.loads(request.content)["messages"][0]["content"])
            # a page: last_chapter; search results: sources (ai_sources, or ai_answer from result ai_result)
            sources = self.ai_sources if self.ai_sources is not None else (
                [{"result_number": self.ai_result, "last_chapter": self.ai_answer}] if self.ai_answer else [])
            answer = {"last_chapter": self.ai_answer, "sources": sources}
            return httpx.Response(200, json={"message": {"content": json.dumps(answer)}})
        if url.startswith("https://html.duckduckgo.com/html/"):
            self.searches.append(request.url.params.get("q"))
            return httpx.Response(200, text=ddg_page(self.search_results))
        status, text = self.pages.get(url, (404, "not found"))
        return httpx.Response(status, text=text)


def ddg_page(results):
    """A DuckDuckGo HTML results page (the same markup as html.duckduckgo.com), with an ad first."""
    from urllib.parse import quote
    blocks = ['<div class="result results_links results_links_deep result--ad "><h2 class="result__title">'
              '<a rel="nofollow" class="result__a" href="https://duckduckgo.com/y.js?ad_domain=shop.example">'
              'Buy 第999話 now</a></h2></div>']
    for title, url, snippet in results:
        link = f"//duckduckgo.com/l/?uddg={quote(url, safe='')}&amp;rut=abc"
        blocks.append(
            f'<div class="result results_links results_links_deep web-result ">'
            f'<div class="links_main links_deep result__body"><h2 class="result__title">'
            f'<a rel="nofollow" class="result__a" href="{link}">{title}</a></h2>'
            f'<a class="result__snippet" href="{link}">{snippet}</a></div></div>')
    return "<html><body>" + "".join(blocks) + "</body></html>"


def import_plugin():
    import importlib.util
    spec = importlib.util.spec_from_file_location("oc_under_test", PLUGIN)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def series_id(client, title):
    return next(s for s in client.get("/api/series").json()["items"] if s["title"] == title)["id"]


def set_links(sid, urls, **values):
    from app.db import SessionLocal
    from app.models import Link, Series
    with SessionLocal() as db:
        db.query(Link).filter_by(series_id=sid).delete()
        for site, url in urls:
            db.add(Link(series_id=sid, site=site, url=url))
        s = db.get(Series, sid)
        for k, v in values.items():
            setattr(s, k, v)
        db.commit()


def fields(client, sid):
    s = client.get(f"/api/series/{sid}").json()
    return {f["key"]: f["value"] for f in s["plugin_fields"] if f["plugin"] == PID}


def field(client, sid):
    """The Official sources list, one line per source."""
    return fields(client, sid).get("official_sources")


def check(client, sid):
    return client.post(f"/api/series/{sid}/actions/{PID}/check").json()


@pytest.fixture()
def web(client):
    from app.db import SessionLocal
    from app.metadata import http
    from app.plugins import Context, manager
    dest = TMP / "data" / "plugins" / PLUGIN.name
    shutil.copy(PLUGIN, dest)
    client.post("/api/plugins/reload")
    fake = FakeWeb()
    old, http.transport = http.transport, httpx.MockTransport(fake)
    with SessionLocal() as db:
        Context(manager.get(PID), db).save_settings({})
        db.commit()
    fake.sid = series_id(client, "Series B")
    yield fake
    http.transport = old
    dest.unlink()
    client.post("/api/plugins/reload")


# ---------------------------------------------------------------- parsing

def test_chapter_numbers():
    m = import_plugin()
    assert m.newest(["[第２０１話]", "[最終話予告]", "[第199話・第200話]"]) == 201
    assert m.newest(["Chapter 44.4", "Special Extra", "Ch. 12"]) == 44
    assert m.newest(["Episode 7", "#8 The end"]) == 8
    assert m.newest(["Volume 12", "Extra"]) is None
    # ranges (a store's volumes, e.g. Omoi): the last number counts
    assert m.newest(["Chapter 23", "Chapters 24-27 Buy Volume 7", "Chapters 44-47 Buy Volume 12"]) == 47
    assert m.newest(["Chapters 44 – 47"]) == 47
    assert m.newest(["第1～4話", "第5〜8話"]) == 8
    assert m.newest(["Chapter 44.4-45"]) == 45
    assert m.mu_chapters("8 Volumes (Complete)") is None
    assert m.mu_chapters("12 Volumes (Ongoing)\n110 Chapters") == 110


def test_site_order():
    m = import_plugin()
    assert m.tier("https://shonenjumpplus.com/episode/1") == 0
    assert m.tier("https://www.ganganonline.com/contents/x/") == 0
    assert m.tier("https://mangaplus.shueisha.co.jp/titles/1") == 2  # .co.jp, but English
    assert m.tier("https://www.viz.com/one-piece") == 2
    assert m.tier("https://www.omoi.com/series/real-girl") == 2
    assert m.tier("https://yenpress.com/series/x") is None          # volumes only
    assert m.tier("https://www.kana.fr/series/x") is None            # French: not used


# ---------------------------------------------------------------- whole check

def test_every_japanese_site_and_mangaupdates(client, web):
    from app.db import SessionLocal
    from app.models import Series
    from app.plugins import Context, manager
    with SessionLocal() as db:  # version 0.1's separate line, which a check removes
        Context(manager.get(PID), db).set_field(db.get(Series, web.sid), "official_chapters", "47 (Omoi)")
        db.commit()
    web.pages["https://shonenjumpplus.com/episode/1"] = (200, JUMP_PAGE)
    web.pages["https://shonenjumpplus.com/rss/series/123"] = (200, JUMP_RSS)
    web.pages["https://comic-days.com/title/9"] = (200, "<li>第198話</li><li>第197話</li>")
    web.pages["https://comic-walker.com/detail/3"] = (403, "Forbidden")
    web.pages["https://api.mangaupdates.com/v1/series/47567127096"] = (
        200, json.dumps({"status": "20 Volumes (Ongoing)\n199 Chapters"}))
    web.pages["https://global.manga-up.com/manga/24"] = (200, MANGA_UP)
    set_links(web.sid, [("Manga UP!", "https://global.manga-up.com/manga/24"),
                        ("Comic Days","https://comic-days.com/title/9"),
                        ("Shonen Jump+", "https://shonenjumpplus.com/episode/1"),
                        ("ComicWalker", "https://comic-walker.com/detail/3")],
              chapters_total=210, mangaupdates_id="47567127096")
    msg = check(client, web.sid)["message"]
    assert "chapter 201" in msg and "3 more sources" in msg
    assert fields(client, web.sid) == {"official_sources": "\n".join([  # one list, the highest first
        "Shonen Jump+: 201", "MangaUpdates: 199", "Comic Days: 198", "ComicWalker: no chapter number found"])}
    assert not any("manga-up" in u for u in web.asked)  # English only when nothing else had a number


def test_mangaupdates_before_english(client, web):
    web.pages["https://api.mangaupdates.com/v1/series/47567127096"] = (
        200, json.dumps({"status": "15 Volumes (Ongoing)\n130 Chapters"}))
    web.pages["https://global.manga-up.com/manga/24"] = (200, MANGA_UP)
    set_links(web.sid, [("Manga UP!", "https://global.manga-up.com/manga/24")],
              chapters_total=None, mangaupdates_id="luo8m60")  # base-36 id from a MangaUpdates URL
    check(client, web.sid)  # no official site: the web search runs too (it finds nothing here)
    assert field(client, web.sid) == "MangaUpdates: 130\nWeb search: no chapter number found"


def test_english_site_last(client, web):
    web.pages["https://mangaplus.shueisha.co.jp/titles/1"] = (403, "Forbidden")  # skipped, not fatal
    web.pages["https://global.manga-up.com/manga/24"] = (200, MANGA_UP)
    set_links(web.sid, [("MANGA Plus", "https://mangaplus.shueisha.co.jp/titles/1"),
                        ("Manga UP!", "https://global.manga-up.com/manga/24")],
              chapters_total=44, mangaupdates_id=None)
    check(client, web.sid)
    assert field(client, web.sid) == ("Manga UP!: 44\nWeb search: no chapter number found\n"
                                      "MANGA Plus: no chapter number found")


def test_ai_reads_other_pages(client, web):
    from app.db import SessionLocal
    from app.plugins import Context, manager
    with SessionLocal() as db:
        Context(manager.get(PID), db).save_settings({"ollama_url": "pc:11434"})
        db.commit()
    # no chapter labels the plugin knows ("12" alone), so the model reads it
    web.pages["https://comic-walker.com/detail/1"] = (200, "<p>最新: 12</p><p>番外編</p>")
    set_links(web.sid, [("ComicWalker", "https://comic-walker.com/detail/1")], mangaupdates_id=None)
    web.ai_answer = 12
    check(client, web.sid)
    assert field(client, web.sid) == "ComicWalker: 12"
    assert "http://pc:11434/api/chat" in web.asked

    web.ai_answer = 99  # made up: not on the page
    set_links(web.sid, [("ComicWalker", "https://comic-walker.com/detail/1")])
    assert "No official chapter count" in check(client, web.sid)["message"]


def test_chapter_labels_on_the_page_before_ai(client, web):
    from app.db import SessionLocal
    from app.plugins import Context, manager
    with SessionLocal() as db:
        Context(manager.get(PID), db).save_settings({"ollama_url": "pc:11434"})
        db.commit()
    web.pages["https://www.omoi.com/series/real-girl"] = (200, (
        "<a>Chapter 1</a><a>Chapter 23</a><p>Published 2026-01-26</p>"
        "<a>Chapters 24-27<br>Buy Volume 7 to unlock</a><a>Chapters 44-47<br>Buy Volume 12 to unlock</a>"))
    set_links(web.sid, [("Omoi", "https://www.omoi.com/series/real-girl")], mangaupdates_id=None,
              chapters_total=None)
    web.ai_answer = 26  # a wrong reading that is on the page: never asked
    check(client, web.sid)
    assert field(client, web.sid) == "Omoi: 47\nWeb search: no chapter number found"
    assert not any(u.endswith("/api/chat") for u in web.asked)


def test_without_ollama_other_pages_are_skipped(client, web):
    web.pages["https://comic-walker.com/detail/2"] = (200, "<p>最新話はこちら</p>")
    set_links(web.sid, [("ComicWalker", "https://comic-walker.com/detail/2")], mangaupdates_id=None)
    assert "No official chapter count" in check(client, web.sid)["message"]
    assert not any(u.endswith("/api/chat") for u in web.asked)


# ---------------------------------------------------------------- web search

def settings(**values):
    from app.db import SessionLocal
    from app.plugins import Context, manager
    with SessionLocal() as db:
        Context(manager.get(PID), db).save_settings(values)
        db.commit()


def no_links(web):
    set_links(web.sid, [], mangaupdates_id=None, title_native="ダンダダン", title_english="Series B")


def test_search_results_page():
    m = import_plugin()
    found = m.search_results(ddg_page([("[第250話]ダンダダン - 龍幸伸 | 少年ジャンプ＋",
                                        "https://shonenjumpplus.com/series/ew102085/latest_free_episode",
                                        "ダンダダンの<b>最新話</b>")]))
    assert found == [{"title": "[第250話]ダンダダン - 龍幸伸 | 少年ジャンプ＋",  # the ad is left out
                      "url": "https://shonenjumpplus.com/series/ew102085/latest_free_episode",
                      "content": "ダンダダンの 最新話"}]


def test_official_result_titles_are_read_without_a_model(client, web):
    no_links(web)  # no Ollama server set
    web.search_results = [
        ("ダンダダン 第251話ネタバレ", "https://spoiler.example.jp/dandadan-251", "最新話の予想"),  # not official
        ("[第219話]ダンダダン - 龍幸伸 | 少年ジャンプ＋", "https://shonenjumpplus.com/episode/219", ""),
        ("[第250話]ダンダダン - 龍幸伸 | 少年ジャンプ＋", "https://shonenjumpplus.com/episode/250", ""),
        ("[第300話]ほかの漫画 | 少年ジャンプ＋", "https://shonenjumpplus.com/episode/300", ""),  # another series
    ]
    msg = check(client, web.sid)["message"]
    assert "chapter 250 (shonenjumpplus.com (web search))" in msg
    assert field(client, web.sid) == "shonenjumpplus.com (web search): 250"  # one line per site, its highest
    assert web.searches[0] == "ダンダダン 漫画 最新話" and "Series B manga latest chapter" in web.searches


def test_the_model_reads_other_results(client, web):
    settings(ollama_url="pc:11434")
    set_links(web.sid, [("ComicWalker", "https://comic-walker.com/detail/4")], mangaupdates_id=None,
              title_native="ダンダダン", title_english="Series B")
    web.pages["https://comic-walker.com/detail/4"] = (403, "Forbidden")
    web.search_results = [("ダンダダン アニメ", "https://anime.example/dandadan", "TVアニメ第2期 2025年放送"),
                          ("ダンダダン 最新話 | コミシー", "https://comicy.jp/comic/view/3018",
                           "最新話は[第２５０話]ダンダダン（2026年10月27日公開）")]
    web.ai_answer, web.ai_result = 250, 2
    assert "chapter 250 (comicy.jp (web search))" in check(client, web.sid)["message"]
    assert field(client, web.sid) == "comicy.jp (web search): 250\nComicWalker: no chapter number found"
    assert "RESULT 2: ダンダダン 最新話" in web.prompts[-1] and "ネタバレ" in web.prompts[-1]

    # gemma4 once gave the chapter as the result number: the chapter label in result 2 still counts
    web.ai_answer, web.ai_result = 250, 250
    assert "chapter 250 (comicy.jp" in check(client, web.sid)["message"]
    web.ai_answer, web.ai_result = 2, 1  # "第2期" is the anime's season, not a chapter label
    assert "No official chapter count" in check(client, web.sid)["message"]
    assert field(client, web.sid) == "ComicWalker: no chapter number found\nWeb search: no chapter number found"
    web.ai_answer, web.ai_result = 999, 9  # made up, and there's no result 9
    assert "No official chapter count" in check(client, web.sid)["message"]


SIX_HALF = [  # what DuckDuckGo found for SIX HALF, and one more site
    ("SIX HALF chapters, Chapter 44.5 [End] - Niadd", "https://www.niadd.com/manga/SIX_HALF/chapters.html", ""),
    ("Six Half - Manga | Kenmei", "https://www.kenmei.co/series/six-half", "Status: Finished Chapters: 48"),
    ("Six Half | MangaBaka", "https://mangabaka.org/manga/1/Six-Half", "Chapters: 48 Volumes: 11"),
]


def test_every_web_source_is_listed_and_the_most_agreed_number_named(client, web):
    settings(ollama_url="pc:11434")
    set_links(web.sid, [], mangaupdates_id=None, title_native="シックス ハーフ", title_english="Series B")
    web.search_results = SIX_HALF
    web.ai_sources = [{"result_number": 1, "last_chapter": 44}, {"result_number": 2, "last_chapter": 48},
                      {"result_number": 3, "last_chapter": 48}]
    msg = check(client, web.sid)["message"]
    assert "chapter 48 (2 of 3 sources agree)" in msg and "2 more sources" in msg
    assert field(client, web.sid) == "\n".join([
        "kenmei.co (web search): 48", "mangabaka.org (web search): 48", "niadd.com (web search): 44"])

    # MangaUpdates gets a vote too: 44 twice and 48 twice is a tie, and the highest wins
    web.pages["https://api.mangaupdates.com/v1/series/47567127096"] = (200, json.dumps({"status": "44 Chapters"}))
    set_links(web.sid, [], mangaupdates_id="47567127096")
    assert "chapter 48 (2 of 4 sources agree)" in check(client, web.sid)["message"]
    web.ai_sources = [{"result_number": 1, "last_chapter": 44}, {"result_number": 2, "last_chapter": 48}]
    web.search_results = SIX_HALF[:2]
    assert "chapter 44 (2 of 3 sources agree)" in check(client, web.sid)["message"]  # MangaUpdates and Niadd


def test_sources_that_disagree(client, web):
    settings(ollama_url="pc:11434")
    no_links(web)
    web.search_results = [("[第249話]ダンダダン | 少年ジャンプ＋", "https://shonenjumpplus.com/episode/249", ""),
                          ("ダンダダン | コミシー", "https://comicy.jp/comic/view/3018", "最新話は第250話")]
    web.ai_answer, web.ai_result = 250, 2  # the model reads comicy; Jump+'s own title is read too
    assert "chapter 250 (2 sources disagree, the highest)" in check(client, web.sid)["message"]
    assert field(client, web.sid) == "comicy.jp (web search): 250\nshonenjumpplus.com (web search): 249"


def test_no_web_search_when_the_links_have_a_number_or_its_off(client, web):
    web.pages["https://comic-days.com/title/9"] = (200, "<li>第198話</li>")
    set_links(web.sid, [("Comic Days","https://comic-days.com/title/9")], mangaupdates_id=None)
    check(client, web.sid)
    assert field(client, web.sid) == "Comic Days: 198"
    assert not web.searches

    settings(web_search="off")
    no_links(web)
    assert "No official chapter count" in check(client, web.sid)["message"]
    assert not web.searches


MU_199 = json.dumps({"status": "20 Volumes (Ongoing)\n199 Chapters"})
JUMP_250 = [("[第250話]ダンダダン | 少年ジャンプ＋", "https://shonenjumpplus.com/episode/250", "")]


def test_web_search_checks_mangaupdates(client, web):
    web.pages["https://api.mangaupdates.com/v1/series/47567127096"] = (200, MU_199)
    set_links(web.sid, [], mangaupdates_id="47567127096", title_native="ダンダダン", title_english="Series B")
    web.search_results = JUMP_250
    assert "chapter 250 (2 sources disagree, the highest)" in check(client, web.sid)["message"]
    assert field(client, web.sid) == "shonenjumpplus.com (web search): 250\nMangaUpdates: 199"

    web.search_results = [("[第199話]ダンダダン | 少年ジャンプ＋", "https://shonenjumpplus.com/episode/199", "")]
    assert "chapter 199 (2 of 2 sources agree)" in check(client, web.sid)["message"]

    web.search_results = []  # nothing found: MangaUpdates
    assert "chapter 199 (MangaUpdates)" in check(client, web.sid)["message"]


def test_web_search_always_or_off(client, web):
    web.pages["https://comic-days.com/title/9"] = (200, "<li>第198話</li>")
    web.pages["https://api.mangaupdates.com/v1/series/47567127096"] = (200, MU_199)
    set_links(web.sid, [("Comic Days","https://comic-days.com/title/9")], mangaupdates_id="47567127096",
              title_native="ダンダダン", title_english="Series B")
    web.search_results = JUMP_250
    settings(web_search="always")
    assert "chapter 250 (shonenjumpplus.com (web search))" in check(client, web.sid)["message"]  # the highest
    assert field(client, web.sid) == "shonenjumpplus.com (web search): 250\nMangaUpdates: 199\nComic Days: 198"

    web.searches.clear()
    settings(web_search="off")
    set_links(web.sid, [], mangaupdates_id="47567127096")
    assert "chapter 199 (MangaUpdates)" in check(client, web.sid)["message"]
    assert not web.searches

    settings(web_search="on")  # saved by version 0.3.0: now the default
    check(client, web.sid)
    assert web.searches


def test_store_episode_counts_arent_chapters(client, web):
    m = import_plugin()
    assert m.tier("https://mechacomic.jp/books/87286") is None  # not read as a link either
    assert m.tier("https://sp.handycomic.jp/product/1") is None
    assert m.tier("https://piccoma.com/web/product/8181") is None  # "39話" there was about 23 chapters
    assert m.newest(["Chapters: 48"]) == 48
    settings(ollama_url="pc:11434")
    set_links(web.sid, [], mangaupdates_id=None, title_native="シックス ハーフ", title_english="Series B")
    web.search_results = [  # what DuckDuckGo found for SIX HALF
        ("【2話無料】シックス ハーフ | 漫画なら、めちゃコミック", "https://mechacomic.jp/books/87286",
         "配信話数 全172話完結 配信巻数 全11巻完結"),
        ("シックス ハーフ | ギャラコミ", "https://galcomi.jp/sd/page/title/1", "配信話数 全172話完結"),
        ("Six Half - Manga | Kenmei", "https://www.kenmei.co/series/six-half", "Status: Finished Chapters: 48"),
    ]
    web.ai_answer, web.ai_result = 172, 1
    assert "No official chapter count" in check(client, web.sid)["message"]
    assert "mechacomic" not in web.prompts[-1]  # the store isn't even shown to the model
    web.ai_answer, web.ai_result = 48, 2
    assert "chapter 48 (kenmei.co (web search))" in check(client, web.sid)["message"]


def test_search_refused(client, web):
    no_links(web)
    web.search_results = []
    assert "No official chapter count" in check(client, web.sid)["message"]  # nothing found: not fatal
    assert web.searches
