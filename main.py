from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description="Trae CN 管家")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--background", action="store_true", help="无计划任务权限时的当前用户后台签到进程")
    parser.add_argument("--self-test", action="store_true", help="不访问真实账号的可执行文件自检")
    parser.add_argument("--ui-smoke", type=Path, help="在指定文件夹生成隔离界面截图并退出")
    args = parser.parse_args()
    if args.self_test:
        from trae_manager.selftest import run_self_test
        result = run_self_test()
        if sys.stdout:
            print(json.dumps(result, ensure_ascii=False))
        return 0 if result["passed"] else 1
    from trae_manager.service import Controller
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    data_dir = args.data_dir or base / "data"
    controller = Controller(data_dir)
    if args.scheduled or args.background:
        try:
            return controller.run_background() if args.background else controller.run_scheduled()
        except Exception:
            return 1
        finally:
            controller.close()
    if args.ui_smoke:
        from trae_manager.selftest import ui_smoke
        return ui_smoke(controller, args.ui_smoke)
    from trae_manager.ui import run
    try:
        return run(controller)
    finally:
        controller.close()


if __name__ == "__main__":
    raise SystemExit(main())
