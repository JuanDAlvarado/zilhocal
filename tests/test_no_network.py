"""No-network test (§2, §12): walk the AST of every source file in the
package and fail if anything imports a networking module. This is the hard
constraint that makes the "fully offline" claim enforceable rather than
aspirational."""

import ast
from pathlib import Path

import fha_calc

DENYLIST = {
    "requests",
    "httpx",
    "urllib",
    "urllib2",
    "urllib3",
    "socket",
    "selenium",
    "playwright",
    "http.client",
    "ftplib",
    "telnetlib",
    "smtplib",
    "aiohttp",
}

PACKAGE_ROOT = Path(fha_calc.__file__).parent


def _imported_module_roots(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0], alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                yield node.module.split(".")[0], node.module


def test_no_networking_imports_in_package():
    offenders = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for root, full_name in _imported_module_roots(tree):
            if root in DENYLIST or full_name in DENYLIST:
                offenders.append(f"{path.relative_to(PACKAGE_ROOT.parent)}: imports {full_name!r}")

    assert not offenders, "networking imports found:\n" + "\n".join(offenders)
