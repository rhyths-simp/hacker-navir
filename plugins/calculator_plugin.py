"""
calculator_plugin.py — Simple calculator for quick math in the terminal
────────────────────────────────────────────────────────────────────────
Ctrl+L  →  Open calculator popup

Drop into ~/.navigator/plugins/ to install.
"""

NAME        = "calculator"
VERSION     = "1.0"
DESCRIPTION = "Quick calculator — Ctrl+L to evaluate math expressions"


def on_calc_key(api, path, selected_item):
    """Open calculator popup and evaluate expressions."""
    try:
        expr = api.prompt("Enter math expression (e.g. 2+2*3):")
        
        if not expr:
            api.show_status("Calculator cancelled.")
            return
        
        # Evaluate the expression safely using eval
        # Only allow safe math operations
        try:
            result = eval(expr, {"__builtins__": {}}, {})
            
            # Format the result nicely
            if isinstance(result, float):
                result_str = f"{result:.10g}"  # Remove trailing zeros
            else:
                result_str = str(result)
            
            # Show result in popup
            api.show_popup("◈ CALCULATOR ◈", [
                ("Expression", expr),
                ("──────────", "──────────────────"),
                ("Result",    result_str),
                ("──────────", "──────────────────"),
                ("",          "Press any key to close"),
            ])
            
        except ZeroDivisionError:
            api.show_status("ERROR: Division by zero!", is_error=True)
        except SyntaxError:
            api.show_status("ERROR: Invalid math expression!", is_error=True)
        except Exception as e:
            api.show_status(f"ERROR: {str(e)[:40]}", is_error=True)
    
    except Exception as e:
        api.show_status(f"Calculator error: {e}", is_error=True)


def register(api):
    """Register the calculator plugin."""
    api.add_keybind("Ctrl+L", "Calculator", on_calc_key)
