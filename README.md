## 前置需求

- **Python 3.8+**  
- **ffmpeg**  
- 一個有效的 **cookies.txt** 檔案（供 yt-dlp 使用，避免因年齡或區域限制而無法存取影片）

## 安裝說明

### 1. 建立與啟動 Python 虛擬環境

在專案根目錄下執行以下指令：

- 建立虛擬環境：
  ```
  python -m venv venv
  ```
### 2.啟動虛擬環境：
Windows:
```
venv\Scripts\activate
pip install -r requirements.txt
```
macOS/Linux:
```
source venv/bin/activate
pip install -r requirements.txt
```
### 3. 設定 cookies.txt
取得 cookies.txt：
你可以透過瀏覽器外掛（例如 Chrome 或 Firefox 的 cookies.txt 外掛）來匯出 cookies。

放置位置：
將取得的 cookies.txt 放置在專案根目錄，或依照你的需求修改 config.txt 中的 cookies_path 欄位以指定正確路徑。

### 4. 安裝 ffmpeg
下載：
造訪 ffmpeg 官方網站 或使用 Windows 版的 FFmpeg Essentials Build。

安裝與設定：
將下載後的壓縮檔解壓縮，並確認 ffmpeg 執行檔的位置。接著，打開 config.txt，將 ffmpeg_path 欄位更新為 ffmpeg 執行檔的完整路徑（例如：ffmpeg-7.1-essentials_build/bin/ffmpeg.exe）。

配置檔 (config.txt) 說明
在 config.txt 中，你需要更新以下欄位：
```
[DEFAULT]
cookies_path = cookies.txt
volume = 0.1
token = 你的Discord Bot Token
playlist = 20
ffmpeg_path = ffmpeg-7.1-essentials_build/bin/ffmpeg.exe
```  
cookies_path: cookies.txt 檔案的路徑。  
volume: 預設音量（範圍 0.0 ~ 2.0）。  
token: 你的 Discord 機器人 Token。  
playlist: 播放清單最大筆數（預設 20 首）。  
ffmpeg_path: ffmpeg 執行檔的完整路徑。  

### 5.執行機器人
一切就緒後，在虛擬環境中執行以下指令啟動機器人：
```
python main.py
```

常用指令  
!join：機器人加入你所在的語音頻道。  
!p [關鍵字或URL]：新增單支歌曲或播放清單到待播清單中。  
!leave：機器人離開語音頻道並清空播放清單。  
!queue：顯示目前的播放清單。  
!skip：跳過當前播放的歌曲。  
!volume [音量值]：設定播放音量。  
  
