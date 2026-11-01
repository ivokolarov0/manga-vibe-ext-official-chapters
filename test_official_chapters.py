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

    def __call__(self, request: httpx.Request):
        url = str(request.url)
        self.asked.append(url)
        if url.endswith("/api/chat"):
            return httpx.Response(200, json={"message": {"content": json.dumps({"last_chapter": self.ai_answer})}})
        status, text = self.pages.get(url, (404, "not found"))
        return httpx.Response(status, text=text)


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


def field(client, sid):
    s = client.get(f"/api/series/{sid}").json()
    return next((f["value"] for f in s["plugin_fields"] if f["plugin"] == PID), None)


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
    assert m.mu_chapters("8 Volumes (Complete)") is None
    assert m.mu_chapters("12 Volumes (Ongoing)\n110 Chapters") == 110


def test_site_order():
    m = import_plugin()
    assert m.tier("https://shonenjumpplus.com/episode/1") == 0
    assert m.tier("https://www.ganganonline.com/contents/x/") == 0
    assert m.tier("https://mangaplus.shueisha.co.jp/titles/1") == 2  # .co.jp, but English
    assert m.tier("https://www.viz.com/one-piece") == 2
    assert m.tier("https://yenpress.com/series/x") is None          # volumes only
    assert m.tier("https://www.kana.fr/series/x") is None            # French: not used


# ---------------------------------------------------------------- whole check

def test_japanese_site_first(client, web):
    web.pages["https://shonenjumpplus.com/episode/1"] = (200, JUMP_PAGE)
    web.pages["https://shonenjumpplus.com/rss/series/123"] = (200, JUMP_RSS)
    web.pages["https://global.manga-up.com/manga/24"] = (200, MANGA_UP)
    set_links(web.sid, [("Manga UP!", "https://global.manga-up.com/manga/24"),
                        ("Shonen Jump+", "https://shonenjumpplus.com/episode/1")],
              chapters_total=210, mangaupdates_id="47567127096")
    assert "chapter 201" in check(client, web.sid)["message"]
    assert field(client, web.sid) == "201 (Shonen Jump+) · AniList says 210"
    assert not any("manga-up" in u or "mangaupdates" in u for u in web.asked)  # stopped at the first answer


def test_mangaupdates_before_english(client, web):
    web.pages["https://api.mangaupdates.com/v1/series/47567127096"] = (
        200, json.dumps({"status": "15 Volumes (Ongoing)\n130 Chapters"}))
    web.pages["https://global.manga-up.com/manga/24"] = (200, MANGA_UP)
    set_links(web.sid, [("Manga UP!", "https://global.manga-up.com/manga/24")],
              chapters_total=None, mangaupdates_id="luo8m60")  # base-36 id from a MangaUpdates URL
    check(client, web.sid)
    assert field(client, web.sid) == "130 (MangaUpdates)"


def test_english_site_last(client, web):
    web.pages["https://mangaplus.shueisha.co.jp/titles/1"] = (403, "Forbidden")  # skipped, not fatal
    web.pages["https://global.manga-up.com/manga/24"] = (200, MANGA_UP)
    set_links(web.sid, [("MANGA Plus", "https://mangaplus.shueisha.co.jp/titles/1"),
                        ("Manga UP!", "https://global.manga-up.com/manga/24")],
              chapters_total=44, mangaupdates_id=None)
    check(client, web.sid)
    assert field(client, web.sid) == "44 (Manga UP!)"  # same as AniList: nothing more to say


def test_ai_reads_other_pages(client, web):
    from app.db import SessionLocal
    from app.plugins import Context, manager
    with SessionLocal() as db:
        Context(manager.get(PID), db).save_settings({"ollama_url": "pc:11434"})
        db.commit()
    web.pages["https://comic-walker.com/detail/1"] = (200, "<p>第1話</p><p>第12話 最新話</p><p>番外編</p>")
    set_links(web.sid, [("ComicWalker", "https://comic-walker.com/detail/1")], mangaupdates_id=None)
    web.ai_answer = 12
    check(client, web.sid)
    assert field(client, web.sid).startswith("12 (ComicWalker)")
    assert "http://pc:11434/api/chat" in web.asked

    web.ai_answer = 99  # made up: not on the page
    set_links(web.sid, [("ComicWalker", "https://comic-walker.com/detail/1")])
    assert "No official chapter count" in check(client, web.sid)["message"]


def test_without_ollama_other_pages_are_skipped(client, web):
    web.pages["https://comic-walker.com/detail/2"] = (200, "<p>第12話</p>")
    set_links(web.sid, [("ComicWalker", "https://comic-walker.com/detail/2")], mangaupdates_id=None)
    assert "No official chapter count" in check(client, web.sid)["message"]
    assert not any(u.endswith("/api/chat") for u in web.asked)
