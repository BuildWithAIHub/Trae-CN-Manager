"""No real tasks, registry writes, or background processes are created."""
from pathlib import Path
from unittest import mock

import pytest

from trae_manager import platforms


def fake_registry():
    registry = mock.MagicMock()
    registry.HKEY_CURRENT_USER = 1
    registry.REG_SZ = 1
    registry.KEY_READ = 1
    registry.KEY_SET_VALUE = 2
    registry.QueryValueEx.side_effect = FileNotFoundError
    return registry


def test_background_command_has_mode_but_no_credentials(tmp_path):
    executable, args = platforms.launch_command(tmp_path, background=True)
    assert args[-1] == "--background"
    assert "--scheduled" not in args
    assert "--data-dir" in args
    assert not any(word in " ".join(args).lower() for word in ("token", "password", "secret"))
    with pytest.raises(ValueError):
        platforms.launch_command(tmp_path, scheduled=True, background=True)


def test_background_registry_uses_distinct_app_value(tmp_path):
    registry = fake_registry()
    with mock.patch.object(platforms, "_WINDOWS", True), mock.patch.object(platforms, "winreg", registry):
        platforms.configure_background_startup(tmp_path, True)
        call = registry.SetValueEx.call_args
        assert call.args[1] == platforms.task_name(tmp_path) + "-Background"
        assert "--background" in call.args[4]
        platforms.configure_background_startup(tmp_path, False)
        assert registry.DeleteValue.call_args.args[1] == platforms.task_name(tmp_path) + "-Background"


def test_task_success_cleans_fallback_without_spawning(tmp_path):
    with mock.patch.object(platforms, "configure_task") as task, mock.patch.object(platforms, "configure_background_startup") as startup, mock.patch.object(platforms, "start_background") as spawn:
        result = platforms.configure_schedule(tmp_path, True, "09:30")
    assert result["backend"] == "task"
    task.assert_called_once_with(tmp_path, True, "09:30")
    startup.assert_called_once_with(tmp_path, False)
    spawn.assert_not_called()


def test_task_denial_falls_back_to_startup(tmp_path):
    with mock.patch.object(platforms, "configure_task", side_effect=RuntimeError("access denied")), mock.patch.object(platforms, "_background_value", return_value=None), mock.patch.object(platforms, "configure_background_startup") as startup, mock.patch.object(platforms, "start_background") as spawn:
        result = platforms.configure_schedule(tmp_path, True, "09:30")
    assert result["backend"] == "startup"
    assert "access denied" in result["message"]
    startup.assert_called_once_with(tmp_path, True)
    spawn.assert_called_once_with(tmp_path)


def test_fallback_can_defer_start_until_settings_commit(tmp_path):
    with mock.patch.object(platforms, "configure_task", side_effect=RuntimeError("denied")), mock.patch.object(platforms, "_background_value", return_value=None), mock.patch.object(platforms, "configure_background_startup"), mock.patch.object(platforms, "start_background") as spawn:
        assert platforms.configure_schedule(tmp_path, True, "09:30", start=False)["backend"] == "startup"
    spawn.assert_not_called()


def test_double_failure_reports_both_backends(tmp_path):
    with mock.patch.object(platforms, "configure_task", side_effect=RuntimeError("task denied")), mock.patch.object(platforms, "_background_value", return_value=None), mock.patch.object(platforms, "configure_background_startup", side_effect=PermissionError("registry denied")):
        with pytest.raises(RuntimeError) as error:
            platforms.configure_schedule(tmp_path, True, "09:30")
    assert "task denied" in str(error.value)
    assert "registry denied" in str(error.value)


def test_launch_failure_rolls_back_existing_registry_value(tmp_path):
    original = ('"C:\\old manager.exe" --background', 1)
    with mock.patch.object(platforms, "configure_task", side_effect=RuntimeError("denied")), mock.patch.object(platforms, "_background_value", return_value=original), mock.patch.object(platforms, "configure_background_startup"), mock.patch.object(platforms, "start_background", side_effect=RuntimeError("spawn failed")), mock.patch.object(platforms, "_restore_background_value") as rollback:
        with pytest.raises(RuntimeError) as error:
            platforms.configure_schedule(tmp_path, True, "09:30")
    rollback.assert_called_once_with(tmp_path, original)
    assert "spawn failed" in str(error.value)


def test_disable_attempts_both_owned_backends_even_if_task_delete_fails(tmp_path):
    with mock.patch.object(platforms, "configure_task", side_effect=RuntimeError("delete denied")), mock.patch.object(platforms, "configure_background_startup") as startup:
        with pytest.raises(RuntimeError):
            platforms.configure_schedule(tmp_path, False, "09:30")
    startup.assert_called_once_with(tmp_path, False)


def test_background_popen_is_hidden_and_has_only_mode_and_path(tmp_path):
    with mock.patch.object(platforms, "_WINDOWS", True), mock.patch.object(platforms.subprocess, "Popen") as spawn:
        platforms.start_background(tmp_path)
    call = spawn.call_args
    assert "--background" in call.args[0]
    assert call.kwargs["shell"] is False
    assert call.kwargs["stdout"] == platforms.subprocess.DEVNULL
    assert call.kwargs["stderr"] == platforms.subprocess.DEVNULL
    assert call.kwargs["close_fds"]
    if hasattr(platforms.subprocess, "CREATE_NO_WINDOW"):
        assert call.kwargs["creationflags"] & platforms.subprocess.CREATE_NO_WINDOW


def test_query_reports_actual_registry_fallback_and_unknown_task_permissions(tmp_path):
    denied = mock.Mock(returncode=1, stdout=b"", stderr=b"Access denied")
    with mock.patch.object(platforms, "_WINDOWS", True), mock.patch.object(platforms.subprocess, "run", return_value=denied), mock.patch.object(platforms, "_background_value", return_value=("synthetic command", 1)):
        result = platforms.query_scheduler(tmp_path)
    assert result["backend"] == "startup"
    assert result["startup_exists"] is True
    assert result["task_exists"] is None


def test_query_absence_is_not_a_task_success(tmp_path):
    absent = mock.Mock(returncode=1, stdout=b"", stderr=b"The system cannot find the file specified")
    with mock.patch.object(platforms, "_WINDOWS", True), mock.patch.object(platforms.subprocess, "run", return_value=absent), mock.patch.object(platforms, "_background_value", return_value=None):
        result = platforms.query_scheduler(tmp_path)
    assert result["backend"] == "off"
    assert result["task_exists"] is False


@pytest.mark.skipif(platforms.os.name != "nt", reason="Existing schtasks integration is Windows-only")
def test_disabling_task_does_not_hide_query_permission_denial(tmp_path):
    denied = mock.Mock(returncode=1, stdout=b"", stderr=b"Access denied")
    with mock.patch.object(platforms.subprocess, "run", return_value=denied) as command:
        with pytest.raises(RuntimeError):
            platforms.configure_task(tmp_path, False, "09:30")
    assert command.call_count == 1


@pytest.mark.skipif(platforms.os.name != "nt", reason="Existing schtasks integration is Windows-only")
def test_disabling_absent_task_is_idempotent(tmp_path):
    absent = mock.Mock(returncode=1, stdout=b"", stderr=b"The system cannot find the file specified")
    with mock.patch.object(platforms.subprocess, "run", return_value=absent) as command:
        platforms.configure_task(tmp_path, False, "09:30")
    assert command.call_count == 1
