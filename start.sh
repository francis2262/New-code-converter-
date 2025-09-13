#!/bin/bash
set -e

# 1) Install python deps
pip install --upgrade pip
pip install -r requirements.txt

# 2) Install Playwright browser (Chromium)
# Run from start so Playwright CLI exists (installation may be slow)
playwright install chromium

# 3) Start Uvicorn
exec uvicorn main:app --host 0.0.0.0 --port $PORT
