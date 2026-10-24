"""Windows integration and catch-up policy, kept separate from account adapters."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

import requests

try:
    import winreg
except ImportError:
    winreg = None

CN_TZ = timezone(timedelta(hours=8))
_WINDOWS = os.name == "nt"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def cn_now() -> datetime:
    return datetime.now(CN_TZ)


def validate_time(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("签到时间格式应为 HH:mm")
    return value


def due_today(schedule_time: str, now: datetime | None = None) -> bool:
    validate_time(schedule_time)
    now = (now or cn_now()).astimezone(CN_TZ)
    return now.strftime("%H:%M") >= schedule_time


def launch_command(data_dir: Path, scheduled=False, background=False) -> tuple[str, list[str]]:
    if scheduled and background:
        raise ValueError("不能同时选择单次计划任务与后台常驻模式")
    if getattr(sys, "frozen", False):
        executable, args = sys.executable, []
    else:
        executable = sys.executable
        if os.name == "nt":
            pythonw = Path(executable).with_name("pythonw.exe")
            if pythonw.exists():
                executable = str(pythonw)
        args = [str(Path(__file__).resolve().parents[1] / "main.py")]
    args += ["--data-dir", str(Path(data_dir).resolve())]
    if scheduled:
        args += ["--scheduled"]
    if background:
        args += ["--background"]
    return executable, args


def task_name(data_dir: Path) -> str:
    digest = hashlib.sha256(str(Path(data_dir).resolve()).lower().encode()).hexdigest()[:12]
    return "TraeCNManager-" + digest


def _task_command_text(result) -> str:
    raw = (result.stdout or b"") + (result.stderr or b"")
    return raw.decode("mbcs" if os.name == "nt" else "utf-8", errors="replace").strip()


def _task_is_missing(result) -> bool:
    text = _task_command_text(result).lower()
    return any(word in text for word in ("cannot find", "does not exist", "找不到", "不存在"))


def task_xml(data_dir: Path, schedule_time: str, now: datetime | None = None) -> str:
    validate_time(schedule_time)
    now = now or cn_now()
    moment = datetime.combine(now.astimezone(CN_TZ).date(),
                              datetime.strptime(schedule_time, "%H:%M").time(), tzinfo=CN_TZ)
    executable, args = launch_command(data_dir, scheduled=True)
    # Offset remains explicit even if Windows uses a different display timezone.
    boundary = moment.isoformat(timespec="seconds")
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
 <RegistrationInfo><Description>Trae CN 管家签到；错过时间在登录后补签。仅当前 Windows 用户运行。</Description></RegistrationInfo>
 <Triggers><CalendarTrigger><StartBoundary>{boundary}</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger><LogonTrigger><Enabled>true</Enabled><Delay>PT1M</Delay></LogonTrigger></Triggers>
 <Principals><Principal id="CurrentUser"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
 <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><AllowHardTerminate>true</AllowHardTerminate><Enabled>true</Enabled><Hidden>true</Hidden><WakeToRun>false</WakeToRun><ExecutionTimeLimit>PT30M</ExecutionTimeLimit><RestartOnFailure><Interval>PT15M</Interval><Count>3</Count></RestartOnFailure></Settings>
 <Actions Context="CurrentUser"><Exec><Command>{escape(executable)}</Command><Arguments>{escape(subprocess.list2cmdline(args))}</Arguments><WorkingDirectory>{escape(str(Path(data_dir).resolve().parent))}</WorkingDirectory></Exec></Actions>
</Task>'''


def configure_task(data_dir: Path, enabled: bool, schedule_time: str) -> None:
    if os.name != "nt":
        if enabled:
            raise RuntimeError("计划任务仅支持 Windows")
        return
    name = task_name(data_dir)
    if not enabled:
        # Delete an existing task only; absent task is a successful disabled state.
        query = subprocess.run(["schtasks.exe", "/Query", "/TN", name], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
        if query.returncode == 0:
            result = subprocess.run(["schtasks.exe", "/Delete", "/TN", name, "/F"],
                                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
            if result.returncode:
                raise RuntimeError("无法删除 Windows 计划任务")
        elif not _task_is_missing(query):
            raise RuntimeError("无法确认或删除 Windows 计划任务：" + (_task_command_text(query)[:300] or "任务查询失败"))
        return
    fd, filename = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as stream:
            stream.write(task_xml(data_dir, schedule_time))
        result = subprocess.run(["schtasks.exe", "/Create", "/TN", name, "/XML", filename, "/F"],
                                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)
        if result.returncode:
            detail = _task_command_text(result)[:400]
            raise RuntimeError("Windows 未能创建计划任务：" + (detail or "请检查任务计划服务及当前用户权限"))
    finally:
        Path(filename).unlink(missing_ok=True)


def configure_startup(data_dir: Path, enabled: bool) -> None:
    if os.name != "nt":
        if enabled:
            raise RuntimeError("开机启动仅支持 Windows")
        return
    import winreg
    executable, args = launch_command(data_dir)
    name = task_name(data_dir)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        if enabled:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, subprocess.list2cmdline([executable] + args))
        else:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass


def _background_name(data_dir: Path) -> str:
    return task_name(data_dir) + "-Background"


def _background_value(data_dir: Path):
    if not _WINDOWS or winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as key:
            return winreg.QueryValueEx(key, _background_name(data_dir))
    except FileNotFoundError:
        return None


def configure_background_startup(data_dir: Path, enabled: bool) -> None:
    """Manage only this data directory's dedicated current-user startup value."""
    if not _WINDOWS or winreg is None:
        if enabled:
            raise RuntimeError("后台开机启动仅支持 Windows 当前用户")
        return
    name = _background_name(data_dir)
    if enabled:
        executable, args = launch_command(data_dir, background=True)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, subprocess.list2cmdline([executable] + args))
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass


def start_background(data_dir: Path) -> subprocess.Popen:
    """Start an independent hidden worker; credentials never enter arguments."""
    if not _WINDOWS:
        raise RuntimeError("后台常驻仅支持 Windows")
    executable, args = launch_command(data_dir, background=True)
    startupinfo = None
    if hasattr(subprocess, "STARTUPINFO"):
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    try:
        return subprocess.Popen(
            [executable] + args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            startupinfo=startupinfo, cwd=str(Path(data_dir).resolve().parent),
        )
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("无法启动后台签到进程，请检查管理器程序路径和权限") from None


def _restore_background_value(data_dir: Path, previous) -> None:
    if previous is None:
        configure_background_startup(data_dir, False)
    else:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.SetValueEx(key, _background_name(data_dir), 0, previous[1], previous[0])


def configure_schedule(data_dir: Path, enabled: bool, schedule_time: str, *, start: bool = True) -> dict:
    """Prefer Task Scheduler, falling back to a visible, truthful HKCU backend.

    A service saving settings should use start=False, commit its vault settings,
    then call start_background if this returns backend='startup'.
    """
    validate_time(schedule_time)
    if not enabled:
        failures = []
        try:
            configure_task(data_dir, False, schedule_time)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            failures.append("计划任务删除失败：" + str(error)[:400])
        try:
            configure_background_startup(data_dir, False)
        except (OSError, RuntimeError) as error:
            failures.append("后台启动项删除失败：" + str(error)[:400])
        if failures:
            raise RuntimeError("；".join(failures))
        return {"backend": "off", "message": "已禁用计划任务及本管理器的后台开机启动项。"}
    try:
        configure_task(data_dir, True, schedule_time)
    except (OSError, RuntimeError, subprocess.SubprocessError) as task_error:
        reason = str(task_error)[:400]
        try:
            previous = _background_value(data_dir)
            configure_background_startup(data_dir, True)
        except (OSError, RuntimeError) as registry_error:
            raise RuntimeError("计划任务不可用：" + reason + "；当前用户后台启动项设置也失败：" + str(registry_error)[:400]) from None
        if start:
            try:
                start_background(data_dir)
            except (OSError, RuntimeError, subprocess.SubprocessError) as launch_error:
                try:
                    _restore_background_value(data_dir, previous)
                    rollback_message = "后台启动项已恢复原值"
                except (OSError, RuntimeError) as rollback_error:
                    rollback_message = "后台启动项回滚失败：" + str(rollback_error)[:300]
                raise RuntimeError("计划任务不可用：" + reason + "；后台进程启动失败：" + str(launch_error)[:300] + "；" + rollback_message) from None
        return {"backend": "startup", "message": "计划任务不可用（" + reason + "）；已改用当前用户后台常驻与登录补签，无需管理员权限。"}
    try:
        configure_background_startup(data_dir, False)
    except (OSError, RuntimeError) as error:
        raise RuntimeError("计划任务已创建，但旧后台启动项清理失败：" + str(error)[:400]) from None
    return {"backend": "task", "message": "已启用 Windows 计划任务；旧后台开机启动项已清理。"}


def query_scheduler(data_dir: Path) -> dict:
    """Read actual task/startup registration without opening the account vault."""
    if not _WINDOWS:
        return {"backend": "off", "task_exists": False, "startup_exists": False, "message": "当前系统不支持 Windows 后台签到。"}
    errors = []
    task_exists = None
    startup_exists = None
    try:
        result = subprocess.run(["schtasks.exe", "/Query", "/TN", task_name(data_dir)], capture_output=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=15)
        if result.returncode == 0:
            task_exists = True
        else:
            if _task_is_missing(result):
                task_exists = False
            else:
                errors.append("无法确认计划任务状态，可能缺少查询权限")
    except (OSError, subprocess.SubprocessError):
        errors.append("无法查询 Windows 计划任务")
    try:
        startup_exists = _background_value(data_dir) is not None
    except (OSError, RuntimeError):
        errors.append("无法查询当前用户后台启动项")
    backend = "task" if task_exists else "startup" if startup_exists else "unknown" if task_exists is None or startup_exists is None else "off"
    description = {"task": "检测到 Windows 计划任务。", "startup": "检测到当前用户后台常驻启动项。", "off": "未检测到后台任务或启动项。", "unknown": "后台注册状态待确认。"}[backend]
    if task_exists and startup_exists:
        description += "同时存在旧后台启动项，保存后台设置可重新整理。"
    return {"backend": backend, "task_exists": task_exists, "startup_exists": startup_exists, "message": description + ("；" + "；".join(errors) if errors else "")}


def check_release(repository: str, current_version: str) -> dict:
    repository = repository.strip().removeprefix("https://github.com/").rstrip("/")
    if not repository:
        return {"status": "unset", "message": "源码上传 GitHub 后，在设置中填写 owner/repository 即可检查更新"}
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("GitHub 仓库请填写 owner/repository")
    try:
        response = requests.get("https://api.github.com/repos/" + repository + "/releases/latest",
                                headers={"Accept": "application/vnd.github+json", "User-Agent": "TraeCNManager"},
                                timeout=(8, 15), allow_redirects=False)
        if response.status_code == 404:
            return {"status": "none", "message": "仓库尚未发布 Release，或不是公开仓库"}
        if response.status_code != 200:
            return {"status": "error", "message": f"GitHub 暂不可用（HTTP {response.status_code}）"}
        release = response.json()
        latest = str(release.get("tag_name", ""))
        def parts(v):
            return tuple(int(n) for n in re.findall(r"\d+", v)[:3])
        newer = parts(latest) > parts(current_version)
        url = str(release.get("html_url", ""))
        if not url.startswith("https://github.com/" + repository + "/releases/"):
            url = "https://github.com/" + repository + "/releases"
        return {"status": "available" if newer else "current", "message":
                f"发现新版本 {latest}" if newer else f"当前版本 {current_version}；最新发布 {latest}", "url": url}
    except (requests.RequestException, ValueError):
        return {"status": "error", "message": "无法连接 GitHub，请检查网络后重试"}
