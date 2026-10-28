"""Bounded GitHub release downloads and validation for the desktop updater.

Network operations are synchronous and belong on a worker, never the GUI thread.
Only the project's HTTPS release URLs are accepted. The archive is verified before
it is extracted or used by the separately running installer.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import ssl
import stat
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener
import zipfile


REPOSITORY = "gyxde/KMLink"
LATEST_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
ASSETS = {"windows-x64": "KMLink-windows-x64.zip", "macos-arm64": "KMLink-macos-arm64.zip",
          "macos-x64": "KMLink-macos-x64.zip"}
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_ENTRIES = 30000
VERSION = re.compile(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
HEX = re.compile(r"[0-9a-fA-F]{64}\Z")
DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}


class UpdateError(RuntimeError):
    pass


class UpdateCancelled(UpdateError):
    pass


@dataclass(frozen=True)
class ReleaseUpdate:
    version: str
    tag: str
    notes: str
    page_url: str
    asset_name: str
    download_url: str
    size: int
    sha256: str | None
    checksum_url: str


@dataclass(frozen=True)
class VerifiedArchive:
    update: ReleaseUpdate
    path: Path
    sha256: str


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not (match := VERSION.fullmatch(value)):
        raise UpdateError("更新版本号无效。")
    return tuple(int(part) for part in match.groups())


def platform_key() -> str:
    if sys.platform == "win32":
        return "windows-x64"
    if sys.platform == "darwin":
        return "macos-arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "macos-x64"
    raise UpdateError("当前系统暂不支持应用内安装更新。")


def update_cache_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        # Keep the helper outside a portable app sometimes installed directly
        # into %LOCALAPPDATA%/KMLink, which will itself be replaced.
        return base / "KMLinkUpdateCache"
    return Path.home() / "Library" / "Caches" / "KMLink" / "updates"


def _release_url(tag: str, name: str) -> str:
    version_tuple(tag)
    if name not in set(ASSETS.values()) | {"SHA256SUMS.txt"}:
        raise UpdateError("更新文件名称无效。")
    return f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"


def _validate_redirect(url: str, *, api: bool = False) -> None:
    parts = urlsplit(url)
    allowed = {"api.github.com"} if api else DOWNLOAD_HOSTS
    try:
        port = parts.port
    except ValueError as exc:
        raise UpdateError("更新服务器返回了无效端口。") from exc
    if parts.scheme != "https" or parts.hostname not in allowed or port not in {None, 443} or parts.username or parts.password:
        raise UpdateError("更新服务器返回了不可信的下载地址。")


def _open(url: str, *, api: bool = False):
    _validate_redirect(url, api=api)
    request = Request(url, headers={"User-Agent": "KMLink-Updater", "Accept": "application/vnd.github+json" if api else "application/octet-stream"})
    class ReleaseRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            _validate_redirect(newurl, api=api)
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    import certifi
    context = ssl.create_default_context(cafile=certifi.where())
    opener = build_opener(HTTPSHandler(context=context), ReleaseRedirect())
    try:
        response = opener.open(request, timeout=20)
        _validate_redirect(response.geturl(), api=api)
        return response
    except HTTPError as exc:
        if exc.code in {403, 429}:
            raise UpdateError("GitHub 暂时限制了更新请求，请稍后再试。") from exc
        if exc.code == 404:
            raise UpdateError("GitHub 上尚无可用的正式版本。") from exc
        raise UpdateError(f"更新服务器返回 HTTP {exc.code}。") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise UpdateError("无法连接 GitHub，请检查网络后重试。") from exc


def _read_limited(url: str, limit: int, *, api: bool = False) -> bytes:
    with _open(url, api=api) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("更新服务器返回的数据过大。")
    return data


def check_for_update(current_version: str, platform_key: str | None = None) -> ReleaseUpdate | None:
    current = version_tuple(current_version)
    target = platform_key or globals()["platform_key"]()
    if target not in ASSETS:
        raise UpdateError("当前系统没有对应的更新包。")
    try:
        release = json.loads(_read_limited(LATEST_API, 1024 * 1024, api=True))
    except (ValueError, UnicodeError) as exc:
        raise UpdateError("GitHub 更新信息格式无效。") from exc
    if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
        raise UpdateError("更新信息不是正式发布版本。")
    tag = release.get("tag_name")
    latest = version_tuple(tag)
    if latest <= current:
        return None
    name = ASSETS[target]
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("更新版本未包含下载文件。")
    found = {}
    for asset in assets:
        if isinstance(asset, dict) and asset.get("name") in {name, "SHA256SUMS.txt"}:
            if asset["name"] in found:
                raise UpdateError("更新版本包含重复文件。")
            found[asset["name"]] = asset
    if set(found) != {name, "SHA256SUMS.txt"}:
        raise UpdateError("最新版本尚未提供此平台的完整更新包。")
    for filename, asset in found.items():
        if asset.get("browser_download_url") != _release_url(tag, filename) or asset.get("state") != "uploaded":
            raise UpdateError("更新下载地址与项目版本不一致。")
    size = found[name].get("size")
    if type(size) is not int or not 0 < size <= MAX_ARCHIVE_BYTES:
        raise UpdateError("更新包大小无效。")
    digest = found[name].get("digest")
    if digest is not None and (not isinstance(digest, str) or not digest.startswith("sha256:") or not HEX.fullmatch(digest[7:])):
        raise UpdateError("GitHub 文件校验值无效。")
    body = release.get("body")
    return ReleaseUpdate(".".join(map(str, latest)), tag, body[:12000] if isinstance(body, str) else "",
                         f"https://github.com/{REPOSITORY}/releases/tag/{tag}", name,
                         _release_url(tag, name), size, digest[7:].lower() if digest else None,
                         _release_url(tag, "SHA256SUMS.txt"))


def _cancelled(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise UpdateCancelled("已取消下载。")


def _checksum(update: ReleaseUpdate) -> str:
    # Validate the dataclass again: callers must not be able to provide an
    # arbitrary URL even if they construct a ReleaseUpdate directly.
    if (version_tuple(update.version) != version_tuple(update.tag)
            or update.download_url != _release_url(update.tag, update.asset_name)
            or update.checksum_url != _release_url(update.tag, "SHA256SUMS.txt")):
        raise UpdateError("更新下载地址无效。")
    try:
        lines = _read_limited(update.checksum_url, 16384).decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise UpdateError("更新校验文件编码无效。") from exc
    matches = []
    for line in lines:
        match = re.fullmatch(r"([0-9A-Fa-f]{64}) [ *](.+)", line)
        if match and match[2] == update.asset_name:
            matches.append(match[1].lower())
    if len(matches) != 1 or (update.sha256 is not None and matches[0] != update.sha256):
        raise UpdateError("GitHub 更新包校验信息不一致。")
    return matches[0]


def download_update(update: ReleaseUpdate, *, cache_dir: Path | None = None,
                    progress: Callable[[int, int], None] | None = None,
                    cancel: threading.Event | None = None) -> VerifiedArchive:
    _cancelled(cancel)
    checksum = _checksum(update)
    root = Path(cache_dir) if cache_dir is not None else update_cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="download-", dir=root))
    part = directory / "download.part"
    destination = directory / update.asset_name
    count = 0
    digest = hashlib.sha256()
    try:
        with _open(update.download_url) as response, part.open("xb") as output:
            while True:
                _cancelled(cancel)
                block = response.read(128 * 1024)
                if not block:
                    break
                count += len(block)
                if count > update.size or count > MAX_ARCHIVE_BYTES:
                    raise UpdateError("更新包超过预期大小。")
                digest.update(block)
                output.write(block)
                if progress is not None:
                    progress(count, update.size)
        _cancelled(cancel)
        if count != update.size or digest.hexdigest() != checksum:
            raise UpdateError("更新包校验失败，未修改当前程序，请重新下载。")
        part.replace(destination)
        return VerifiedArchive(update, destination, checksum)
    except Exception:
        # This directory was created above exclusively for this download.
        shutil.rmtree(directory, ignore_errors=True)
        raise


def archive_platform(archive: VerifiedArchive) -> str:
    for target, filename in ASSETS.items():
        if filename == archive.update.asset_name:
            return target
    raise UpdateError("更新包平台无效。")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_payload(root: Path, target: str, version: str) -> Path:
    root = Path(root)
    if target == "windows-x64":
        manifest = root / "update-manifest.json"
        executable = root / "KMLink.exe"
        if not (root / "_internal").is_dir():
            raise UpdateError("Windows 更新包缺少运行组件。")
    elif target in {"macos-arm64", "macos-x64"}:
        import plistlib
        manifest = root / "Contents" / "Resources" / "update-manifest.json"
        executable = root / "Contents" / "MacOS" / "KMLink"
        try:
            info = plistlib.loads((root / "Contents" / "Info.plist").read_bytes())
        except (OSError, ValueError) as exc:
            raise UpdateError("Mac 更新包信息无效。") from exc
        if info.get("CFBundleIdentifier") != "com.kmlink.desktop" or info.get("CFBundleShortVersionString") != version:
            raise UpdateError("Mac 更新包身份或版本不一致。")
    else:
        raise UpdateError("更新包平台无效。")
    try:
        if manifest.stat().st_size > 4096:
            raise UpdateError("更新包清单过大。")
        details = json.loads(manifest.read_text(encoding="utf-8"))
        if details != {"schema": 1, "app": "KMLink", "version": version, "platform": target}:
            raise UpdateError("更新包清单与当前平台或版本不一致。")
        if not executable.is_file() or executable.stat().st_size == 0:
            raise UpdateError("更新包缺少程序文件。")
    except (OSError, ValueError) as exc:
        raise UpdateError("更新包缺少有效的版本清单。") from exc
    return executable


def extract_archive(archive: VerifiedArchive, destination: Path) -> Path:
    """Extract a verified, bounded archive without following archive links.

    Mac framework links are made last, then resolved within the app directory.
    AppleDouble metadata entries are ignored; executable Unix modes are kept.
    """
    # Extract the same immutable bytes that were verified, rather than
    # reopening a user-writable cache after the hash check (also for UAC).
    try:
        if archive.path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise UpdateError("更新包超过大小限制。")
        with archive.path.open("rb") as source:
            content = source.read(MAX_ARCHIVE_BYTES + 1)
    except OSError as exc:
        raise UpdateError("无法读取已下载的更新包。") from exc
    if len(content) > MAX_ARCHIVE_BYTES or hashlib.sha256(content).hexdigest() != archive.sha256:
        raise UpdateError("已下载的更新包发生变化，拒绝安装。")
    target = archive_platform(archive)
    expected_root = "KMLink" if target == "windows-x64" else "KMLink.app"
    destination = Path(destination)
    if destination.exists():
        raise UpdateError("更新解压目录已存在。")
    destination.mkdir(parents=True)
    links = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as package:
            entries = package.infolist()
            if len(entries) > MAX_ENTRIES or sum(e.file_size for e in entries) > MAX_EXPANDED_BYTES:
                raise UpdateError("更新包解压大小超出限制。")
            seen = set()
            prepared = []
            for entry in entries:
                # ZipInfo normalizes native separators on Windows; inspect the
                # original archive spelling before that normalization as well.
                name = entry.orig_filename
                parts = PurePosixPath(name).parts
                if (not name or "\\" in name or "\0" in name or name.startswith("/")
                        or any(p in {".", ".."} or ":" in p or p.rstrip(" .") != p
                               or any(ord(char) < 32 for char in p) for p in parts)):
                    raise UpdateError("更新包包含不安全的文件路径。")
                if target == "windows-x64" and any(
                        p.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL",
                                                              *(f"COM{i}" for i in range(1, 10)),
                                                              *(f"LPT{i}" for i in range(1, 10))}
                        for p in parts):
                    raise UpdateError("更新包包含 Windows 保留设备名称。")
                if parts[0] == "__MACOSX" and target.startswith("macos-"):
                    continue
                if parts[0] != expected_root:
                    raise UpdateError("更新包目录结构不匹配。")
                key = name.rstrip("/").casefold()
                if key in seen:
                    raise UpdateError("更新包包含重复路径。")
                seen.add(key)
                mode = entry.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind not in {0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK}:
                    raise UpdateError("更新包包含不支持的特殊文件。")
                if entry.flag_bits & 1:
                    raise UpdateError("更新包不应加密。")
                if stat.S_ISLNK(mode):
                    if target == "windows-x64" or entry.file_size > 4096:
                        raise UpdateError("更新包包含不允许的链接。")
                    links.append((entry, destination.joinpath(*parts)))
                else:
                    prepared.append((entry, destination.joinpath(*parts), mode))
            # Files are written before creating any archive symlinks.
            for entry, output, mode in prepared:
                if entry.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                with package.open(entry) as source, output.open("xb") as sink:
                    shutil.copyfileobj(source, sink, 1024 * 1024)
                if target.startswith("macos-"):
                    output.chmod((mode & 0o777) or 0o644)
            root = destination / expected_root
            for entry, output in links:
                link = package.read(entry).decode("utf-8")
                if not link or "\\" in link or "\0" in link or PurePosixPath(link).is_absolute():
                    raise UpdateError("更新包包含不安全的符号链接。")
                resolved = (output.parent / link).resolve()
                if not resolved.is_relative_to(root.resolve()):
                    raise UpdateError("更新包符号链接指向程序外部。")
                output.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(link, output)
            for _, output in links:
                resolved = output.resolve(strict=True)
                if not resolved.is_relative_to(root.resolve()):
                    raise UpdateError("更新包链接链超出程序目录。")
            validate_payload(root, target, archive.update.version)
            return root
    except Exception as exc:
        shutil.rmtree(destination, ignore_errors=True)
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError("无法解压或验证更新包。") from exc


# Lazy exports keep networking/archive code usable without importing the
# platform installer and avoid a circular dependency between these modules.
def installation_supported() -> bool:
    from .update_install import installation_supported as supported
    return supported()


def prepare_install(archive: VerifiedArchive, *, parent_pid: int | None = None):
    from .update_install import prepare_install as prepare
    return prepare(archive, parent_pid=parent_pid)


def discard_install(plan) -> None:
    from .update_install import discard_install as discard
    discard(plan)


def launch_install(plan) -> None:
    from .update_install import launch_install as launch
    launch(plan)


def consume_install_result() -> dict | None:
    from .update_install import consume_install_result as consume
    return consume()


def run_installer_cli(arguments: list[str]) -> int | None:
    from .update_install import run_installer_cli as run
    return run(arguments)
