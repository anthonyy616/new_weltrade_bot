from dataclasses import dataclass, field, asdict
from typing import Dict, Optional, Tuple, List, Any
import asyncio
import json
import time
import logging
import MetaTrader5 as mt5_module
from datetime import datetime

from core.engine.activity_logger import ActivityLogger
from core.persistence.repository import Repository

logger = logging.getLogger("pair_strategy")
mt5: Any = mt5_module



@dataclass
class GridLevel:
    """Represents a single grid level with its positions"""
    price: float
    active: bool = False
    
    # Position tracking (ticket -> {leg, direction, entry, tp, sl, lot})
    positions: Dict[int, dict] = field(default_factory=dict)
    
    def get_buy_tickets(self) -> List[int]:
        """Get all BUY tickets at this level (for FIFO closing)"""
        return [t for t, info in self.positions.items() if info['direction'] == 'buy']
    
    def get_sell_tickets(self) -> List[int]:
        """Get all SELL tickets at this level (for FIFO closing)"""
        return [t for t, info in self.positions.items() if info['direction'] == 'sell']


@dataclass
class StrategyState:
    """Complete state for Grid Bounce Strategy"""
    phase: str = "IDLE"  # IDLE, SINGLE_LEVEL, TWO_LEVELS, RESETTING
    
    # Grid configuration
    center_price: float = 0.0  # Initial startup price
    grid_level_1: Optional[GridLevel] = None  # First level (always center at startup)
    grid_level_2: Optional[GridLevel] = None  # Second level (activated on first move)
    
    # Position management
    position_counter: int = 0  # Counts toward max_positions (excludes initial 2)
    total_positions: int = 0   # Total open positions (for tracking)
    
    # Movement tracking
    last_move_direction: str = ""  # "UP" or "DOWN"
    
    # Cycle tracking
    cycle_count: int = 0
    realized_pnl: float = 0.0
    
    # Ticket tracking (global across all levels)
    ticket_map: Dict[int, dict] = field(default_factory=dict)
    ticket_touch_flags: Dict[int, dict] = field(default_factory=dict)


# core logic for managing the 2-grid bounce strategy
class GridBounceStrategyEngine:
    """
    2-Grid Level Bouncing Strategy Engine
    
    Lifecycle:
    1. Start at center → open initial BUY + SELL pair
    2. Wait for grid_distance move (up or down)
    3. On move: close opposite position at origin, open 3 new at destination
    4. Bounce between 2 levels until TP/SL nuclear reset
    5. Reset → restart from current price as new center
    """
    
    MAGIC_NUMBER = 123456
    
    def __init__(self, config_manager, symbol: str, user_id: str = "default", 
                 session_logger=None):
        self.config_manager = config_manager
        self.symbol = symbol
        self.user_id = user_id
        self.session_logger = session_logger
        
        self.state = StrategyState()
        self.running = False
        self.graceful_stop = False
        
        self.execution_lock = asyncio.Lock()
        self.activity_log = ActivityLogger(symbol, user_id, session_logger)
        self.repository: Optional[Repository] = None
        self._position_drop_detected = False
    
    # Config accessors
    @property
    def config(self) -> Dict[str, Any]:
        return self.config_manager.get_symbol_config(self.symbol) or {}

    @property
    def grid_distance(self) -> float:
        return float(self.config.get('grid_distance', 50.0))
    
    @property
    def max_positions(self) -> int:
        return int(self.config.get('max_positions', 3))
    
    @property
    def pair_buy_lot(self) -> float:
        return float(self.config.get('pair_buy_lot', 0.01))
    
    @property
    def pair_sell_lot(self) -> float:
        return float(self.config.get('pair_sell_lot', 0.01))
    
    @property
    def single_lot(self) -> float:
        return float(self.config.get('single_lot', 0.01))
    
    @property
    def tp_pips(self) -> float:
        return float(self.config.get('tp_pips', 150.0))
    
    @property
    def sl_pips(self) -> float:
        return float(self.config.get('sl_pips', 200.0))

    @property
    def current_price(self) -> float:
        tick = mt5.symbol_info_tick(self.symbol)
        if tick:
            return (tick.ask + tick.bid) / 2
        return self.state.center_price

    async def start_ticker(self):
        """Compatibility hook for orchestrator config refreshes."""
        return None
    

#startup logic and main loop

    async def start(self):
        """
        Start strategy - open initial BUY + SELL at center price
        """
        if self.running:
            return
        
        self.running = True
        self.graceful_stop = False
        
        # Get current tick
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            self.activity_log.log_error("Failed to get tick for start")
            return
        
        center = (tick.ask + tick.bid) / 2
        self.state.center_price = center
        
        # Initialize center as grid_level_1
        self.state.grid_level_1 = GridLevel(price=center, active=True)
        
        self.activity_log.log_start(self.state.cycle_count, center)
        
        # Open initial pair at center
        buy_ticket, buy_entry = await self._execute_market_order(
            "buy", self.pair_buy_lot, "CenterBuy", center
        )
        sell_ticket, sell_entry = await self._execute_market_order(
            "sell", self.pair_sell_lot, "CenterSell", center
        )
        
        # Store in grid_level_1
        if buy_ticket:
            self.state.grid_level_1.positions[buy_ticket] = {
                'leg': 'CenterBuy',
                'direction': 'buy',
                'entry': buy_entry,
                'tp': buy_entry + self.tp_pips,
                'sl': buy_entry - self.sl_pips,
                'lot': self.pair_buy_lot
            }
            self.state.ticket_map[buy_ticket] = self.state.grid_level_1.positions[buy_ticket]
            self._init_touch_flags(buy_ticket)
            self.activity_log.log_fire(
                self.state.cycle_count, "CenterBuy", buy_entry, 
                self.pair_buy_lot, buy_entry + self.tp_pips, 
                buy_entry - self.sl_pips, buy_ticket
            )
        
        if sell_ticket:
            self.state.grid_level_1.positions[sell_ticket] = {
                'leg': 'CenterSell',
                'direction': 'sell',
                'entry': sell_entry,
                'tp': sell_entry - self.tp_pips,
                'sl': sell_entry + self.sl_pips,
                'lot': self.pair_sell_lot
            }
            self.state.ticket_map[sell_ticket] = self.state.grid_level_1.positions[sell_ticket]
            self._init_touch_flags(sell_ticket)
            self.activity_log.log_fire(
                self.state.cycle_count, "CenterSell", sell_entry,
                self.pair_sell_lot, sell_entry - self.tp_pips,
                sell_entry + self.sl_pips, sell_ticket
            )
        
        self.state.phase = "SINGLE_LEVEL"
        self.state.total_positions = 2
        # position_counter stays at 0 (these 2 don't count toward max)
        
        await self.save_state()

    #tick handler - same as old one

    async def on_external_tick(self, tick_data: dict):
        """
        Called by orchestrator on every tick
        """
        if not self.running or self.state.phase == "IDLE":
            return
        
        ask = tick_data.get('ask', 0)
        bid = tick_data.get('bid', 0)
        
        if ask <= 0 or bid <= 0:
            return
        
        async with self.execution_lock:
            # 1. Update touch flags FIRST (PRESERVED)
            self._update_touch_flags(ask, bid)
            
            # 2. Check position drops (TP/SL detection) (PRESERVED)
            await self._check_position_drops(ask, bid)
            
            # 3. Check if any position closed -> nuclear reset
            if await self._check_nuclear_reset_trigger():
                return  # Reset triggered, exit
            
            # 4. Check for grid distance triggers
            await self._check_grid_triggers(ask, bid)

    #grid distance trigger logic

    async def _check_grid_triggers(self, ask: float, bid: float):
        """
        Check if price has moved grid_distance from current level(s)
        and execute appropriate actions
        """
        if self.state.phase == "IDLE" or self.state.phase == "RESETTING":
            return
        
        mid = (ask + bid) / 2
        grid_dist = self.grid_distance
        
        # --- SINGLE LEVEL PHASE ---
        if self.state.phase == "SINGLE_LEVEL":
            if not self.state.grid_level_1:
                return
            center = self.state.grid_level_1.price
            
            # Check DOWN movement (center - grid_distance)
            if mid <= center - grid_dist:
                await self._activate_second_level_down(ask, bid)
                return
            
            # Check UP movement (center + grid_distance)
            if mid >= center + grid_dist:
                await self._activate_second_level_up(ask, bid)
                return
        
        # --- TWO LEVELS PHASE ---
        elif self.state.phase == "TWO_LEVELS":
            if not self.state.grid_level_1 or not self.state.grid_level_2:
                return

            level_1_price = self.state.grid_level_1.price
            level_2_price = self.state.grid_level_2.price
            
            # Determine which level is upper and which is lower
            upper_price = max(level_1_price, level_2_price)
            lower_price = min(level_1_price, level_2_price)
            
            upper_level = self.state.grid_level_1 if level_1_price == upper_price else self.state.grid_level_2
            lower_level = self.state.grid_level_1 if level_1_price == lower_price else self.state.grid_level_2
            
            # Check if moving DOWN (from upper to lower)
            if mid <= lower_price and self.state.last_move_direction != "DOWN_TO_LOWER":
                await self._bounce_down(upper_level, lower_level, ask, bid)
                return
            
            # Check if moving UP (from lower to upper)
            if mid >= upper_price and self.state.last_move_direction != "UP_TO_UPPER":
                await self._bounce_up(lower_level, upper_level, ask, bid)
                return


    async def _activate_second_level_down(self, ask: float, bid: float):
        """
        First grid distance hit - moving DOWN from center
        
        Actions:
        1. Close SELL at center (grid_level_1) - FIFO
        2. Activate grid_level_2 at (center - grid_distance)
        3. Open 3 positions at grid_level_2: Pair BS + Single SELL
        """
        center_level = self.state.grid_level_1
        if not center_level:
            return
        new_price = center_level.price - self.grid_distance
        
        self.activity_log.log_info(f"Moving DOWN: Grid distance reached at {new_price:.2f}")
        
        # Step 1: Close SELL at center (FIFO)
        sell_tickets = center_level.get_sell_tickets()
        if sell_tickets:
            oldest_sell = sell_tickets[0]  # FIFO
            if self._close_position(oldest_sell):
                self.activity_log.log_info(f"Closed SELL at center (ticket {oldest_sell})")
                self._remove_ticket_from_tracking(oldest_sell, center_level)
        
        # Step 2: Activate grid_level_2
        self.state.grid_level_2 = GridLevel(price=new_price, active=True)
        self.state.phase = "TWO_LEVELS"
        self.state.last_move_direction = "DOWN_TO_LOWER"
        
        self.activity_log.log_grid_activation("Lower Level", new_price)
        
        # Step 3: Check max_positions before opening
        if self.state.position_counter >= self.max_positions:
            self.activity_log.log_info(f"Max positions ({self.max_positions}) reached - skipping new opens")
            await self.save_state()
            return
        
        # Open 3 positions at new level
        await self._open_triple_positions(
            self.state.grid_level_2, 
            ask, bid, 
            direction="DOWN"  # Opened because we moved down
        )
        
        self.state.position_counter += 3
        await self.save_state()


    async def _activate_second_level_up(self, ask: float, bid: float):
        """
        First grid distance hit - moving UP from center
        
        Actions:
        1. Close BUY at center (grid_level_1) - FIFO
        2. Activate grid_level_2 at (center + grid_distance)
        3. Open 3 positions at grid_level_2: Pair BS + Single BUY
        """
        center_level = self.state.grid_level_1
        if not center_level:
            return
        new_price = center_level.price + self.grid_distance
        
        self.activity_log.log_info(f"Moving UP: Grid distance reached at {new_price:.2f}")
        
        # Step 1: Close BUY at center (FIFO)
        buy_tickets = center_level.get_buy_tickets()
        if buy_tickets:
            oldest_buy = buy_tickets[0]  # FIFO
            if self._close_position(oldest_buy):
                self.activity_log.log_info(f"Closed BUY at center (ticket {oldest_buy})")
                self._remove_ticket_from_tracking(oldest_buy, center_level)
        
        # Step 2: Activate grid_level_2
        self.state.grid_level_2 = GridLevel(price=new_price, active=True)
        self.state.phase = "TWO_LEVELS"
        self.state.last_move_direction = "UP_TO_UPPER"
        
        self.activity_log.log_grid_activation("Upper Level", new_price)
        
        # Step 3: Check max_positions
        if self.state.position_counter >= self.max_positions:
            self.activity_log.log_info(f"Max positions ({self.max_positions}) reached - skipping new opens")
            await self.save_state()
            return
        
        # Open 3 positions at new level
        await self._open_triple_positions(
            self.state.grid_level_2,
            ask, bid,
            direction="UP"  # Opened because we moved up
        )
        
        self.state.position_counter += 3
        await self.save_state()


    async def _bounce_down(self, upper_level: GridLevel, lower_level: GridLevel, 
                        ask: float, bid: float):
        """
        Bounce DOWN from upper level to lower level
        
        Actions:
        1. Close SELL at upper level (FIFO)
        2. Open 3 positions at lower level: Pair BS + Single SELL
        """
        self.activity_log.log_info(f"Bouncing DOWN to {lower_level.price:.2f}")
        
        # Step 1: Close SELL at upper (FIFO)
        sell_tickets = upper_level.get_sell_tickets()
        if sell_tickets:
            oldest_sell = sell_tickets[0]
            if self._close_position(oldest_sell):
                self.activity_log.log_info(f"Closed SELL at upper (ticket {oldest_sell})")
                self._remove_ticket_from_tracking(oldest_sell, upper_level)
        
        # Step 2: Check max_positions
        if self.state.position_counter >= self.max_positions:
            self.activity_log.log_info(f"Max positions ({self.max_positions}) reached - skipping new opens")
            self.state.last_move_direction = "DOWN_TO_LOWER"
            await self.save_state()
            return
        
        # Step 3: Open 3 positions at lower
        await self._open_triple_positions(lower_level, ask, bid, direction="DOWN")
        
        self.state.position_counter += 3
        self.state.last_move_direction = "DOWN_TO_LOWER"
        await self.save_state()


    async def _bounce_up(self, lower_level: GridLevel, upper_level: GridLevel,
                        ask: float, bid: float):
        """
        Bounce UP from lower level to upper level
        
        Actions:
        1. Close BUY at lower level (FIFO)
        2. Open 3 positions at upper level: Pair BS + Single BUY
        """
        self.activity_log.log_info(f"Bouncing UP to {upper_level.price:.2f}")
        
        # Step 1: Close BUY at lower (FIFO)
        buy_tickets = lower_level.get_buy_tickets()
        if buy_tickets:
            oldest_buy = buy_tickets[0]
            if self._close_position(oldest_buy):
                self.activity_log.log_info(f"Closed BUY at lower (ticket {oldest_buy})")
                self._remove_ticket_from_tracking(oldest_buy, lower_level)
        
        # Step 2: Check max_positions
        if self.state.position_counter >= self.max_positions:
            self.activity_log.log_info(f"Max positions ({self.max_positions}) reached - skipping new opens")
            self.state.last_move_direction = "UP_TO_UPPER"
            await self.save_state()
            return
        
        # Step 3: Open 3 positions at upper
        await self._open_triple_positions(upper_level, ask, bid, direction="UP")
        
        self.state.position_counter += 3
        self.state.last_move_direction = "UP_TO_UPPER"
        await self.save_state()


    #position opening helper (triple opens for grid activation and bounces)

    async def _open_triple_positions(self, grid_level: GridLevel, ask: float, bid: float,
                                    direction: str):
        """
        Open 3 positions at a grid level:
        - 1 Pair Buy
        - 1 Pair Sell
        - 1 Single (Buy if direction="UP", Sell if direction="DOWN")
        
        Args:
            grid_level: GridLevel object to store positions in
            ask, bid: Current prices
            direction: "UP" or "DOWN" (determines single trade direction)
        """
        target_price = grid_level.price
        open_count = 0
        
        # Open Pair Buy
        buy_ticket, buy_entry = await self._execute_market_order(
            "buy", self.pair_buy_lot, "PairBuy", target_price
        )
        if buy_ticket:
            open_count += 1
            grid_level.positions[buy_ticket] = {
                'leg': 'PairBuy',
                'direction': 'buy',
                'entry': buy_entry,
                'tp': buy_entry + self.tp_pips,
                'sl': buy_entry - self.sl_pips,
                'lot': self.pair_buy_lot
            }
            self.state.ticket_map[buy_ticket] = grid_level.positions[buy_ticket]
            self._init_touch_flags(buy_ticket)
            self.activity_log.log_fire(
                self.state.cycle_count, "PairBuy", buy_entry,
                self.pair_buy_lot, buy_entry + self.tp_pips,
                buy_entry - self.sl_pips, buy_ticket
            )
        
        # Open Pair Sell
        sell_ticket, sell_entry = await self._execute_market_order(
            "sell", self.pair_sell_lot, "PairSell", target_price
        )
        if sell_ticket:
            open_count += 1
            grid_level.positions[sell_ticket] = {
                'leg': 'PairSell',
                'direction': 'sell',
                'entry': sell_entry,
                'tp': sell_entry - self.tp_pips,
                'sl': sell_entry + self.sl_pips,
                'lot': self.pair_sell_lot
            }
            self.state.ticket_map[sell_ticket] = grid_level.positions[sell_ticket]
            self._init_touch_flags(sell_ticket)
            self.activity_log.log_fire(
                self.state.cycle_count, "PairSell", sell_entry,
                self.pair_sell_lot, sell_entry - self.tp_pips,
                sell_entry + self.sl_pips, sell_ticket
            )
        
        # Open Single (direction-dependent)
        if direction == "UP":
            # Moving UP -> Single BUY
            single_ticket, single_entry = await self._execute_market_order(
                "buy", self.single_lot, "SingleBuy", target_price
            )
            if single_ticket:
                open_count += 1
                grid_level.positions[single_ticket] = {
                    'leg': 'SingleBuy',
                    'direction': 'buy',
                    'entry': single_entry,
                    'tp': single_entry + self.tp_pips,
                    'sl': single_entry - self.sl_pips,
                    'lot': self.single_lot
                }
                self.state.ticket_map[single_ticket] = grid_level.positions[single_ticket]
                self._init_touch_flags(single_ticket)
                self.activity_log.log_fire(
                    self.state.cycle_count, "SingleBuy", single_entry,
                    self.single_lot, single_entry + self.tp_pips,
                    single_entry - self.sl_pips, single_ticket
                )
        
        elif direction == "DOWN":
            # Moving DOWN -> Single SELL
            single_ticket, single_entry = await self._execute_market_order(
                "sell", self.single_lot, "SingleSell", target_price
            )
            if single_ticket:
                open_count += 1
                grid_level.positions[single_ticket] = {
                    'leg': 'SingleSell',
                    'direction': 'sell',
                    'entry': single_entry,
                    'tp': single_entry - self.tp_pips,
                    'sl': single_entry + self.sl_pips,
                    'lot': self.single_lot
                }
                self.state.ticket_map[single_ticket] = grid_level.positions[single_ticket]
                self._init_touch_flags(single_ticket)
                self.activity_log.log_fire(
                    self.state.cycle_count, "SingleSell", single_entry,
                    self.single_lot, single_entry - self.tp_pips,
                    single_entry + self.sl_pips, single_ticket
                )
        
        self.state.total_positions += open_count

    #TP/SL detection helpers (Same as old logic)

    def _update_touch_flags(self, ask: float, bid: float):
        """
        PRESERVED FROM ORIGINAL - Latch touch flags when price crosses TP/SL
        """
        for ticket, info in list(self.state.ticket_map.items()):
            if not info:
                continue
            
            direction = info.get("direction", "")
            tp_price = info.get("tp", 0)
            sl_price = info.get("sl", 0)
            
            flags = self.state.ticket_touch_flags.get(ticket)
            if flags is None:
                flags = {"tp_touched": False, "sl_touched": False}
                self.state.ticket_touch_flags[ticket] = flags
            
            if direction == "buy":
                if not flags['tp_touched'] and bid >= tp_price:
                    flags['tp_touched'] = True
                if not flags['sl_touched'] and bid <= sl_price:
                    flags['sl_touched'] = True
            else:  # sell
                if not flags['tp_touched'] and ask <= tp_price:
                    flags['tp_touched'] = True
                if not flags['sl_touched'] and ask >= sl_price:
                    flags['sl_touched'] = True


    async def _check_position_drops(self, ask: float, bid: float):
        """
        PRESERVED FROM ORIGINAL - Detect positions closed by MT5 (TP/SL hit)
        """
        positions = mt5.positions_get(symbol=self.symbol)
        current_tickets = set()
        if positions:
            for pos in positions:
                current_tickets.add(pos.ticket)
        
        tracked_tickets = set(self.state.ticket_map.keys())
        dropped = tracked_tickets - current_tickets
        if dropped:
            self._position_drop_detected = True
        
        for ticket in dropped:
            info = self.state.ticket_map.get(ticket)
            if not info:
                continue
            
            leg = info.get("leg", "")
            direction = info.get("direction", "")
            entry = info.get("entry", 0)
            tp_price = info.get("tp", 0)
            sl_price = info.get("sl", 0)
            lot = info.get("lot", 0)
            
            # Determine TP or SL using touch flags
            flags = self.state.ticket_touch_flags.get(ticket, {})
            is_tp = flags.get("tp_touched", False)
            is_sl = flags.get("sl_touched", False)
            
            # Fallback inference
            if not is_tp and not is_sl:
                check_price = bid if direction == "buy" else ask
                tp_dist = abs(check_price - tp_price)
                sl_dist = abs(check_price - sl_price)
                is_tp = tp_dist < sl_dist
                is_sl = not is_tp
            
            # Calculate PnL
            close_price = tp_price if is_tp else sl_price
            if direction == "buy":
                realized = (close_price - entry) * lot
            else:
                realized = (entry - close_price) * lot
            
            self.state.realized_pnl += realized
            
            # Log
            if is_tp:
                self.activity_log.log_tp_hit(ticket, leg, close_price, realized, "")
            else:
                self.activity_log.log_sl_hit(ticket, leg, close_price, realized)
            
            # Remove from tracking
            self._remove_ticket_from_all_levels(ticket)
            
            # Decrement total
            self.state.total_positions -= 1
        
        if dropped:
            await self.save_state()

    # Nuclear reset check (SAME but modified for 2-level logic)

    async def _check_nuclear_reset_trigger(self) -> bool:
        """
        Check if ANY position was closed (TP or SL hit)
        If yes -> trigger nuclear reset
        
        Returns True if reset was triggered
        """
        # If any position dropped, _check_position_drops already handled logging
        # Now we just check if total_positions decreased
        
        if self._position_drop_detected:
            self.activity_log.log_info("Position closed via TP/SL - triggering nuclear reset")
            self._position_drop_detected = False
            await self._nuclear_reset_and_restart("TP_SL_HIT", self.state.realized_pnl)
            return True
        
        return False


    async def _nuclear_reset_and_restart(self, reason: str, total_pnl: float):
        """
        PRESERVED BUT MODIFIED FROM ORIGINAL
        
        Nuclear reset - close ALL positions, reset state, then:
        - If graceful_stop is True: stop completely
        - Otherwise: auto-restart new cycle at current price
        """
        old_cycle = self.state.cycle_count
        
        print(f"[RESET] {self.symbol}: Cycle {old_cycle} ended. Reason: {reason}, PnL: ${total_pnl:.2f}")
        
        self.state.phase = "RESETTING"
        self.activity_log.log_phase_transition("*", "RESETTING")
        
        # Close ALL positions
        positions = mt5.positions_get(symbol=self.symbol)
        closed_count = 0
        if positions:
            for pos in positions:
                if self._close_position(pos.ticket):
                    closed_count += 1
            print(f"[RESET] {self.symbol}: Closed {closed_count}/{len(positions)} positions")
        
        # Log reset
        self.activity_log.log_reset(old_cycle, old_cycle + 1, reason, total_pnl)
        
        # Reset state but increment cycle
        self._reset_state()
        self.state.cycle_count = old_cycle + 1
        
        # Check graceful stop
        if self.graceful_stop:
            self.running = False
            self.graceful_stop = False
            self.state.phase = "IDLE"
            self.activity_log.log_stop(self.state.cycle_count, "graceful_stop_complete")
            await self.save_state()
            print(f"[STOP] {self.symbol}: Graceful stop complete.")
            return
        
        # Auto-restart at CURRENT price (where TP/SL was hit)
        self.running = False  # Reset flag so start() doesn't exit early
        print(f"[RESTART] {self.symbol}: Starting new cycle {self.state.cycle_count}")
        await self.start()


    def _reset_state(self):
        """Reset state to defaults (except cycle_count)"""
        cycle = self.state.cycle_count
        self.state = StrategyState()
        self.state.cycle_count = cycle


    #Helper methods for order execution, position closing, and tracking management (SAME as old logic but adapted for new state structure)

    def _remove_ticket_from_tracking(self, ticket: int, grid_level: GridLevel):
        """Remove ticket from a specific grid level"""
        if ticket in grid_level.positions:
            del grid_level.positions[ticket]
        if ticket in self.state.ticket_map:
            del self.state.ticket_map[ticket]
        if ticket in self.state.ticket_touch_flags:
            del self.state.ticket_touch_flags[ticket]


    def _remove_ticket_from_all_levels(self, ticket: int):
        """Remove ticket from all grid levels and global tracking"""
        info = self.state.ticket_map.get(ticket)
        if self.state.grid_level_1 and ticket in self.state.grid_level_1.positions:
            del self.state.grid_level_1.positions[ticket]
        if self.state.grid_level_2 and ticket in self.state.grid_level_2.positions:
            del self.state.grid_level_2.positions[ticket]
        if ticket in self.state.ticket_map:
            del self.state.ticket_map[ticket]
        if ticket in self.state.ticket_touch_flags:
            del self.state.ticket_touch_flags[ticket]
        if info and info.get("leg") not in {"CenterBuy", "CenterSell"} and self.state.position_counter > 0:
            self.state.position_counter -= 1


    def _init_touch_flags(self, ticket: int):
        """Initialize touch flags for a new ticket"""
        self.state.ticket_touch_flags[ticket] = {
            "tp_touched": False,
            "sl_touched": False
        }


    def _close_position(self, ticket: int) -> bool:
        """
        PRESERVED FROM ORIGINAL - Close a single MT5 position
        """
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return False
        
        pos = positions[0]
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            return False
        
        if pos.type == mt5.ORDER_TYPE_BUY:
            close_type = mt5.ORDER_TYPE_SELL
            close_price = tick.bid
        else:
            close_type = mt5.ORDER_TYPE_BUY
            close_price = tick.ask
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": pos.volume,
            "type": close_type,
            "position": ticket,
            "price": close_price,
            "deviation": 50,
            "magic": self.MAGIC_NUMBER,
            "comment": "close",
            "type_filling": mt5.ORDER_FILLING_FOK
        }
        
        result = mt5.order_send(request)
        return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE


    async def _execute_market_order(self, direction: str, lot_size: float,
                                    leg_name: str, target_price: float) -> Tuple[int, float]:
        """
        PRESERVED FROM ORIGINAL (with minor modifications)
        Send market order to MT5, returns (ticket, entry_price)
        """
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            self.activity_log.log_error(f"No tick for {leg_name}")
            return 0, 0.0
        
        # Determine execution parameters
        if direction == "buy":
            exec_price = tick.ask
            tp = exec_price + self.tp_pips
            sl = exec_price - self.sl_pips
            order_type = mt5.ORDER_TYPE_BUY
            check_price = tick.bid
        else:
            exec_price = tick.bid
            tp = exec_price - self.tp_pips
            sl = exec_price + self.sl_pips
            order_type = mt5.ORDER_TYPE_SELL
            check_price = tick.ask
        
        # Stops level safety
        symbol_info = mt5.symbol_info(self.symbol)
        if symbol_info:
            point = symbol_info.point
            stops_level = max(symbol_info.trade_stops_level, 10)
            min_dist = stops_level * point
            
            if direction == "buy":
                if sl > check_price - min_dist:
                    sl = check_price - min_dist
                if tp < check_price + min_dist:
                    tp = check_price + min_dist
            else:
                if sl < check_price + min_dist:
                    sl = check_price + min_dist
                if tp > check_price - min_dist:
                    tp = check_price - min_dist
        
        # Snapshot existing tickets
        positions_before = mt5.positions_get(symbol=self.symbol)
        existing_tickets = set(pos.ticket for pos in positions_before) if positions_before else set()
        
        # Send order
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": float(lot_size),
            "type": order_type,
            "price": exec_price,
            "sl": float(sl),
            "tp": float(tp),
            "magic": self.MAGIC_NUMBER,
            "comment": f"{leg_name} C{self.state.cycle_count}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
            "deviation": 200
        }
        
        result = mt5.order_send(request)
        
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            error = mt5.last_error() if result is None else result.comment
            self.activity_log.log_error(f"{leg_name} order failed: {error}")
            return 0, 0.0
        
        ticket = result.order
        
        # Wait for position to appear
        await asyncio.sleep(0.1)
        
        # Find new position
        positions_after = mt5.positions_get(symbol=self.symbol)
        actual_entry = exec_price
        actual_ticket = ticket
        
        if positions_after:
            for pos in positions_after:
                if pos.ticket not in existing_tickets:
                    actual_ticket = pos.ticket
                    actual_entry = pos.price_open
                    break
            else:
                for pos in positions_after:
                    if pos.ticket == ticket:
                        actual_ticket = pos.ticket
                        actual_entry = pos.price_open
                        break
        
        return actual_ticket, actual_entry

    async def save_state(self):
        """Persist the current strategy state."""
        if self.repository is None:
            self.repository = Repository(self.symbol)
            await self.repository.initialize()

        metadata = json.dumps(
            {
                "phase": self.state.phase,
                "center_price": self.state.center_price,
                "grid_level_1": self.state.grid_level_1.price if self.state.grid_level_1 else 0.0,
                "grid_level_2": self.state.grid_level_2.price if self.state.grid_level_2 else 0.0,
                "position_counter": self.state.position_counter,
                "total_positions": self.state.total_positions,
                "last_move_direction": self.state.last_move_direction,
                "realized_pnl": self.state.realized_pnl,
            }
        )

        await self.repository.save_state(
            phase=self.state.phase,
            center_price=self.state.center_price,
            iteration=self.state.cycle_count,
            cycle_id=self.state.cycle_count,
            anchor_price=self.state.grid_level_1.price if self.state.grid_level_1 else 0.0,
            metadata=metadata,
        )


    #Graceful stop and position terminate (same as old logic)

    async def stop(self):
        """
        PRESERVED FROM ORIGINAL
        Graceful stop - complete current cycle before stopping
        """
        if not self.running:
            return
        
        print(f"[STOP] {self.symbol}: Graceful stop initiated.")
        self.graceful_stop = True
        self.activity_log.log_graceful_stop(self.state.cycle_count, "manual/timeout")
        
        # If idle or no positions, stop immediately
        if self.state.phase == "IDLE" or self.state.total_positions == 0:
            self.running = False
            self.activity_log.log_stop(self.state.cycle_count, "graceful_stop_immediate")
            await self.save_state()
            print(f"[STOP] {self.symbol}: Stopped immediately (no positions).")


    async def terminate(self):
        """
        PRESERVED FROM ORIGINAL
        Nuclear reset - close ALL positions immediately, don't restart
        """
        print(f"[TERMINATE] {self.symbol}: Closing ALL positions...")
        self.activity_log.log_info("TERMINATE: Closing all positions...")
        
        # Close all positions
        positions = mt5.positions_get(symbol=self.symbol)
        closed_count = 0
        if positions:
            for pos in positions:
                if self._close_position(pos.ticket):
                    closed_count += 1
        
        print(f"[TERMINATE] {self.symbol}: Closed {closed_count} positions.")
        self.activity_log.log_info(f"TERMINATE: Closed {closed_count} positions")
        
        # Full reset
        self._reset_state()
        self.running = False
        self.graceful_stop = False
        self.state.phase = "IDLE"
        self.state.cycle_count = 0
        
        await self.save_state()
        print(f"[TERMINATE] {self.symbol}: Terminated completely.")


    #Status API

    def get_status(self) -> dict:
        """
        PRESERVED FROM ORIGINAL (with field updates)
        Return status dict for API polling
        """
        return {
            "running": self.running,
            "phase": self.state.phase,
            "cycle_count": self.state.cycle_count,
            "center_price": self.state.center_price,
            "grid_level_1_price": self.state.grid_level_1.price if self.state.grid_level_1 else 0,
            "grid_level_2_price": self.state.grid_level_2.price if self.state.grid_level_2 else 0,
            "open_positions": self.state.total_positions,
            "position_counter": self.state.position_counter,
            "max_positions": self.max_positions,
            "realized_pnl": self.state.realized_pnl,
            "graceful_stop": self.graceful_stop,
            "is_resetting": self.state.phase == "RESETTING",
            "step": self.state.cycle_count,
            "iteration": self.state.cycle_count,
        }