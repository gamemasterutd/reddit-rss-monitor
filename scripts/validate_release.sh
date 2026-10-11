#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3}"

echo "Checking public files"
"$PYTHON" scripts/check_public_files.py

echo "Checking Python syntax"
"$PYTHON" -m compileall -q reddit_monitor tests scripts

echo "Checking shell syntax"
for script in ./*.command scripts/*.sh; do
  bash -n "$script"
done

echo "Running local fixture tests"
"$PYTHON" -m unittest discover -s tests -v

echo "Checking standard-library runtime imports"
"$PYTHON" - <<'PY'
import ast
import pkgutil
import sys
import sysconfig
from pathlib import Path

stdlib = set(getattr(sys, 'stdlib_module_names', ()))
stdlib.update(sys.builtin_module_names)
stdlib_paths = [sysconfig.get_path('stdlib'), str(Path(sysconfig.get_path('stdlib')) / 'lib-dynload')]
extension_path = sysconfig.get_config_var('DESTSHARED')
if extension_path:
    stdlib_paths.append(extension_path)
stdlib.update(module.name for module in pkgutil.iter_modules(stdlib_paths))
stdlib.add('__future__')
violations = []
for path in Path('reddit_monitor').rglob('*.py'):
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name.split('.')[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names = [node.module.split('.')[0]]
        for name in names:
            if name not in stdlib and name != 'reddit_monitor':
                violations.append((str(path), name))
if violations:
    raise SystemExit('Unexpected runtime imports: %r' % violations)
print('Standard-library runtime audit passed.')
PY

echo "VALIDATION PASSED"
