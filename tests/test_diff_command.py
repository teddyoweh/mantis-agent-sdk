"""/diff — split_git_diff parses `git diff` into per-file (path, hunk_lines)."""

from __future__ import annotations

from mantis_agent.tui import split_git_diff

_TWO_FILE = """diff --git a/foo.py b/foo.py
index 111..222 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,3 @@
 def f():
-    return 1
+    return 2
 # end
diff --git a/bar.txt b/bar.txt
index 333..444 100644
--- a/bar.txt
+++ b/bar.txt
@@ -1 +1 @@
-old
+new
"""


def test_splits_files() -> None:
    out = split_git_diff(_TWO_FILE)
    assert [p for p, _ in out] == ["foo.py", "bar.txt"]


def test_strips_git_headers() -> None:
    out = split_git_diff(_TWO_FILE)
    for _p, lines in out:
        # No git header lines survive — only @@ hunks and +/- /space body.
        assert not any(ln.startswith(("+++", "---", "index ", "diff --git")) for ln in lines)
        assert all(ln.startswith(("@@", "+", "-", " ")) for ln in lines)


def test_keeps_body() -> None:
    out = split_git_diff(_TWO_FILE)
    foo = dict(out)["foo.py"]
    assert "-    return 1" in foo and "+    return 2" in foo
    assert "@@ -1,3 +1,3 @@" in foo


def test_new_file_diff() -> None:
    new = """diff --git a/new.py b/new.py
new file mode 100644
index 000..abc
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+import os
+print(os)
"""
    out = split_git_diff(new)
    assert out[0][0] == "new.py"
    assert out[0][1] == ["@@ -0,0 +1,2 @@", "+import os", "+print(os)"]


def test_binary_files_skipped() -> None:
    b = """diff --git a/img.png b/img.png
index 111..222 100644
Binary files a/img.png and b/img.png differ
"""
    # No hunk body → the file yields nothing (nothing renderable).
    assert split_git_diff(b) == []


def test_empty() -> None:
    assert split_git_diff("") == []
