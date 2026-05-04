# core/persistence/repository.py
import aiosqlite
import logging
import time
from typing import Dict, List, Any, Tuple
import os

# Ensure db directory exists
os.makedirs("db", exist_ok=True)
DB_PATH = "db/grid_v3.db"

class Repository:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.db = None

    async def initialize(self):
        """Connect and ensure schema exists."""
        self.db = await aiosqlite.connect(DB_PATH)
        self.db.row_factory = aiosqlite.Row
        
        # Read schema file
        schema_path = os.path.join("db", "schema.sql")
        # Adjust path if running from root or core
        if not os.path.exists(schema_path):
             # Try absolute path based on project root assumption or relative
             current_dir = os.path.dirname(os.path.abspath(__file__))
             # core/persistence/ -> db/schema.sql? No, db is at root usually.
             # Assuming running from root:
             schema_path = "db/schema.sql"
        
        # Fallback to absolute path relative to this file if simple path fails
        if not os.path.exists(schema_path):
             # c:\...\core\persistence\..\..\db\schema.sql
             root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
             schema_path = os.path.join(root_dir, "db", "schema.sql")

        with open(schema_path, "r") as f:
            await self.db.executescript(f.read())
        
        # MIGRATION: Add tp_blocked column to grid_pairs if it doesn't exist
        try:
            await self.db.execute("ALTER TABLE grid_pairs ADD COLUMN tp_blocked BOOLEAN DEFAULT 0")
            print(f"[REPOS] Migration: added 'tp_blocked' column to 'grid_pairs'")
        except Exception:
            pass

        # MIGRATION: Add group_id column to grid_pairs if it doesn't exist
        try:
            await self.db.execute("ALTER TABLE grid_pairs ADD COLUMN group_id INTEGER DEFAULT 0")
            print(f"[REPOS] Migration: added 'group_id' column to 'grid_pairs'")
        except Exception:
            pass
            
        # MIGRATION: Add metadata column to symbol_state if it doesn't exist
        try:
            await self.db.execute("ALTER TABLE symbol_state ADD COLUMN metadata TEXT DEFAULT '{}'")
            print(f"[REPOS] Migration: added 'metadata' column to 'symbol_state'")
        except Exception:
            pass

        # MIGRATION: Add metadata column to grid_pairs if it doesn't exist
        try:
            await self.db.execute("ALTER TABLE grid_pairs ADD COLUMN metadata TEXT DEFAULT '{}'")
            print(f"[REPOS] Migration: added 'metadata' column to 'grid_pairs'")
        except Exception:
            pass
            
        await self.db.commit()

    async def get_state(self) -> Dict[str, Any]:
        """Load symbol-level state (phase, center_price, cycle_id, anchor_price)."""
        async with self.db.execute(
            "SELECT * FROM symbol_state WHERE symbol = ?", (self.symbol,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return {}

    async def save_state(self, phase: str, center_price: float, iteration: int,
                         cycle_id: int = 0, anchor_price: float = 0.0, metadata: str = '{}'):
        """Upsert symbol state including cycle management fields."""
        await self.db.execute(
            """
            INSERT INTO symbol_state (symbol, phase, center_price, iteration, last_update_time, cycle_id, anchor_price, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                phase=excluded.phase,
                center_price=excluded.center_price,
                iteration=excluded.iteration,
                last_update_time=excluded.last_update_time,
                cycle_id=excluded.cycle_id,
                anchor_price=excluded.anchor_price,
                metadata=excluded.metadata
            """,
            (self.symbol, phase, center_price, iteration, time.time(), cycle_id, anchor_price, metadata)
        )
        await self.db.commit()

    async def get_pairs(self) -> List[Dict[str, Any]]:
        """Load all active pairs for this symbol."""
        async with self.db.execute(
            "SELECT * FROM grid_pairs WHERE symbol = ?", (self.symbol,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def upsert_pair(self, pair_data: Dict[str, Any], metadata: str = '{}'):
        """Insert or Update a single pair (Atomic operation)."""
        # Extract fields from pair_data dict
        await self.db.execute(
            """
            INSERT INTO grid_pairs (
                symbol, pair_index, buy_price, sell_price, 
                buy_ticket, sell_ticket, buy_filled, sell_filled,
                buy_pending_ticket, sell_pending_ticket,
                trade_count, next_action, is_reopened,
                buy_in_zone, sell_in_zone,
                hedge_ticket, hedge_direction, hedge_active,
                locked_buy_entry, locked_sell_entry, tp_blocked, group_id, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, pair_index) DO UPDATE SET
                buy_price=excluded.buy_price,
                sell_price=excluded.sell_price,
                buy_ticket=excluded.buy_ticket,
                sell_ticket=excluded.sell_ticket,
                buy_filled=excluded.buy_filled,
                sell_filled=excluded.sell_filled,
                buy_pending_ticket=excluded.buy_pending_ticket,
                sell_pending_ticket=excluded.sell_pending_ticket,
                trade_count=excluded.trade_count,
                next_action=excluded.next_action,
                is_reopened=excluded.is_reopened,
                buy_in_zone=excluded.buy_in_zone,
                sell_in_zone=excluded.sell_in_zone,
                hedge_ticket=excluded.hedge_ticket,
                hedge_direction=excluded.hedge_direction,
                hedge_active=excluded.hedge_active,
                locked_buy_entry=excluded.locked_buy_entry,
                locked_sell_entry=excluded.locked_sell_entry,
                tp_blocked=excluded.tp_blocked,
                group_id=excluded.group_id,
                metadata=excluded.metadata
            """,
            (
                self.symbol, pair_data['index'], pair_data['buy_price'], pair_data['sell_price'],
                pair_data.get('buy_ticket', 0), pair_data.get('sell_ticket', 0),
                pair_data.get('buy_filled', 0), pair_data.get('sell_filled', 0),
                pair_data.get('buy_pending_ticket', 0), pair_data.get('sell_pending_ticket', 0),
                pair_data.get('trade_count', 0), pair_data.get('next_action', 'buy'),
                pair_data.get('is_reopened', 0), pair_data.get('buy_in_zone', 0),
                pair_data.get('sell_in_zone', 0),
                pair_data.get('hedge_ticket', 0),
                pair_data.get('hedge_direction', None),
                pair_data.get('hedge_active', 0),
                pair_data.get('locked_buy_entry', 0.0),
                pair_data.get('locked_sell_entry', 0.0),
                int(pair_data.get('tp_blocked', False)),
                pair_data.get('group_id', 0),
                metadata
            )
        )
        await self.db.commit()

    async def delete_pair(self, pair_index: int):
        """Remove a pair (used in Leapfrog)."""
        await self.db.execute(
            "DELETE FROM grid_pairs WHERE symbol = ? AND pair_index = ?",
            (self.symbol, pair_index)
        )
        await self.db.commit()

    # ========================================================================
    # TICKET MAP (Groups + 3-Cap Strategy)
    # ========================================================================

    async def save_ticket(self, ticket: int, cycle_id: int, pair_index: int,
                          leg: str, trade_count: int = 0,
                          entry_price: float = 0.0, tp_price: float = 0.0, sl_price: float = 0.0):
        """Save ticket → (pair, leg, prices) mapping for deterministic TP/SL detection."""
        await self.db.execute(
            """
            INSERT INTO ticket_map (ticket, symbol, cycle_id, pair_index, leg, trade_count, entry_price, tp_price, sl_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticket) DO UPDATE SET
                cycle_id=excluded.cycle_id,
                pair_index=excluded.pair_index,
                leg=excluded.leg,
                trade_count=excluded.trade_count,
                entry_price=excluded.entry_price,
                tp_price=excluded.tp_price,
                sl_price=excluded.sl_price
            """,
            (ticket, self.symbol, cycle_id, pair_index, leg, trade_count, entry_price, tp_price, sl_price)
        )
        await self.db.commit()

    async def get_ticket_map(self) -> Dict[int, Tuple[int, str, float, float, float]]:
        """Load all ticket mappings for this symbol.

        Returns:
            Dict[ticket, (pair_index, leg, entry_price, tp_price, sl_price)]
        """
        async with self.db.execute(
            "SELECT ticket, pair_index, leg, entry_price, tp_price, sl_price FROM ticket_map WHERE symbol = ?",
            (self.symbol,)
        ) as cursor:
            rows = await cursor.fetchall()
            return {row['ticket']: (row['pair_index'], row['leg'], row['entry_price'], row['tp_price'], row['sl_price']) for row in rows}

    async def delete_ticket(self, ticket: int):
        """Remove a ticket from the map (on position close)."""
        await self.db.execute(
            "DELETE FROM ticket_map WHERE ticket = ?",
            (ticket,)
        )
        await self.db.commit()

    async def clear_ticket_map(self):
        """Clear all tickets for this symbol (on fresh start)."""
        await self.db.execute(
            "DELETE FROM ticket_map WHERE symbol = ?",
            (self.symbol,)
        )
        await self.db.commit()

    # ========================================================================
    # TRADE HISTORY
    # ========================================================================

    async def log_trade(self, event: Dict[str, Any]):
        """Log a trade event to history table (Permanent storage)."""
        await self.db.execute(
            """
            INSERT INTO trade_history (symbol, timestamp, event_type, pair_index, direction, price, lot_size, ticket, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.symbol, event['timestamp'], event['event_type'], 
                event['pair_index'], event['direction'], event['price'], 
                event['lot_size'], event['ticket'], event.get('notes', '')
            )
        )
        await self.db.commit()

    async def close(self):
        if self.db:
            await self.db.close()
