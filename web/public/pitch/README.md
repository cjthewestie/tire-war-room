# Pitch 投影片（3D 工廠白板）

3D 工廠（`/factory`）大廳東牆旁的白板會顯示這個資料夾裡的投影片。按 **▶ Demo（6 分鐘）** 後，3:00 鏡頭會飛到白板，換成 Pitch 階段。

## 怎麼換成自己的投影片

1. 把 PPT / Keynote / Google 簡報匯出成圖片，一頁一張（16:9，建議 1920×1080 PNG 或 JPG）。
   - PowerPoint：檔案 → 匯出 → 檔案類型選 PNG →「所有投影片」。
   - Keynote：檔案 → 輸出至 → 影像 → PNG。
   - 只有 PDF：`pdftoppm -png -r 150 deck.pdf slide` 會產生 `slide-01.png`、`slide-02.png`…（macOS 可用 `brew install poppler` 安裝）。
2. 照順序命名後放進這個資料夾：`web/public/pitch/01.png`、`02.png`、`03.png`…
3. 編輯同一個資料夾的 `slides.json`，照播放順序列出檔名：

   ```json
   { "slides": ["01.png", "02.png", "03.png"] }
   ```

4. 重新整理 `/factory` 就會看到。`slides.json` 是空的（`"slides": []`）或不存在時，白板顯示 3 張預設頁（「Pitch 投影片放這裡」、專案名稱、「待 CJ 嵌入」）。

不用重新 build：伺服器直接讀 `web/public/pitch/`（FastAPI 的 `/pitch`）。`pnpm dev` 也一樣。

## 播放時的按鍵

- `←` / `→`（或簡報筆的 PageUp / PageDown）：換頁（Pitch 階段或全螢幕時）
- 點白板：全螢幕看投影片，`Esc` 關閉
- `N` 下一步、`B` 暫停／繼續、`G` 重來
