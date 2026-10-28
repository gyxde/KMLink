"""Build on the target operating system and architecture, without global installs."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
from importlib import metadata
import inspect
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parent.parent


def filter_windows_system_icu(binaries):
    """Keep Windows' own ICU shims when Qt imports their unversioned API."""
    from pathlib import Path
    import re
    import pefile

    entries = list(binaries)
    qt_core = next((source for target, source, _ in entries
                    if Path(target).name.lower() == "qt6core.dll"), None)
    if qt_core is None:
        return entries
    with pefile.PE(str(qt_core), fast_load=True) as image:
        image.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
        system_icu = any(
            library.dll.lower() in {b"icuuc.dll", b"icuin.dll", b"icu.dll"}
            and any(symbol.name and not re.search(rb"_\d+$", symbol.name)
                    for symbol in library.imports)
            for library in getattr(image, "DIRECTORY_ENTRY_IMPORT", [])
        )
    if not system_icu:
        return entries
    # PyInstaller may resolve icuuc.dll from PATH to a third-party ICU build
    # exporting versioned symbols (e.g. ucnv_open_78), instead of Windows'
    # unversioned ucnv_open API. Do not bundle or copy system ICU; let Windows
    # resolve its own shims. Versioned-ICU Qt builds retain their bundled ICU.
    return [entry for entry in entries
            if not (Path(entry[0]).name.lower() in {"icuuc.dll", "icuin.dll", "icu.dll"}
                    or re.fullmatch(r"icudt.*\.dll", Path(entry[0]).name.lower()))]


def default_notice_dir() -> Path:
    if sys.platform == "darwin":
        return ROOT / "dist" / "KMLink.app" / "Contents" / "Resources" / "licenses"
    return ROOT / "dist" / "KMLink" / "licenses"


def finalize_macos_bundle(notice_dir: Path) -> None:
    """Reseal resources added after PyInstaller signed the macOS app.

    PyInstaller has already signed nested binaries. Re-sign only the outer
    bundle after adding the license files, then verify every nested signature.
    An ad-hoc signature preserves local executability; it is not notarization.
    """
    if sys.platform != "darwin":
        return
    app = ROOT / "dist" / "KMLink.app"
    if not Path(notice_dir).resolve().is_relative_to(app.resolve()):
        return
    if not (app / "Contents" / "MacOS" / "KMLink").is_file():
        raise FileNotFoundError("Build KMLink.app before adding notices inside its bundle.")
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)


def write_notice_bundle(destination: Path | str | None = None) -> Path:
    """Copy installed runtime licenses after a build, without network access.

    The installed distribution metadata is the version authority. The JSON
    inventory contains only generated files and stable hashes, not timestamps
    or absolute paths. Existing unrelated files in the destination are kept.
    """
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    output = Path(destination) if destination is not None else default_notice_dir()
    output.mkdir(parents=True, exist_ok=True)
    components = []
    generated = set()
    warnings = []

    def copy_file(source: Path, target: Path) -> str:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        relative = target.relative_to(output).as_posix()
        generated.add(relative)
        return relative

    copy_file(ROOT / "LICENSE", output / "KMLink-MIT-LICENSE.txt")
    supplementary = ROOT / "packaging" / "notices"
    for source in sorted(supplementary.iterdir()):
        if source.is_file():
            copy_file(source, output / "supplementary" / source.name)

    pending = ["PySide6-Essentials", "pynput", "cryptography"]
    visited = set()
    while pending:
        requested = pending.pop(0)
        key = canonicalize_name(requested)
        if key in visited:
            continue
        visited.add(key)
        distribution = metadata.distribution(requested)
        info = distribution.metadata
        name = info.get("Name", requested)
        package_id = canonicalize_name(name)
        version = distribution.version
        license_files = []
        for entry in sorted(distribution.files or [], key=str):
            relative = Path(str(entry))
            if ".." in relative.parts:
                continue
            basename = relative.name.lower()
            if not re.match(r"^(licen[cs]e|copying|notice)", basename):
                continue
            if relative.suffix.lower() not in {"", ".txt", ".md", ".rst", ".lgpl", ".gpl", ".apache", ".bsd"}:
                continue
            source = Path(distribution.locate_file(entry))
            if source.is_file():
                license_files.append(copy_file(source, output / "packages" / package_id / relative))
        if not license_files:
            warnings.append(f"No license file was present in installed metadata for {name} {version}.")
        project_urls = sorted(info.get_all("Project-URL") or [])
        homepage = info.get("Home-page")
        if homepage:
            project_urls.append("Homepage, " + homepage)
        components.append({
            "name": name, "version": version, "kind": "runtime distribution",
            "license": info.get("License-Expression") or info.get("License") or "See upstream license files",
            "copyright_author": info.get("Author") or info.get("Author-email") or "See license files",
            "source_distribution": f"https://pypi.org/project/{name}/{version}/#files",
            "project_urls": project_urls,
            "license_files": license_files,
        })
        for requirement_text in distribution.requires or []:
            requirement = Requirement(requirement_text)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)

    # Only the PyInstaller bootloader/runtime is distributed, not the build CLI.
    try:
        bootloader = metadata.distribution("pyinstaller")
        bootloader_files = []
        for entry in bootloader.files or []:
            if Path(str(entry)).name.lower() == "copying.txt":
                source = Path(bootloader.locate_file(entry))
                if source.is_file():
                    bootloader_files.append(copy_file(source, output / "PyInstaller-COPYING.txt"))
                    break
        components.append({
            "name": "PyInstaller bootloader/runtime", "version": bootloader.version,
            "kind": "frozen application loader", "license": bootloader.metadata.get("License", "See COPYING"),
            "source_distribution": f"https://pypi.org/project/pyinstaller/{bootloader.version}/#files",
            "project_urls": ["Source, https://github.com/pyinstaller/pyinstaller"],
            "license_files": bootloader_files,
        })
    except metadata.PackageNotFoundError:
        warnings.append("PyInstaller metadata was unavailable; include its COPYING file when distributing a frozen build.")

    python_license = None
    for directory in (Path(sys.base_prefix), Path(sys.prefix)):
        for filename in ("LICENSE.txt", "LICENSE"):
            source = directory / filename
            if source.is_file():
                python_license = copy_file(source, output / "Python-LICENSE.txt")
                break
        if python_license:
            break
    if not python_license:
        warnings.append("Python's LICENSE file was not found in the build interpreter; include it before distribution.")
    components.append({
        "name": "Python", "version": platform.python_version(), "kind": "embedded interpreter",
        "license": "Python Software Foundation License and bundled component licenses",
        "source_distribution": f"https://www.python.org/downloads/release/python-{platform.python_version().replace('.', '')}/",
        "project_urls": ["Source, https://github.com/python/cpython"],
        "license_files": [python_license] if python_license else [],
    })
    components.sort(key=lambda item: item["name"].lower())
    lines = [
        "KMLink third-party software notices", "=================================", "",
        "KMLink's original application code is licensed under MIT; see KMLink-MIT-LICENSE.txt.",
        "This build uses Qt/PySide6, Shiboken and pynput. Their GNU LGPL/GPL license texts",
        "and their original distribution notices are included below and in supplementary/.",
        "The open-source LGPLv3 alternative is used for the Qt/PySide6/Shiboken libraries.",
        "The Qt libraries are dynamically linked. Keep the distributed library files",
        "replaceable by interface-compatible versions; the source project and build scripts",
        "can also be used to rebuild KMLink against modified dependencies.",
        "No restriction on reverse engineering for debugging modifications to LGPL libraries",
        "is imposed by KMLink. Upstream copyright notices and license terms remain in force.",
        "", "Installed versions and upstream source locations:", "",
    ]
    for item in components:
        lines.extend([f"{item['name']} {item['version']} ({item['kind']})",
                      "  License: " + item["license"],
                      "  Source/downloads: " + item["source_distribution"]])
        lines.extend("  " + url for url in item.get("project_urls", []))
        lines.extend("  Notice: " + name for name in item["license_files"])
        lines.append("")
    lines.extend([
        "Qt itself contains third-party components; module-level source, copyright and license",
        "inventories are maintained at https://doc.qt.io/qt-6/licenses-used-in-qt.html .",
        "The Qt for Python inventory is https://doc.qt.io/qtforpython-6/licenses.html .",
        "Qt source archives: https://download.qt.io/archive/qt/ . Match the Qt/PySide version above.",
        "This inventory records available installed notices; it is not a compliance certification.",
    ])
    if warnings:
        lines.extend(["", "Build-environment notice gaps:", *["- " + warning for warning in warnings]])
    notice = output / "THIRD-PARTY-NOTICES.txt"
    notice.write_text("\n".join(lines) + "\n", encoding="utf-8")
    generated.add(notice.relative_to(output).as_posix())
    hashes = {name: hashlib.sha256((output / name).read_bytes()).hexdigest() for name in sorted(generated)}
    manifest = {"schema": 1, "target_platform": sys.platform, "components": components,
                "files_sha256": hashes, "warnings": warnings}
    (output / "inventory.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def spec_source(platform: str) -> str:
    if platform not in {"win32", "darwin"}:
        raise ValueError("KMLink packaging currently supports Windows and macOS only.")
    with (ROOT / "pyproject.toml").open("rb") as project_file:
        app_version = tomllib.load(project_file).get("project", {}).get("version")
    if not isinstance(app_version, str) or not app_version.strip():
        raise ValueError("pyproject.toml must define a non-empty project.version string.")
    backend = "win32" if platform == "win32" else "darwin"
    excluded_backends = {"win32", "darwin", "xorg", "uinput", "dummy"} - {backend}
    excludes = ["Xlib", "evdev"]
    for name in sorted(excluded_backends):
        excludes.extend([f"pynput.keyboard._{name}", f"pynput.mouse._{name}", f"pynput._util.{name}"])
    if platform == "win32":
        excludes.extend(["Quartz", "AppKit", "Foundation", "objc"])
    manifest = str(ROOT / "packaging" / "windows.manifest") if platform == "win32" else None
    # Pynput chooses its backend dynamically; include the current OS explicitly.
    source = f'''# Generated by scripts/build.py. Build on the target OS/architecture.
from PyInstaller.utils.hooks import collect_submodules

root = {str(ROOT)!r}
excluded_backends = {sorted(excluded_backends)!r}
def include_module(name):
    return not any(name.endswith("._" + b) or name.endswith("." + b) for b in excluded_backends)

hiddenimports = collect_submodules("pynput", filter=include_module)
'''
    if platform == "darwin":
        source += '''# These PyObjC frameworks export symbols dynamically.
hiddenimports += collect_submodules("HIServices")
hiddenimports += collect_submodules("Quartz")
hiddenimports += collect_submodules("ApplicationServices")
'''
    source += f'''
a = Analysis(
    [{str(ROOT / "packaging" / "launcher.py")!r}],
    pathex=[{str(ROOT / "src")!r}],
    binaries=[], datas=[], hiddenimports=hiddenimports,
    hookspath=[], hooksconfig={{}}, runtime_hooks=[],
    excludes={excludes!r}, noarchive=False,
)
'''
    if platform == "win32":
        source += inspect.getsource(filter_windows_system_icu)
        source += '\na.binaries = filter_windows_system_icu(a.binaries)\n'
    source += f'''
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="KMLink", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False,
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
    manifest={manifest!r},
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="KMLink")
'''
    if platform == "darwin":
        source += f'''app = BUNDLE(
    coll, name="KMLink.app", icon=None, bundle_identifier="com.kmlink.desktop",
    info_plist={{
        "CFBundleDisplayName": "KMLink",
        "CFBundleShortVersionString": {app_version!r},
        "NSHighResolutionCapable": True,
        "NSInputMonitoringUsageDescription": "KMLink listens to your keyboard and mouse while you control your paired computer.",
        "NSAccessibilityUsageDescription": "KMLink uses accessibility access to send keyboard and mouse input to your paired computer.",
        "NSLocalNetworkUsageDescription": "KMLink connects directly to your paired computer on your local network.",
    }},
)
'''
    return source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec-only", action="store_true", help="Generate the PyInstaller spec without building.")
    parser.add_argument("--notices-only", action="store_true", help="Collect licenses into an existing build without running PyInstaller.")
    parser.add_argument("--notice-dir", type=Path, help="Override the licenses directory (default: current platform's dist bundle).")
    args = parser.parse_args()
    if sys.platform not in {"win32", "darwin"}:
        parser.error("Run this script on Windows or macOS; cross-building is not supported.")
    if args.notices_only:
        notice_dir = write_notice_bundle(args.notice_dir)
        finalize_macos_bundle(notice_dir)
        print(notice_dir)
        return 0
    output_dir = ROOT / "packaging" / "generated"
    output_dir.mkdir(parents=True, exist_ok=True)
    spec = output_dir / "KMLink.spec"
    spec.write_text(spec_source(sys.platform), encoding="utf-8")
    if args.spec_only:
        print(spec)
        return 0
    if importlib.util.find_spec("PyInstaller") is None:
        parser.error("PyInstaller is missing. Run the project setup script or install -e '.[build]' in the project virtual environment.")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"), str(spec)],
        cwd=ROOT, check=True,
    )
    notice_dir = write_notice_bundle(args.notice_dir)
    finalize_macos_bundle(notice_dir)
    print(f"Third-party notices: {notice_dir}")
    print(f"Built KMLink for {sys.platform}: {ROOT / 'dist'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
