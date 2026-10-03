"""Optional bounded Python profile behind the existing local repair executor.

No platform inspection runs at import. This is an import/runtime input proof,
not an OS sandbox: executed project code retains approved host-user authority.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import sysconfig
import time

PROFILE = "repo-python-pytest-publication-v1"
PACKAGES = ("pytest", "_pytest", "pluggy", "packaging", "iniconfig", "pygments")
BOUNDS = {"max_bytes": 96 * 1024 * 1024, "max_files": 8000, "max_depth": 16, "max_file_bytes": 48 * 1024 * 1024, "max_seconds": 15}

# Every startup/import path is copied and checked before the child starts.
# Only fixed internal bootstrap arguments precede the approved test arguments.
BOOTSTRAP = """import os,sys,json,hashlib
from pathlib import Path
root=Path(sys.executable).parent.parent
stdlib=root/'lib'/('python'+str(sys.version_info.major)+'.'+str(sys.version_info.minor))
sys.path[:]=[str(stdlib),str(stdlib/'lib-dynload'),str(root/'packages')]
expected=json.loads(sys.argv[1])
maps=Path('/proc/self/maps').read_text().splitlines()
library=root/'lib'/expected['libpython_name']
metadata=library.stat()
rows=[line.split(maxsplit=5) for line in maps if 'libpython' in line]
if not rows or any(len(row)!=6 or row[5]!=str(library) or int(row[4])!=metadata.st_ino or row[3]!=f'{os.major(metadata.st_dev):02x}:{os.minor(metadata.st_dev):02x}' for row in rows):
    raise SystemExit('actual copied libpython binding unavailable')
if hashlib.sha256(library.read_bytes()).hexdigest()!=expected['libpython_sha256']:
    raise SystemExit('actual copied libpython bytes changed')
import types,_pytest._py.error,_pytest._py.path
py=types.ModuleType('py')
py.error=_pytest._py.error; py.path=_pytest._py.path
sys.modules['py']=py; sys.modules['py.error']=py.error; sys.modules['py.path']=py.path
import pytest
if not Path(pytest.__file__).is_relative_to(root/'packages'):
    raise SystemExit('trusted pytest origin unavailable')
origins={name:getattr(module,'__file__',None) for name,module in sys.modules.items() if getattr(module,'__file__',None)}
if any(not Path(path).is_relative_to(root) for path in origins.values()):
    raise SystemExit('undeclared trusted runner import origin')
def import_audit(event,args):
    if event=='import' and len(args)>1 and args[1] and not any(Path(args[1]).is_relative_to(allowed) for allowed in (root,Path(os.getcwd()))):
        raise ImportError('module import outside declared runtime/project roots')
sys.addaudithook(import_audit)
proof={'loaded_libpython_path':str(library),'loaded_libpython_sha256':expected['libpython_sha256'],'loaded_libpython_device':metadata.st_dev,'loaded_libpython_inode':metadata.st_ino,'pytest_origin':pytest.__file__,'py_compatibility':'fixed_bootstrap_facade_from_attested_pytest','trusted_runner_origins':origins,'import_roots':list(sys.path),'effective_environment':dict(os.environ),'bootstrap_sha256':expected['bootstrap_sha256']}
proof_path=Path(sys.argv[2])
descriptor=os.open(proof_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
with os.fdopen(descriptor,'w') as handle: json.dump(proof,handle,sort_keys=True)
sys.path.insert(0,os.getcwd())
raise SystemExit(pytest.main(['-p','no:cacheprovider',*sys.argv[3:]]))
"""


class RuntimeUnavailable(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def posture_projection(proof, configuration_revision):
    """Canonical scalar authority; the full captured proof stays private."""
    if not isinstance(proof, dict) or proof.get("available") is not True or proof.get("profile") != PROFILE:
        raise RuntimeUnavailable("publication_runtime_full_proof_unavailable")
    bounds = proof.get("runtime_bounds")
    if not isinstance(bounds, dict) or set(bounds) != set(BOUNDS) or any(type(bounds[key]) is not int or bounds[key] != limit for key, limit in BOUNDS.items()):
        raise RuntimeUnavailable("publication_runtime_bounds_invalid")
    for key, maximum in (("runtime_bytes", BOUNDS["max_bytes"]), ("runtime_files", BOUNDS["max_files"])):
        if type(proof.get(key)) is not int or not 0 < proof[key] <= maximum:
            raise RuntimeUnavailable("publication_runtime_bounds_invalid")
    hashes = ("runtime_closure_sha256", "bootstrap_sha256", "helper_sha256", "interpreter_sha256", "libpython_sha256", "link_provenance_sha256", "source_metadata_sha256")
    if any(not isinstance(proof.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", proof[key]) is None for key in hashes) or not isinstance(configuration_revision, str) or re.fullmatch(r"[0-9a-f]{64}", configuration_revision) is None:
        raise RuntimeUnavailable("publication_runtime_digest_invalid")
    disabled = ("site_enabled", "user_site_enabled", "ambient_packages_enabled", "plugins_autoload")
    if any(proof.get(key) is not False for key in disabled) or proof.get("isolation_claim") != "none" or proof.get("network_isolation") != "not_verified" or proof.get("source_trust") != "selected_host_owner_and_primary_group" or proof.get("same_owner_group_containment") != "not_claimed" or not isinstance(proof.get("loaded_library"), dict):
        raise RuntimeUnavailable("publication_runtime_posture_invalid")
    template = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HOME": "job_owned_home", "TMPDIR": "job_owned_tmp", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    return {
        "publication_runtime_proof_sha256": digest(proof),
        **{key: proof[key] for key in hashes},
        "actual_loaded_library_provenance_sha256": digest(proof["loaded_library"]),
        "environment_template_sha256": digest(template),
        "configuration_revision": configuration_revision,
        "runtime_bytes": proof["runtime_bytes"], "runtime_files": proof["runtime_files"],
        **{"runtime_" + key: value for key, value in BOUNDS.items()},
        **{key: False for key in disabled},
        "runtime_proof_available": True,
        "source_trust": "selected_host_owner_and_primary_group",
        "same_owner_group_containment": "not_claimed",
        "copied_inputs_immutable": True,
    }


def metadata(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink, value.st_uid, value.st_gid, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _open(path):
    # uv's trusted installed package files legitimately share cache inodes.
    # This source-runtime reader captures/checks that provenance; the project
    # snapshot walker still rejects hardlinks and is not changed here.
    from src.execution.repo_worker import _open_directory_descriptor
    parent = _open_directory_descriptor(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            os.close(descriptor)
            raise RuntimeUnavailable("publication_runtime_entry_invalid")
        return descriptor, info
    finally:
        os.close(parent)


def _read(path, *, deadline):
    if time.monotonic() >= deadline:
        raise RuntimeUnavailable("publication_runtime_deadline")
    descriptor, before = _open(path)
    group_untrusted = bool(before.st_mode & 0o020) and not (before.st_uid == os.getuid() and before.st_gid == os.getgid())
    if before.st_size > BOUNDS["max_file_bytes"] or before.st_uid not in {0, os.getuid()} or before.st_mode & 0o002 or group_untrusted:
        os.close(descriptor)
        raise RuntimeUnavailable("publication_runtime_file_untrusted")
    with os.fdopen(descriptor, "rb") as handle:
        # The descriptor already proves a finite ordinary-file size. Reading
        # that size plus one detects growth without allocating 48 MiB for
        # every small stdlib/package file in the exposed closure.
        raw = handle.read(before.st_size + 1)
        if metadata(before) != metadata(os.fstat(handle.fileno())):
            raise RuntimeUnavailable("publication_runtime_file_changed")
    if len(raw) > BOUNDS["max_file_bytes"] or time.monotonic() >= deadline:
        raise RuntimeUnavailable("publication_runtime_bound")
    return raw, before


def resolve_entry(path, roots):
    """Retain verified final-file link provenance; no linked parent traversal."""
    from src.execution.repo_worker import _open_directory_descriptor
    current = Path(os.path.abspath(path))
    links = []
    for _ in range(8):
        if not any(current.is_relative_to(root) for root in roots):
            raise RuntimeUnavailable("publication_runtime_link_escape")
        parent = _open_directory_descriptor(current.parent)
        try:
            before = os.stat(current.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISLNK(before.st_mode):
                if not stat.S_ISREG(before.st_mode):
                    raise RuntimeUnavailable("publication_runtime_entry_invalid")
                return current, links
            target = os.readlink(current.name, dir_fd=parent)
            after = os.stat(current.name, dir_fd=parent, follow_symlinks=False)
            if metadata(before) != metadata(after):
                raise RuntimeUnavailable("publication_runtime_link_changed")
            links.append({"path": str(current), "target": target, "identity": list(metadata(before))})
            current = Path(os.path.abspath(target if os.path.isabs(target) else current.parent / target))
        finally:
            os.close(parent)
    raise RuntimeUnavailable("publication_runtime_link_depth")


def loaded_libpython(path):
    """Prove the library loaded by this selected interpreter, not its name."""
    if sys.platform != "linux":
        raise RuntimeUnavailable("publication_loaded_library_proof_unavailable")
    descriptor, before = _open(path)
    os.close(descriptor)
    rows = [line.split(maxsplit=5) for line in Path("/proc/self/maps").read_text().splitlines() if "libpython" in line]
    device = f"{os.major(before.st_dev):02x}:{os.minor(before.st_dev):02x}"
    if not rows or any(len(row) != 6 or row[5] != str(path) or row[3] != device or int(row[4]) != before.st_ino for row in rows):
        raise RuntimeUnavailable("publication_loaded_library_identity_unproven")
    return {"path": str(path), "device": before.st_dev, "inode": before.st_ino}


def capture(*, deadline_at=None):
    """Capture all intended exposed bytes, with finite independent bounds."""
    deadline = min(deadline_at or float("inf"), time.monotonic() + BOUNDS["max_seconds"])
    base = Path(sys.base_prefix).absolute()
    entry = Path(sys.executable).absolute()
    roots = [base, entry.parent.parent]
    executable, executable_links = resolve_entry(entry, roots)
    library, library_links = resolve_entry(Path(sysconfig.get_config_var("LIBDIR")) / sysconfig.get_config_var("LDLIBRARY"), roots)
    loaded = loaded_libpython(library)
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    sources = [(executable, "bin/python"), (library, "lib/" + library.name)]
    trees = [(Path(sysconfig.get_path("stdlib")), "lib/" + version)]
    for package in PACKAGES:
        spec = importlib.util.find_spec(package)
        if not spec or not spec.origin:
            raise RuntimeUnavailable("publication_runtime_package_unavailable")
        trees.append((Path(spec.origin).parent.absolute(), "packages/" + package))
    from src.execution.repo_worker import _open_directory_descriptor
    for root, target in trees:
        descriptor = _open_directory_descriptor(root)
        os.close(descriptor)
        for directory, dirs, files in os.walk(root, followlinks=False):
            relative_dir = Path(directory).relative_to(root)
            if len(relative_dir.parts) > BOUNDS["max_depth"] or time.monotonic() >= deadline:
                raise RuntimeUnavailable("publication_runtime_bound")
            dirs[:] = sorted(name for name in dirs if name not in {"__pycache__", "site-packages", "dist-packages"})
            for name in dirs:
                if (Path(directory) / name).is_symlink():
                    raise RuntimeUnavailable("publication_runtime_ambient_exposure")
            for name in sorted(files):
                if name.endswith((".pyc", ".pyo")):
                    continue
                path = Path(directory) / name
                if path.is_symlink() or name.endswith(".pth"):
                    raise RuntimeUnavailable("publication_runtime_link_or_pth_unsupported")
                sources.append((path, target + "/" + path.relative_to(root).as_posix()))
                if len(sources) > BOUNDS["max_files"]:
                    raise RuntimeUnavailable("publication_runtime_bound")
    records, files, total = [], [], 0
    for source, target in sources:
        raw, info = _read(source, deadline=deadline)
        total += len(raw)
        if total > BOUNDS["max_bytes"]:
            raise RuntimeUnavailable("publication_runtime_bound")
        item = {"path": target, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "mode": "100755" if info.st_mode & 0o111 else "100644"}
        files.append(item)
        records.append({"source": str(source), "identity": list(metadata(info)), "file": item})
    files.sort(key=lambda item: item["path"])
    proof = {"available": True, "profile": PROFILE, "runtime_binding": "bounded_exposed_python_closure", "runtime_closure_sha256": digest(files), "source_metadata_sha256": digest([{key: record[key] for key in ("source", "identity")} for record in records]), "source_trust": "selected_host_owner_and_primary_group", "same_owner_group_containment": "not_claimed", "copied_file_modes": ["0400", "0500"], "runtime_files": len(files), "runtime_bytes": total, "runtime_bounds": BOUNDS, "bootstrap_sha256": hashlib.sha256(BOOTSTRAP.encode()).hexdigest(), "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "interpreter_sha256": next(item["sha256"] for item in files if item["path"] == "bin/python"), "libpython_name": library.name, "libpython_sha256": next(item["sha256"] for item in files if item["path"] == "lib/" + library.name), "link_provenance_sha256": digest(executable_links + library_links), "loaded_library": loaded, "site_enabled": False, "user_site_enabled": False, "ambient_packages_enabled": False, "plugins_autoload": False, "network_isolation": "not_verified", "isolation_claim": "none"}
    return {"proof": proof, "records": records, "links": executable_links + library_links}


def materialize(root, captured, *, deadline_at):
    from src.execution.repo_worker import _open_directory_descriptor
    root.mkdir(mode=0o700)
    deadline = min(deadline_at, time.monotonic() + BOUNDS["max_seconds"])
    for link in captured["links"]:
        path = Path(link["path"])
        parent = _open_directory_descriptor(path.parent)
        try:
            if list(metadata(os.stat(path.name, dir_fd=parent, follow_symlinks=False))) != link["identity"] or os.readlink(path.name, dir_fd=parent) != link["target"]:
                raise RuntimeUnavailable("publication_runtime_link_changed")
        finally:
            os.close(parent)
    for record in captured["records"]:
        item = record["file"]
        raw, info = _read(Path(record["source"]), deadline=deadline)
        if list(metadata(info)) != record["identity"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise RuntimeUnavailable("publication_runtime_copy_source_changed")
        target = root / item["path"]
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o500 if item["mode"] == "100755" else 0o400)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
    # CPython's linked SONAME must resolve to the exact copied target. The
    # source alias text/provenance was verified above; this is job-owned.
    soname = sysconfig.get_config_var("LDLIBRARY")
    if soname != captured["proof"]["libpython_name"]:
        (root / "lib" / soname).symlink_to(captured["proof"]["libpython_name"])
    verify(root, captured, deadline_at=deadline_at)


def verify(root, captured, *, deadline_at):
    deadline = min(deadline_at, time.monotonic() + BOUNDS["max_seconds"])
    seen = []
    for record in captured["records"]:
        item = record["file"]
        raw, info = _read(root / item["path"], deadline=deadline)
        if info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != (0o500 if item["mode"] == "100755" else 0o400):
            raise RuntimeUnavailable("publication_runtime_copy_permissions_changed")
        seen.append({"path": item["path"], "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "mode": "100755" if info.st_mode & 0o111 else "100644"})
    if digest(sorted(seen, key=lambda item: item["path"])) != captured["proof"]["runtime_closure_sha256"]:
        raise RuntimeUnavailable("publication_runtime_materialization_changed")
    # Extra files can affect imports even if every declared file still matches.
    expected = {record["file"]["path"] for record in captured["records"]}
    alias = "lib/" + sysconfig.get_config_var("LDLIBRARY")
    if alias not in expected:
        expected.add(alias)
        path = root / alias
        if not path.is_symlink() or os.readlink(path) != captured["proof"]["libpython_name"]:
            raise RuntimeUnavailable("publication_runtime_alias_changed")
    actual = set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        if len(Path(directory).relative_to(root).parts) > BOUNDS["max_depth"] or time.monotonic() >= deadline:
            raise RuntimeUnavailable("publication_runtime_bound")
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise RuntimeUnavailable("publication_runtime_extra_link")
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() and relative != alias:
                raise RuntimeUnavailable("publication_runtime_extra_link")
            actual.add(relative)
            if len(actual) > BOUNDS["max_files"] + 1:
                raise RuntimeUnavailable("publication_runtime_bound")
    if actual != expected:
        raise RuntimeUnavailable("publication_runtime_extra_input")


def child_environment(stage):
    # Construct, never filter/copy ambient ENV. Paths are server-owned roles.
    return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HOME": str(stage / "home"), "TMPDIR": str(stage / "tmp"), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}


def argv(root, captured, proof_path, test_args):
    context = {key: captured["proof"][key] for key in ("libpython_name", "libpython_sha256", "bootstrap_sha256")}
    return [str(root / "bin/python"), "-I", "-S", "-B", "-c", BOOTSTRAP, json.dumps(context, sort_keys=True), str(proof_path), *test_args]
