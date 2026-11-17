# EDD 網站更新追蹤機器人

每 30 分鐘自動檢查 <https://baby0900343376-collab.github.io/>，有更新就發到 Discord 頻道，並把每一次的變動記錄在 [UPDATES.md](UPDATES.md)。

跑在 GitHub Actions 上，**免費、不用開著電腦**。

## 會追蹤什麼

| 類別 | 內容 | 通知範例 |
|---|---|---|
| 📄 網頁內容 | 關於、作品、販售區、聯絡、頁尾的文字 | ✏️ **關於** 修改：… |
| 🧰 精選作品 | 網站從 GitHub 自動抓的公開專案（跟網站用同一份排除清單） | 🆕 新作品、🔄 有新的更新、✏️ 介紹變更 |
| 🛍️ 插件販售 | 網站從 Tebex 自動抓的商品 | 🛒 新商品上架、價格變動、下架 |

網站裡 `GH` 和 `TEBEX` 的設定（排除哪些 repo、Tebex Token）機器人會直接從網頁讀，改網站就好，不用改機器人。

## 設定步驟（約 5 分鐘）

### 1. 建立 Discord Webhook

1. 在 Discord 對要接收通知的頻道按 ⚙ **編輯頻道** → **整合** → **Webhook** → **新 Webhook**
2. 名稱、頭像可以隨意改，按 **複製 Webhook 網址**

> ⚠ Webhook 網址等於這個頻道的發文權限，不要貼到公開的地方或檔案裡。

### 2. 建立 GitHub repo

1. 到 <https://github.com/new>，名稱例如 `site-update-bot`，選 **Public**（公開 repo 的 Actions 完全免費），其他選項不要勾
2. 點 **uploading an existing file**，把這個資料夾**裡面**的東西拖進去：`watch.py`、`README.md`、`.github` 資料夾
3. 按 **Commit changes**

> 如果 `.github` 資料夾拖不進去：改用 **Add file → Create new file**，檔名打 `.github/workflows/watch.yml`，把 `watch.yml` 的內容整個貼上，再按 **Commit changes**。

### 3. 把 Webhook 網址存成 Secret

repo 的 **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

- Name：`DISCORD_WEBHOOK_URL`
- Secret：貼上第 1 步複製的網址

### 4. 測試

1. repo 上方點 **Actions**，如果出現提示，按 **I understand my workflows, go ahead and enable them**
2. 左邊點 **網站更新追蹤** → 右邊 **Run workflow**
3. 勾選「只送一則測試訊息」→ **Run workflow** → Discord 應該會收到「✅ 測試訊息」
4. 再按一次 **Run workflow**（不要勾）→ 收到「✅ 開始追蹤 EDD 的網站」就完成了

之後每 30 分鐘會自動檢查一次，有變動才會發訊息。

## 常見問題

- **改了網站，多久會收到通知？** GitHub Pages 部署約 1～2 分鐘，加上排程間隔，通常 30 分鐘內。急的話到 Actions 手動按 **Run workflow**。
- **Actions 失敗、顯示紅色 ✗**：點進去看「檢查網站」那一步的訊息。最常見的是 Secret 名稱打錯，必須是 `DISCORD_WEBHOOK_URL`。
- **想看所有歷史更新**：看 repo 裡的 [UPDATES.md](UPDATES.md)，或 `state/updates.jsonl`（機器可讀）。
- **想改檢查頻率**：改 `.github/workflows/watch.yml` 裡的 `cron`。例如每小時一次：`'7 * * * *'`。
- **想追蹤別的網站**：在 workflow 的「檢查網站」步驟加 `SITE_URL: https://…`。
- **GitHub 會不會自己把排程關掉？** 60 天沒有任何更新的 repo 會被停用排程，所以機器人每週會在 `state/heartbeat.txt` 寫一次記錄，保持活動。

## 本機測試

```bash
python watch.py --dry-run        # 只印出訊息，不發到 Discord、不存記錄
```

只用 Python 標準函式庫，不需要安裝套件。
