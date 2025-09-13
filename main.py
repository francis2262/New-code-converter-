# main.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Literal, List, Optional, Dict, Any
import re
import json
import time

# We'll use sync playwright to keep code simpler for phone editing.
from playwright.sync_api import sync_playwright, Error as PlaywrightError

app = FastAPI(title="Bet Code Converter")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend from /static
app.mount("/", StaticFiles(directory="static", html=True), name="static")

# ---------------------------
# Models & small helpers
# ---------------------------
class ConvertRequest(BaseModel):
    code: str
    from_platform: Literal["sportybet", "bet9ja"]
    to_platform: Literal["sportybet", "bet9ja"]

# Minimal market map you can extend later
MARKET_MAP = {
    ("sportybet", "1X2"): "Match Result",
    ("sportybet", "GG"): "Both Teams To Score",
    ("sportybet", "O/U 2.5"): "Over/Under 2.5 Goals",
    ("bet9ja", "Match Result"): "1X2",
    ("bet9ja", "Both Teams To Score"): "GG",
    ("bet9ja", "Over/Under 2.5 Goals"): "O/U 2.5",
}

def map_markets(legs: List[Dict[str, Any]], from_plat: str, to_plat: str) -> List[Dict[str, Any]]:
    out = []
    for leg in legs:
        market_in = leg.get("market", "")
        mapped = MARKET_MAP.get((from_plat, market_in), market_in)
        out.append({**leg, "market": mapped})
    return out

def generate_booking_code_for_demo(to_plat: str, source_code: str) -> str:
    prefix = "BJ" if to_plat == "bet9ja" else "SP"
    return f"{prefix}{abs(hash(source_code)) % 100000:05d}"

# ---------------------------
# Playwright scraper for SportyBet
# - tries a few known URLs and selectors
# - returns standard slip: {"legs":[{home,away,market,pick,odds}, ...]}
# ---------------------------
def fetch_sportybet_slip_playwright(code: str, timeout: int = 25) -> Optional[Dict[str, Any]]:
    """
    Uses Playwright to open SportyBet share/booking page, extract a slip.
    Returns None on failure.
    """
    # Candidate URLs (SportyBet changes, we try a few patterns)
    urls = [
        f"https://www.sportybet.com/ng/m/sporty/booking?bookingCode={code}",
        f"https://www.sportybet.com/ng/m/?b={code}",
        f"https://www.sportybet.com/ng/m/sporty-code-share/{code}",
        f"https://www.sportybet.com/share/{code}",
    ]

    selectors_to_try = [
        "div.share-bet-slip",
        "div.booking-container",
        "div.bet-slip",
        "div.sports-bet-slip",
        "div[class*='slip']",
    ]

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
            page = browser.new_page()
            page.set_default_timeout(timeout * 1000)

            for url in urls:
                try:
                    page.goto(url)
                except Exception:
                    # try next URL
                    continue

                # 1) Try to read obvious slip container text
                for sel in selectors_to_try:
                    try:
                        if page.query_selector(sel):
                            text = page.inner_text(sel)
                            legs = parse_slip_from_text(text)
                            if legs:
                                browser.close()
                                return {"legs": legs}
                    except Exception:
                        continue

                # 2) Try to find embedded JSON in scripts (common)
                page_html = page.content()
                payload = extract_json_payload_from_html(page_html)
                if payload:
                    legs = parse_slip_from_payload(payload)
                    if legs:
                        browser.close()
                        return {"legs": legs}

            browser.close()
    except PlaywrightError as e:
        # Playwright not installed or browser failed
        return None
    except Exception:
        return None
    return None

def parse_slip_from_text(text: str) -> Optional[List[Dict[str, Any]]]:
    # Try simple "Home vs Away" extraction lines
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    legs = []
    for line in lines:
        m = re.search(r"(.+?)\s+v(?:s|\.)?\s+(.+)", line, flags=re.I)
        if not m:
            m = re.search(r"(.+?)\s+vs\s+(.+)", line, flags=re.I)
        if m:
            home = m.group(1).strip()
            away = m.group(2).strip()
            legs.append({"home": home, "away": away, "market": "Unknown", "pick": "", "odds": None})
    return legs if legs else None

def extract_json_payload_from_html(html: str) -> Optional[Dict]:
    # Search for JSON blobs in inline scripts
    patterns = [
        r"window\.__INITIAL_STATE__\s*=\s*({.*?});",
        r"window\.__DATA__\s*=\s*({.*?});",
        r"var\s+initialState\s*=\s*({.*?});",
        r"({\"booking\".*?})",
        r"({\"bets\".*?})",
    ]
    for pat in patterns:
        m = re.search(pat, html, flags=re.S)
        if not m:
            continue
        try:
            payload = json.loads(m.group(1))
            return payload
        except Exception:
            # sometimes there is trailing JS; try a best-effort cleanup
            txt = m.group(1)
            try:
                # remove trailing commas etc. This is best-effort and may fail.
                return json.loads(txt)
            except Exception:
                continue
    return None

def parse_slip_from_payload(payload: Dict) -> Optional[List[Dict[str, Any]]]:
    # Try common keys
    for key in ("booking", "slip", "bets", "items", "data"):
        data = payload.get(key)
        if not data:
            continue
        if isinstance(data, list):
            legs = []
            for it in data:
                home = it.get("home") or it.get("team1") or it.get("homeName") or ""
                away = it.get("away") or it.get("team2") or it.get("awayName") or ""
                market = it.get("market") or it.get("marketName") or it.get("type") or ""
                pick = it.get("pick") or it.get("selection") or ""
                odds = it.get("odds") or it.get("price") or it.get("odd") or None
                try:
                    odds = float(odds) if odds else None
                except Exception:
                    odds = None
                legs.append({"home": home, "away": away, "market": market, "pick": pick, "odds": odds})
            if legs:
                return legs
    return None

# ---------------------------
# API endpoints
# ---------------------------
@app.post("/api/convert")
def convert(req: ConvertRequest):
    # If from==to, reject
    if req.from_platform == req.to_platform:
        return {"ok": False, "message": "From/To platforms are the same.", "converted_code": None, "preview": None}

    slip = None

    # Only sportybet scraping implemented here. Bet9ja could be added with similar logic.
    if req.from_platform == "sportybet":
        slip = fetch_sportybet_slip_playwright(req.code)
    elif req.from_platform == "bet9ja":
        # For now keep a demo fallback (you can implement Bet9ja scraping later)
        demo = {
            "BJ99999": {"legs": [{"home":"Barcelona","away":"Real Madrid","market":"O/U 2.5","pick":"OVER","odds":1.95}]}
        }
        slip = demo.get(req.code)

    if not slip:
        return {"ok": False, "message": "Code not found or could not fetch (try a valid SportyBet code).", "converted_code": None, "preview": None}

    # Convert markets
    preview = {"legs": map_markets(slip["legs"], req.from_platform, req.to_platform)}
    converted_code = generate_booking_code_for_demo(req.to_platform, req.code)

    return {"ok": True, "message": "Converted (live scrape).", "converted_code": converted_code, "preview": preview}

@app.get("/api/health")
def health():
    return {"status": "ok", "timestamp": int(time.time())}

# ---------------------------
# WebSocket chat (real-time)
# ---------------------------
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: str):
        to_remove = []
        for conn in self.active:
            try:
                await conn.send_text(message)
            except Exception:
                to_remove.append(conn)
        for c in to_remove:
            self.disconnect(c)

manager = ConnectionManager()

@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            text = await websocket.receive_text()
            await manager.broadcast(text)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
