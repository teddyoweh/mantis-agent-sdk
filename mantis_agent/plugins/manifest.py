"""The plugin manifest — schema, validator, capability conformance, integrity.

``mantis-plugin.json`` is what turns a directory of markdown into a package.
Two of its fields carry the weight:

``provides``
    Everything the plugin contributes, by kind and name. It drives namespacing,
    collision detection, and uninstall — and it is what the approval prompt
    lists, so content that is *not* in it is content the user was never shown.

``capabilities``
    Which executable surfaces the plugin uses: shell blocks in skills, hook
    commands, stdio MCP servers, Python tool modules. This is the security
    model. Signatures prove who published; only a capability list bounds what
    was accepted.

And the property that makes the second one mean anything:

    **A declared capability is a contract checked against the content.**

A manifest saying ``shellBlocks: false`` whose skill declares a shell block
fails :func:`verify_capabilities` and the install aborts. Without that check
the prompt is a decoration a hostile author fills in with zeros — and a user
who reads "no shell blocks" and clicks install has been told something false by
software they trusted, which is worse than not asking at all.

Over-declaration is not an error. A manifest claiming ``pythonTools: true``
with no ``tools/`` directory has only asked the user for *more* than it takes;
that is reported as a smell (``overdeclared_capabilities``) so authoring tools
can flag it, and it never blocks an install.

The validator is strict in the direction that matters: unknown keys are
rejected. A typo'd ``capabilties`` block that gets silently ignored declares
nothing while looking, to its author and to any reviewer, like it declares
everything.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Tuple, Union

from . import CapabilityUndeclaredError, IntegrityError, ManifestInvalidError, ManifestVersionError
from .archive import safe_member_path

__all__ = [
    "CONTENT_KINDS",
    "MANIFEST_FILENAME",
    "SCHEMA_VERSION",
    "Author",
    "Capabilities",
    "ConformanceReport",
    "Integrity",
    "IntegrityReport",
    "Manifest",
    "Provides",
    "conformance_report",
    "file_digest",
    "integrity_report",
    "load_manifest",
    "parse_manifest",
    "plugin_content_files",
    "verify_capabilities",
    "verify_integrity",
]

MANIFEST_FILENAME = "mantis-plugin.json"
SCHEMA_VERSION = 1

#: Plugin names are namespace segments: they appear in ``python-pack:py-style``
#: and in ``mcp__python-pack__pyright__tool``. Lowercase so one plugin cannot
#: have two spellings on a case-insensitive filesystem; no ``__`` because that
#: is the MCP namespace delimiter and a name containing it is not injectively
#: parseable (see ``mcp/manager.py::_ns_segment``, which exists for this).
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: Semantic version, the subset the resolver will have to compare.
_VERSION_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:[0-9A-Za-z-]+)(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

_HEX_RE = re.compile(r"^[0-9a-f]+$")
_SAFE_URL_SCHEMES = ("https://", "http://")
_DIGEST_LENGTHS = {"sha256": 64, "sha512": 128}

#: ``provides`` key -> (directory, accepted filename patterns). This is also
#: the map that decides what counts as *content* for conformance: anything
#: outside these directories (README, LICENSE, assets) is inert.
CONTENT_KINDS: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "skills": ("skills", ("*.md", "*/SKILL.md")),
    "agents": ("agents", ("*.md",)),
    "workflows": ("workflows", ("*.md",)),
    "rules": ("rules", ("*.md",)),
    "tools": ("tools", ("*.py",)),
}

#: Files that carry declarations rather than prose.
_MCP_FILE = "mcp.json"
_HOOKS_FILE = "hooks.json"

_PROVIDES_KEYS = tuple(CONTENT_KINDS) + ("mcpServers", "hooks", "settings")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Author:
    name: str = ""
    url: str = ""


@dataclass(frozen=True)
class Provides:
    """What the plugin contributes. Tuples, so a parsed manifest is hashable
    and cannot be mutated after the user approved what it said."""

    skills: Tuple[str, ...] = ()
    agents: Tuple[str, ...] = ()
    workflows: Tuple[str, ...] = ()
    rules: Tuple[str, ...] = ()
    tools: Tuple[str, ...] = ()
    mcp_servers: Tuple[str, ...] = ()
    hooks: Tuple[str, ...] = ()
    settings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Capabilities:
    """The executable surfaces, all defaulting to "not used".

    Defaults matter here: a manifest that omits ``capabilities`` entirely
    declares the *narrowest* possible set, so forgetting the block cannot
    accidentally grant anything — it can only fail conformance."""

    shell_blocks: bool = False
    hook_commands: bool = False
    mcp_stdio: bool = False
    python_tools: bool = False
    network: Tuple[str, ...] = ()
    filesystem_writes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Integrity:
    algorithm: str = "sha256"
    #: relative POSIX path -> bare lowercase hex digest
    files: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Manifest:
    name: str
    version: str
    schema_version: int = SCHEMA_VERSION
    description: str = ""
    author: Author = field(default_factory=Author)
    license: str = ""
    homepage: str = ""
    requires: Mapping[str, str] = field(default_factory=dict)
    dependencies: Mapping[str, str] = field(default_factory=dict)
    provides: Provides = field(default_factory=Provides)
    capabilities: Capabilities = field(default_factory=Capabilities)
    integrity: Integrity = field(default_factory=Integrity)

    def to_dict(self) -> Dict[str, Any]:
        """Back to the on-disk shape, round-tripping through
        :func:`parse_manifest` unchanged — which is what lets ``pack`` write a
        manifest it just validated."""
        out: Dict[str, Any] = {
            "schemaVersion": self.schema_version,
            "name": self.name,
            "version": self.version,
        }
        if self.description:
            out["description"] = self.description
        if self.author.name or self.author.url:
            out["author"] = {k: v for k, v in asdict(self.author).items() if v}
        if self.license:
            out["license"] = self.license
        if self.homepage:
            out["homepage"] = self.homepage
        if self.requires:
            out["requires"] = dict(self.requires)
        if self.dependencies:
            out["dependencies"] = dict(self.dependencies)
        provides = {
            _camel(k): list(v) for k, v in asdict(self.provides).items() if v
        }
        if provides:
            out["provides"] = provides
        caps = {
            _camel(k): (list(v) if isinstance(v, tuple) else v)
            for k, v in asdict(self.capabilities).items()
            if v
        }
        if caps:
            out["capabilities"] = caps
        if self.integrity.files:
            out["integrity"] = {
                "algorithm": self.integrity.algorithm,
                "files": dict(self.integrity.files),
            }
        return out


def _camel(snake: str) -> str:
    head, *rest = snake.split("_")
    return head + "".join(p.title() for p in rest)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _fail(msg: str) -> "ManifestInvalidError":
    return ManifestInvalidError(f"{MANIFEST_FILENAME}: {msg}")


def _str(data: Mapping[str, Any], key: str, *, default: str = "") -> str:
    v = data.get(key, default)
    if not isinstance(v, str):
        raise _fail(f"{key!r} must be a string, got {type(v).__name__}")
    return v


def _str_map(data: Mapping[str, Any], key: str) -> Dict[str, str]:
    v = data.get(key, {})
    if not isinstance(v, dict):
        raise _fail(f"{key!r} must be an object of name -> version range")
    out: Dict[str, str] = {}
    for k, val in v.items():
        if not isinstance(k, str) or not k.strip():
            raise _fail(f"{key!r} has an empty name")
        if not isinstance(val, str) or not val.strip():
            raise _fail(f"{key}[{k!r}] must be a non-empty version range string")
        out[k] = val
    return out


def _str_tuple(data: Mapping[str, Any], key: str, *, where: str) -> Tuple[str, ...]:
    v = data.get(key, [])
    if isinstance(v, str) or not isinstance(v, (list, tuple)):
        raise _fail(f"{where}.{key} must be a list of strings")
    for item in v:
        if not isinstance(item, str) or not item.strip():
            raise _fail(f"{where}.{key} must contain non-empty strings")
    return tuple(v)


def _bool(data: Mapping[str, Any], key: str) -> bool:
    v = data.get(key, False)
    if not isinstance(v, bool):
        # ``"no"`` and ``0`` are both truthy-or-falsy in ways that would make a
        # capability declaration a coin flip. Only real booleans.
        raise _fail(f"capabilities.{key} must be true or false")
    return v


def parse_manifest(source: Union[str, bytes, Mapping[str, Any]]) -> Manifest:
    """Validate a manifest document and return the typed form.

    Accepts the decoded object or the raw JSON text, because the two callers —
    a file on disk and a marketplace index entry — each already have one.
    """
    if isinstance(source, (str, bytes)):
        try:
            data = json.loads(source)
        except ValueError as exc:
            raise _fail(f"not valid JSON ({exc})") from exc
    else:
        data = source
    if not isinstance(data, dict):
        raise _fail("top level must be a JSON object")

    known = {
        "schemaVersion", "name", "version", "description", "author", "license",
        "homepage", "requires", "dependencies", "provides", "capabilities", "integrity",
    }
    unknown = sorted(set(data) - known)
    if unknown:
        raise _fail(f"unknown key(s): {', '.join(unknown)}")

    sv = data.get("schemaVersion")
    if not isinstance(sv, int) or isinstance(sv, bool) or sv != SCHEMA_VERSION:
        raise ManifestVersionError(
            f"{MANIFEST_FILENAME}: schemaVersion {sv!r} is not supported "
            f"(this build understands {SCHEMA_VERSION})"
        )

    name = data.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name) or "__" in name:
        raise _fail(
            f"name {name!r} must be lowercase [a-z0-9._-], start alphanumeric, "
            "be at most 64 characters, and contain no '__'"
        )
    version = data.get("version")
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise _fail(f"version {version!r} must be semantic (MAJOR.MINOR.PATCH)")

    homepage = _str(data, "homepage")
    if homepage and not homepage.startswith(_SAFE_URL_SCHEMES):
        # A homepage is rendered in the approval prompt. ``javascript:`` and
        # ``file:`` have no business being there.
        raise _fail(f"homepage {homepage!r} must be an http(s) URL")

    author_raw = data.get("author", {})
    if not isinstance(author_raw, dict):
        raise _fail("author must be an object with 'name' and optional 'url'")
    author_unknown = sorted(set(author_raw) - {"name", "url"})
    if author_unknown:
        raise _fail(f"author has unknown key(s): {', '.join(author_unknown)}")
    author = Author(name=_str(author_raw, "name"), url=_str(author_raw, "url"))
    if author.url and not author.url.startswith(_SAFE_URL_SCHEMES):
        raise _fail(f"author.url {author.url!r} must be an http(s) URL")

    return Manifest(
        name=name,
        version=version,
        schema_version=sv,
        description=_str(data, "description"),
        author=author,
        license=_str(data, "license"),
        homepage=homepage,
        requires=_str_map(data, "requires"),
        dependencies=_str_map(data, "dependencies"),
        provides=_parse_provides(data.get("provides", {})),
        capabilities=_parse_capabilities(data.get("capabilities", {})),
        integrity=_parse_integrity(data.get("integrity", {})),
    )


def _parse_provides(raw: Any) -> Provides:
    if not isinstance(raw, dict):
        raise _fail("provides must be an object")
    unknown = sorted(set(raw) - set(_PROVIDES_KEYS))
    if unknown:
        raise _fail(f"provides has unknown kind(s): {', '.join(unknown)}")
    return Provides(
        skills=_str_tuple(raw, "skills", where="provides"),
        agents=_str_tuple(raw, "agents", where="provides"),
        workflows=_str_tuple(raw, "workflows", where="provides"),
        rules=_str_tuple(raw, "rules", where="provides"),
        tools=_str_tuple(raw, "tools", where="provides"),
        mcp_servers=_str_tuple(raw, "mcpServers", where="provides"),
        hooks=_str_tuple(raw, "hooks", where="provides"),
        settings=_str_tuple(raw, "settings", where="provides"),
    )


def _parse_capabilities(raw: Any) -> Capabilities:
    if not isinstance(raw, dict):
        raise _fail("capabilities must be an object")
    known = {
        "shellBlocks", "hookCommands", "mcpStdio", "pythonTools",
        "network", "filesystemWrites",
    }
    unknown = sorted(set(raw) - known)
    if unknown:
        raise _fail(f"capabilities has unknown key(s): {', '.join(unknown)}")
    return Capabilities(
        shell_blocks=_bool(raw, "shellBlocks"),
        hook_commands=_bool(raw, "hookCommands"),
        mcp_stdio=_bool(raw, "mcpStdio"),
        python_tools=_bool(raw, "pythonTools"),
        network=_str_tuple(raw, "network", where="capabilities"),
        filesystem_writes=_str_tuple(raw, "filesystemWrites", where="capabilities"),
    )


def _parse_integrity(raw: Any) -> Integrity:
    if not isinstance(raw, dict):
        raise _fail("integrity must be an object")
    unknown = sorted(set(raw) - {"algorithm", "files"})
    if unknown:
        raise _fail(f"integrity has unknown key(s): {', '.join(unknown)}")
    algorithm = _str(raw, "algorithm", default="sha256").lower()
    if algorithm not in _DIGEST_LENGTHS:
        raise _fail(
            f"integrity.algorithm {algorithm!r} is not supported "
            f"({', '.join(sorted(_DIGEST_LENGTHS))})"
        )
    files_raw = raw.get("files", {})
    if not isinstance(files_raw, dict):
        raise _fail("integrity.files must be an object of path -> digest")
    files: Dict[str, str] = {}
    for rel, digest in files_raw.items():
        if not isinstance(rel, str):
            raise _fail("integrity.files keys must be strings")
        try:
            # The same path rules extraction enforces: a hash map keyed by
            # ``../../etc/passwd`` would have verification reading — and later
            # code writing — outside the plugin root.
            path = safe_member_path(rel)
        except Exception as exc:
            raise _fail(f"integrity.files path {rel!r} is unsafe: {exc}") from exc
        if not isinstance(digest, str):
            raise _fail(f"integrity.files[{rel!r}] must be a digest string")
        value = digest.split(":", 1)[1] if ":" in digest else digest
        value = value.strip().lower()
        if len(value) != _DIGEST_LENGTHS[algorithm] or not _HEX_RE.match(value):
            raise _fail(f"integrity.files[{rel!r}] is not a {algorithm} digest")
        files[path] = value
    return Integrity(algorithm=algorithm, files=files)


def load_manifest(path: Union[str, Path]) -> Manifest:
    """Read and validate the manifest of a plugin root (or the file itself)."""
    p = Path(path)
    if p.is_dir():
        p = p / MANIFEST_FILENAME
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise _fail(f"cannot read {p} ({exc.strerror or exc})") from exc
    return parse_manifest(raw)


# ---------------------------------------------------------------------------
# Content scanning
# ---------------------------------------------------------------------------


def plugin_content_files(root: Union[str, Path]) -> Dict[str, Tuple[Path, ...]]:
    """Every file that is *content*, grouped by ``provides`` kind.

    README, LICENSE, and ``assets/`` are deliberately absent: they are inert,
    and a conformance check that complained about a LICENSE would train people
    to ignore it."""
    base = Path(root)
    out: Dict[str, Tuple[Path, ...]] = {}
    for kind, (subdir, patterns) in CONTENT_KINDS.items():
        d = base / subdir
        found: list[Path] = []
        if d.is_dir():
            for pattern in patterns:
                found.extend(p for p in sorted(d.glob(pattern)) if p.is_file())
        out[kind] = tuple(sorted(set(found)))
    return out


def _rel(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


#: A shell block is the ``shell:`` list in a definition's frontmatter (see the
#: skills/commands plan): entries with a ``run:`` command executed *before* the
#: body reaches the model. Detection is deliberately coarse — a top-level
#: ``shell:`` key at all, or a ``{{shell.x}}`` substitution in the body — since
#: the failure mode of over-detecting is an author adding one honest line to
#: their manifest, and the failure mode of under-detecting is a command running
#: on a machine whose owner was told none would.
_FRONTMATTER_SHELL_RE = re.compile(r"^shell\s*:", re.MULTILINE)
_SHELL_SUBST_RE = re.compile(r"\{\{\s*shell\.")


def _split_frontmatter(text: str) -> Tuple[str, str]:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[3:end], text[end + 4 :]
    return "", text


def _declares_shell_block(text: str) -> bool:
    frontmatter, body = _split_frontmatter(text)
    return bool(_FRONTMATTER_SHELL_RE.search(frontmatter) or _SHELL_SUBST_RE.search(body))


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover — raced deletion
        return ""


def _json_or_empty(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A malformed declaration file is a conformance question, not a parse
        # question: it declares nothing, so it grants nothing.
        return {}


def _mcp_entries(root: Path) -> Iterable[Tuple[str, Mapping[str, Any]]]:
    data = _json_or_empty(root / _MCP_FILE)
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if isinstance(servers, dict):
        for name, cfg in servers.items():
            if isinstance(cfg, dict):
                yield str(name), cfg


def _hook_entries(root: Path) -> Iterable[Mapping[str, Any]]:
    data = _json_or_empty(root / _HOOKS_FILE)
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if isinstance(hooks, list):
        for h in hooks:
            if isinstance(h, dict):
                yield h


# ---------------------------------------------------------------------------
# Capability conformance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConformanceReport:
    """The whole picture in one object, because the installer needs to render
    one prompt — not stop at the first problem and ask again after each fix."""

    #: (capability, evidence) for surfaces used but not declared. Fatal.
    undeclared_capabilities: Tuple[Tuple[str, str], ...] = ()
    #: Content files absent from ``provides``. Fatal — the prompt never showed
    #: them, so approving the prompt did not approve them.
    undeclared_content: Tuple[str, ...] = ()
    #: Names in ``provides`` with no file behind them. A packaging mistake, not
    #: a security one: it can only make the plugin do less than it claimed.
    missing_content: Tuple[str, ...] = ()
    #: Capabilities declared but unused. Also not fatal, also worth saying.
    overdeclared_capabilities: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.undeclared_capabilities and not self.undeclared_content


def conformance_report(root: Union[str, Path], manifest: Manifest) -> ConformanceReport:
    """Compare what ``root`` contains against what ``manifest`` declared."""
    base = Path(root)
    caps = manifest.capabilities
    used: Dict[str, str] = {}  # capability -> first piece of evidence

    content = plugin_content_files(base)

    for kind in ("skills", "agents", "workflows", "rules"):
        for path in content[kind]:
            if _declares_shell_block(_read(path)):
                used.setdefault("shellBlocks", _rel(base, path))
    for path in content["tools"]:
        used.setdefault("pythonTools", _rel(base, path))
    for name, cfg in _mcp_entries(base):
        # ``command`` means a local process; ``url`` means a remote endpoint.
        if cfg.get("command"):
            used.setdefault("mcpStdio", f"{_MCP_FILE}:{name}")
    for hook in _hook_entries(base):
        if hook.get("command"):
            used.setdefault("hookCommands", f"{_HOOKS_FILE}:{hook.get('event', '?')}")

    declared = {
        "shellBlocks": caps.shell_blocks,
        "pythonTools": caps.python_tools,
        "mcpStdio": caps.mcp_stdio,
        "hookCommands": caps.hook_commands,
    }
    undeclared = tuple(
        (cap, evidence) for cap, evidence in sorted(used.items()) if not declared.get(cap)
    )
    overdeclared = tuple(cap for cap, on in sorted(declared.items()) if on and cap not in used)

    # ``provides`` vs. content, both ways. A file's declared name is its stem
    # (``skills/py-style.md`` -> ``py-style``), or its directory for the
    # ``skills/<slug>/SKILL.md`` layout the rest of Mantis uses.
    undeclared_content: list[str] = []
    missing_content: list[str] = []
    for kind, paths in content.items():
        declared_names = set(getattr(manifest.provides, kind))
        present = {_content_name(base, kind, path): _rel(base, path) for path in paths}
        undeclared_content += [rel for n, rel in sorted(present.items()) if n not in declared_names]
        missing_content += [f"{kind}:{n}" for n in sorted(declared_names - set(present))]

    declared_servers = set(manifest.provides.mcp_servers)
    for name, _cfg in _mcp_entries(base):
        if name not in declared_servers:
            undeclared_content.append(f"{_MCP_FILE}:{name}")

    # Hooks are declared as ``Event:label`` (the plan's ``PostToolUse:format``).
    # Either spelling counts as disclosed — the event alone already tells the
    # user when the hook fires, which is the part that bounds the risk.
    declared_hooks = set(manifest.provides.hooks)
    declared_events = {h.split(":", 1)[0] for h in declared_hooks}
    for hook in _hook_entries(base):
        event = str(hook.get("event", "?"))
        label = "{}:{}".format(event, hook.get("id") or hook.get("name") or "")
        if label not in declared_hooks and event not in declared_events:
            undeclared_content.append(f"{_HOOKS_FILE}:{event}")

    return ConformanceReport(
        undeclared_capabilities=undeclared,
        undeclared_content=tuple(sorted(set(undeclared_content))),
        missing_content=tuple(missing_content),
        overdeclared_capabilities=overdeclared,
    )


def _content_name(base: Path, kind: str, path: Path) -> str:
    subdir = base / CONTENT_KINDS[kind][0]
    rel = path.relative_to(subdir)
    # ``<slug>/SKILL.md`` is named by its directory; everything else by stem.
    return rel.parts[0] if len(rel.parts) > 1 else rel.stem


def verify_capabilities(root: Union[str, Path], manifest: Manifest) -> ConformanceReport:
    """:func:`conformance_report`, raising on anything the approval prompt
    would have failed to disclose."""
    report = conformance_report(root, manifest)
    if report.ok:
        return report
    problems = [f"{cap} used by {evidence}" for cap, evidence in report.undeclared_capabilities]
    problems += [f"{rel} is not listed in provides" for rel in report.undeclared_content]
    raise CapabilityUndeclaredError(
        f"plugin {manifest.name!r} exceeds its declared capabilities: " + "; ".join(problems)
    )


# ---------------------------------------------------------------------------
# Per-file integrity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntegrityReport:
    checked: int = 0
    mismatched: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()
    unlisted: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.mismatched and not self.missing


def file_digest(path: Union[str, Path], algorithm: str = "sha256") -> str:
    """``"<algorithm>:<hex>"`` for one file, streamed so a large asset does not
    have to be resident. The prefixed form is what the manifest stores."""
    h = hashlib.new(algorithm)
    with open(str(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return f"{algorithm}:{h.hexdigest()}"


def integrity_report(root: Union[str, Path], manifest: Manifest) -> IntegrityReport:
    """Verify ``integrity.files`` against what is on disk under ``root``."""
    base = Path(root)
    algorithm = manifest.integrity.algorithm
    listed = manifest.integrity.files
    mismatched: list[str] = []
    missing: list[str] = []
    for rel, expected in sorted(listed.items()):
        p = base / rel
        if not p.is_file():
            missing.append(rel)
            continue
        if file_digest(p, algorithm).split(":", 1)[1] != expected:
            mismatched.append(rel)
    unlisted: list[str] = []
    if listed:
        # Only meaningful once something is listed: an unhashed plugin has not
        # claimed anything about its files, so nothing about it is "extra".
        for kind_paths in plugin_content_files(base).values():
            for p in kind_paths:
                rel = _rel(base, p)
                if rel not in listed:
                    unlisted.append(rel)
    return IntegrityReport(
        checked=len(listed),
        mismatched=tuple(mismatched),
        missing=tuple(missing),
        unlisted=tuple(sorted(unlisted)),
    )


def verify_integrity(
    root: Union[str, Path], manifest: Manifest, *, allow_unlisted: bool = False
) -> IntegrityReport:
    """Raise :class:`IntegrityError` unless every hashed file matches.

    ``allow_unlisted`` exists because content added *after* the hashes were
    generated is a real, non-malicious authoring mistake — but the default is
    to refuse it, since "the hashes covered everything except the file the
    attacker added" is not integrity.
    """
    report = integrity_report(root, manifest)
    bad = list(report.mismatched) + list(report.missing)
    if not allow_unlisted:
        bad += list(report.unlisted)
    if bad:
        raise IntegrityError(
            f"plugin {manifest.name!r} failed integrity verification: "
            + ", ".join(
                [f"{r} (modified)" for r in report.mismatched]
                + [f"{r} (missing)" for r in report.missing]
                + ([] if allow_unlisted else [f"{r} (not in manifest)" for r in report.unlisted])
            )
        )
    return report
