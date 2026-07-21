"""Validates architectural constraints across the project codebase."""
import ast
import os
import sys


def main():
    """Execute the architectural validation checks."""
    errors = []
    
    # Define directories to check
    # Check all python files in the project for rule A (DB writes in async def)
    # Check app/ui/ for rule B (blocking ops in async def)
    
    for root, _, files in os.walk('app'):
        for file in files:
            if not file.endswith('.py'):
                continue
            
            filepath = os.path.join(root, file)
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
                
            try:
                tree = ast.parse(content, filename=filepath)
            except SyntaxError:
                continue
                
            is_ui_file = 'app/ui' in filepath.replace('\\', '/')
            
            class Visitor(ast.NodeVisitor):
                def __init__(self):
                    self.async_context = []
                    
                def visit_AsyncFunctionDef(self, node):
                    self.async_context.append(node.name)
                    self.generic_visit(node)
                    self.async_context.pop()
                    
                def visit_Call(self, node):
                    if not self.async_context:
                        self.generic_visit(node)
                        return
                    
                    # Rule A: Direct DB writes in async def (execute, executemany, commit)
                    if isinstance(node.func, ast.Attribute):
                        method_name = node.func.attr
                        if method_name in ('execute', 'executemany', 'commit'):
                            if method_name == 'commit':
                                errors.append(f"{filepath}:{node.lineno}: Direct database commit() inside async function '{self.async_context[-1]}'")
                            elif method_name in ('execute', 'executemany'):
                                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                                    query = node.args[0].value.upper()
                                    if any(q in query for q in ('INSERT ', 'UPDATE ', 'DELETE ', 'CREATE ', 'DROP ')):
                                        errors.append(f"{filepath}:{node.lineno}: Direct database write ({method_name} with {query.split()[0]}) inside async function '{self.async_context[-1]}'")
                                    
                    # Rule B: Synchronous blocking operations in UI-bound logic
                    if is_ui_file:
                        # Check for time.sleep
                        if isinstance(node.func, ast.Attribute) and node.func.attr == 'sleep' and isinstance(node.func.value, ast.Name) and node.func.value.id == 'time':
                            errors.append(f"{filepath}:{node.lineno}: Synchronous time.sleep() inside async function '{self.async_context[-1]}'")
                        
                        # Check for requests.get, requests.post, etc.
                        if isinstance(node.func, ast.Attribute) and node.func.attr in ('get', 'post', 'put', 'delete', 'patch') and isinstance(node.func.value, ast.Name) and node.func.value.id == 'requests':
                            errors.append(f"{filepath}:{node.lineno}: Synchronous requests.{node.func.attr}() inside async function '{self.async_context[-1]}'")
                        
                        # Check for open()
                        if isinstance(node.func, ast.Name) and node.func.id == 'open':
                            errors.append(f"{filepath}:{node.lineno}: Synchronous open() inside async function '{self.async_context[-1]}'")
                            
                        # Check for Path.read_text, Path.write_text, etc.
                        if isinstance(node.func, ast.Attribute) and node.func.attr.startswith(('read_', 'write_')):
                            # This is a heuristic for Path methods
                            errors.append(f"{filepath}:{node.lineno}: Synchronous Path.{node.func.attr}() inside async function '{self.async_context[-1]}'")

                    self.generic_visit(node)

            Visitor().visit(tree)
            
    if errors:
        print("Architectural Violations Found:")
        for error in errors:
            print(error)
        sys.exit(1)
    else:
        print("No architectural violations found.")
        sys.exit(0)

if __name__ == '__main__':
    main()
