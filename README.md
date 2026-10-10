## 前置需求

- **Python 3.10+**（yt-dlp 新版已不支援 3.8 / 3.9）
- **ffmpeg**
- （選用）**cookies.txt**：遇到年齡限制或「請登入以確認你不是機器人」時才需要

## 安裝說明

### 1. 建立並啟動 Python 虛擬環境，安裝套件

Windows:
```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```
macOS/Linux:
```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` 會一併安裝：
- `discord.py[voice]`：包含 Discord 語音新版必須的端對端加密（DAVE）套件 `davey`
- `yt-dlp[default,deno]`：YouTube 現在需要 JavaScript 執行環境（deno）才能解析影片

> YouTube 常常改版，播不了時先試試 `pip install -U -r requirements.txt` 更新。

### 2. Discord Developer Portal 設定

1. 到 https://discord.com/developers/applications 選你的機器人 → **Bot**
2. 開啟 **Message Content Intent**（`!` 前綴指令需要）
3. 邀請機器人時，OAuth2 URL Generator 勾選 `bot` 和 `applications.commands`（斜線指令需要），
   權限至少要有：檢視頻道、傳送訊息、連接、說話

### 3. 安裝 ffmpeg

下載 Windows 版 FFmpeg Essentials Build 並解壓縮。
在 config.txt 的 `ffmpeg_path` 填入 ffmpeg.exe 的路徑（相對路徑以 main.py 所在資料夾為準）。
如果 ffmpeg 已經加入系統 PATH，路徑填錯也會自動使用 PATH 裡的 ffmpeg。

### 4. （選用）設定 cookies.txt

用瀏覽器擴充功能（例如 "Get cookies.txt LOCALLY"）在登入 YouTube 後匯出 cookies.txt，放在專案資料夾。
沒有這個檔案機器人也能正常運作。cookies 會過期，播不了時記得重新匯出。
機器人只會讀取這個檔案，不會改寫它。

### 5. 設定 config.txt

把 `config.example.txt` 複製一份並改名為 `config.txt`，再填入你的設定
（`config.txt` 含有 Token，已列在 `.gitignore`，不會被上傳到 GitHub）：

```
[DEFAULT]
cookies_path = cookies.txt
volume = 0.1
token = 你的Discord Bot Token
playlist = 20
ffmpeg_path = ffmpeg-7.1-essentials_build/bin/ffmpeg.exe
prefix = !
idle_timeout = 300
```
- cookies_path：cookies.txt 路徑（檔案不存在時自動略過）
- volume：預設音量（0.0 ~ 2.0）
- token：Discord 機器人 Token
- playlist：播放清單一次最多加入幾首（預設 20）
- ffmpeg_path：ffmpeg 執行檔路徑
- prefix：預設的前綴指令符號（選填，預設 `!`；各伺服器可用 `/prefix` 另外設定）
- idle_timeout：播放清單空了之後幾秒自動離開（選填，預設 300）

### 6. 執行機器人

```
python main.py
```
啟動時會自動同步斜線指令。第一次同步後，斜線指令可能要等幾分鐘才會出現在 Discord，
也可以重新開啟 Discord（Ctrl+R）讓它更新。

## 功能

- **多伺服器同時使用**：每個伺服器各自有播放清單、音量、循環模式，互不影響；
  音量與前綴會記在 `guild_settings.json`，重開機器人也不會不見
- **YouTube**：關鍵字搜尋、影片網址、播放清單
- **Spotify**：歌曲、專輯、播放清單連結（含 `spotify.link` 短網址），**不需要 Spotify API 金鑰**。
  Spotify 不提供音訊，所以機器人會用「歌名 + 歌手」到 YouTube 找同一首歌來播
- **Bilibili（B站）**：影片網址（含手機版、`b23.tv` 短網址）、多 P 影片（整組加入；網址帶 `?p=2` 則只播那一 P）、
  合集 / 系列 / 收藏夾，以及用 `/bilibili` 在 B 站搜尋。
  部分影片限中國大陸觀看或需要登入（例如番劇、付費內容）會無法播放
- **控制面板**：「正在播放」訊息有進度條、封面，下方按鈕可以 暫停/繼續、跳過、停止、循環、隨機、調音量、看清單
- 只有和機器人在同一個語音頻道的人可以控制播放
- 播放清單空了或語音頻道沒人時會自動離開

## 指令（`/` 與 `!` 都可以用）

| 指令 | 縮寫 | 說明 |
|---|---|---|
| `play <關鍵字或網址>` | `!p` | 點歌（關鍵字搜 YouTube；網址支援 YouTube / Spotify / B站） |
| `bilibili <關鍵字>` | `!bp`、`!bili` | 在 B 站搜尋並播放 |
| `nowplaying` | `!np` | 顯示正在播放 + 控制面板 |
| `queue` | `!q` | 顯示播放清單（按鈕翻頁） |
| `skip` | `!s` | 跳過目前歌曲 |
| `pause` / `resume` | | 暫停 / 繼續 |
| `stop` | | 停止並清空待播清單 |
| `loop [off/song/queue]` | | 循環模式（不填則依序切換） |
| `shuffle` | | 隨機打亂待播清單 |
| `remove <編號>` | `!rm` | 移除待播清單中的某首歌 |
| `clear` | | 清空待播清單 |
| `volume <數值>` | `!vol` | 設定音量，`50` = 50%（最高 200），也可用舊寫法 `0.5` |
| `join` / `leave` | `!dc` | 加入 / 離開語音頻道 |
| `prefix <符號>` | | 修改此伺服器的前綴（需「管理伺服器」權限） |
| `help` | | 顯示指令說明 |
