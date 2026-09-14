"""Names a module uses but never defines or imports.

ast.parse only proves a file is Python. A mechanical edit can introduce a
call to a helper that was never written and the file still parses.
"""
import ast, builtins, pathlib, sys

ROOT = pathlib.Path(sys.argv[1])
BUILTIN = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__"}

for f in sorted(list(ROOT.glob("agents/*.py")) + list(ROOT.glob("core/*.py"))
                + list(ROOT.glob("core/publishers/*.py")) + list(ROOT.glob("bin/*.py"))):
    try:
        tree = ast.parse(f.read_text())
    except SyntaxError as e:
        print("%s: SYNTAX ERROR line %s: %s" % (f.name, e.lineno, e.msg))
        continue
    defined, used = set(BUILTIN), {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef):
            defined.add(n.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(n.name)
            for a in n.args.args + n.args.kwonlyargs + n.args.posonlyargs:
                defined.add(a.arg)
            if n.args.vararg: defined.add(n.args.vararg.arg)
            if n.args.kwarg: defined.add(n.args.kwarg.arg)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                defined.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.Name):
            if isinstance(n.ctx, (ast.Store, ast.Del)):
                defined.add(n.id)
            else:
                used.setdefault(n.id, n.lineno)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            defined.add(n.name)
        elif isinstance(n, ast.comprehension):
            for t in ast.walk(n.target):
                if isinstance(t, ast.Name):
                    defined.add(t.id)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for it in n.items:
                if it.optional_vars is not None:
                    for t in ast.walk(it.optional_vars):
                        if isinstance(t, ast.Name):
                            defined.add(t.id)
        elif isinstance(n, ast.Lambda):
            for a in n.args.args + n.args.kwonlyargs + n.args.posonlyargs:
                defined.add(a.arg)
        elif isinstance(n, ast.Global) or isinstance(n, ast.Nonlocal):
            defined.update(n.names)
    missing = {k: v for k, v in used.items() if k not in defined}
    if missing:
        print("%s:" % str(f.relative_to(ROOT)))
        for k, v in sorted(missing.items(), key=lambda kv: kv[1]):
            print("    line %-5s %s" % (v, k))
