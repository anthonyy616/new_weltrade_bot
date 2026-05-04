from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from core.bot_manager import BotManager
from core.trading_engine import TradingEngine 
from supabase import create_client, Client
import asyncio
import os
import signal
import sys
from dotenv import load_dotenv
from cachetools import TTLCache 

load_dotenv()

# --- FRESH SESSION: Clean stale DB on boot ---
DB_PATH = "db/grid_v3.db"
if os.path.exists(DB_PATH):
    try:
        os.remove(DB_PATH)
        print(f"[STARTUP] Cleaned stale DB: {DB_PATH}")
    except Exception as e:
        print(f"[STARTUP] Could not clean DB (may be locked): {e}")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Auth Cache (30 seconds - shorter TTL for multi-user support)
auth_cache = TTLCache(maxsize=100, ttl=30)

# --- 1. Initialize Core Systems ---
bot_manager = BotManager()
trading_engine = TradingEngine(bot_manager)

@app.on_event("startup")
async def startup_event():
    print("[SERVER] Starting: Launching Monolith Engine...")
    asyncio.create_task(trading_engine.start())


# --- Pydantic Models for Config ---

class SymbolConfig(BaseModel):
    """Config for a single symbol (Pair Strategy)"""
    enabled: Optional[bool] = None
    grid_distance: Optional[float] = None        # Pips between atomic fires
    tp_pips: Optional[float] = None              # Take profit distance
    sl_pips: Optional[float] = None              # Stop loss distance
    bx_lot: Optional[float] = None               # Initial Buy lot
    sy_lot: Optional[float] = None               # Initial Sell lot
    sx_lot: Optional[float] = None               # Completing Sell lot
    by_lot: Optional[float] = None               # Completing Buy lot
    single_fire_lot: Optional[float] = None      # Single fire lot size
    single_fire_tp_pips: Optional[float] = None  # Single fire TP distance
    single_fire_sl_pips: Optional[float] = None  # Single fire SL distance
    protection_distance: Optional[float] = None  # Pips before nuclear reset on reversal

class GlobalConfig(BaseModel):
    """Global settings"""
    max_runtime_minutes: Optional[int] = None

class ConfigUpdate(BaseModel):
    """Multi-asset config update payload"""
    global_settings: Optional[GlobalConfig] = None
    symbols: Optional[Dict[str, SymbolConfig]] = None


# --- 2. Auth Helper ---
def verify_token_sync(token):
    """
    Verify Supabase token with short-term caching.
    Cache by token for 30 seconds to reduce API calls while allowing multiple users.
    """
    if token in auth_cache: 
        return auth_cache[token]
    
    try:
        user = supabase.auth.get_user(token)
        if user and user.user:
            auth_cache[token] = user
            return user
    except Exception as e:
        print(f"[AUTH] Token validation error: {e}")
        # Remove from cache if validation failed
        if token in auth_cache:
            del auth_cache[token]
    return None

async def get_current_bot(request: Request):
    """
    Get or create bot instance for the authenticated user.
    Each user gets their own isolated bot instance.
    """
    auth_header = request.headers.get('Authorization')
    if not auth_header: 
        # [DEBUG] Allow debug token for testing without Supabase
        # raise HTTPException(401, "Missing token")
        print("[AUTH] No token provided, defaulting to debug user due to testing environment.")
        return await bot_manager.get_or_create_bot("92b17ba5-59c0-48c2-85fb-d78f9a38655c")

    if auth_header == "Bearer DEBUG":
         return await bot_manager.get_or_create_bot("92b17ba5-59c0-48c2-85fb-d78f9a38655c")
    
    try:
        token = auth_header.split(" ")[1]
        user = await asyncio.to_thread(verify_token_sync, token)
    except Exception as e:
        print(f"[AUTH] Check Failed: {e}")
        raise HTTPException(401, "Auth Validation Failed")

    if not user: 
        raise HTTPException(401, "Invalid Token")
    
    # Each user gets their own bot instance (multi-tenant support)
    return await bot_manager.get_or_create_bot(user.user.id)

# --- 3. API Routes (Defined BEFORE Static Mount) ---

@app.get("/env")
async def get_env():
    return { "SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY }

@app.get("/health", status_code=200)
@app.head("/health", status_code=200)
async def health_check():
    """Lightweight health check for VPS monitoring (GET/HEAD)"""
    return {"status": "ok"}

@app.get("/config")
async def get_config(bot = Depends(get_current_bot)):
    """Get full multi-asset config"""
    return bot.config

@app.post("/config")
async def update_config(config: ConfigUpdate, bot = Depends(get_current_bot)):
    """Update multi-asset config"""
    update_data = {}
    
    # Handle global settings
    if config.global_settings:
        update_data["global"] = {
            k: v for k, v in config.global_settings.model_dump().items() 
            if v is not None
        }
    
    # Handle symbol-specific settings
    if config.symbols:
        update_data["symbols"] = {}
        for symbol, sym_cfg in config.symbols.items():
            sym_data = {k: v for k, v in sym_cfg.model_dump().items() if v is not None}
            if sym_data:
                update_data["symbols"][symbol] = sym_data
    
    updated = bot.config_manager.update_config(update_data)
    return updated


# --- Per-Symbol Control Endpoints ---

@app.post("/control/start")
async def start_all(bot = Depends(get_current_bot)):
    """Start all enabled symbols - always starts with fresh DB"""
    # Clean stale DB for fresh session
    if os.path.exists(DB_PATH):
        try:
            os.remove(DB_PATH)
            print(f"[START] Cleaned DB for fresh session: {DB_PATH}")
        except Exception as e:
            print(f"[START] Could not clean DB: {e}")
            return {
                "status": "blocked",
                "error": f"DB file locked ({e}). Please terminate all or restart bot."
            }
    
    # [FIX] Auto-Restart Trading Engine if stopped
    if not trading_engine.running:
        print("[SERVER] Restarting Trading Engine...")
        asyncio.create_task(trading_engine.start())
        
        # Wait for engine to initialize (up to 5s)
        for _ in range(10):
            if trading_engine.running:
                break
            await asyncio.sleep(0.5)
        
    await bot.start()
    return {"status": "started", "symbols": bot.config_manager.get_enabled_symbols()}

@app.post("/control/stop")
async def stop_all(bot = Depends(get_current_bot)):
    """Stop all symbols"""
    await bot.stop()
    return {"status": "stopped"}

@app.post("/control/start/{symbol}")
async def start_symbol(symbol: str, bot = Depends(get_current_bot)):
    """Start a specific symbol"""
    # Clean stale DB for fresh session
    if os.path.exists(DB_PATH):
        try:
            os.remove(DB_PATH)
            print(f"[START] Cleaned DB for fresh session: {DB_PATH}")
        except Exception as e:
            print(f"[START] Could not clean DB: {e}")
            return {
                "status": "blocked",
                "error": f"DB file locked ({e}). Please terminate all or restart bot."
            }
    
    # [FIX] Auto-Restart Trading Engine if stopped
    if not trading_engine.running:
        print("[SERVER] Restarting Trading Engine...")
        asyncio.create_task(trading_engine.start())
        
        # Wait for engine to initialize (up to 5s)
        for _ in range(10):
            if trading_engine.running:
                break
            await asyncio.sleep(0.5)

    # Enable the symbol first
    bot.config_manager.enable_symbol(symbol, True)
    await bot.start_symbol(symbol)
    return {"status": "started", "symbol": symbol}

@app.post("/control/stop/{symbol}")
async def stop_symbol(symbol: str, bot = Depends(get_current_bot)):
    """Stop a specific symbol"""
    await bot.stop_symbol(symbol)
    return {"status": "stopped", "symbol": symbol}

@app.post("/control/terminate/{symbol}")
async def terminate_symbol(symbol: str, bot = Depends(get_current_bot)):
    """Nuclear reset - close all positions for a symbol immediately"""
    await bot.terminate_symbol(symbol)
    return {"status": "terminated", "symbol": symbol}

@app.post("/control/terminate-all")
async def terminate_all(bot = Depends(get_current_bot)):
    """Nuclear reset - close all positions for all symbols and clean DB"""
    await bot.terminate_all()
    
    # Clean DB after termination for complete reset
    db_cleaned = True
    db_warning = None
    
    if os.path.exists(DB_PATH):
        try:
            os.remove(DB_PATH)
            print(f"[TERMINATE] Cleaned DB after nuclear reset: {DB_PATH}")
        except Exception as e:
            print(f"[TERMINATE] Could not clean DB: {e}")
            db_cleaned = False
            db_warning = f"Could not delete DB file ({e}). Please retry or restart."
    
    return {
        "status": "terminated_all",
        "db_cleaned": db_cleaned,
        "warning": db_warning
    }

@app.get("/status")
async def get_status(bot = Depends(get_current_bot)):
    """Get status for all active strategies"""
    return bot.get_status()

# --- History Endpoints ---

@app.get("/history")
async def get_history(bot = Depends(get_current_bot)):
    """Get list of session history files for this user"""
    sessions = bot.session_logger.get_sessions()
    return sessions

# NOTE: Specific routes MUST come BEFORE parameterized routes in FastAPI
@app.get("/history/groups")
async def get_group_logs(bot = Depends(get_current_bot)):
    """Get list of group log files for this user"""
    from pathlib import Path
    import itertools
    log_dir = bot.session_logger.log_dir
    logs = []
    if log_dir.exists():
        # TASK 3 FIX: Include both .log and .txt file types
        log_files = itertools.chain(
            log_dir.glob("groups_log_*.txt"),  # Table snapshots
            log_dir.glob("groups_*.log"),      # Event logs (Group Strategy)
            log_dir.glob("activity_*.log")     # Activity logs (Pair Strategy)
        )
        for file in sorted(log_files, key=lambda f: f.stat().st_mtime, reverse=True):
            logs.append({
                "id": file.stem,
                "name": file.name,
                "path": str(file)
            })
        # Also include group table files
        for file in sorted(log_dir.glob("group_*_table.txt"), reverse=True):
            logs.append({
                "id": file.stem,
                "name": file.name,
                "path": str(file)
            })
    return logs

@app.get("/history/groups/{filename}")
async def get_group_log_content(filename: str, bot = Depends(get_current_bot)):
    """Get contents of a specific group log file"""
    from pathlib import Path
    from fastapi.responses import PlainTextResponse
    log_dir = bot.session_logger.log_dir
    log_path = log_dir / filename
    if log_path.exists() and log_path.is_file():
        return PlainTextResponse(log_path.read_text(encoding="utf-8"))
    raise HTTPException(404, "Group log not found")

@app.get("/history/{session_id}")
async def get_session_log(session_id: str, bot = Depends(get_current_bot)):
    """Get contents of a specific session log"""
    content = bot.session_logger.get_session_content(session_id)
    if content:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(content)
    raise HTTPException(404, "Session not found")

# --- Activity Log Endpoints ---

@app.get("/history/activity")
async def get_activity_logs(bot = Depends(get_current_bot)):
    """Get list of activity log files for this user"""
    from pathlib import Path
    user_id = getattr(bot, 'user_id', 'default')
    log_dir = Path(f"logs/activity/{user_id}")
    logs = []
    if log_dir.exists():
        for file in sorted(log_dir.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True):
            logs.append({
                "id": file.stem,
                "name": file.name,
                "path": str(file),
                "size": file.stat().st_size,
                "modified": file.stat().st_mtime
            })
    return logs

@app.get("/history/activity/{filename}")
async def get_activity_log_content(filename: str, bot = Depends(get_current_bot)):
    """Get contents of a specific activity log file"""
    from pathlib import Path
    from fastapi.responses import PlainTextResponse
    user_id = getattr(bot, 'user_id', 'default')
    log_dir = Path(f"logs/activity/{user_id}")
    log_path = log_dir / filename
    if log_path.exists() and log_path.is_file():
        return PlainTextResponse(log_path.read_text(encoding="utf-8"))
    raise HTTPException(404, "Activity log not found")

# Mount static folder for assets (css/js images)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Serve UI at Root (GET/HEAD)
@app.get("/")
@app.head("/")
async def read_index():
    return FileResponse('static/index.html')


# --- 4. Simplified Signal Handling ---
def cleanup_handler(signum, frame):
    """Handle SIGINT (Ctrl+C) - exit cleanly. DB cleanup handled on next startup."""
    print("\n[SERVER] Caught Signal. Exiting...")
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup_handler)
print("[SERVER] Signal Handler Registered")