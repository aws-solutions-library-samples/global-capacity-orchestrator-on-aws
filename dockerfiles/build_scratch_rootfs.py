"""Assemble the distroless runtime rootfs the service images copy onto scratch.

Every service Dockerfile in this directory is a two-stage build: a
``python:X.Y.Z-slim`` builder installs the locked dependency set, applies the
APT security patches, and precompiles the app tree; then this script stages a
minimal root filesystem under ``/rootfs`` which the final ``FROM scratch``
stage copies wholesale. The deployed image therefore contains no shell, no
package manager, no coreutils — only the CPython runtime, the service's
site-packages, the application tree, and the exact shared libraries those
binaries link against.

Why hand-assembled scratch instead of gcr.io/distroless:
- The platform pins CPython 3.14.6; no distroless base ships it (Google's
  ``distroless/python3`` tracks Debian's interpreter, currently 3.13).
- Copying the ELF closure from the *patched* builder keeps the existing
  APT_SECURITY_EPOCH workflow meaningful: ``apt-get upgrade`` in the builder
  is what patches the glibc/OpenSSL bits that actually ship.
- No second upstream registry: ``python:<pin>-slim`` remains the only base
  image dependency, watched by the existing Dependabot docker config.

Scanner visibility is preserved deliberately: for every Debian package that
owns a copied file, its dpkg status paragraph is written to
``/var/lib/dpkg/status.d/<package>`` (the distroless convention Trivy reads)
and ``/etc/os-release`` is carried over, so ``security:trivy:container-scan``
keeps flagging CVEs in the shipped libraries instead of going blind. Each
package's ``/usr/share/doc/<pkg>/copyright`` ships too (license compliance
for the redistributed Debian bits).

The script is stdlib-only, runs as root inside the builder stage, and fails
loudly: an unresolvable ``ldd`` entry, a library with no owning package, or a
missing trust anchor each abort the image build rather than surfacing as a
crash-looping pod. The final stage additionally runs an exec-form import
smoke as the runtime user, so a closure gap in a *new* dependency breaks the
build, not the deployment.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

ROOTFS = Path("/rootfs")
USR_LOCAL = Path("/usr/local")
APP_TREE = Path("/app/gco")
DPKG_STATUS = Path("/var/lib/dpkg/status")

# Runtime identity baked into the synthesized /etc/passwd. Matches the
# runAsUser/runAsGroup 1000 enforced by every pod securityContext.
RUNTIME_USER = "gco"
RUNTIME_UID = 1000
RUNTIME_HOME = "/home/gco"

# dlopen'd by glibc for thread-cancellation unwinding; never appears as a
# DT_NEEDED of CPython, so ldd cannot discover it. Seeded explicitly.
FORCED_LIBS = ("libgcc_s.so.1",)
# Legacy NSS plugins. glibc 2.41 (trixie) has files/dns builtin, but ship
# them when present so name resolution keeps working even if the base image
# ever reverts to plugin-based lookup.
OPTIONAL_NSS_LIBS = ("libnss_files.so.2", "libnss_dns.so.2")

_LDD_RESOLVED = re.compile(r"^\s*\S+\s+=>\s+(/\S+)\s+\(0x[0-9a-f]+\)\s*$")
_LDD_DIRECT = re.compile(r"^\s*(/\S+)\s+\(0x[0-9a-f]+\)\s*$")


def fail(message: str) -> None:
    print(f"build_scratch_rootfs: ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def multiarch_dir() -> Path:
    triplet = sysconfig.get_config_var("MULTIARCH")
    if not triplet:
        fail("sysconfig reports no MULTIARCH triplet")
    return Path("/usr/lib") / str(triplet)


# Debian is merged-/usr: /lib, /lib64, /bin, /sbin are symlinks into /usr and
# every file physically lives there. ldd reports the alias paths (the kernel's
# PT_INTERP is /lib64/ld-linux-*.so.*), so staged paths are normalized into
# /usr and the aliases are recreated as symlinks, mirroring the builder.
_MERGED_USR_ALIASES = ("lib", "lib64", "bin", "sbin")


def canonical_usr_path(source: Path) -> Path:
    """Rewrite a merged-/usr alias path (/lib/..., /bin/...) to its /usr form."""
    parts = source.relative_to("/").parts
    if parts and parts[0] in _MERGED_USR_ALIASES:
        return Path("/usr").joinpath(*parts)
    return source


def stage_path(source: Path) -> Path:
    return ROOTFS / canonical_usr_path(source).relative_to("/")


def copy_file(source: Path) -> None:
    """Copy one regular file into the rootfs, preserving mode."""
    destination = stage_path(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copy2(source, destination, follow_symlinks=False)


def replicate_symlink_chain(path: Path) -> Path:
    """Recreate ``path`` in the rootfs, link by link, returning the real file.

    Debian SONAME paths are symlink chains (e.g. ``libz.so.1`` ->
    ``libz.so.1.3.1``); the dynamic linker resolves the chain at runtime, so
    every hop must exist in the final image exactly as it does in the builder.
    """
    current = path
    for _ in range(16):
        destination = stage_path(current)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if current.is_symlink():
            target = os.readlink(current)
            if not destination.is_symlink():
                destination.symlink_to(target)
            current = Path(os.path.normpath(current.parent / target))
            continue
        copy_file(current)
        return current
    fail(f"symlink chain too deep at {path}")
    raise AssertionError  # unreachable


def seed_binaries() -> list[Path]:
    """Every ELF object whose dependency closure must ship."""
    seeds = [USR_LOCAL / "bin" / f"python{sys.version_info.major}.{sys.version_info.minor}"]
    # Stdlib C extensions (drives libsqlite3, liblzma, libffi, libssl, ...).
    seeds += sorted((USR_LOCAL / "lib").glob("python*/lib-dynload/*.so"))
    # libpython itself plus every compiled site-packages extension. manylinux
    # policy caps their externals at glibc/libgcc/libstdc++, but the closure
    # is computed from the actual binaries rather than trusting the policy.
    seeds += sorted((USR_LOCAL / "lib").glob("libpython*.so*"))
    seeds += sorted((USR_LOCAL / "lib").glob("python*/site-packages/**/*.so*"))
    lib_dir = multiarch_dir()
    for name in FORCED_LIBS:
        forced = lib_dir / name
        if not forced.exists():
            fail(f"forced library missing from builder: {forced}")
        seeds.append(forced)
    for name in OPTIONAL_NSS_LIBS:
        optional = lib_dir / name
        if optional.exists():
            seeds.append(optional)
    return [seed for seed in seeds if not seed.is_dir()]


def resolve_closure(seeds: list[Path]) -> set[Path]:
    """Union of ldd-resolved shared-object paths across all seeds.

    Seeds are ldd'd in chunks with per-file attribution. A stdlib
    ``lib-dynload`` extension with unresolvable dependencies is skipped with a
    notice instead of failing: ``python:*-slim`` itself ships such modules
    (``_tkinter`` links libtk/libX11, which slim never installs), so they are
    equally unimportable in today's images — excluding them preserves parity.
    Unresolved dependencies anywhere else (interpreter, libpython,
    site-packages, forced seeds) abort the build: those are objects the
    services can actually reach at runtime.
    """
    per_seed_libs: dict[str, set[Path]] = {}
    per_seed_missing: dict[str, list[str]] = {}
    for start in range(0, len(seeds), 64):
        chunk = [str(path) for path in seeds[start : start + 64]]
        result = subprocess.run(["ldd", *chunk], capture_output=True, text=True, check=False)
        # With multiple arguments ldd prefixes each file's section with a
        # "<path>:" header; with a single argument it prints none.
        current = chunk[0]
        for line in result.stdout.splitlines():
            if line.startswith("/") and line.rstrip().endswith(":"):
                current = line.rstrip().rstrip(":")
                continue
            if "not a dynamic executable" in line or "statically linked" in line:
                continue
            if "not found" in line:
                per_seed_missing.setdefault(current, []).append(line.strip())
                continue
            match = _LDD_RESOLVED.match(line) or _LDD_DIRECT.match(line)
            if match:
                per_seed_libs.setdefault(current, set()).add(Path(match.group(1)))

    resolved: set[Path] = set()
    for seed, libraries in per_seed_libs.items():
        if seed not in per_seed_missing:
            resolved.update(libraries)
    for seed, missing in sorted(per_seed_missing.items()):
        if "/lib-dynload/" in seed:
            print(
                f"build_scratch_rootfs: skipping stdlib extension already "
                f"broken in the builder image: {Path(seed).name} "
                f"(missing: {', '.join(missing)})"
            )
        else:
            fail(f"unresolved shared library dependency in {seed}: {missing}")
    if not resolved:
        fail("ldd resolved no shared libraries; closure computation broke")
    return resolved


def owning_packages(real_files: set[Path]) -> set[str]:
    """Map copied real files to the Debian packages that own them."""
    packages: set[str] = set()
    unmatched: set[Path] = set()
    # dpkg's database records merged-/usr files under /usr; query that form.
    paths = sorted({str(canonical_usr_path(path)) for path in real_files})
    for start in range(0, len(paths), 64):
        chunk = paths[start : start + 64]
        result = subprocess.run(["dpkg", "-S", *chunk], capture_output=True, text=True, check=False)
        matched_in_chunk: set[str] = set()
        for line in result.stdout.splitlines():
            head, separator, path = line.partition(": ")
            if not separator or "diversion" in head:
                continue
            matched_in_chunk.add(path.strip())
            for name in head.split(","):
                packages.add(name.strip().split(":")[0])
        unmatched.update(Path(path) for path in chunk if path not in matched_in_chunk)
    orphans = {path for path in unmatched if not str(path).startswith("/usr/local/")}
    if orphans:
        # /usr/local is CPython + wheels (not dpkg-owned, same as today's
        # images); anything else without provenance is a hard error.
        fail(f"copied libraries with no owning Debian package: {sorted(orphans)}")
    return packages


def write_dpkg_metadata(packages: set[str]) -> None:
    """Emit distroless-style /var/lib/dpkg/status.d entries + copyright files.

    Trivy identifies Debian packages in shell-less images from status.d;
    omitting this would silently exempt the shipped glibc/OpenSSL from the CI
    container scan, which is the opposite of the point.
    """
    paragraphs: dict[str, str] = {}
    for paragraph in DPKG_STATUS.read_text(encoding="utf-8").split("\n\n"):
        match = re.search(r"^Package:\s*(\S+)", paragraph, re.MULTILINE)
        if match:
            paragraphs[match.group(1)] = paragraph.strip() + "\n"
    status_dir = stage_path(Path("/var/lib/dpkg/status.d"))
    status_dir.mkdir(parents=True, exist_ok=True)
    for package in sorted(packages):
        if package not in paragraphs:
            fail(f"package {package} owns shipped files but has no status paragraph")
        (status_dir / package).write_text(paragraphs[package], encoding="utf-8")
        copyright_file = Path("/usr/share/doc") / package / "copyright"
        if copyright_file.exists():
            destination = stage_path(copyright_file)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(copyright_file, destination)
        else:
            fail(f"missing license text for redistributed package: {copyright_file}")


def copy_trust_and_time() -> None:
    """CA trust anchors (TLS to AWS APIs) and zoneinfo."""
    # Dereference the hashed-symlink farm into real files so nothing dangles
    # (the links point into /usr/share/ca-certificates, which does not ship).
    shutil.copytree("/etc/ssl/certs", stage_path(Path("/etc/ssl/certs")), symlinks=False)
    for config in (Path("/etc/ssl/openssl.cnf"),):
        if config.exists():
            copy_file(config)
    # OpenSSL's compiled-in OPENSSLDIR: replicate its symlinks verbatim; their
    # /etc/ssl targets were materialized above.
    ssl_dir = Path("/usr/lib/ssl")
    for entry in ssl_dir.iterdir():
        destination = stage_path(entry)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if entry.is_symlink():
            destination.symlink_to(os.readlink(entry))
        elif entry.is_file():
            copy_file(entry)
    stage_path(Path("/etc/ssl/private")).mkdir(mode=0o700, parents=True, exist_ok=True)
    bundle = stage_path(Path("/etc/ssl/certs/ca-certificates.crt"))
    if not bundle.exists() or bundle.stat().st_size == 0:
        fail("CA bundle missing or empty after staging")

    shutil.copytree("/usr/share/zoneinfo", stage_path(Path("/usr/share/zoneinfo")), symlinks=True)
    stage_path(Path("/etc/localtime")).symlink_to("/usr/share/zoneinfo/Etc/UTC")
    stage_path(Path("/etc/timezone")).write_text("Etc/UTC\n", encoding="utf-8")


def write_identity_and_os_metadata() -> None:
    """Minimal NSS database, os-release, and top-level filesystem shape."""
    etc = stage_path(Path("/etc"))
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "passwd").write_text(
        "root:x:0:0:root:/root:/usr/sbin/nologin\n"
        f"{RUNTIME_USER}:x:{RUNTIME_UID}:{RUNTIME_UID}:{RUNTIME_USER}:"
        f"{RUNTIME_HOME}:/usr/sbin/nologin\n",
        encoding="utf-8",
    )
    (etc / "group").write_text(f"root:x:0:\n{RUNTIME_USER}:x:{RUNTIME_UID}:\n", encoding="utf-8")
    (etc / "nsswitch.conf").write_text(
        "passwd: files\ngroup: files\nhosts: files dns\n", encoding="utf-8"
    )
    # OS identification for scanners and humans. Trivy's Debian detection
    # keys on /etc/debian_version (verified empirically — os-release alone
    # yields family "none" and silently disables the dpkg CVE mapping);
    # os-release ships too, as a regular file at both canonical paths
    # (Debian's /etc symlink form is invisible to scanners that read tar
    # layers without symlink resolution).
    copy_file(Path("/etc/debian_version"))
    copy_file(Path("/usr/lib/os-release"))
    shutil.copy2("/usr/lib/os-release", etc / "os-release", follow_symlinks=True)

    # Merged-/usr symlinks. The kernel resolves PT_INTERP
    # (/lib64/ld-linux-*.so.*) through these; without them nothing executes.
    for alias in _MERGED_USR_ALIASES:
        if Path("/", alias).is_symlink() and (ROOTFS / f"usr/{alias}").exists():
            (ROOTFS / alias).symlink_to(f"usr/{alias}")

    home = stage_path(Path(RUNTIME_HOME))
    home.mkdir(parents=True, exist_ok=True)
    for scratch_dir in ("tmp", "var/tmp"):
        path = ROOTFS / scratch_dir
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o1777)


def main() -> None:
    if ROOTFS.exists():
        fail("/rootfs already exists; refusing to assemble over prior state")

    # The interpreter, stdlib, and site-packages, minus ensurepip: pip itself
    # is uninstalled by the Dockerfile, and dropping ensurepip's bundled pip
    # wheel keeps "reinstall the installer" out of reach at runtime too.
    shutil.copytree(
        USR_LOCAL,
        stage_path(USR_LOCAL),
        symlinks=True,
        ignore=shutil.ignore_patterns("ensurepip"),
    )
    # The precompiled application tree (sole content of /app besides cwd).
    shutil.copytree(APP_TREE, stage_path(APP_TREE), symlinks=True)

    closure = resolve_closure(seed_binaries())
    real_files: set[Path] = set()
    for library in sorted(closure):
        real_files.add(replicate_symlink_chain(library))

    write_identity_and_os_metadata()
    copy_trust_and_time()

    packages = owning_packages(real_files)
    # Always attribute the non-library payloads staged above.
    packages.update({"ca-certificates", "tzdata", "base-files"})
    write_dpkg_metadata(packages)

    # Pre-warm the dynamic linker cache for the staged tree (ld.so falls back
    # to default path search without it, but the cache is free to generate).
    subprocess.run(["ldconfig", "-r", str(ROOTFS)], check=True)

    interpreter = [path for path in closure if "ld-linux" in path.name]
    if not interpreter:
        fail("dynamic linker never entered the closure")
    print(
        f"build_scratch_rootfs: staged {len(closure)} shared objects from "
        f"{len(packages)} Debian packages: {' '.join(sorted(packages))}"
    )


if __name__ == "__main__":
    main()
