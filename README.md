# 文稿室｜投稿審查系統

個人期中專題。使用 Flask、SQLite 和 Jinja 製作，提供作者與編輯兩個工作區。

## 在 Windows PowerShell 啟動

解壓縮後，在 VS Code 開啟 `submission-review` 資料夾，再開啟終端機。依序執行以下指令。

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m flask --app app create-editor editor
.\.venv\Scripts\python.exe -m flask --app app run
```

建立編輯帳號時，終端機會要求輸入兩次密碼，輸入時畫面不會顯示字元。這個指令只需執行一次。瀏覽器開啟 <http://127.0.0.1:5000/>，點「註冊成為作者」建立作者帳號。

首次啟動或建立編輯帳號時，系統會建立 `instance/review.sqlite3`。之後重新啟動只要執行最後一行。預設重新啟動後需重新登入。如需保留登入狀態，可以在啟動前設定固定且私密的 `SECRET_KEY` 環境變數。

## 展示流程

1. 註冊作者，登入後新增草稿。
2. 填寫類別、標題和文章內容，按「儲存並送審」。
3. 登出後用編輯帳號登入，在待審查清單開啟稿件並退回。
4. 作者登入，修改稿件後重新送審。
5. 編輯通過稿件。作者回到「我的投稿」查看「已通過」狀態。

## 用 Codex 修改

把解壓後的 `submission-review` 資料夾作為 Codex 的工作目錄。下面是最常用的檔案。

| 檔案 | 用途 |
| --- | --- |
| `app.py` | 登入、註冊、角色權限、投稿和審查流程 |
| `schema.sql` | 帳號和稿件的 SQLite 表格 |
| `templates/auth.html` | 登入與註冊頁 |
| `templates/list.html` | 我的投稿與待審查清單 |
| `templates/draft.html` | 稿件表單和審查頁 |
| `static/style.css` | 網頁樣式與背景配置 |
| `static/writers.png` | 登入與註冊頁共用的作家群像 |
