"""Transactional installer launched from a private copy of the frozen app.

The running application is never overwritten. A helper with its own complete
runtime waits for that application to exit, installs a verified candidate next
to the old app, and retains the old directory for recovery.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass

from .updater import (ASSETS, ReleaseUpdate, UpdateError, VerifiedArchive, _checksum, archive_platform,
                      extract_archive, file_sha256, platform_key, update_cache_dir,
                      validate_payload, version_tuple)


@dataclass(frozen=True)
class InstallPlan:
    path: Path
    workspace: Path
    target: Path
    version: str


def installation_supported() -> bool:
    return bool(getattr(sys, "frozen", False) and sys.platform in {"win32", "darwin"})


def _current_root() -> Path:
    executable = Path(sys.executable).resolve(strict=True)
    if sys.platform == "win32" and executable.name.casefold() == "kmlink.exe":
        return executable.parent
    if (sys.platform == "darwin" and executable.name == "KMLink"
            and executable.parent.name == "MacOS" and executable.parent.parent.name == "Contents"
            and executable.parents[2].suffix == ".app"):
        return executable.parents[2]
    raise UpdateError("无法确认当前应用的安装目录，请使用正式安装包运行。")


def _executable(root: Path, target: str) -> Path:
    return root / "KMLink.exe" if target == "windows-x64" else root / "Contents" / "MacOS" / "KMLink"


def _manifest_path(root: Path, target: str) -> Path:
    return root / "update-manifest.json" if target == "windows-x64" else root / "Contents" / "Resources" / "update-manifest.json"


def _read_current_version(root: Path, target: str) -> str:
    try:
        path = _manifest_path(root, target)
        if path.stat().st_size > 4096:
            raise UpdateError("当前安装目录的更新信息无效。")
        version = json.loads(path.read_text(encoding="utf-8"))["version"]
        version_tuple(version)
        validate_payload(root, target, version)
        return version
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UpdateError("当前应用缺少有效的更新信息，请先使用完整安装包。") from exc


def _safe_path(path: Path) -> Path:
    result = path.resolve()
    if any(ord(char) < 32 for char in str(result)):
        raise UpdateError("安装路径包含不支持的控制字符。")
    return result


def prepare_install(archive: VerifiedArchive, *, parent_pid: int | None = None) -> InstallPlan:
    if not installation_supported():
        raise UpdateError("源码运行模式可以检查和下载更新，请使用正式安装包以启用自动安装。")
    target_platform = archive_platform(archive)
    if target_platform != platform_key():
        raise UpdateError("下载的更新包与当前应用平台不匹配。")
    target = _safe_path(_current_root())
    old_version = _read_current_version(target, target_platform)
    if version_tuple(archive.update.version) <= version_tuple(old_version):
        raise UpdateError("下载的版本没有高于当前版本。")
    cache = _safe_path(update_cache_dir())
    cache.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="install-", dir=cache))
    try:
        if target.is_relative_to(workspace) or workspace.is_relative_to(target):
            raise UpdateError("更新暂存目录不能放在应用目录内。")
        payload = extract_archive(archive, workspace / "unpacked")
        if target_platform.startswith("macos-"):
            _verify_macos(payload)
        helper = workspace / "helper" / target.name
        helper.parent.mkdir()
        shutil.copytree(target, helper, symlinks=True)
        # Retain a private verified archive for the helper's second validation.
        saved_archive = workspace / archive.path.name
        shutil.copyfile(archive.path, saved_archive)
        if file_sha256(saved_archive) != archive.sha256:
            raise UpdateError("更新暂存文件校验失败。")
        token = uuid.uuid4().hex
        data = {"schema": 1, "app": "KMLink", "token": token,
                "workspace": str(workspace), "target": str(target), "helper": str(helper),
                "platform": target_platform, "old_version": old_version,
                "version": archive.update.version, "parent_pid": parent_pid or os.getpid(),
                "archive": str(saved_archive), "sha256": archive.sha256,
                "old_executable_sha256": file_sha256(_executable(target, target_platform)),
                "release": asdict(archive.update)}
        original_directory = archive.path.resolve().parent
        if original_directory.parent == cache and original_directory.name.startswith("download-"):
            data["download_dir"] = str(original_directory)
        plan_path = workspace / "plan.json"
        plan_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return InstallPlan(plan_path, workspace, target, archive.update.version)
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise


def discard_install(plan: InstallPlan) -> None:
    """Discard only an unlaunched workspace created by prepare_install."""
    workspace = _safe_path(plan.workspace)
    if (workspace.parent != _safe_path(update_cache_dir()) or not workspace.name.startswith("install-")
            or plan.path.resolve() != workspace / "plan.json" or (workspace / "launched").exists()):
        return
    shutil.rmtree(workspace, ignore_errors=True)


def _fresh_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("_PYI_") and key != "_MEIPASS2"}
    # Required for frozen subprocesses that outlive the original application.
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environment


def _spawn(arguments: list[str], *, environment: dict | None = None):
    options = {"cwd": str(Path(arguments[0]).parent), "stdin": subprocess.DEVNULL,
               "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
               "env": environment or _fresh_environment(), "close_fds": True}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return subprocess.Popen(arguments, **options)


def launch_install(plan: InstallPlan) -> None:
    data = _load_plan(plan.path)
    marker = plan.workspace / "launched"
    try:
        with marker.open("x", encoding="ascii") as started:
            started.write("started")
    except FileExistsError as exc:
        raise UpdateError("此更新已经开始安装，请等待安装完成。") from exc
    try:
        _spawn([str(_executable(Path(data["helper"]), data["platform"])), "--apply-update", str(plan.path)])
    except Exception as exc:
        marker.unlink(missing_ok=True)
        raise UpdateError("无法启动更新程序，当前版本未被修改。") from exc


def _load_plan(path: Path) -> dict:
    path = _safe_path(Path(path))
    if path.name != "plan.json" or path.stat().st_size > 32768:
        raise UpdateError("更新安装计划无效。")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("app") != "KMLink":
        raise UpdateError("更新安装计划格式无效。")
    workspace = _safe_path(Path(data["workspace"]))
    target = _safe_path(Path(data["target"]))
    helper = _safe_path(Path(data["helper"]))
    archive = _safe_path(Path(data["archive"]))
    if path.parent != workspace or not workspace.name.startswith("install-"):
        raise UpdateError("更新安装计划目录无效。")
    if (not helper.is_relative_to(workspace / "helper") or not archive.is_relative_to(workspace)
            or target == target.parent or target.is_relative_to(workspace) or workspace.is_relative_to(target)):
        raise UpdateError("更新安装计划中的路径无效。")
    if data.get("platform") not in ASSETS or not isinstance(data.get("token"), str) or not re.fullmatch(r"[0-9a-f]{32}", data["token"]):
        raise UpdateError("更新安装目标无效。")
    if type(data.get("parent_pid")) is not int or data["parent_pid"] <= 0:
        raise UpdateError("更新安装进程无效。")
    version_tuple(data["version"])
    version_tuple(data["old_version"])
    if not re.fullmatch(r"[0-9a-f]{64}", data.get("sha256", "")):
        raise UpdateError("更新安装校验值无效。")
    if file_sha256(archive) != data["sha256"]:
        raise UpdateError("暂存更新包发生变化，拒绝安装。")
    update = ReleaseUpdate(**data["release"])
    if update.version != data["version"] or update.asset_name != ASSETS[data["platform"]]:
        raise UpdateError("更新安装版本不一致。")
    validate_payload(helper, data["platform"], data["old_version"])
    if file_sha256(_executable(helper, data["platform"])) != data.get("old_executable_sha256"):
        raise UpdateError("更新辅助程序与原安装不一致。")
    validate_payload(workspace / "unpacked" / ("KMLink" if data["platform"] == "windows-x64" else "KMLink.app"),
                     data["platform"], data["version"])
    return data


def _paths(data: dict) -> tuple[Path, Path, Path, Path]:
    target = Path(data["target"])
    candidate = target.parent / f".{target.name}.update-{data['token']}"
    backup = target.parent / f"{target.name}.backup-{data['token']}"
    failed = target.parent / f".{target.name}.failed-{data['token']}"
    return target, candidate, backup, failed


def _payload(data: dict) -> Path:
    return Path(data["workspace"]) / "unpacked" / ("KMLink" if data["platform"] == "windows-x64" else "KMLink.app")


def _verify_macos(path: Path) -> None:
    if sys.platform == "darwin":
        result = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(path)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        if result.returncode:
            raise UpdateError("Mac 更新包签名校验失败，当前版本未被修改。")


def _wait_for_parent(pid: int, timeout: float = 120.) -> None:
    if pid == os.getpid():
        raise UpdateError("更新程序不能等待自身退出。")
    if os.name == "nt":
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            if ctypes.get_last_error() == 87:  # Already exited / no such PID.
                return
            raise UpdateError("无法等待旧版程序退出，请关闭旧版后重试。")
        try:
            if api.WaitForSingleObject(handle, int(timeout * 1000)) != 0:
                raise UpdateError("旧版程序没有退出，更新已取消。")
        finally:
            api.CloseHandle(handle)
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(.1)
    raise UpdateError("旧版程序没有退出，更新已取消。")


def _writable_parent(target: Path) -> bool:
    try:
        with tempfile.TemporaryFile(dir=target.parent):
            return True
    except PermissionError:
        return False


def _transaction(data: dict) -> None:
    target, candidate, backup, _ = _paths(data)
    validate_payload(target, data["platform"], data["old_version"])
    if file_sha256(_executable(target, data["platform"])) != data.get("old_executable_sha256"):
        raise UpdateError("原安装已被其他进程修改，请重新启动后检查更新。")
    if candidate.exists() or backup.exists():
        raise UpdateError("更新目标的暂存目录已存在，未覆盖任何文件。")
    update = ReleaseUpdate(**data["release"])
    if _checksum(update) != data["sha256"]:
        raise UpdateError("GitHub 的更新校验值已变化，请重新检查更新。")
    # Rebuild the candidate from the hashed archive, never trust a previously
    # extracted and subsequently mutable payload. This directory shares the
    # destination volume, so the final replacement can be atomic.
    extracted = target.parent / f".{target.name}.extract-{data['token']}"
    if extracted.exists():
        raise UpdateError("更新目标的解压目录已存在，未覆盖任何文件。")
    try:
        fresh = extract_archive(VerifiedArchive(update, Path(data["archive"]), data["sha256"]), extracted)
        fresh.replace(candidate)
        extracted.rmdir()
        validate_payload(candidate, data["platform"], data["version"])
        _verify_macos(candidate)
        try:
            target.replace(backup)
            try:
                candidate.replace(target)
            except Exception:
                backup.replace(target)
                raise
        except OSError as exc:
            raise UpdateError("无法替换应用目录，请关闭其他 KMLink 实例后重试。") from exc
    finally:
        # Neither directory existed on entry and both names were derived from
        # this plan. Never remove the target, recovery backup, or any links.
        for temporary in (candidate, extracted):
            if temporary.parent == target.parent and not temporary.is_symlink():
                shutil.rmtree(temporary, ignore_errors=True)


def _rollback(data: dict) -> None:
    target, _, backup, failed = _paths(data)
    validate_payload(backup, data["platform"], data["old_version"])
    if failed.exists():
        raise UpdateError("回滚目录已存在，旧版本备份仍保留。")
    if target.exists():
        target.replace(failed)
    try:
        backup.replace(target)
    except Exception:
        if failed.exists() and not target.exists():
            failed.replace(target)
        raise


def _smoke_check(data: dict) -> None:
    executable = _executable(Path(data["target"]), data["platform"])
    environment = _fresh_environment()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    options = {"env": environment, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
               "stderr": subprocess.DEVNULL, "cwd": str(executable.parent), "timeout": 45}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run([str(executable), "--smoke-test"], **options)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError("新版启动检查失败，正在恢复旧版本。") from exc
    if result.returncode:
        raise UpdateError("新版启动检查失败，正在恢复旧版本。")


def _elevate_windows(data: dict, plan_path: Path, operation: str) -> None:
    from ctypes import wintypes
    class ExecuteInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                    ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR),
                    ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                    ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ExecuteInfo)]
    shell.ShellExecuteExW.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    executable = _executable(Path(data["helper"]), data["platform"])
    parameters = subprocess.list2cmdline(["--apply-update", str(plan_path), "--elevated", operation])
    info = ExecuteInfo(cbSize=ctypes.sizeof(ExecuteInfo), fMask=0x00000040 | 0x00000100,
                       lpVerb="runas", lpFile=str(executable), lpParameters=parameters,
                       lpDirectory=str(executable.parent), nShow=0)
    previous_reset = os.environ.get("PYINSTALLER_RESET_ENVIRONMENT")
    os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    try:
        started = shell.ShellExecuteExW(ctypes.byref(info))
    finally:
        if previous_reset is None:
            os.environ.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
        else:
            os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = previous_reset
    if not started or not info.hProcess:
        raise UpdateError("没有获得安装更新所需的管理员授权，当前版本已保留。")
    try:
        if kernel.WaitForSingleObject(info.hProcess, 300000) != 0:
            raise UpdateError("管理员更新程序未及时完成，请保留现有备份。")
        result = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(info.hProcess, ctypes.byref(result)) or result.value:
            raise UpdateError("管理员更新未完成，当前版本或其备份已保留。")
    finally:
        kernel.CloseHandle(info.hProcess)


def _elevate_macos(data: dict, operation: str) -> None:
    executable = _executable(Path(data["helper"]), data["platform"])
    command = " ".join(shlex.quote(argument) for argument in
                       ["/usr/bin/env", "PYINSTALLER_RESET_ENVIRONMENT=1", str(executable), "--apply-update",
                        str(Path(data["workspace"]) / "plan.json"), "--elevated", operation])
    # Only the file transaction is elevated. The ordinary helper performs the
    # launch, so the replacement GUI never inherits administrator privileges.
    apple_script = "do shell script " + json.dumps(command, ensure_ascii=False) + " with administrator privileges"
    result = subprocess.run(["/usr/bin/osascript", "-e", apple_script],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    if result.returncode:
        raise UpdateError("没有完成管理员授权或应用替换，旧版本及备份已保留。")


def _operate(data: dict, plan_path: Path, operation: str) -> None:
    action = _transaction if operation == "install" else _rollback
    if _writable_parent(Path(data["target"])):
        action(data)
    elif data["platform"] == "windows-x64":
        _elevate_windows(data, plan_path, operation)
    else:
        _elevate_macos(data, operation)
    version = data["version"] if operation == "install" else data["old_version"]
    validate_payload(Path(data["target"]), data["platform"], version)


def _write_result(data: dict, success: bool, message: str) -> None:
    root = update_cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    result = {"success": success, "version": data.get("version", ""), "message": message[:1000]}
    temporary = root / ("result-" + uuid.uuid4().hex + ".json")
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temporary.replace(root / "last-install-result.json")
    # The new GUI may start before this helper has quite exited. Cleanup waits
    # for this exact process to disappear before removing its private runtime.
    finished = {"helper_pid": os.getpid(), "download_dir": data.get("download_dir")}
    (Path(data["workspace"]) / "finished.json").write_text(json.dumps(finished), encoding="utf-8")


def _record_result_safely(data: dict, success: bool, message: str) -> None:
    try:
        _write_result(data, success, message)
    except Exception:
        # Reporting and deferred cleanup are optional. A full or unwritable
        # cache must not roll back a valid installation or prevent reopening.
        pass


def _process_alive(pid: int) -> bool:
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _cleanup_completed(workspace: Path, *, timeout: float = 60.) -> None:
    cache = update_cache_dir().resolve()
    try:
        if workspace.is_symlink():
            return
        workspace = workspace.resolve()
        if workspace.parent != cache or not workspace.name.startswith("install-"):
            return
        if Path(sys.executable).resolve().is_relative_to(workspace):
            return
        marker = workspace / "finished.json"
        if marker.stat().st_size > 4096:
            return
        info = json.loads(marker.read_text(encoding="utf-8"))
        pid = info.get("helper_pid")
        if type(pid) is not int or pid <= 0 or pid == os.getpid():
            return
        deadline = time.monotonic() + timeout
        while _process_alive(pid):
            if time.monotonic() >= deadline:
                return
            time.sleep(.25)
        # Revalidate before each removal. These are generated cache directories,
        # never the installed application or its retained recovery backup.
        download = info.get("download_dir")
        if isinstance(download, str):
            original = Path(download)
            resolved = original.resolve()
            if (not original.is_symlink() and resolved.parent == cache
                    and resolved.name.startswith("download-")
                    and not Path(sys.executable).resolve().is_relative_to(resolved)):
                shutil.rmtree(resolved, ignore_errors=True)
        if workspace.parent == cache and not workspace.is_symlink():
            shutil.rmtree(workspace, ignore_errors=True)
    except (OSError, ValueError, TypeError):
        # A subsequent startup retries any completed workspace still present.
        return


def _schedule_completed_cleanup() -> None:
    try:
        cache = update_cache_dir()
        completed = [path for path in cache.glob("install-*") if (path / "finished.json").is_file()][:4]
    except OSError:
        return
    for path in completed:
        threading.Thread(target=_cleanup_completed, args=(path,), daemon=True,
                         name="KMLink update cleanup").start()


def consume_install_result() -> dict | None:
    _schedule_completed_cleanup()
    path = update_cache_dir() / "last-install-result.json"
    try:
        if path.stat().st_size > 4096:
            path.unlink(missing_ok=True)
            return None
        result = json.loads(path.read_text(encoding="utf-8"))
        path.unlink(missing_ok=True)
        if (isinstance(result, dict) and type(result.get("success")) is bool
                and isinstance(result.get("message"), str) and isinstance(result.get("version"), str)):
            return result
    except (OSError, ValueError):
        pass
    return None


def run_installer_cli(arguments: list[str]) -> int | None:
    if not arguments or arguments[0] != "--apply-update":
        return None
    if len(arguments) not in {2, 4} or (len(arguments) == 4 and (arguments[2] != "--elevated" or arguments[3] not in {"install", "rollback"})):
        return 2
    data = None
    installed = False
    try:
        path = Path(arguments[1])
        data = _load_plan(path)
        if len(arguments) == 4:
            # Elevated process performs filesystem operations only. It never
            # starts either the new or old graphical application.
            (_transaction if arguments[3] == "install" else _rollback)(data)
            return 0
        _wait_for_parent(data["parent_pid"])
        _operate(data, path, "install")
        installed = True
        _smoke_check(data)
        _record_result_safely(data, True, f"已更新到 {data['version']}。旧版备份已保留。")
        _spawn([str(_executable(Path(data["target"]), data["platform"]))])
        return 0
    except Exception as exc:
        if data is not None and len(arguments) == 2:
            message = str(exc) or "安装更新失败。"
            if installed:
                try:
                    _operate(data, Path(arguments[1]), "rollback")
                    message += " 已恢复旧版本。"
                except Exception:
                    message += " 自动恢复未完成，请保留应用目录旁的备份。"
            _record_result_safely(data, False, message)
            try:
                _spawn([str(_executable(Path(data["target"]), data["platform"]))])
            except Exception:
                pass
        return 1
