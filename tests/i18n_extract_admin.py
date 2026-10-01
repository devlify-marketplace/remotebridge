import re, glob, json, ast, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def extract():
    strings=set()
    for f in glob.glob(os.path.join(ROOT, "admin", "templates", "*.html")):
        with open(f, encoding='utf-8') as fh:
            src=fh.read()
        for m in re.finditer(r'_\(\s*(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\')', src):
            t=m.group(1) if m.group(1) is not None else m.group(2)
            strings.add(t.replace('\\"','"').replace("\\'","'").replace("\\\\","\\"))
    with open(os.path.join(ROOT, "admin", "server.py"), encoding="utf-8") as fh:
        tree=ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node,ast.Call) and getattr(node.func,"id",None)=="tr" and node.args:
            a=node.args[0]
            if isinstance(a,ast.Constant) and isinstance(a.value,str): strings.add(a.value)
    strings|={"bug","question","idea","other","open","resolved"}
    return sorted(strings)

