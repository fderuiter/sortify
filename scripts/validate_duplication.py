"""
Lightweight Production-Scoped Duplication Check
Scans the 'app/' directory for duplicated logic.
"""

import ast
import os
import sys

MIN_DUPLICATE_LINES = 10

def get_docstring_lines(filepath, content):
    doc_lines = set()
    try:
        tree = ast.parse(content, filename=filepath)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
                    doc_node = node.body[0]
                    if hasattr(doc_node, 'lineno') and hasattr(doc_node, 'end_lineno'):
                        for ln in range(doc_node.lineno, doc_node.end_lineno + 1):
                            doc_lines.add(ln)
    except SyntaxError:
        pass
    return doc_lines

def get_normalized_lines(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        print(f"Error reading {filepath}: {e}")
        return []
        
    doc_lines = get_docstring_lines(filepath, content)
    
    normalized = []
    lines = content.split('\n')
    for i, line in enumerate(lines):
        line_num = i + 1
        if line_num in doc_lines:
            continue
            
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
            
        normalized.append((line_num, stripped))
        
    return normalized

def main():
    print(f"Running duplication check (window size: {MIN_DUPLICATE_LINES} lines)...")
    
    # 1. Collect all production files
    py_files = []
    for root, _, files in os.walk('app'):
        for file in files:
            if file.endswith('.py'):
                py_files.append(os.path.join(root, file))
                
    # 2. Extract normalized lines
    file_lines = {}
    for filepath in py_files:
        file_lines[filepath] = get_normalized_lines(filepath)
        
    # 3. Find duplicates using a sliding window
    windows = {}
    for filepath, norm_lines in file_lines.items():
        if len(norm_lines) < MIN_DUPLICATE_LINES:
            continue
            
        for i in range(len(norm_lines) - MIN_DUPLICATE_LINES + 1):
            window = tuple(text for _, text in norm_lines[i:i+MIN_DUPLICATE_LINES])
            start_line = norm_lines[i][0]
            end_line = norm_lines[i+MIN_DUPLICATE_LINES-1][0]
            
            if window not in windows:
                windows[window] = []
            
            # Avoid counting identical overlapping windows in the same file (edge case)
            # by checking if we already added this file and it overlaps.
            # But simpler: just append.
            windows[window].append((filepath, start_line, end_line))
            
    # 4. Filter and report
    duplicates_found = False
    reported_locations = set()
    
    for window, occurrences in windows.items():
        if len(occurrences) > 1:
            # Check if all these occurrences are already fully covered by a previously reported larger block
            # To simplify, we just track the exact (filepath, start_line) to avoid printing every single sliding step
            # of a 20-line duplicate block (which would produce 11 windows of size 10).
            
            # If any of the occurrences is new, we report it.
            is_new = False
            for filepath, start_line, _ in occurrences:
                if (filepath, start_line) not in reported_locations:
                    is_new = True
                    break
                    
            if is_new:
                duplicates_found = True
                print(f"\nDuplicate block found ({MIN_DUPLICATE_LINES} lines):")
                for filepath, start_line, end_line in occurrences:
                    print(f"  - {filepath} (lines {start_line}-{end_line})")
                    reported_locations.add((filepath, start_line))
                
                print("  Code:")
                for line in window[:3]:
                    print(f"    {line}")
                print("    ...")
                
    if duplicates_found:
        print("\nError: Code duplication detected in production directories.")
        sys.exit(1)
    else:
        print("No duplication found. Great job!")
        sys.exit(0)

if __name__ == '__main__':
    main()
