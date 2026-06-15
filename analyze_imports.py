#!/usr/bin/env python3
"""Analyze imports in registry.py to find unused and duplicate imports."""

import ast
import re
from pathlib import Path

def analyze_imports(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
        lines = content.split('\n')

    tree = ast.parse(content)

    # Track all imports with their line numbers
    imports_map = {}
    type_checking_imports = set()

    # Find TYPE_CHECKING block
    in_type_checking = False
    type_checking_start = 0
    type_checking_end = 0

    for i, line in enumerate(lines, 1):
        if 'if TYPE_CHECKING:' in line:
            in_type_checking = True
            type_checking_start = i
        elif in_type_checking and line and not line.startswith(' ') and not line.startswith('\t'):
            type_checking_end = i - 1
            break

    # Extract imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split('.')[-1]
                if node.lineno >= type_checking_start and node.lineno <= type_checking_end:
                    type_checking_imports.add(name)
                if name not in imports_map:
                    imports_map[name] = []
                imports_map[name].append(node.lineno)

        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                if node.lineno >= type_checking_start and node.lineno <= type_checking_end:
                    type_checking_imports.add(name)
                if name not in imports_map:
                    imports_map[name] = []
                imports_map[name].append(node.lineno)

    # Get actual code (excluding import section and docstrings)
    code_start = 23  # After last import
    actual_code = '\n'.join(lines[code_start:])

    # Remove TYPE_CHECKING imports from analysis of actual usage
    # These are only for type hints

    unused = []
    duplicates = []

    for name, line_numbers in sorted(imports_map.items()):
        # Skip __future__ and special names
        if name in ['annotations', '__future__']:
            continue

        # Check for duplicates
        if len(line_numbers) > 1:
            duplicates.extend(line_numbers[1:])  # Keep first, mark rest as duplicates

        # Check if used in actual code
        # For TYPE_CHECKING imports, check in type annotations
        if name in type_checking_imports:
            # These are used in type hints, not unused
            continue

        # Check usage in actual code
        pattern = r'\b' + re.escape(name) + r'\b'
        matches = re.findall(pattern, actual_code)

        if len(matches) == 0:
            unused.append(line_numbers[0])

    return {
        'file': filepath,
        'unused_lines': sorted(unused),
        'duplicate_lines': sorted(duplicates),
        'kept': len(set(imports_map.keys()) - set([k for k, v in imports_map.items() if v[0] in unused or any(ln in duplicates for ln in v[1:])]))
    }

if __name__ == '__main__':
    result = analyze_imports('backend/tools/registry.py')
    print(f"File: {result['file']}")
    print(f"Unused import lines: {result['unused_lines']}")
    print(f"Duplicate import lines: {result['duplicate_lines']}")
    print(f"Kept imports: {result['kept']}")
