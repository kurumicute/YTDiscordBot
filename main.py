import asyncio
import urllib.parse
from collections import defaultdict, deque
from concurrent.futures import ProcessPoolExecutor
import yt_dlp
import discord
from discord.ext import commands
import configparser

# 讀取 config.txt 設定

config = configparser.ConfigParser()
files = config.read("config.txt", encoding="utf-8")  # 指定編碼為 UTF-8
print("已讀取的檔案：", files)

cookies_path = config["DEFAULT"]["cookies_path"]
volume = float(config["DEFAULT"]["volume"])
token = config["DEFAULT"]["token"]
playlist = int(config["DEFAULT"]["playlist"])
ffmpeg_path = config["DEFAULT"]["ffmpeg_path"]

# 初始化 Bot
intents = discord.Intents.default()
intents.message_content = True  # 若需要存取訊息內容則開啟此 Intent
bot = commands.Bot(command_prefix="!", intents=intents)

# 非同步執行緒池
process_pool = ProcessPoolExecutor()

# yt-dlp 提取播放清單時使用的選項（只取得基本資訊）
YDL_OPTIONS = {
    "format": "bestaudio/best",
    "playlistend": playlist,  # 使用 config 中的歌單上限 預設20首
    "cookiefile": cookies_path,
    "noplaylist": False,      # 允許處理播放清單
    "extract_flat": True,     # 僅提取基本資訊
    "skip_download": True,
    "quiet": True,
    "nocheckcertificate": True,
}

# FFmpeg 參數（注意：executable 將在播放時指定）
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -err_detect ignore_err',
    'options': '-vn'
}

# 每個群組的播放清單（以 guild ID 為 key）
queues = defaultdict(deque)
# 用來保存預先提取的下一首歌曲的 (title, audio_url)
prefetched = {}

async def async_get_video_info(query):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(process_pool, get_video_info, query)

def simplify_url(url):
    try:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        if "v" in qs:
            video_id = qs["v"][0]
            return f"https://www.youtube.com/watch?v={video_id}"
    except Exception as e:
        print("URL 簡化失敗：", e)
    return url

def get_playlist_info(url):
    songs = []
    with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            print("提取播放清單資訊失敗：", e)
            return songs

        if "entries" in info:
            for i, entry in enumerate(info["entries"]):
                if i >= playlist:
                    break
                if not entry:
                    continue
                try:
                    video_id = entry.get("id")
                    if video_id:
                        video_url = f"https://www.youtube.com/watch?v={video_id}"
                        title = entry.get("title") or "Unknown Title"
                        songs.append((title, video_url))
                except Exception as e:
                    print("處理播放清單 entry 時發生錯誤：", e)
                    continue
        else:
            # 若 URL 指向單支影片
            video_id = info.get("id")
            if video_id:
                video_url = f"https://www.youtube.com/watch?v={video_id}"
                title = info.get("title") or "Unknown Title"
                songs.append((title, video_url))
    return songs

def get_video_info(query):
    if not query.startswith("http"):
        search_query = f"ytsearch:{query}"
    else:
        search_query = query
    with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
        try:
            info = ydl.extract_info(search_query, download=False)
        except Exception as e:
            print("提取影片資訊失敗：", e)
            return None
        if "entries" in info:
            if not info["entries"]:
                print("搜尋關鍵詞沒有返回任何結果")
                return None
            entry = info["entries"][0]
        else:
            entry = info
        video_id = entry.get("id")
        title = entry.get("title") or "Unknown Title"
        if video_id:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            return (title, video_url)
    return None

def get_full_audio_url(video_url):
    opts = {
        "format": "bestaudio/best",
        "cookiefile": cookies_path,
        "noplaylist": True,
        "quiet": True,
        "nocheckcertificate": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(video_url, download=False)
        except Exception as e:
            print("提取完整影片資訊失敗：", e)
            return None
        video_link = None
        if "formats" in info:
            for fmt in info["formats"]:
                candidate = fmt.get("url")
                if candidate and candidate.startswith("http") and fmt.get("acodec") != "none":
                    video_link = candidate
                    break
        if not video_link:
            video_link = simplify_url(video_url)
        return video_link

async def prefetch_audio(guild_id, title, video_url):
    audio_url = await asyncio.to_thread(get_full_audio_url, video_url)
    prefetched[guild_id] = (title, audio_url)

async def play_next(ctx):
    guild_id = ctx.guild.id
    if queues[guild_id]:
        if guild_id in prefetched:
            title, audio_url = prefetched.pop(guild_id)
            queues[guild_id].popleft()  # 移除已預取的歌曲
        else:
            title, video_url = queues[guild_id].popleft()
            audio_url = await asyncio.to_thread(get_full_audio_url, video_url)
        # 如果播放清單中還有下一首，預取下一首音訊
        if queues[guild_id]:
            next_title, next_video_url = queues[guild_id][0]
            asyncio.create_task(prefetch_audio(guild_id, next_title, next_video_url))
        
        voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
        if voice_client and voice_client.is_connected():
            await ctx.send(f"🎵 正在播放：{title}")
            try:
                # 指定 ffmpeg 的完整檔案位置，使用 ffmpeg_path
                source = discord.FFmpegPCMAudio(audio_url, executable=ffmpeg_path, **FFMPEG_OPTIONS)
                audio = discord.PCMVolumeTransformer(source, volume)
                def after_play(error):
                    if error:
                        print(f"播放時發生錯誤：{error}")
                    asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
                voice_client.play(audio, after=after_play)
            except Exception as e:
                await ctx.send(f"❌ 播放 {title} 時發生錯誤：{str(e)}，跳過此曲。")
                await play_next(ctx)
        else:
            await ctx.send("❌ 無法播放，機器人未連接語音頻道。")
    else:
        # 如果播放清單空了，等待一段時間後斷線
        await asyncio.sleep(300)
        voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
        if voice_client and not queues[guild_id]:
            await voice_client.disconnect()
            
@bot.event 
async def on_ready():
    print(f"✅ {bot.user} 已成功登入！")

@bot.command(name="join")
async def join(ctx):
    if ctx.author.voice:
        channel = ctx.author.voice.channel
        await channel.connect()
        await ctx.send(f"🎶 已加入語音頻道：{channel.name}")
    else:
        await ctx.send("❌ 你需要先加入一個語音頻道！")

@bot.command(name="p", aliases=["play", "P", "PLAY"])
async def play_command(ctx, *, query: str):
    # 檢查是否已有連線至語音頻道
    voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if not voice_client:
        if ctx.author.voice:
            channel = ctx.author.voice.channel
            try:
                voice_client = await channel.connect()
            except Exception as e:
                await ctx.send("❌ 加入語音頻道失敗: " + str(e))
                return
        else:
            await ctx.send("❌ 你需要先加入一個語音頻道！")
            return

    await ctx.send("🔍 取得音樂連結中...")
    try:
        songs = []
        # 如果 query 為 URL
        if query.startswith("http"):
            # 判斷是否包含播放清單參數（例如 "list="）
            if "list=" in query:
                # 異步呼叫同步的 get_playlist_info
                songs = await asyncio.to_thread(get_playlist_info, query)
            else:
                # 單支影片 URL
                video_info = await asyncio.to_thread(get_video_info, query)
                if video_info:
                    songs = [video_info]
        else:
            # 非 URL，視為關鍵字搜尋
            video_info = await asyncio.to_thread(get_video_info, query)
            if video_info:
                songs = [video_info]

        if not songs:
            await ctx.send("❌ 未能取得音樂連結，請檢查輸入或關鍵字。")
            return

        # 將取得的歌曲加入播放清單（以 guild id 為 key）
        queues[ctx.guild.id].extend(songs)
        if len(songs) > 1:
            await ctx.send(f"🎶 **已加入播放清單，共 {len(songs)} 首歌曲到待播清單！**")
        else:
            await ctx.send(f"🎶 **已加入 {songs[0][0]} 到待播清單！**")

        # 如果目前沒在播放，則開始播放
        if not voice_client.is_playing():
            await play_next(ctx)

    except Exception as e:
        await ctx.send(f"❌ 無法解析音樂：{str(e)}")
        
@bot.command(name="leave")
async def leave(ctx):
    voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if voice_client:
        await voice_client.disconnect()
        if ctx.guild.id in queues:
            queues[ctx.guild.id].clear()
        await ctx.send("👋 已離開語音頻道並清空播放清單！")
    else:
        await ctx.send("❌ 機器人目前不在任何語音頻道中")

@bot.command(name="queue")
async def show_queue(ctx):
    guild_id = ctx.guild.id
    if not queues[guild_id]:
        await ctx.send("📭 播放清單目前是空的！")
        return

    # 將隊列內容轉成串列，並依每頁 20 首分割
    songs = list(queues[guild_id])
    pages = []
    for i in range(0, len(songs), 20):
        page_lines = []
        for idx, (title, _) in enumerate(songs[i:i+20]):
            page_lines.append(f"{i+idx+1}. {title}")
        pages.append("\n".join(page_lines))
    
    total_pages = len(pages)
    current_page = 0

    # 傳送初始頁面訊息，並保留字元數預留安全範圍
    message = await ctx.send(f"🎵 **播放清單 - 第 {current_page+1}/{total_pages} 頁：**\n{pages[current_page][:1900]}")
    
    # 若只有一頁，則不需要添加反應
    if total_pages == 1:
        return

    # 添加左右翻頁的反應表情
    await message.add_reaction("⬅️")
    await message.add_reaction("➡️")

    def check(reaction, user):
        return (
            user == ctx.author and 
            str(reaction.emoji) in ["⬅️", "➡️"] and 
            reaction.message.id == message.id
        )

    # 利用迴圈等待使用者反應
    while True:
        try:
            reaction, user = await bot.wait_for("reaction_add", timeout=60.0, check=check)
            if str(reaction.emoji) == "➡️" and current_page < total_pages - 1:
                current_page += 1
                await message.edit(content=f"🎵 **播放清單 - 第 {current_page+1}/{total_pages} 頁：**\n{pages[current_page][:1900]}")
            elif str(reaction.emoji) == "⬅️" and current_page > 0:
                current_page -= 1
                await message.edit(content=f"🎵 **播放清單 - 第 {current_page+1}/{total_pages} 頁：**\n{pages[current_page][:1900]}")
            # 移除使用者的反應，讓使用者可以重複點擊
            await message.remove_reaction(reaction, user)
        except asyncio.TimeoutError:
            # 超過 60 秒無反應後離開翻頁模式
            break

@bot.command(name="skip")
async def skip(ctx):
    voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if voice_client and voice_client.is_playing():
        voice_client.stop()  # 停止播放後，after callback 會自動呼叫 play_next
        await ctx.send("THE 跳！")
    else:
        await ctx.send("❌ 目前沒有音樂在播放！")

@bot.command(name="volume")
async def set_volume(ctx, volume_value: float):
    global volume
    voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    
    if voice_client and voice_client.is_playing() and voice_client.source:
        volume = max(0.0, min(volume_value, 2.0))  # 限制音量在 0.0 到 2.0 之間
        voice_client.source.volume = volume
        await ctx.send(f"🔊 音量已設置為 {volume * 100:.0f}%")
    else:
        await ctx.send("❌ 目前沒有音樂在播放！")

# 啟動 Bot
bot.run(token)
