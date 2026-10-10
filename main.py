import asyncio
import configparser
import json
import os
import random
import re
import shutil
import time
from collections import deque
from dataclasses import dataclass
from typing import Literal, Optional

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

import spotify

# ───────────────────────── 設定 ─────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config = configparser.ConfigParser()
files = config.read(os.path.join(BASE_DIR, "config.txt"), encoding="utf-8")
print("已讀取的檔案：", files)
if not files or not config["DEFAULT"].get("token"):
    raise SystemExit("❌ 找不到 config.txt 或沒有填 token：請把 config.example.txt 複製成 config.txt 並填入 Bot Token")

cookies_path = config["DEFAULT"].get("cookies_path", "cookies.txt")
default_volume = float(config["DEFAULT"].get("volume", "0.1"))
token = config["DEFAULT"]["token"]
playlist = int(config["DEFAULT"].get("playlist", "20"))
ffmpeg_path = config["DEFAULT"].get("ffmpeg_path", "ffmpeg")
default_prefix = config["DEFAULT"].get("prefix", "!")
idle_timeout = int(config["DEFAULT"].get("idle_timeout", "300"))

SETTINGS_FILE = os.path.join(BASE_DIR, "guild_settings.json")


def resolve_path(path):
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


# ffmpeg：優先使用 config 指定的路徑，找不到就用系統 PATH 裡的 ffmpeg
if os.path.isfile(resolve_path(ffmpeg_path)):
    ffmpeg_path = resolve_path(ffmpeg_path)
else:
    ffmpeg_path = shutil.which("ffmpeg") or ffmpeg_path
print("使用的 ffmpeg：", ffmpeg_path)

# cookies：檔案存在才使用（不存在時 yt-dlp 會直接報錯）
cookies_file = resolve_path(cookies_path) if cookies_path else None
if cookies_file and not os.path.isfile(cookies_file):
    print(f"⚠️ 找不到 cookies 檔案：{cookies_file}，將不使用 cookies")
    cookies_file = None

# ───────────────────── 各伺服器設定（存檔） ─────────────────────
try:
    with open(SETTINGS_FILE, encoding="utf-8") as f:
        guild_settings = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    guild_settings = {}


def get_setting(guild_id, key, default):
    return guild_settings.get(str(guild_id), {}).get(key, default)


def set_setting(guild_id, key, value):
    guild_settings.setdefault(str(guild_id), {})[key] = value
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(guild_settings, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)


# ───────────────────────── Bot ─────────────────────────
async def get_prefix(bot, message):
    prefix = get_setting(message.guild.id, "prefix", default_prefix) if message.guild else default_prefix
    return commands.when_mentioned_or(prefix)(bot, message)


intents = discord.Intents.default()
intents.message_content = True  # 前綴指令需要，請在 Developer Portal 開啟 Message Content Intent
intents.voice_states = True


class MusicBot(commands.Bot):
    async def setup_hook(self):
        synced = await self.tree.sync()
        print(f"已同步 {len(synced)} 個斜線指令")


bot = MusicBot(command_prefix=get_prefix, intents=intents, help_command=None)

# ───────────────────────── yt-dlp ─────────────────────────
BASE_YDL_OPTIONS = {
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
    "skip_download": True,
}
if cookies_file:
    BASE_YDL_OPTIONS["cookiefile"] = cookies_file

# 搜尋 / 播放清單：只取基本資訊，速度較快
FLAT_YDL_OPTIONS = {
    **BASE_YDL_OPTIONS,
    "extract_flat": "in_playlist",
    "playlistend": playlist,  # 使用 config 中的歌單上限，預設 20 首
}

# 取得實際音訊串流網址
STREAM_YDL_OPTIONS = {
    **BASE_YDL_OPTIONS,
    "format": "bestaudio/best",
    "noplaylist": True,
}


class YDL(yt_dlp.YoutubeDL):
    """只讀取 cookies.txt、不寫回，避免同時查詢多首歌時互相覆寫造成檔案損壞"""

    def save_cookies(self):
        pass


FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
FFMPEG_OPTIONS = "-vn"

COLOR_YOUTUBE = 0xFF0033
COLOR_SPOTIFY = 0x1DB954
COLOR_BILIBILI = 0x00AEEC
COLOR_INFO = 0x5865F2
COLOR_ERROR = 0xED4245
LOOP_LABELS = {"off": "關閉", "song": "單曲循環", "queue": "清單循環"}
SOURCES = {
    # 來源: (顯示名稱, 圖示, 顏色)
    "youtube": ("YouTube", "🔴", COLOR_YOUTUBE),
    "spotify": ("Spotify", "🟢", COLOR_SPOTIFY),
    "bilibili": ("Bilibili", "📺", COLOR_BILIBILI),
}
BILIBILI_RE = re.compile(r"https?://(?:[\w-]+\.)*(?:bilibili\.com|b23\.tv|bili2233\.cn)/", re.I)
BILIBILI_SEARCH_COUNT = 5  # B 站搜尋會混入課程等無法播放的結果，多抓幾筆再過濾


def is_bilibili(url):
    return bool(url and BILIBILI_RE.match(url))


@dataclass(eq=False)
class Song:
    title: str
    url: Optional[str]            # 影片網址（Spotify 歌曲在播放前才到 YouTube 搜尋）
    requester: Optional[discord.abc.User] = None
    duration: Optional[int] = None
    thumbnail: Optional[str] = None
    author: Optional[str] = None
    source: str = "youtube"       # youtube / spotify / bilibili
    page_url: Optional[str] = None  # 顯示用的連結
    search_query: Optional[str] = None
    needs_info: bool = False      # 標題等資訊尚未取得（B 站清單只會給網址）

    @property
    def link(self):
        return self.page_url or self.url

    @property
    def source_name(self):
        return SOURCES[self.source][0]

    @property
    def icon(self):
        return SOURCES[self.source][1]

    @property
    def color(self):
        return SOURCES[self.source][2]


def fmt_duration(seconds):
    if seconds is None:
        return "直播/未知"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def progress_bar(elapsed, total, length=16):
    if not total:
        return "🔴 LIVE"
    filled = min(length - 1, int(length * elapsed / total))
    return "▬" * filled + "🔘" + "▬" * (length - filled - 1)


def short(text, limit=60):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def escape(text):
    return discord.utils.escape_markdown(text or "")


def https(url):
    return "https://" + url[len("http://"):] if url and url.startswith("http://") else url


def youtube_thumbnail(video_id):
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else None


def song_from_entry(entry, requester):
    video_id = entry.get("id")
    url = entry.get("webpage_url") or entry.get("url")
    is_bili = is_bilibili(url) or (entry.get("ie_key") or entry.get("extractor_key") or "").startswith("BiliBili")
    if is_bili:
        if not url or "/video/" not in url:
            return None  # 課程、番劇等無法直接播放的結果
        url = https(url)
    elif not (url and url.startswith("http") and "watch?v=" in url) and video_id:
        url = f"https://www.youtube.com/watch?v={video_id}"
    if not url:
        return None
    title = entry.get("title")
    return Song(
        title=title or url,
        url=url,
        requester=requester,
        duration=entry.get("duration"),
        thumbnail=https(entry.get("thumbnail")) or (None if is_bili else youtube_thumbnail(video_id)),
        author=entry.get("uploader") or entry.get("channel"),
        source="bilibili" if is_bili else "youtube",
        needs_info=not title,
    )


def extract(opts, target, retries=2):
    """執行 yt-dlp；B 站風控偶爾會隨機回 412，稍等後重試通常就會成功"""
    for attempt in range(retries + 1):
        try:
            with YDL(opts) as ydl:
                return ydl.extract_info(target, download=False)
        except Exception as e:
            if attempt < retries and "412" in str(e):
                time.sleep(1 + attempt)
                continue
            print("yt-dlp 提取失敗：", e)
            return None


def get_songs(query, requester, site="youtube"):
    """搜尋關鍵字、單支影片或播放清單，回傳 (清單名稱或 None, [Song, ...])"""
    is_url = query.startswith("http")
    opts = dict(FLAT_YDL_OPTIONS)
    if is_url:
        # YouTube 影片網址只播單曲；B 站多 P 影片會整組加入（網址有 ?p= 則只播那一 P）
        if not is_bilibili(query) and "list=" not in query:
            opts["noplaylist"] = True
        search = query
    elif site == "bilibili":
        search = f"bilisearch{BILIBILI_SEARCH_COUNT}:{query}"
    else:
        search = f"ytsearch1:{query}"
    info = extract(opts, search)
    if not info:
        return None, []

    entries = info.get("entries")
    playlist_title = info.get("title") if entries is not None and is_url else None
    if entries is None:
        entries = [info]
    is_bili_search = not is_url and site == "bilibili"
    limit = playlist if is_url else 1
    songs = []
    for entry in entries:
        if not entry:
            continue
        song = song_from_entry(entry, requester)
        if song and is_bili_search:
            # B 站搜尋結果可能是已刪除或限中國大陸的影片，跳過讀不到的
            load_song_info(song)
            if song.needs_info:
                continue
        if song:
            songs.append(song)
        if len(songs) >= limit:
            break
    return playlist_title, songs


def fill_info(song, info):
    if song.needs_info:
        song.title = info.get("title") or song.title
        song.needs_info = False
    if not song.url:
        song.url = info.get("webpage_url")
    if song.duration is None:
        song.duration = info.get("duration")
    if not song.thumbnail:
        song.thumbnail = https(info.get("thumbnail"))
    if not song.author:
        song.author = info.get("uploader") or info.get("channel")


def load_song_info(song):
    """補齊只有網址的歌曲資訊（標題、長度、封面）"""
    opts = {**FLAT_YDL_OPTIONS, "noplaylist": True}
    info = extract(opts, song.url)
    if info:
        fill_info(song, info)


async def load_missing_info(songs, concurrency=5):
    sem = asyncio.Semaphore(concurrency)

    async def load(song):
        async with sem:
            await asyncio.to_thread(load_song_info, song)

    await asyncio.gather(*(load(s) for s in songs if s.needs_info))


def get_stream_info(song):
    """取得可直接給 ffmpeg 播放的串流網址；Spotify 歌曲會先到 YouTube 搜尋"""
    target = song.url or f"ytsearch1:{song.search_query}"
    info = extract(STREAM_YDL_OPTIONS, target)
    if not info:
        return None
    if "entries" in info:
        info = next((e for e in info["entries"] if e), None)
        if not info:
            return None

    fill_info(song, info)

    # B 站等網站需要帶 Referer 等 header，ffmpeg 才能讀取
    stream_url = info.get("url")
    headers = info.get("http_headers") or {}
    if not stream_url:
        for fmt in reversed(info.get("formats") or []):
            if fmt.get("url") and fmt.get("acodec") not in (None, "none"):
                stream_url = fmt["url"]
                headers = fmt.get("http_headers") or headers
                break
    if not stream_url:
        return None
    return {"url": stream_url, "headers": headers}


def make_source(stream, volume):
    before = FFMPEG_BEFORE_OPTIONS
    if stream["headers"]:
        header_str = "".join(f"{k}: {v}\r\n" for k, v in stream["headers"].items())
        before += f' -headers "{header_str}"'
    source = discord.FFmpegPCMAudio(
        stream["url"], executable=ffmpeg_path, before_options=before, options=FFMPEG_OPTIONS
    )
    return discord.PCMVolumeTransformer(source, volume)


async def get_spotify_songs(url, requester):
    kind, name, page_url, cover, tracks = await spotify.resolve(url, playlist)
    songs = [
        Song(
            title=f"{t['artist']} - {t['title']}" if t["artist"] else t["title"],
            url=None,
            requester=requester,
            duration=t["duration"],
            thumbnail=t["thumbnail"],
            author=t["artist"],
            source="spotify",
            page_url=t["url"],
            search_query=f"{t['title']} {t['artist']} audio",
        )
        for t in tracks
    ]
    return (name if kind != "track" else None), page_url, cover, songs


# ───────────────────── 每個伺服器的播放狀態 ─────────────────────
class GuildState:
    def __init__(self, guild_id):
        self.guild_id = guild_id
        self.queue = deque()
        self.current = None
        self.volume = get_setting(guild_id, "volume", default_volume)
        self.loop = "off"
        self.text_channel = None
        self.prefetched = None        # (Song, stream)
        self.prefetch_task = None
        self.idle_task = None
        self.skip_requested = False
        self.lock = asyncio.Lock()
        self.np_message = None        # 「正在播放」訊息（附控制按鈕）
        self.started_at = 0.0
        self.paused_at = None

    def elapsed(self):
        if not self.current:
            return 0
        now = self.paused_at or asyncio.get_running_loop().time()
        return max(0, now - self.started_at)

    def cancel_tasks(self):
        for task in (self.prefetch_task, self.idle_task):
            if task and not task.done():
                task.cancel()
        self.prefetch_task = self.idle_task = None
        self.prefetched = None

    def reset(self):
        self.cancel_tasks()
        self.queue.clear()
        self.current = None
        self.loop = "off"
        self.paused_at = None


states: dict[int, GuildState] = {}


def get_state(guild_id):
    if guild_id not in states:
        states[guild_id] = GuildState(guild_id)
    return states[guild_id]


# ───────────────────────── Embed ─────────────────────────
def now_playing_embed(guild, state, paused=False):
    song = state.current
    if not song:
        return discord.Embed(description="目前沒有播放任何歌曲", color=COLOR_INFO)
    status = "⏸️ 已暫停" if paused else "🎵 正在播放"
    embed = discord.Embed(
        title=short(song.title, 200),
        url=song.link,
        color=song.color,
    )
    embed.set_author(name=status)
    elapsed = state.elapsed()
    embed.description = f"`{fmt_duration(elapsed)}` {progress_bar(elapsed, song.duration)} `{fmt_duration(song.duration)}`"
    if song.author:
        embed.add_field(name="演出者", value=escape(short(song.author, 100)))
    embed.add_field(name="來源", value=f"{song.icon} {song.source_name}")
    embed.add_field(name="點播", value=song.requester.mention if song.requester else "—")
    embed.add_field(name="音量", value=f"{state.volume * 100:.0f}%")
    embed.add_field(name="循環", value=LOOP_LABELS[state.loop])
    nxt = state.queue[0].title if state.queue else "無"
    embed.add_field(name=f"下一首（剩 {len(state.queue)} 首）", value=escape(short(nxt, 100)))
    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)
    embed.set_footer(text=guild.name, icon_url=guild.icon.url if guild.icon else None)
    return embed


def added_embed(songs, requester, position, playlist_name=None, playlist_url=None, cover=None):
    first = songs[0]
    if len(songs) == 1:
        embed = discord.Embed(title=short(first.title, 200), url=first.link, color=first.color)
        embed.set_author(name="✅ 已加入待播清單")
        embed.add_field(name="長度", value=fmt_duration(first.duration))
        embed.add_field(name="順位", value="即將播放" if position == 0 else f"第 {position + 1} 首")
        if first.thumbnail:
            embed.set_thumbnail(url=first.thumbnail)
    else:
        total = sum(s.duration or 0 for s in songs)
        embed = discord.Embed(title=short(playlist_name or "播放清單", 200), url=playlist_url, color=first.color)
        embed.set_author(name=f"✅ 已加入 {len(songs)} 首歌曲")
        preview = "\n".join(f"`{i + 1}.` {escape(short(s.title))}" for i, s in enumerate(songs[:5]))
        if len(songs) > 5:
            preview += f"\n…還有 {len(songs) - 5} 首"
        embed.description = preview
        embed.add_field(name="總長度", value=fmt_duration(total))
        if cover or first.thumbnail:
            embed.set_thumbnail(url=cover or first.thumbnail)
    embed.set_footer(text=f"由 {requester.display_name} 點播", icon_url=requester.display_avatar.url)
    return embed


def info_embed(text, color=COLOR_INFO):
    return discord.Embed(description=text, color=color)


def error_embed(text):
    return discord.Embed(description=f"❌ {text}", color=COLOR_ERROR)


def queue_pages(state, per_page=10):
    songs = list(state.queue)
    total = sum(s.duration or 0 for s in songs)
    header = ""
    if state.current:
        header = f"**正在播放**\n{state.current.icon} [{escape(short(state.current.title))}]({state.current.link}) `{fmt_duration(state.current.duration)}`\n\n"
    pages = []
    for i in range(0, max(len(songs), 1), per_page):
        chunk = songs[i:i + per_page]
        if chunk:
            lines = [
                f"`{i + idx + 1}.` {s.icon} {escape(short(s.title, 55))} `{fmt_duration(s.duration)}`"
                for idx, s in enumerate(chunk)
            ]
            body = "**待播清單**\n" + "\n".join(lines)
        else:
            body = "📭 待播清單是空的"
        embed = discord.Embed(title="🎶 播放清單", description=header + body, color=COLOR_INFO)
        pages.append(embed)
    for n, embed in enumerate(pages):
        embed.set_footer(
            text=f"第 {n + 1}/{len(pages)} 頁 · 共 {len(songs)} 首 · 總長 {fmt_duration(total)} · 循環：{LOOP_LABELS[state.loop]}"
        )
    return pages


# ───────────────────────── 按鈕介面 ─────────────────────────
class QueueView(discord.ui.View):
    def __init__(self, pages, author_id):
        super().__init__(timeout=120)
        self.pages = pages
        self.page = 0
        self.author_id = author_id
        self.message = None
        self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= len(self.pages) - 1

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("只有下指令的人可以翻頁喔！", ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction, button):
        self.page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction, button):
        self.page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class PlayerControls(discord.ui.View):
    """「正在播放」訊息下方的控制按鈕"""

    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.sync_buttons()

    def sync_buttons(self):
        state = get_state(self.guild_id)
        guild = bot.get_guild(self.guild_id)
        vc = guild.voice_client if guild else None
        paused = bool(vc and vc.is_paused())
        self.pause_button.emoji = "▶️" if paused else "⏸️"
        self.pause_button.style = discord.ButtonStyle.success if paused else discord.ButtonStyle.secondary
        self.loop_button.emoji = {"off": "➡️", "song": "🔂", "queue": "🔁"}[state.loop]
        self.loop_button.style = discord.ButtonStyle.secondary if state.loop == "off" else discord.ButtonStyle.primary

    async def interaction_check(self, interaction):
        vc = interaction.guild.voice_client
        voice = getattr(interaction.user, "voice", None)
        if not vc or not voice or voice.channel != vc.channel:
            await interaction.response.send_message("你需要和機器人在同一個語音頻道才能操作喔！", ephemeral=True)
            return False
        return True

    async def refresh(self, interaction):
        state = get_state(self.guild_id)
        vc = interaction.guild.voice_client
        self.sync_buttons()
        await interaction.response.edit_message(
            embed=now_playing_embed(interaction.guild, state, paused=bool(vc and vc.is_paused())), view=self
        )

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.secondary, row=0)
    async def pause_button(self, interaction, button):
        vc = interaction.guild.voice_client
        state = get_state(self.guild_id)
        if vc.is_paused():
            resume_playback(vc, state)
        elif vc.is_playing():
            pause_playback(vc, state)
        await self.refresh(interaction)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip_button(self, interaction, button):
        vc = interaction.guild.voice_client
        if vc.is_playing() or vc.is_paused():
            get_state(self.guild_id).skip_requested = True
            vc.stop()
        await interaction.response.send_message(
            embed=info_embed(f"⏭️ {interaction.user.mention} 跳過了這首歌"), delete_after=10
        )

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop_button(self, interaction, button):
        stop_playback(interaction.guild)
        await interaction.response.send_message(
            embed=info_embed(f"⏹️ {interaction.user.mention} 停止播放並清空了待播清單")
        )

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary, row=0)
    async def loop_button(self, interaction, button):
        state = get_state(self.guild_id)
        state.loop = {"off": "song", "song": "queue", "queue": "off"}[state.loop]
        await self.refresh(interaction)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=0)
    async def shuffle_button(self, interaction, button):
        state = get_state(self.guild_id)
        shuffle_queue(state)
        await self.refresh(interaction)

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, row=1)
    async def vol_down(self, interaction, button):
        change_volume(interaction.guild, get_state(self.guild_id).volume - 0.05)
        await self.refresh(interaction)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, row=1)
    async def vol_up(self, interaction, button):
        change_volume(interaction.guild, get_state(self.guild_id).volume + 0.05)
        await self.refresh(interaction)

    @discord.ui.button(emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def refresh_button(self, interaction, button):
        await self.refresh(interaction)

    @discord.ui.button(emoji="📜", label="清單", style=discord.ButtonStyle.secondary, row=1)
    async def queue_button(self, interaction, button):
        pages = queue_pages(get_state(self.guild_id))
        view = QueueView(pages, interaction.user.id) if len(pages) > 1 else discord.utils.MISSING
        await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True)


# ───────────────────────── 播放控制 ─────────────────────────
def pause_playback(vc, state):
    vc.pause()
    state.paused_at = bot.loop.time()


def resume_playback(vc, state):
    if state.paused_at:
        state.started_at += bot.loop.time() - state.paused_at
    state.paused_at = None
    vc.resume()


def shuffle_queue(state):
    items = list(state.queue)
    random.shuffle(items)
    state.queue = deque(items)
    state.prefetched = None


def change_volume(guild, value):
    state = get_state(guild.id)
    state.volume = round(max(0.0, min(value, 2.0)), 2)  # 限制音量在 0.0 到 2.0 之間
    vc = guild.voice_client
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = state.volume
    set_setting(guild.id, "volume", state.volume)


def stop_playback(guild):
    state = get_state(guild.id)
    state.cancel_tasks()
    state.queue.clear()
    state.loop = "off"
    vc = guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()  # after callback 會觸發 play_next，清單是空的就會進入閒置


async def send(state, **kwargs):
    if not state.text_channel:
        return None
    try:
        return await state.text_channel.send(**kwargs)
    except discord.HTTPException as e:
        print("傳送訊息失敗：", e)
        return None


async def clear_np_message(state):
    """移除上一則「正在播放」訊息的按鈕"""
    msg, state.np_message = state.np_message, None
    if msg:
        try:
            embed = msg.embeds[0] if msg.embeds else None
            if embed:
                embed.set_author(name="✔️ 已播放")
                embed.description = None
            await msg.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass


async def prefetch_next(guild_id):
    state = get_state(guild_id)
    if not state.queue:
        return
    song = state.queue[0]
    if state.prefetched and state.prefetched[0] is song:
        return
    stream = await asyncio.to_thread(get_stream_info, song)
    if stream and state.queue and state.queue[0] is song:
        state.prefetched = (song, stream)


async def idle_disconnect(guild, delay):
    await asyncio.sleep(delay)
    state = get_state(guild.id)
    vc = guild.voice_client
    if vc and not vc.is_playing() and not vc.is_paused() and not state.queue:
        await vc.disconnect()
        await clear_np_message(state)
        await send(state, embed=info_embed("💤 閒置過久，已自動離開語音頻道。"))


async def on_song_end(guild_id, song, error):
    if error:
        print(f"[{guild_id}] 播放時發生錯誤：{error}")
    state = get_state(guild_id)
    if state.current is song:
        if not state.skip_requested:
            if state.loop == "song":
                state.queue.appendleft(song)
            elif state.loop == "queue":
                state.queue.append(song)
        elif state.loop == "queue":
            state.queue.append(song)
        state.current = None
    state.skip_requested = False
    guild = bot.get_guild(guild_id)
    if guild:
        await play_next(guild)


async def play_next(guild):
    state = get_state(guild.id)
    async with state.lock:
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            state.current = None
            return
        if vc.is_playing() or vc.is_paused():
            return

        while state.queue:
            song = state.queue.popleft()
            if state.prefetched and state.prefetched[0] is song:
                stream = state.prefetched[1]
                state.prefetched = None
            else:
                stream = await asyncio.to_thread(get_stream_info, song)

            if not stream:
                await send(state, embed=error_embed(f"無法取得 **{escape(song.title)}** 的音訊，跳過此曲。"))
                continue

            vc = guild.voice_client
            if not vc or not vc.is_connected():
                state.current = None
                return

            try:
                audio = make_source(stream, state.volume)
            except Exception as e:
                await send(state, embed=error_embed(f"播放 **{escape(song.title)}** 時發生錯誤：{e}，跳過此曲。"))
                continue

            def after_play(error, gid=guild.id, s=song):
                asyncio.run_coroutine_threadsafe(on_song_end(gid, s, error), bot.loop)

            state.current = song
            state.started_at = bot.loop.time()
            state.paused_at = None
            if state.idle_task:
                state.idle_task.cancel()
                state.idle_task = None
            vc.play(audio, after=after_play)

            await clear_np_message(state)
            state.np_message = await send(
                state, embed=now_playing_embed(guild, state), view=PlayerControls(guild.id)
            )
            if state.queue:
                state.prefetch_task = asyncio.create_task(prefetch_next(guild.id))
            return

        # 播放清單空了，閒置一段時間後自動斷線
        state.current = None
        await clear_np_message(state)
        if state.idle_task:
            state.idle_task.cancel()
        state.idle_task = asyncio.create_task(idle_disconnect(guild, idle_timeout))


async def ensure_voice(ctx):
    """確保機器人在使用者所在的語音頻道，回傳 VoiceClient 或 None"""
    voice = ctx.author.voice
    vc = ctx.guild.voice_client
    if not voice or not voice.channel:
        await ctx.send(embed=error_embed("你需要先加入一個語音頻道！"))
        return None
    if vc and vc.is_connected():
        if vc.channel != voice.channel:
            if vc.is_playing() or vc.is_paused():
                await ctx.send(embed=error_embed(f"機器人正在 {vc.channel.mention} 播放中，請到同一個頻道點歌！"))
                return None
            await vc.move_to(voice.channel)
        return vc
    try:
        return await voice.channel.connect(self_deaf=True)
    except Exception as e:
        await ctx.send(embed=error_embed(f"加入語音頻道失敗：{e}"))
        return None


def require_same_channel():
    """只有和機器人在同一個語音頻道的人可以操作"""
    async def predicate(ctx):
        vc = ctx.guild.voice_client
        if not vc:
            raise commands.CheckFailure("機器人目前不在任何語音頻道中")
        if not ctx.author.voice or ctx.author.voice.channel != vc.channel:
            raise commands.CheckFailure("你需要和機器人在同一個語音頻道才能操作")
        return True
    return commands.check(predicate)


# ───────────────────────── 事件 ─────────────────────────
@bot.event
async def on_ready():
    print(f"✅ {bot.user} 已成功登入！目前在 {len(bot.guilds)} 個伺服器中")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="/play"))


@bot.event
async def on_guild_remove(guild):
    state = states.pop(guild.id, None)
    if state:
        state.cancel_tasks()


@bot.event
async def on_voice_state_update(member, before, after):
    guild = member.guild
    vc = guild.voice_client

    # 機器人被踢出 / 斷線時清除狀態
    if member.id == bot.user.id:
        if before.channel and not after.channel:
            state = states.get(guild.id)
            if state:
                state.reset()
                await clear_np_message(state)
        return

    # 頻道只剩機器人時，一段時間後自動離開
    if vc and before.channel == vc.channel and after.channel != vc.channel:
        if not any(not m.bot for m in vc.channel.members):
            state = get_state(guild.id)
            if state.idle_task:
                state.idle_task.cancel()
            state.idle_task = asyncio.create_task(leave_if_alone(guild, 60))


async def leave_if_alone(guild, delay):
    await asyncio.sleep(delay)
    vc = guild.voice_client
    if vc and not any(not m.bot for m in vc.channel.members):
        state = get_state(guild.id)
        state.reset()
        await vc.disconnect()
        await clear_np_message(state)
        await send(state, embed=info_embed("👋 語音頻道沒有人了，已自動離開。"))


@bot.event
async def on_command_error(ctx, error):
    while getattr(error, "original", None) is not None:  # 拆開 Hybrid / Invoke 包裝的錯誤
        error = error.original
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        msg = f"缺少參數：`{error.param.name}`"
    elif isinstance(error, (commands.BadArgument, commands.BadLiteralArgument)):
        msg = "參數格式錯誤"
    elif isinstance(error, commands.NoPrivateMessage):
        msg = "這個指令只能在伺服器中使用"
    elif isinstance(error, commands.MissingPermissions):
        msg = "你沒有權限使用這個指令（需要「管理伺服器」權限）"
    elif isinstance(error, commands.CheckFailure):
        msg = str(error) or "你無法使用這個指令"
    else:
        print(f"指令 {ctx.command} 發生錯誤：{error!r}")
        msg = f"發生錯誤：{error}"
    try:
        await ctx.send(embed=error_embed(msg), ephemeral=True)
    except discord.HTTPException:
        pass


# ───────────────────────── 指令 ─────────────────────────
@bot.hybrid_command(name="join", description="讓機器人加入你所在的語音頻道")
@commands.guild_only()
async def join(ctx):
    await ctx.defer()
    vc = await ensure_voice(ctx)
    if not vc:
        return
    get_state(ctx.guild.id).text_channel = ctx.channel
    await ctx.send(embed=info_embed(f"🎶 已加入語音頻道：{vc.channel.mention}"))


async def enqueue(ctx, query, site="youtube"):
    await ctx.defer()
    vc = await ensure_voice(ctx)
    if not vc:
        return
    state = get_state(ctx.guild.id)
    state.text_channel = ctx.channel
    query = query.strip().strip("<>")

    loading = None
    if ctx.interaction is None:
        loading = await ctx.send(embed=info_embed("🔍 搜尋中..."))

    playlist_url = cover = None
    try:
        if spotify.is_spotify(query):
            playlist_name, playlist_url, cover, songs = await get_spotify_songs(query, ctx.author)
        else:
            playlist_name, songs = await asyncio.to_thread(get_songs, query, ctx.author, site)
            playlist_url = query if playlist_name else None
            await load_missing_info(songs)
    except spotify.SpotifyError as e:
        songs, error = [], str(e)
    else:
        error = "未能取得音樂，請檢查網址或關鍵字。"

    if loading:
        try:
            await loading.delete()
        except discord.HTTPException:
            pass
    if not songs:
        await ctx.send(embed=error_embed(error))
        return

    position = len(state.queue) + (1 if state.current else 0)
    state.queue.extend(songs)
    await ctx.send(embed=added_embed(songs, ctx.author, position, playlist_name, playlist_url, cover))

    if not vc.is_playing() and not vc.is_paused():
        await play_next(ctx.guild)
    elif len(state.queue) == len(songs):
        state.prefetch_task = asyncio.create_task(prefetch_next(ctx.guild.id))


@bot.hybrid_command(name="play", aliases=["p", "P", "PLAY"], description="播放歌曲（關鍵字、YouTube / Spotify / B站連結）")
@commands.guild_only()
@app_commands.describe(query="關鍵字（搜尋 YouTube），或 YouTube / Spotify / Bilibili 的歌曲、播放清單連結")
async def play_command(ctx, *, query: str):
    await enqueue(ctx, query)


@bot.hybrid_command(name="bilibili", aliases=["bp", "bili"], description="在 B 站搜尋並播放")
@commands.guild_only()
@app_commands.describe(query="要在 Bilibili 搜尋的關鍵字（也可以直接貼 B 站連結）")
async def bilibili_command(ctx, *, query: str):
    await enqueue(ctx, query, site="bilibili")


@bot.hybrid_command(name="nowplaying", aliases=["np"], description="顯示目前播放的歌曲與控制面板")
@commands.guild_only()
async def nowplaying(ctx):
    state = get_state(ctx.guild.id)
    if not state.current:
        await ctx.send(embed=error_embed("目前沒有音樂在播放！"))
        return
    state.text_channel = ctx.channel
    vc = ctx.guild.voice_client
    await clear_np_message(state)
    state.np_message = await ctx.send(
        embed=now_playing_embed(ctx.guild, state, paused=bool(vc and vc.is_paused())),
        view=PlayerControls(ctx.guild.id),
    )


@bot.hybrid_command(name="queue", aliases=["q"], description="顯示目前的播放清單")
@commands.guild_only()
async def show_queue(ctx):
    pages = queue_pages(get_state(ctx.guild.id))
    if len(pages) == 1:
        await ctx.send(embed=pages[0])
        return
    view = QueueView(pages, ctx.author.id)
    view.message = await ctx.send(embed=pages[0], view=view)


@bot.hybrid_command(name="skip", aliases=["s"], description="跳過目前播放的歌曲")
@commands.guild_only()
@require_same_channel()
async def skip(ctx):
    vc = ctx.guild.voice_client
    if vc.is_playing() or vc.is_paused():
        get_state(ctx.guild.id).skip_requested = True
        vc.stop()  # 停止後 after callback 會自動播放下一首
        await ctx.send(embed=info_embed("⏭️ THE 跳！"))
    else:
        await ctx.send(embed=error_embed("目前沒有音樂在播放！"))


@bot.hybrid_command(name="pause", description="暫停播放")
@commands.guild_only()
@require_same_channel()
async def pause(ctx):
    vc = ctx.guild.voice_client
    if vc.is_playing():
        pause_playback(vc, get_state(ctx.guild.id))
        await ctx.send(embed=info_embed("⏸️ 已暫停"))
    else:
        await ctx.send(embed=error_embed("目前沒有音樂在播放！"))


@bot.hybrid_command(name="resume", description="繼續播放")
@commands.guild_only()
@require_same_channel()
async def resume(ctx):
    vc = ctx.guild.voice_client
    if vc.is_paused():
        resume_playback(vc, get_state(ctx.guild.id))
        await ctx.send(embed=info_embed("▶️ 繼續播放"))
    else:
        await ctx.send(embed=error_embed("目前沒有暫停中的音樂！"))


@bot.hybrid_command(name="stop", description="停止播放並清空待播清單")
@commands.guild_only()
@require_same_channel()
async def stop(ctx):
    stop_playback(ctx.guild)
    await ctx.send(embed=info_embed("⏹️ 已停止播放並清空待播清單"))


@bot.hybrid_command(name="loop", description="設定循環模式")
@commands.guild_only()
@app_commands.describe(mode="off = 關閉、song = 單曲循環、queue = 清單循環（不填則切換）")
async def loop_command(ctx, mode: Optional[Literal["off", "song", "queue"]] = None):
    state = get_state(ctx.guild.id)
    state.loop = mode or {"off": "song", "song": "queue", "queue": "off"}[state.loop]
    await ctx.send(embed=info_embed(f"🔁 循環模式：**{LOOP_LABELS[state.loop]}**"))


@bot.hybrid_command(name="shuffle", description="隨機打亂待播清單")
@commands.guild_only()
@require_same_channel()
async def shuffle(ctx):
    state = get_state(ctx.guild.id)
    if len(state.queue) < 2:
        await ctx.send(embed=error_embed("待播清單的歌曲太少，無法打亂"))
        return
    shuffle_queue(state)
    await ctx.send(embed=info_embed(f"🔀 已打亂 {len(state.queue)} 首歌曲"))


@bot.hybrid_command(name="remove", aliases=["rm"], description="從待播清單移除指定歌曲")
@commands.guild_only()
@require_same_channel()
@app_commands.describe(index="歌曲在待播清單中的編號（用 /queue 查看）")
async def remove(ctx, index: int):
    state = get_state(ctx.guild.id)
    if not 1 <= index <= len(state.queue):
        await ctx.send(embed=error_embed(f"編號要在 1 ~ {len(state.queue)} 之間"))
        return
    song = state.queue[index - 1]
    del state.queue[index - 1]
    if state.prefetched and state.prefetched[0] is song:
        state.prefetched = None
    await ctx.send(embed=info_embed(f"🗑️ 已移除：**{escape(song.title)}**"))


@bot.hybrid_command(name="clear", description="清空待播清單（不影響目前播放的歌）")
@commands.guild_only()
@require_same_channel()
async def clear(ctx):
    state = get_state(ctx.guild.id)
    count = len(state.queue)
    state.queue.clear()
    state.prefetched = None
    await ctx.send(embed=info_embed(f"🧹 已清空 {count} 首歌曲"))


@bot.hybrid_command(name="leave", aliases=["dc"], description="離開語音頻道並清空播放清單")
@commands.guild_only()
async def leave(ctx):
    vc = ctx.guild.voice_client
    if not vc:
        await ctx.send(embed=error_embed("機器人目前不在任何語音頻道中"))
        return
    state = get_state(ctx.guild.id)
    state.reset()
    await vc.disconnect()
    await clear_np_message(state)
    await ctx.send(embed=info_embed("👋 已離開語音頻道並清空播放清單！"))


@bot.hybrid_command(name="volume", aliases=["vol"], description="設定音量（例如 50 = 50%，最高 200）")
@commands.guild_only()
@app_commands.describe(percent="音量百分比，例如 50 代表 50%（0 ~ 200）")
async def set_volume(ctx, percent: Optional[float] = None):
    state = get_state(ctx.guild.id)
    if percent is None:
        await ctx.send(embed=info_embed(f"🔊 目前音量：{state.volume * 100:.0f}%"))
        return
    # 相容舊用法：輸入 0 ~ 2 視為倍率（例如 0.5 = 50%、1 = 100%），大於 2 視為百分比
    value = percent if percent <= 2 else percent / 100
    change_volume(ctx.guild, value)
    await ctx.send(embed=info_embed(f"🔊 音量已設置為 {state.volume * 100:.0f}%（此伺服器會記住這個設定）"))


@bot.hybrid_command(name="prefix", description="設定此伺服器的前綴指令符號")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
@app_commands.describe(new_prefix="新的前綴符號，例如 ! 或 ?")
async def set_prefix(ctx, new_prefix: str):
    new_prefix = new_prefix.strip()
    if not new_prefix or len(new_prefix) > 5 or " " in new_prefix:
        await ctx.send(embed=error_embed("前綴需為 1 ~ 5 個字元，且不能有空白"))
        return
    set_setting(ctx.guild.id, "prefix", new_prefix)
    await ctx.send(embed=info_embed(f"✅ 此伺服器的前綴已改為 `{new_prefix}`（斜線指令照常可用）"))


@bot.hybrid_command(name="help", description="顯示指令說明")
async def help_command(ctx):
    p = get_setting(ctx.guild.id, "prefix", default_prefix) if ctx.guild else default_prefix
    embed = discord.Embed(
        title="🎵 音樂機器人指令",
        description=f"所有指令都可以用 `/` 或 `{p}` 開頭\n支援 **YouTube**、**Spotify**（歌曲、專輯、播放清單）與 **Bilibili**（影片、多P、合集）",
        color=COLOR_INFO,
    )
    embed.add_field(name="▶️ 播放", value=(
        f"`play <關鍵字或網址>`（`{p}p`）點歌\n"
        f"`bilibili <關鍵字>`（`{p}bp`）在 B 站搜尋\n"
        f"`nowplaying`（`{p}np`）正在播放 + 控制面板\n"
        f"`queue`（`{p}q`）播放清單"
    ), inline=False)
    embed.add_field(name="🎛️ 控制", value=(
        f"`pause` / `resume` 暫停 / 繼續\n"
        f"`skip`（`{p}s`）跳過 · `stop` 停止並清空\n"
        f"`loop [off/song/queue]` 循環 · `shuffle` 隨機\n"
        f"`remove <編號>` 移除 · `clear` 清空待播\n"
        f"`volume <0~200>`（`{p}vol`）音量"
    ), inline=False)
    embed.add_field(name="⚙️ 其他", value=(
        f"`join` 加入語音 · `leave`（`{p}dc`）離開\n"
        f"`prefix <符號>` 修改此伺服器的前綴（需管理伺服器權限）"
    ), inline=False)
    embed.set_footer(text="「正在播放」訊息下方的按鈕也可以直接控制播放")
    await ctx.send(embed=embed)


if __name__ == "__main__":
    bot.run(token)
