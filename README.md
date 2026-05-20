# 🎾 Tennis Predictor — ATP/WTA 巡迴賽預測系統

參考 [mlb-predictor](https://github.com/willy0929716513-debug/mlb-predictor) 架構，針對網球特性重新設計。

## 核心模型

### 1. Markov Chain 發球/接球模型

```
p  = P(發球方贏得一分)     ← 各球員場地特化統計
   = 0.5 × (svpt_won + 1 - opp_rtpt_won) + surface_adj

g  = P(發球方贏得一局)     ← 解析公式（含 deuce 級數列）
g  = p⁴(1 + 4q + 10q²) + 20p³q³ × p²/(p²+q²)

s  = P(一方贏得一盤)      ← 動態規劃（支持 tiebreak）
m  = P(一方贏得比賽)     ← 遞迴（BO3 / BO5 大滿貫）
```

### 2. ELO 評分（輔助模型）

- 場地特化 ELO：沿場 / 天濮 / 草地 分別檔廚
- 資料來源：[Tennis Abstract](https://github.com/JeffSackmann/tennis_atp)（免費）
- 公式：P = 1 / (1 + 10^(ΔELO/400))

### 3. 混合模型

```
blend = 0.60 × Markov勝率 + 0.40 × ELO勝率 + H2H御正
```

## 資料來源

| 來源 | 用途 | 費用 |
|------|------|------|
| [Tennis Abstract](https://github.com/JeffSackmann/tennis_atp) | 歷史ELO、球員資料 | 免費 |
| [The Odds API](https://the-odds-api.com) | 即時賠率 | 免費額度 |
| ntfy.sh | 推播通知 | 免費 |
| GitHub Actions | 自動調度 | 免費 |

## 幕後仸款標準

| 類型 | Edge 門滚 | 模型勝率門滚 | Kelly |
|------|---------|-----------|-------|
| 匆贏 (ML) | ≥ 6% | ≥ 60% | 1/4 Kelly, $50–$200 |

## 场地調整量

| 場地 | 發球勝分率調整 |
|------|----------|
| 天濮場 | +2.2pp （發球更大優勢） |
| 沿場 | -2.0pp （接球方更容易破發） |
| 硬地 | 基準（不調整） |

## Setup

```bash
pip install -r requirements.txt
export ODDS_API_KEY=your_key
export NTFY_TOPIC=your_ntfy_topic
python tennis_bot.py
```

### GitHub Secrets

| Secret | 用途 |
|--------|------|
| `ODDS_API_KEY` | The Odds API 金鑰 |
| `NTFY_TOPIC` | ntfy.sh 主題 |
| `DISCORD_WEBHOOK` | Discord 通知 (optional) |
| `GIST_TOKEN` | 戰績歷史儲存 (optional) |
| `GIST_ID` | Gist ID (optional) |

## 自動執行時間

- **07:00 TW** — 歐洲下午賽前預測
- **18:00 TW** — 美洲日間賽前
- **21:00 TW** — 場中賠率監控
