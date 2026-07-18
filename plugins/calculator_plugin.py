"""
calculator_plugin.py — Advanced calculator for developers with history, memory, and persistence
────────────────────────────────────────────────────────────────────────────────────────────────
Ctrl+L  →  Open calculator with history
Ctrl+Shift+L  →  View saved calculations

Features:
  • Advanced math operations (sin, cos, log, sqrt, etc.)
  • Calculation history with recall
  • Memory functions (M+, M-, MR, MC)
  • Persistent storage of calculations
  • Auto-cleanup of old history (>100 entries)
  • Save/delete favorite calculations
  • Continue calculation mode
  • Base conversions (hex, binary, octal)

Drop into ~/.navigator/plugins/ to install.
"""

import os
import json
import math
from datetime import datetime, timedelta

NAME        = "calculator_pro"
VERSION     = "2.0"
DESCRIPTION = "Advanced calculator with history, memory, and persistent storage"

# ── Configuration ────────────────────────────────────────────────────────────

CALC_DIR    = os.path.expanduser("~/.navigator/calc")
HISTORY_FILE = os.path.join(CALC_DIR, "history.json")
SAVED_FILE   = os.path.join(CALC_DIR, "saved.json")
MEMORY_FILE  = os.path.join(CALC_DIR, "memory.json")

# Keep history for 30 days
HISTORY_RETENTION_DAYS = 30
MAX_HISTORY_ENTRIES    = 100


# ── Initialization ───────────────────────────────────────────────────────────

def _init_calc_dir():
    """Create calculator directory and files if they don't exist."""
    try:
        os.makedirs(CALC_DIR, exist_ok=True)
        
        # Initialize history file
        if not os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "w") as f:
                json.dump([], f)
        
        # Initialize saved calculations file
        if not os.path.exists(SAVED_FILE):
            with open(SAVED_FILE, "w") as f:
                json.dump({}, f)
        
        # Initialize memory file
        if not os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "w") as f:
                json.dump({"M": 0.0, "last_result": 0.0}, f)
    except Exception:
        pass


# ── History Management ───────────────────────────────────────────────────────

def _load_history():
    """Load calculation history from disk."""
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_history(history):
    """Save calculation history to disk and cleanup old entries."""
    try:
        # Remove entries older than retention period
        cutoff_time = (datetime.now() - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
        history = [h for h in history if h.get("timestamp", "") > cutoff_time]
        
        # Keep only last N entries
        if len(history) > MAX_HISTORY_ENTRIES:
            history = history[-MAX_HISTORY_ENTRIES:]
        
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except Exception:
        pass


def _add_to_history(expression, result):
    """Add a calculation to history."""
    history = _load_history()
    history.append({
        "expression": expression,
        "result": result,
        "timestamp": datetime.now().isoformat()
    })
    _save_history(history)


# ── Memory Management ────────────────────────────────────────────────────────

def _load_memory():
    """Load memory state from disk."""
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {"M": 0.0, "last_result": 0.0}


def _save_memory(memory):
    """Save memory state to disk."""
    try:
        with open(MEMORY_FILE, "w") as f:
            json.dump(memory, f)
    except Exception:
        pass


# ── Saved Calculations ───────────────────────────────────────────────────────

def _load_saved():
    """Load saved calculations from disk."""
    try:
        if os.path.exists(SAVED_FILE):
            with open(SAVED_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_saved(saved):
    """Save saved calculations to disk."""
    try:
        with open(SAVED_FILE, "w") as f:
            json.dump(saved, f, indent=2)
    except Exception:
        pass


# ── Math Evaluation ──────────────────────────────────────────────────────────

def _get_safe_math_namespace():
    """Return a safe namespace for math evaluation."""
    return {
        # Basic functions
        "abs": abs,
        "round": round,
        "min": min,
        "max": max,
        "pow": pow,
        
        # Math module functions
        "sin": math.sin,
        "cos": math.cos,
        "tan": math.tan,
        "asin": math.asin,
        "acos": math.acos,
        "atan": math.atan,
        "sinh": math.sinh,
        "cosh": math.cosh,
        "tanh": math.tanh,
        
        "sqrt": math.sqrt,
        "cbrt": lambda x: x ** (1/3),
        "exp": math.exp,
        "log": math.log,
        "log10": math.log10,
        "log2": math.log2,
        "ln": math.log,
        
        "ceil": math.ceil,
        "floor": math.floor,
        "factorial": math.factorial,
        "gcd": math.gcd,
        
        # Constants
        "pi": math.pi,
        "e": math.e,
        "tau": math.tau,
        "inf": math.inf,
    }


def _evaluate_expression(expr, memory):
    """Safely evaluate a mathematical expression."""
    expr = expr.strip()
    
    # Handle special commands
    if expr.upper() == "PI":
        return math.pi
    elif expr.upper() == "E":
        return math.e
    elif expr.upper() == "M":
        return memory.get("M", 0.0)
    elif expr.upper() == "ANS":
        return memory.get("last_result", 0.0)
    
    # Replace common aliases
    expr = expr.replace("^", "**")  # Power operator
    expr = expr.replace("÷", "/")   # Division
    expr = expr.replace("×", "*")   # Multiplication
    expr = expr.replace("ANS", str(memory.get("last_result", 0.0)))
    expr = expr.replace("M", str(memory.get("M", 0.0)))
    
    # Evaluate
    namespace = _get_safe_math_namespace()
    result = eval(expr, {"__builtins__": {}}, namespace)
    
    return result


def _format_result(result):
    """Format calculation result nicely."""
    if isinstance(result, float):
        # Remove trailing zeros
        formatted = f"{result:.15g}"
    else:
        formatted = str(result)
    
    return formatted


# ── Base Conversion ──────────────────────────────────────────────────────────

def _convert_base(number, from_base, to_base):
    """Convert number between bases."""
    # Convert to decimal first
    if from_base == 10:
        decimal = int(number)
    else:
        decimal = int(number, from_base)
    
    # Convert to target base
    if to_base == 16:
        return hex(decimal)[2:].upper()
    elif to_base == 2:
        return bin(decimal)[2:]
    elif to_base == 8:
        return oct(decimal)[2:]
    else:
        return str(decimal)


# ── UI Components ────────────────────────────────────────────────────────────

def _show_history_menu(api):
    """Show history with options to recall or delete."""
    history = _load_history()
    
    if not history:
        api.show_popup("◈ CALCULATOR HISTORY ◈", [
            ("", "No history available"),
            ("", "Press any key to close"),
        ])
        return None
    
    # Show last 20 entries
    lines = [("", "Recent Calculations")]
    lines.append(("──────", "────────────────────────────────"))
    
    for i, entry in enumerate(history[-20:]):
        expr = entry.get("expression", "?")[:20]
        result = str(entry.get("result", "?"))[:15]
        lines.append((expr, result))
    
    lines.append(("──────", "────────────────────────────────"))
    lines.append(("", "Press any key to close"))
    
    api.show_popup("◈ CALCULATOR HISTORY ◈", lines)


def _show_saved_menu(api):
    """Show saved calculations."""
    saved = _load_saved()
    
    if not saved:
        api.show_popup("◈ SAVED CALCULATIONS ◈", [
            ("", "No saved calculations"),
            ("", "Press any key to close"),
        ])
        return None
    
    lines = [("Name", "Expression")]
    lines.append(("──────", "────────────────────────────────"))
    
    for name, expr in list(saved.items())[:15]:
        name_short = name[:12]
        expr_short = expr[:20]
        lines.append((name_short, expr_short))
    
    lines.append(("──────", "────────────────────────────────"))
    lines.append(("", "Press any key to close"))
    
    api.show_popup("◈ SAVED CALCULATIONS ◈", lines)


# ── Main Calculator ──────────────────────────────────────────────────────────

def _show_calculator_help():
    """Return calculator help text."""
    return [
        ("FUNCTIONS", ""),
        ("sin, cos, tan", "Trigonometric"),
        ("sqrt, cbrt, exp", "Root & exponential"),
        ("log, log10, log2", "Logarithms"),
        ("abs, round, ceil", "Rounding"),
        ("factorial, gcd", "Special"),
        ("", ""),
        ("MEMORY", ""),
        ("M+num: add to M", "MR: recall M"),
        ("MC: clear M", ""),
        ("", ""),
        ("CONVERSIONS", ""),
        ("hex(123), bin(8)", "Base conversion"),
        ("", ""),
        ("CONSTANTS", ""),
        ("pi, e, tau", "Mathematical"),
    ]


def on_calc_key(api, path, selected_item):
    """Open advanced calculator with history and memory."""
    _init_calc_dir()
    memory = _load_memory()
    
    try:
        # Show help popup
        help_lines = _show_calculator_help()
        api.show_popup("◈ CALCULATOR HELP ◈", help_lines)
        
        # Main calculator loop
        while True:
            expr = api.prompt("Enter expression (or 'h' for help, 'H' for history, 'S' for saved):")
            
            if not expr:
                break
            
            expr_upper = expr.upper()
            
            # Handle special commands
            if expr_upper == "H":
                _show_history_menu(api)
                continue
            elif expr_upper == "S":
                _show_saved_menu(api)
                continue
            elif expr_upper.startswith("SAVE "):
                name = expr[5:].strip()
                if name and len(history) > 0:
                    last_expr = history[-1]["expression"]
                    saved = _load_saved()
                    saved[name] = last_expr
                    _save_saved(saved)
                    api.show_status(f"✓ Saved: {name}")
                continue
            elif expr_upper.startswith("DELETE "):
                name = expr[7:].strip()
                saved = _load_saved()
                if name in saved:
                    del saved[name]
                    _save_saved(saved)
                    api.show_status(f"✓ Deleted: {name}")
                continue
            elif expr_upper == "CLEAR":
                memory["M"] = 0.0
                _save_memory(memory)
                api.show_status("Memory cleared.")
                continue
            elif expr_upper.startswith("M+"):
                try:
                    val = float(expr[2:].strip())
                    memory["M"] += val
                    _save_memory(memory)
                    api.show_status(f"M = {memory['M']}")
                except:
                    api.show_status("Invalid memory operation.", is_error=True)
                continue
            elif expr_upper.startswith("M-"):
                try:
                    val = float(expr[2:].strip())
                    memory["M"] -= val
                    _save_memory(memory)
                    api.show_status(f"M = {memory['M']}")
                except:
                    api.show_status("Invalid memory operation.", is_error=True)
                continue
            elif expr_upper == "MR":
                api.show_status(f"M = {memory['M']}")
                continue
            elif expr_upper == "MC":
                memory["M"] = 0.0
                _save_memory(memory)
                api.show_status("Memory cleared.")
                continue
            
            # Evaluate expression
            try:
                result = _evaluate_expression(expr, memory)
                result_str = _format_result(result)
                
                # Update memory with last result
                memory["last_result"] = float(result) if isinstance(result, (int, float)) else 0.0
                _save_memory(memory)
                
                # Add to history
                _add_to_history(expr, result_str)
                
                # Show result
                api.show_popup("◈ CALCULATOR ◈", [
                    ("Expression", expr),
                    ("──────────", "──────────────────────"),
                    ("Result",    result_str),
                    ("──────────", "──────────────────────"),
                    ("Memory",    f"M = {memory['M']}"),
                    ("──────────", "──────────────────────"),
                    ("",          "Press any key to continue..."),
                ])
                
            except ZeroDivisionError:
                api.show_status("ERROR: Division by zero!", is_error=True)
            except ValueError as e:
                api.show_status(f"ERROR: Invalid value - {str(e)[:30]}", is_error=True)
            except SyntaxError:
                api.show_status("ERROR: Invalid syntax!", is_error=True)
            except Exception as e:
                api.show_status(f"ERROR: {str(e)[:40]}", is_error=True)
    
    except Exception as e:
        api.show_status(f"Calculator error: {e}", is_error=True)


def on_show_saved(api, path, selected_item):
    """View saved calculations (Ctrl+Shift+L equivalent)."""
    _init_calc_dir()
    _show_saved_menu(api)


def register(api):
    """Register the advanced calculator plugin."""
    _init_calc_dir()
    api.add_keybind("Ctrl+L", "Calculator (Advanced)", on_calc_key)
    api.add_keybind("Ctrl+K", "Saved Calculations", on_show_saved)
