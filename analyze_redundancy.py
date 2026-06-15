#!/usr/bin/env python3
"""Analyze backend/ for redundancies: unused imports/functions/classes, duplicates, stale TODOs."""

import ast
import os
import re
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Set, Tuple

class RedundancyAnalyzer(ast.NodeVisitor):
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.imports = []  # (module, name, lineno)
        self.defined_names = set()  # Classes, functions defined
        self.used_names = set()  # Names referenced in code
        self.todos = []  # (line_content, lineno)

    def visit_Import(self, node):
        for alias in node.names:
            name = alias.asname if alias.asname else alias.name
            self.imports.append(('import', alias.name, name, node.lineno))
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        module = node.module or ''
        for alias in node.names:
            name = alias.asname if alias.asname else alias.name
            self.imports.append(('from', module, name, node.lineno))
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self.defined_names.add(node.name)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self.defined_names.add(node.name)
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        self.defined_names.add(node.name)
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.used_names.add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node):
        # Track attribute access
        self.generic_visit(node)

def analyze_file(filepath: Path) -> Dict:
    """Analyze a single Python file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            lines = content.split('\n')

        # Parse AST
        try:
            tree = ast.parse(content, str(filepath))
        except SyntaxError:
            return None

        analyzer = RedundancyAnalyzer(str(filepath))
        analyzer.visit(tree)

        # Find TODOs
        todos = []
        for i, line in enumerate(lines, 1):
            if re.search(r'#\s*TODO|#\s*FIXME|#\s*XXX', line, re.IGNORECASE):
                todos.append((line.strip(), i))

        return {
            'filepath': str(filepath),
            'imports': analyzer.imports,
            'defined_names': analyzer.defined_names,
            'used_names': analyzer.used_names,
            'todos': todos,
            'lines': lines
        }
    except Exception as e:
        print(f"Error analyzing {filepath}: {e}")
        return None

def find_unused_imports(file_data: Dict) -> List[str]:
    """Find unused imports in a file."""
    unused = []
    used_names = file_data['used_names']

    for imp_type, module_or_name, name, lineno in file_data['imports']:
        # Skip star imports
        if name == '*':
            continue

        # Check if the imported name is used
        if name not in used_names:
            if imp_type == 'import':
                unused.append(f"{file_data['filepath']}:{lineno} - unused import: {module_or_name}")
            else:
                unused.append(f"{file_data['filepath']}:{lineno} - unused from {module_or_name} import {name}")

    return unused

def find_duplicate_imports(all_files: List[Dict]) -> List[str]:
    """Find duplicate imports across files."""
    duplicates = []

    for file_data in all_files:
        if not file_data:
            continue

        seen_imports = {}
        for imp_type, module_or_name, name, lineno in file_data['imports']:
            key = (imp_type, module_or_name, name)
            if key in seen_imports:
                duplicates.append(
                    f"{file_data['filepath']}:{lineno} - duplicate import of {name} "
                    f"(also at line {seen_imports[key]})"
                )
            else:
                seen_imports[key] = lineno

    return duplicates

def categorize_todos(all_files: List[Dict]) -> List[str]:
    """Categorize TODOs as potentially stale."""
    stale_keywords = [
        'temp', 'temporary', 'placeholder', 'hack', 'workaround',
        'quick', 'dirty', 'remove', 'cleanup', 'old', 'legacy',
        'deprecated', 'fix later', 'revisit'
    ]

    stale_todos = []

    for file_data in all_files:
        if not file_data:
            continue

        for todo_line, lineno in file_data['todos']:
            todo_lower = todo_line.lower()
            if any(keyword in todo_lower for keyword in stale_keywords):
                stale_todos.append(f"{file_data['filepath']}:{lineno} - {todo_line}")

    return stale_todos

def find_unused_functions_classes(all_files: List[Dict]) -> List[str]:
    """Find potentially unused functions/classes."""
    # Build global usage map
    all_defined = {}  # name -> [files where defined]
    all_used = set()  # all names used across all files

    for file_data in all_files:
        if not file_data:
            continue

        for name in file_data['defined_names']:
            if name not in all_defined:
                all_defined[name] = []
            all_defined[name].append(file_data['filepath'])

        all_used.update(file_data['used_names'])

    unused = []
    for name, files in all_defined.items():
        # Skip private names (could be used via getattr)
        if name.startswith('_'):
            continue

        # Skip common special methods
        if name in ['__init__', '__str__', '__repr__', '__call__']:
            continue

        # If defined but never used
        if name not in all_used:
            for filepath in files:
                unused.append(f"{filepath} - potentially unused: {name}")

    return unused

def main():
    backend_dir = Path(__file__).parent / 'backend'

    # Collect all Python files
    py_files = list(backend_dir.rglob('*.py'))

    print(f"Analyzing {len(py_files)} Python files...")

    # Analyze all files
    all_file_data = []
    for py_file in py_files:
        file_data = analyze_file(py_file)
        all_file_data.append(file_data)

    # Find redundancies
    unused_imports = []
    for file_data in all_file_data:
        if file_data:
            unused_imports.extend(find_unused_imports(file_data))

    duplicate_imports = find_duplicate_imports(all_file_data)
    stale_todos = categorize_todos(all_file_data)
    unused_defs = find_unused_functions_classes(all_file_data)

    # Combine results
    unused = unused_imports + unused_defs
    duplicates = duplicate_imports

    # Print results
    print("\n=== UNUSED IMPORTS/FUNCTIONS/CLASSES ===")
    for item in unused[:50]:  # Limit output
        print(item)

    print(f"\n=== DUPLICATE IMPORTS ===")
    for item in duplicates[:30]:
        print(item)

    print(f"\n=== STALE TODOs ===")
    for item in stale_todos[:30]:
        print(item)

    print(f"\n=== SUMMARY ===")
    print(f"Unused items: {len(unused)}")
    print(f"Duplicate imports: {len(duplicates)}")
    print(f"Stale TODOs: {len(stale_todos)}")

if __name__ == '__main__':
    main()
