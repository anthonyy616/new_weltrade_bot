import json
import os
from typing import Dict, Any, List, Optional

# All available trading symbols
AVAILABLE_SYMBOLS = [
    # FX Indices
    "FX Vol 20", "FX Vol 40", "FX Vol 60", "FX Vol 80", "FX Vol 99",
    # SFX Indices
    "SFX Vol 20", "SFX Vol 40", "SFX Vol 60", "SFX Vol 80", "SFX Vol 99",
    # FlipX Indices
    "FlipX 1", "FlipX 2", "FlipX 3", "FlipX 4", "FlipX 5",
    # PainX Indices
    "PainX 400", "PainX 600", "PainX 800", "PainX 999", "PainX 1200",
    # GainX Indices
    "GainX 400", "GainX 600", "GainX 800", "GainX 999", "GainX 1200",
    # Other Indices
    "SwitchX 600", "SwitchX 1200", "SwitchX 1800", "BreakX 1200", "BreakX 1800"
]

MAX_POSITION_LIMIT = 18

def get_default_symbol_config() -> Dict[str, Any]:
      return {
          "enabled": False,
          "grid_distance": 50.0,       # Pips between grid levels
          "tp_pips": 150.0,            # TP distance for all positions
          "sl_pips": 200.0,            # SL distance for all positions
          # Pair arrays include center pair + one entry per 3-position group.
          # single_lots include one entry per 3-position group.
          "pair_buy_lots": [0.01, 0.01],
          "pair_sell_lots": [0.01, 0.01],
          "single_lots": [0.01],
          "max_positions": 3,          # Must be multiple of 3 (3, 6, 9, etc.)
      }

class ConfigManager:
    """
    Multi-Asset Configuration Manager
    
    Structure:
    {
        "global": {
            "max_runtime_minutes": 0
        },
        "symbols": {
            "FX Vol 20": { ...symbol config... },
            "FX Vol 40": { ...symbol config... },
            ...
        }
    }
    """
    
    def __init__(self, user_id: str = "default", config_file: str = "config.json"):
        self.user_id = user_id
        
        # If a specific user is logged in, use their unique config file
        if user_id and user_id != "default":
            self.config_file = f"config_{user_id}.json"
        else:
            self.config_file = config_file
            
        self.config: Dict[str, Any] = {}
        self.load_config()

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    loaded = json.load(f)
                    
                # Check if it's the new multi-asset format
                # New format has "symbols" as a DICT, old format has it as a LIST
                symbols_data = loaded.get("symbols")
                is_new_format = isinstance(symbols_data, dict) and "global" in loaded
                
                if is_new_format:
                    self.config = loaded
                else:
                    # Migrate from old format (symbols is a list or missing)
                    print(f"[CONFIG] Migrating config to multi-asset format...")
                    self.config = self._migrate_old_config(loaded)
                    self.save_config()
                    
            except Exception as e:
                print(f"[CONFIG] Error loading config {self.config_file}: {e}")
                self.config = self._get_defaults()
        else:
            print(f"[CONFIG] Creating new config file: {self.config_file}")
            self.config = self._get_defaults()
            self.save_config()

    def _migrate_old_config(self, old_config: Dict[str, Any]) -> Dict[str, Any]:
        """Migrate from old single-asset config to new multi-asset format"""
        new_config = self._get_defaults()
        
        # Migrate global settings
        if "max_runtime_minutes" in old_config:
            new_config["global"]["max_runtime_minutes"] = old_config["max_runtime_minutes"]
        
        # Migrate old symbols to new format
        old_symbols = old_config.get("symbols", ["FX Vol 20"])
        for symbol in old_symbols:
            if symbol in new_config["symbols"]:
                sym_cfg = new_config["symbols"][symbol]
                sym_cfg["enabled"] = True
                sym_cfg["spread"] = old_config.get("spread", 20.0)
                sym_cfg["max_positions"] = old_config.get("max_positions", 5)
                sym_cfg["buy_stop_tp"] = old_config.get("buy_stop_tp", 50.0)
                sym_cfg["buy_stop_sl"] = old_config.get("buy_stop_sl", 75.0)
                sym_cfg["sell_stop_tp"] = old_config.get("sell_stop_tp", 50.0)
                sym_cfg["sell_stop_sl"] = old_config.get("sell_stop_sl", 75.0)
                sym_cfg["hedge_enabled"] = old_config.get("hedge_enabled", True)
                sym_cfg["hedge_lot_size"] = old_config.get("hedge_lot_size", 0.01)
                
                # Migrate lot sizes (old format was center_lot_first, etc.)
                max_pos = sym_cfg["max_positions"]
                groups = max(1, int(max_pos) // 3)
                # pair_* arrays: center + groups. single_* arrays: groups.
                base_pair_buy = old_config.get("pair_buy_lot", 0.01)
                base_pair_sell = old_config.get("pair_sell_lot", 0.01)
                base_single = old_config.get("single_lot", 0.01)
                sym_cfg["pair_buy_lots"] = [float(base_pair_buy)] * (groups + 1)
                sym_cfg["pair_sell_lots"] = [float(base_pair_sell)] * (groups + 1)
                sym_cfg["single_lots"] = [float(base_single)] * groups
                
        return new_config

    def save_config(self):
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=4)
        except Exception as e:
            print(f" Error saving config: {e}")

    def update_config(self, new_config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update config with new values.
        Handles both flat updates and nested symbol updates.
        """
        # Handle global settings
        if "global" in new_config:
            self.config["global"].update(new_config["global"])
        
        # Handle symbol-specific settings
        if "symbols" in new_config:
            for symbol, sym_cfg in new_config["symbols"].items():
                if symbol in self.config["symbols"]:
                    # Merge provided fields
                    self.config["symbols"][symbol].update(sym_cfg)

                    # Validate grid_distance: must be > 0
                    grid_dist = self.config["symbols"][symbol].get("grid_distance", 50.0)
                    self.config["symbols"][symbol]["grid_distance"] = max(1.0, float(grid_dist))

                    # Validate tp_pips and sl_pips: must be > 0
                    tp = self.config["symbols"][symbol].get("tp_pips", 150.0)
                    sl = self.config["symbols"][symbol].get("sl_pips", 200.0)
                    self.config["symbols"][symbol]["tp_pips"] = max(1.0, float(tp))
                    self.config["symbols"][symbol]["sl_pips"] = max(1.0, float(sl))

                    # Validate and normalize max_positions
                    max_pos = int(self.config["symbols"][symbol].get("max_positions", 3))
                    # Clamp between 3 and MAX_POSITION_LIMIT
                    max_pos = max(3, min(MAX_POSITION_LIMIT, max_pos))
                    if max_pos % 3 != 0:
                        raise ValueError(f"max_positions must be multiple of 3, got {max_pos}")
                    self.config["symbols"][symbol]["max_positions"] = max_pos

                    groups = max(1, max_pos // 3)
                    pair_len = groups + 1

                    # Normalize lot arrays: accept either scalar fields or new arrays
                    # Priority: provided arrays in new_config -> existing arrays -> scalar fields -> default
                    # Pair Buy (center + groups)
                    if "pair_buy_lots" in sym_cfg and isinstance(sym_cfg["pair_buy_lots"], list):
                        arr = [max(0.01, float(x)) for x in sym_cfg["pair_buy_lots"]]
                    else:
                        # Scalar fallback
                        scalar = sym_cfg.get("pair_buy_lot", self.config["symbols"][symbol].get("pair_buy_lots", [0.01])[0])
                        arr = [max(0.01, float(scalar))] * pair_len
                    # Pad/trim to groups
                    if len(arr) < pair_len:
                        arr += [arr[-1]] * (pair_len - len(arr))
                    arr = arr[:pair_len]
                    self.config["symbols"][symbol]["pair_buy_lots"] = arr

                    # Pair Sell (center + groups)
                    if "pair_sell_lots" in sym_cfg and isinstance(sym_cfg["pair_sell_lots"], list):
                        arr = [max(0.01, float(x)) for x in sym_cfg["pair_sell_lots"]]
                    else:
                        scalar = sym_cfg.get("pair_sell_lot", self.config["symbols"][symbol].get("pair_sell_lots", [0.01])[0])
                        arr = [max(0.01, float(scalar))] * pair_len
                    if len(arr) < pair_len:
                        arr += [arr[-1]] * (pair_len - len(arr))
                    arr = arr[:pair_len]
                    self.config["symbols"][symbol]["pair_sell_lots"] = arr

                    # Single
                    if "single_lots" in sym_cfg and isinstance(sym_cfg["single_lots"], list):
                        arr = [max(0.01, float(x)) for x in sym_cfg["single_lots"]]
                    else:
                        scalar = sym_cfg.get("single_lot", self.config["symbols"][symbol].get("single_lots", [0.01])[0])
                        arr = [max(0.01, float(scalar))] * groups
                    if len(arr) < groups:
                        arr += [arr[-1]] * (groups - len(arr))
                    arr = arr[:groups]
                    self.config["symbols"][symbol]["single_lots"] = arr
        
        self.save_config()
        return self.config

    def get_config(self) -> Dict[str, Any]:
        return self.config
    
    def get_global_config(self) -> Dict[str, Any]:
        """Get global settings"""
        return self.config.get("global", {})
    
    def get_symbol_config(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get config for a specific symbol"""
        return self.config.get("symbols", {}).get(symbol)
    
    def get_enabled_symbols(self) -> List[str]:
        """Get list of symbols that are enabled"""
        enabled = []
        for symbol, cfg in self.config.get("symbols", {}).items():
            if cfg.get("enabled", False):
                enabled.append(symbol)
        return enabled
    
    def enable_symbol(self, symbol: str, enabled: bool = True):
        """Enable or disable a symbol"""
        if symbol in self.config.get("symbols", {}):
            self.config["symbols"][symbol]["enabled"] = enabled
            self.save_config()
    
    def _get_defaults(self) -> Dict[str, Any]:
        """Generate default multi-asset config structure"""
        return {
            "global": {
                "max_runtime_minutes": 0
            },
            "symbols": {
                symbol: get_default_symbol_config()
                for symbol in AVAILABLE_SYMBOLS
            }
        }