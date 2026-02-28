#!/usr/bin/env bash

cd /home/qbornet/bet-bot
python3 -m venv .venv/
source .venv/bin/activate

python3 -m pip install -r requirements.txt 2>&1 >/dev/null
nohup python3 bot/main.py
