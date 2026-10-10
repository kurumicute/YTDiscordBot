import json
import re

import aiohttp

SPOTIFY_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?(?:embed/)?|spotify:)(track|album|playlist)[/:]([A-Za-z0-9]+)"
)
SHORT_LINK_RE = re.compile(r"https?://(?:spotify\.link|spotify\.app\.link)/\S+")
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', re.S)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"}


class SpotifyError(Exception):
    pass


def is_spotify(query):
    return bool(SPOTIFY_RE.search(query) or SHORT_LINK_RE.match(query))


def _clean(text):
    return (text or "").replace("\xa0", " ").strip()


def _best_image(entity):
    images = (entity.get("visualIdentity") or {}).get("image") or []
    if not images:
        images = (entity.get("coverArt") or {}).get("sources") or []
    if not images:
        return None
    return max(images, key=lambda i: i.get("maxWidth") or i.get("width") or 0).get("url")


async def _resolve_short_link(session, url):
    async with session.get(url, headers=HEADERS, allow_redirects=True) as resp:
        final = str(resp.url)
        if SPOTIFY_RE.search(final):
            return final
        text = await resp.text()
    m = SPOTIFY_RE.search(text)
    if not m:
        raise SpotifyError("無法解析 Spotify 短網址")
    return f"https://open.spotify.com/{m.group(1)}/{m.group(2)}"


async def resolve(url, limit):
    """回傳 (類型, 名稱, 網址, 封面, [ {title, artist, duration, url, thumbnail}, ... ])"""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        if SHORT_LINK_RE.match(url):
            url = await _resolve_short_link(session, url)
        m = SPOTIFY_RE.search(url)
        if not m:
            raise SpotifyError("不支援的 Spotify 連結（只支援歌曲、專輯、播放清單）")
        kind, sid = m.groups()
        async with session.get(f"https://open.spotify.com/embed/{kind}/{sid}", headers=HEADERS) as resp:
            if resp.status != 200:
                raise SpotifyError(f"Spotify 回應錯誤（HTTP {resp.status}），連結可能是私人或已失效")
            html = await resp.text()

    data = NEXT_DATA_RE.search(html)
    if not data:
        raise SpotifyError("無法讀取 Spotify 頁面資料")
    try:
        entity = json.loads(data.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, TypeError, json.JSONDecodeError):
        raise SpotifyError("找不到這個 Spotify 內容（可能是私人、已刪除，或網址有誤）")

    cover = _best_image(entity)
    name = _clean(entity.get("name") or entity.get("title"))
    page_url = f"https://open.spotify.com/{kind}/{sid}"

    tracks = []
    if kind == "track":
        artist = ", ".join(_clean(a.get("name")) for a in entity.get("artists") or [])
        tracks.append({
            "title": name,
            "artist": artist or _clean(entity.get("subtitle")),
            "duration": (entity.get("duration") or 0) // 1000 or None,
            "url": page_url,
            "thumbnail": cover,
        })
    else:
        for t in entity.get("trackList") or []:
            if len(tracks) >= limit:
                break
            if t.get("entityType", "track") != "track" or not t.get("title"):
                continue
            track_id = (t.get("uri") or "").rsplit(":", 1)[-1]
            tracks.append({
                "title": _clean(t.get("title")),
                "artist": _clean(t.get("subtitle")),
                "duration": (t.get("duration") or 0) // 1000 or None,
                "url": f"https://open.spotify.com/track/{track_id}" if track_id else page_url,
                "thumbnail": cover,
            })
    if not tracks:
        raise SpotifyError("這個 Spotify 連結裡沒有可播放的歌曲")
    return kind, name, page_url, cover, tracks
