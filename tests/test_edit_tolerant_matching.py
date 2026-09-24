"""edit_file / multi_edit tolerant matching cascade + line-ending/BOM preservation.

Cascade: exact → line-number prefix stripped → trailing whitespace ignored →
indentation ignored (new_string re-indented) → unique fuzzy block. Each stage is
accepted only on a unique match; the result names the rule that fired."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from mantis_agent.builtin_tools.fs import (
    _apply_edit,
    edit_file,
    multi_edit,
    read_file,
    write_file,
)


def _edit(f: Path, old: str, new: str, replace_all: bool = False) -> str:
    return anyio.run(lambda: edit_file.fn(path=str(f), old_string=old, new_string=new,
                                          replace_all=replace_all))


# -- cascade stages ------------------------------------------------------------


def test_exact_match_has_no_note() -> None:
    out, note = _apply_edit("a = 1\nb = 2\n", "b = 2", "b = 3", False, "f")
    assert out == "a = 1\nb = 3\n" and note == ""


def test_line_number_prefix_stage_notes_it() -> None:
    out, note = _apply_edit("a = 1\nb = 2\n", "  2\tb = 2", "b = 3", False, "f")
    assert out == "a = 1\nb = 3\n" and "line numbers" in note


def test_trailing_whitespace_stage() -> None:
    text = "def f():   \n    return 1\t\nx = 2\n"
    out, note = _apply_edit(text, "def f():\n    return 1\n", "def f():\n    return 9\n", False, "f")
    assert out == "def f():\n    return 9\nx = 2\n"
    assert note == "matched ignoring trailing whitespace"


def test_indentation_stage_off_by_one_level_reindents_new() -> None:
    text = "class A:\n    def f(self):\n        return 1\n\n    def g(self):\n        pass\n"
    old = "def f(self):\n    return 1"
    new = "def f(self):\n    x = 2\n    return x"
    out, note = _apply_edit(text, old, new, False, "f")
    assert note == "matched ignoring indentation"
    assert out == ("class A:\n    def f(self):\n        x = 2\n        return x\n\n"
                   "    def g(self):\n        pass\n")


def test_indentation_stage_spaces_to_tabs() -> None:
    text = "func main() {\n\tif x {\n\t\treturn 1\n\t}\n}\n"
    old = "    if x {\n        return 1\n    }"
    new = "    if x {\n        log()\n        return 2\n    }"
    out, note = _apply_edit(text, old, new, False, "f")
    assert "indentation" in note
    assert out == "func main() {\n\tif x {\n\t\tlog()\n\t\treturn 2\n\t}\n}\n"


def test_indentation_stage_tabs_to_spaces() -> None:
    text = "def f():\n  if x:\n    return 1\n"  # 2-space file
    old = "if x:\n\treturn 1"
    new = "if x:\n\ty = 3\n\treturn y"
    out, _ = _apply_edit(text, old, new, False, "f")
    assert out == "def f():\n  if x:\n    y = 3\n    return y\n"


def test_fuzzy_stage_unique_block() -> None:
    text = ("def load(path):\n    with open(path) as fh:\n        data = fh.read()\n"
            "    return parse(data)\n\ndef other():\n    return 0\n")
    # One token wrong ("contents" vs "data") — a typical paraphrase.
    old = ("def load(path):\n    with open(path) as fh:\n        contents = fh.read()\n"
           "    return parse(data)\n")
    new = "def load(path):\n    return parse(Path(path).read_text())\n"
    out, note = _apply_edit(text, old, new, False, "f")
    assert note.startswith("fuzzy-matched lines 1-4")
    assert out == ("def load(path):\n    return parse(Path(path).read_text())\n\n"
                   "def other():\n    return 0\n")


def test_fuzzy_needs_clear_winner() -> None:
    block = "for item in items:\n    total += item.price * qty\n    count += 1\n"
    text = block + "\n" + block.replace("qty", "qtx") + "\n"
    old = block.replace("qty", "qtz")  # equally close to both copies
    with pytest.raises(ValueError, match="ambiguous"):
        _apply_edit(text, old, "x\n", False, "f")


def test_fuzzy_never_for_replace_all() -> None:
    text = ("def load(path):\n    with open(path) as fh:\n        data = fh.read()\n"
            "    return parse(data)\n")
    old = text.replace("data = fh", "contents = fh")
    with pytest.raises(ValueError, match="not found"):
        _apply_edit(text, old, "x\n", True, "f")
    _apply_edit(text, old, "x\n", False, "f")  # sanity: would match without it


# -- ambiguity at every stage ---------------------------------------------------


@pytest.mark.parametrize(("text", "old", "how"), [
    ("x = 1\ny = 2\nx = 1\n", "x = 1", ""),
    ("x = 1\ny = 2\nx = 1\n", "  3\tx = 1", ""),
    ("x = 1  \ny = 2\nx = 1 \n", "x = 1\n", "trailing whitespace"),
    ("    x = 1\ny = 2\n\tx = 1\n", "\t\tx = 1\n", "indentation"),
])
def test_ambiguity_rejected_at_each_stage(text: str, old: str, how: str) -> None:
    with pytest.raises(ValueError, match="not unique") as ei:
        _apply_edit(text, old, "z = 0", False, "f")
    assert "lines 1, 3" in str(ei.value)
    assert how in str(ei.value)


def test_replace_all_on_lenient_stage_replaces_every_match() -> None:
    text = "x = 1  \ny = 2\nx = 1 \n"
    out, note = _apply_edit(text, "x = 1\n", "x = 5\n", True, "f")
    assert out == "x = 5\ny = 2\nx = 5\n" and "2 places" in note


# -- tool wiring: notes, CRLF, BOM, miss hint -------------------------------------


def test_edit_result_names_the_rule(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("if a:\n\tdo()\n")
    out = _edit(f, "if a:\n    do()", "if a:\n    do2()")
    assert "(matched ignoring indentation)" in out.splitlines()[0]
    assert f.read_text() == "if a:\n\tdo2()\n"


def test_crlf_preserved_by_edit_file(tmp_path: Path) -> None:
    f = tmp_path / "win.txt"
    f.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    _edit(f, "two\nthree", "TWO\nTHREE\nFOUR")
    assert f.read_bytes() == b"one\r\nTWO\r\nTHREE\r\nFOUR\r\n"


def test_crlf_old_string_from_model_still_matches(tmp_path: Path) -> None:
    f = tmp_path / "win.txt"
    f.write_bytes(b"one\r\ntwo\r\n")
    _edit(f, "one\r\ntwo", "1\r\n2")
    assert f.read_bytes() == b"1\r\n2\r\n"


def test_crlf_and_bom_preserved_by_multi_edit(tmp_path: Path) -> None:
    f = tmp_path / "win.cs"
    f.write_bytes(b"\xef\xbb\xbfclass A\r\n{\r\n    int x = 1;\r\n}\r\n")
    out = anyio.run(lambda: multi_edit.fn(path=str(f), edits=[
        {"old_string": "class A\n", "new_string": "class B\n"},
        {"old_string": "    int x = 1;  ", "new_string": "    int x = 2;"},
    ]))
    assert f.read_bytes() == b"\xef\xbb\xbfclass B\r\n{\r\n    int x = 2;\r\n}\r\n"
    assert "edit #2 matched ignoring trailing whitespace" in out.splitlines()[0]


def test_bom_first_line_matches(tmp_path: Path) -> None:
    f = tmp_path / "bom.py"
    f.write_bytes(b"\xef\xbb\xbfimport os\n")
    _edit(f, "import os\n", "import sys\n")
    assert f.read_bytes() == b"\xef\xbb\xbfimport sys\n"


def test_write_file_keeps_crlf_and_bom_of_existing_file(tmp_path: Path) -> None:
    f = tmp_path / "win.txt"
    f.write_bytes(b"\xef\xbb\xbfa\r\nb\r\n")
    anyio.run(lambda: read_file.fn(path=str(f)))
    anyio.run(lambda: write_file.fn(path=str(f), content="x\ny\n"))
    assert f.read_bytes() == b"\xef\xbb\xbfx\r\ny\r\n"


def test_write_file_new_and_lf_files_unchanged(tmp_path: Path) -> None:
    f = tmp_path / "new.txt"
    anyio.run(lambda: write_file.fn(path=str(f), content="x\ny\n"))
    assert f.read_bytes() == b"x\ny\n"


def test_miss_hint_shows_numbered_closest_block(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text("import os\n\ndef compute(a, b):\n    total = a + b\n    return total\n")
    with pytest.raises(ValueError) as ei:
        _edit(f, "def compute(x, y):\n    result = x * y\n    return result", "pass")
    msg = str(ei.value)
    assert "not found" in msg
    assert "3\tdef compute(a, b):\n4\t    total = a + b\n5\t    return total" in msg


def test_multi_edit_miss_is_atomic_and_prefixed(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_bytes(b"a = 1\r\nb = 2\r\n")
    with pytest.raises(ValueError, match=r"edit #2: old_string not found"):
        anyio.run(lambda: multi_edit.fn(path=str(f), edits=[
            {"old_string": "a = 1", "new_string": "a = 9"},
            {"old_string": "zzz qqq", "new_string": "x"},
        ]))
    assert f.read_bytes() == b"a = 1\r\nb = 2\r\n"


# -- re-indent is a constant shift, never a guessed-unit rescale ------------------


def test_reindent_keeps_docstring_body_relative_indent() -> None:
    text = "def f():\n    if a:\n        pass\n"
    new = 'if a:\n    s = """\nline\n  two\n"""\n'
    out, note = _apply_edit(text, "if a:\n    pass\n", new, False, "f")
    assert note == "matched ignoring indentation"
    # Every line shifted by exactly the anchor delta (+4) — no unit rescaling.
    assert out == 'def f():\n    if a:\n        s = """\n    line\n      two\n    """\n'


def test_reindent_keeps_hanging_indent() -> None:
    text = "def f():\n    x = 1\n    return x\n"
    out, _ = _apply_edit(text, "        x = 1", "        foo(\n                arg)", False, "f")
    assert out == "def f():\n    foo(\n            arg)\n    return x\n"


def test_reindent_yaml_with_odd_indent_elsewhere() -> None:
    text = "a:\n  b:\n    c: 1\n    d: |\n       odd\n  e: 2\n"
    out, _ = _apply_edit(text, "b:\n  c: 1\n", "b:\n  c: 1\n  x: 2\n", False, "f")
    assert out == "a:\n  b:\n    c: 1\n    x: 2\n    d: |\n       odd\n  e: 2\n"


def test_reindent_shallower_new_line_strips_down_to_zero() -> None:
    text = "class A:\n    def f(self):\n        return 1\n"
    old = "            return 1"  # model over-indented by 4
    new = "            return 2\n  # note"
    out, _ = _apply_edit(text, old, new, False, "f")
    assert out == "class A:\n    def f(self):\n        return 2\n# note\n"


def test_no_tab_conversion_when_ambiguous() -> None:
    # old_string mixes tabs and spaces → no conversion, pure prefix shift.
    text = "\tif x:\n\t\ty()\n"
    out, _ = _apply_edit(text, "if x:\n\t  y()", "if x:\n\t  z()", False, "f")
    assert out == "\tif x:\n\t\t  z()\n"


# -- fuzzy stage requires mostly-identical lines -----------------------------------


def test_fuzzy_rejects_block_where_every_line_changed() -> None:
    body = "\n".join(f"    value_{i} = compute_the_thing({i}, sEt, rEsult)" for i in range(30))
    text = "def f():\n" + body.replace("sEt", "set").replace("rEsult", "result") + "\n"
    with pytest.raises(ValueError, match="not found"):
        _apply_edit(text, body + "\n", "x\n", False, "f")


def test_fuzzy_note_reports_differing_lines() -> None:
    text = ("def load(path):\n    with open(path) as fh:\n        data = fh.read()\n"
            "    return parse(data)\n")
    old = text.replace("data = fh", "contents = fh")
    _, note = _apply_edit(text, old, "x\n", False, "f")
    assert note.endswith("1 line differed")


# -- line-number strip only for a fully-numbered excerpt ----------------------------


def test_tsv_content_not_mangled_by_line_number_strip() -> None:
    text = "id\tname\n1\talice\n2\tbob\n"
    # Only some lines look numbered → real TSV content, stripped form must not apply.
    out, note = _apply_edit(text, "id\tname\n1\talice", "id\tname\n1\tALICE", False, "f")
    assert out == "id\tname\n1\tALICE\n2\tbob\n" and note == ""
    with pytest.raises(ValueError, match="not found"):
        _apply_edit("x\nalice\n", "id\tname\n1\talice", "q", False, "f")


def test_numbered_excerpt_with_blank_line_keeps_the_blank() -> None:
    text = "a = 1\n\nb = 2\n"
    out, note = _apply_edit(text, "1\ta = 1\n2\t\n3\tb = 2", "a = 1\nb = 3", False, "f")
    assert out == "a = 1\nb = 3\n" and "line numbers" in note


# -- performance: no quadratic scans on big files ------------------------------------


def _big_file() -> str:
    return "\n".join(f"    result_{i} = transform(item_{i % 97}, factor={i % 13})"
                     for i in range(2000)) + "\n"


def test_unrelated_60_line_old_string_misses_fast() -> None:
    import time

    old = "\n".join(f"def unrelated_{i}(a, b):  # completely different text {i}"
                    for i in range(60))
    t0 = time.monotonic()
    with pytest.raises(ValueError, match="not found"):
        _apply_edit(_big_file(), old, "x", False, "f")
    assert time.monotonic() - t0 < 1.0


def test_20_line_near_miss_hint_is_fast() -> None:
    import time

    lines = _big_file().split("\n")
    old = "\n".join(lines[1000:1020]).replace("transform", "transfrom")
    t0 = time.monotonic()
    with pytest.raises(ValueError) as ei:
        _apply_edit(_big_file(), old, "x", False, "f")
    assert time.monotonic() - t0 < 1.0
    assert "1001\t    result_1000" in str(ei.value)


def test_120_line_fuzzy_near_miss_is_fast() -> None:
    import time

    lines = _big_file().split("\n")
    block = lines[500:620]
    old = "\n".join(block[:60] + [block[60] + "  # stale"] + block[61:]) + "\n"
    t0 = time.monotonic()
    out, note = _apply_edit(_big_file(), old, "NEW\n", False, "f")
    assert time.monotonic() - t0 < 1.0
    assert note.startswith("fuzzy-matched lines 501-620")
    assert out.split("\n")[500] == "NEW"
