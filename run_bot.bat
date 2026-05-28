@echo off
cd /d "%~dp0"

set ODDS_API_KEY=在這裡貼你的KEY
set DISCORD_WEBHOOK=在這裡貼你的WEBHOOK
set GIST_TOKEN=在這裡貼你的GIST_TOKEN
set GIST_ID=在這裡貼你的GIST_ID

python tennis_bot.py >> logs\tennis_bot.log 2>&1
