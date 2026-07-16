"""Utility functions for handling paths and sanitizing filenames."""
import re

RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
}

def sanitize_name(name: str) -> str:
    """Sanitize a file or folder name for Windows.
    
    Strips illegal characters and appends _safe to reserved names.
    """
    if not name:
        return name
        
    # Replace illegal characters with underscore (or just strip them)
    # The requirement says "strip illegal path characters" but in the example:
    # "Data: Archives" -> "Data_ Archives" so we should replace `:` with `_`.
    # Let's replace `< > : " / \\ | ? *` with `_`.
    safe_name = re.sub(r'[<>:"/\\|?*]', '_', name)
    
    # Strip trailing periods and spaces (also problematic on Windows)
    safe_name = safe_name.rstrip('. ')
    
    # Check if the name matches a reserved name (case-insensitive, optionally with an extension)
    upper_name = safe_name.upper()
    base_name = upper_name.split('.')[0]
    
    if base_name in RESERVED_NAMES:
        # Need to append _safe suffix. For "CON" -> "CON_safe".
        # If there's an extension, e.g. "CON.txt" -> "CON_safe.txt"?
        # The scenario says "CON" -> "CON_safe".
        
        # Let's preserve the original casing and just append _safe to the base name
        parts = safe_name.split('.')
        parts[0] = parts[0] + '_safe'
        safe_name = '.'.join(parts)
        
    if not safe_name:
        safe_name = "Unnamed_safe"
        
    return safe_name

def is_valid_name(name: str) -> bool:
    """Check if a file or folder name is valid for Windows."""
    if not name:
        return False
        
    if re.search(r'[<>:"/\\|?*]', name):
        return False
        
    if name != name.rstrip('. '):
        return False
        
    base_name = name.upper().split('.')[0]
    if base_name in RESERVED_NAMES:
        return False
        
    return True
