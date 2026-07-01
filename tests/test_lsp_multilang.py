"""lsp multi-language support — JS/TS/Go/Rust/Java/Ruby/C via regex."""

from __future__ import annotations

from pathlib import Path

import anyio

from mantis_agent.builtin_tools.codenav import find_definitions, find_symbols, lsp


def _write(tmp_path: Path, name: str, body: str) -> Path:
    (tmp_path / name).write_text(body)
    return tmp_path


def test_typescript_definitions(tmp_path: Path) -> None:
    _write(tmp_path, "a.ts", "export function greet(n: string) {}\n"
                             "export class Widget {}\n"
                             "interface Opts { x: number }\n"
                             "type ID = string\n"
                             "const TIMEOUT = 30\n")
    kinds = {k for _r, _l, k in find_definitions("greet", str(tmp_path))}
    assert kinds == {"function"}
    assert find_definitions("Widget", str(tmp_path))[0][2] == "class"
    assert find_definitions("Opts", str(tmp_path))[0][2] == "interface"
    assert find_definitions("ID", str(tmp_path))[0][2] == "type"
    assert find_definitions("TIMEOUT", str(tmp_path))[0][2] == "const"


def test_go_and_rust(tmp_path: Path) -> None:
    _write(tmp_path, "s.go", "func Handler(w int) {}\ntype Server struct {}\n")
    _write(tmp_path, "l.rs", "pub fn run() {}\nstruct Config {}\nenum State {}\n")
    assert find_definitions("Handler", str(tmp_path))[0][2] == "func"
    assert find_definitions("Server", str(tmp_path))[0][2] == "type"
    assert find_definitions("run", str(tmp_path))[0][2] == "fn"
    assert find_definitions("Config", str(tmp_path))[0][2] == "type"


def test_ruby_and_java(tmp_path: Path) -> None:
    _write(tmp_path, "u.rb", "class User\n  def name\n  end\nend\nmodule Auth\nend\n")
    _write(tmp_path, "M.java", "public class Main {}\n")
    assert find_definitions("User", str(tmp_path))[0][2] == "class"
    assert find_definitions("name", str(tmp_path))[0][2] == "def"
    assert find_definitions("Auth", str(tmp_path))[0][2] == "module"
    assert find_definitions("Main", str(tmp_path))[0][2] == "class"


def test_mixed_python_and_ts_symbols(tmp_path: Path) -> None:
    _write(tmp_path, "m.py", "class Alpha:\n    def meth(self): pass\n")
    _write(tmp_path, "a.ts", "export function beta() {}\n")
    syms = {(k, n) for _r, _l, k, n, _c in find_symbols(str(tmp_path))}
    assert ("class", "Alpha") in syms       # python ast
    assert ("method", "meth") in syms       # python method
    assert ("function", "beta") in syms     # ts regex


def test_lsp_tool_ts(tmp_path: Path) -> None:
    _write(tmp_path, "a.ts", "export class Foo {}\n")
    out = anyio.run(lambda: lsp.fn(operation="definition", symbol="Foo", path=str(tmp_path)))
    assert "a.ts:1: [class] Foo" in out


def test_control_flow_not_matched(tmp_path: Path) -> None:
    # a call / if-brace must NOT be reported as a definition
    _write(tmp_path, "a.ts", "greet();\nif (x) {\n}\n")
    assert find_definitions("greet", str(tmp_path)) == []
    assert find_definitions("if", str(tmp_path)) == []
