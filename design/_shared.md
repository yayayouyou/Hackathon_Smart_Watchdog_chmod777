# 版型提案用的共同設定（給 .dc.html 抄）

色票與字體直接取自 `webapp/style.css` 的 `:root`，未四捨五入：

    --ink:#14202A  --ink-2:#3D4E5C  --ink-3:#6B7C8A  --ink-4:#93A2AE
    --paper:#EFF3F5  --panel:#FFFFFF  --sunk:#E6EDF0  --rule:#CFD9E0
    --pub:#2A78D6（公立） --np:#EB6834（非營利） --prv:#1BAF7A（私立）
    --seal:#B23A2F（稽查紅） --seal-soft:#B23A2F14
    --warn:#B07A16  --good:#2C7A5B
    --c1:#ECCECB --c2:#DCA6A1 --c3:#C9756D --c4:#B23A2F（行政區優先度）
    --k0:#C8D0D7 --k1:#C4A9DA --k2:#9E72C0 --k3:#7340A0 --k4:#4A1D78（裁罰件數）
    --shadow:0 1px 2px #14202a12, 0 6px 16px #14202a0e
    字體 IBM Plex Sans / IBM Plex Mono + PingFang TC / Noto Sans TC

## 版型裡出現的數字全部是實測值，不要編

| 數字 | 出處 |
|---|---|
| 全市 1,213 園 | `data/processed/institutions_ntpc.csv` |
| 1,386 筆裁罰、483 園有紀錄 | `penalties_ntpc.csv` |
| 裁罰分級 730／189／175／73／46 | 前端圖例實測 |
| 132 份非營利財報、22 園法遵未通過、4 園高嚴重度 | `compliance_findings.csv` |
| AUC 0.658、前 100 名 2.29 倍 | 時間軸回測 |
| 1,501 筆官方事件 | `official_events_ntpc.csv` |
| 即時管道 4/6、31 則、涉 8 園 | 最近一次掃描 2026-09-08 |
| 162 份 PDF | `data/raw` |
| 建議書 143 份 | `audit_letters/` |

## 用詞界線（四個版型都要守）

- 寫「建議查核」「優先序」，**不寫**「高風險名單」「疑似不法」。
- 無資料寫「資料不足」，**不寫**「低風險」。
- 裁罰件數是**公開事實**，可以直說；分數是推論，不單獨對外顯示。
