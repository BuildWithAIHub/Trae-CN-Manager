"""Single EXE distribution; embeds the complete app and installs per user."""
from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import queue

from installer.core import install


def default_directory():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs" / "TraeCNManager"


def create_shortcut(directory):
    desktop_buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, desktop_buffer) != 0:
        raise OSError("未能取得当前用户桌面目录")
    link = Path(desktop_buffer.value) / "Trae CN 管家.lnk"
    # Values are arguments, never interpolated into shell code.
    script = 'param([string]$Link,[string]$Target,[string]$Working)\n$s=(New-Object -ComObject WScript.Shell).CreateShortcut($Link)\n$s.TargetPath=$Target\n$s.WorkingDirectory=$Working\n$s.IconLocation=$Target+",0"\n$s.Save()\n'
    with tempfile.TemporaryDirectory(prefix="tcn-shortcut-") as temporary:
        path = Path(temporary) / "shortcut.ps1"
        path.write_text(script, encoding="utf-8-sig")
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(path), str(link), str(directory / "TraeCNManager.exe"), str(directory)], check=True, timeout=15, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


def payload_path():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1] / "build" / "installer")) / "app-payload.zip"


def execute(destination, shortcut, progress=None):
    result = install(payload_path(), Path(destination), progress)
    if shortcut:
        try:
            create_shortcut(Path(result["directory"]))
            result["shortcut"] = True
        except Exception:
            # An optional shortcut failure must not misreport installation failure.
            result["shortcut"] = False
            result["notice"] = "安装已完成，桌面快捷方式未创建。请从安装目录打开 TraeCNManager.exe。"
    return result


COLORS = {"bg": "#121918", "bar": "#17221e", "card": "#1b2923",
          "input": "#13201a", "fg": "#edf6f0", "muted": "#9eb0a6",
          "border": "#34483d", "accent": "#a4edbd", "hover": "#2a4133"}


def prepare_windows_display():
    """Set awareness before Tk creates any HWND; the frozen EXE also has a manifest."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TraeCNManager.Setup")
    except (AttributeError, OSError):
        pass
    try:
        awareness = ctypes.windll.user32.SetProcessDpiAwarenessContext
        awareness.argtypes = [ctypes.c_void_p]
        awareness.restype = ctypes.c_bool
        # ERROR_ACCESS_DENIED is expected when the EXE manifest already applied it.
        awareness(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


def _native_window(window):
    from ctypes import wintypes
    user = ctypes.windll.user32
    user.GetParent.argtypes = [wintypes.HWND]
    user.GetParent.restype = wintypes.HWND
    return user.GetParent(window.winfo_id()) or window.winfo_id()


def _configure_display(root):
    from tkinter import font
    dpi = 96
    if os.name == "nt":
        try:
            from ctypes import wintypes
            get_dpi = ctypes.windll.user32.GetDpiForWindow
            get_dpi.argtypes, get_dpi.restype = [wintypes.HWND], wintypes.UINT
            dpi = get_dpi(_native_window(root)) or 96
        except (AttributeError, OSError):
            dpi = round(root.winfo_fpixels("1i"))
    root._display_scale = max(1, dpi / 96)
    families = set(font.families(root))
    root._font_family = next((name for name in ("Microsoft YaHei", "微软雅黑", "Microsoft YaHei UI", "Segoe UI")
                              if name in families), "TkDefaultFont")
    root.tk.call("tk", "scaling", dpi / 72)
    root.option_add("*Font", _font(root))


def _px(widget, value):
    return max(0, round(value * getattr(widget._root(), "_display_scale", 1)))


def _font(widget, pixels=14, weight="normal"):
    return (getattr(widget._root(), "_font_family", "Segoe UI"), -_px(widget, pixels), weight)


def _scale_layout(window):
    """Scale pixel spacing exactly once; text row/column counts stay in text units."""
    import tkinter as tk
    scale = getattr(window._root(), "_display_scale", 1)
    for widget in [window, *window.winfo_children()]:
        if widget is not window:
            _scale_layout(widget)
        if getattr(widget, "_layout_scaled", False):
            continue
        widget._layout_scaled = True
        if scale == 1:
            continue
        fields = ["padx", "pady", "borderwidth", "highlightthickness", "wraplength"]
        if widget.winfo_class() in {"Frame", "Canvas"}:
            fields += ["width", "height"]
        for option in fields:
            try:
                value = float(widget.cget(option))
                if value:
                    widget.configure(**{option: round(value * scale)})
            except (ValueError, TypeError, tk.TclError):
                pass
        if widget.winfo_manager() == "pack":
            info = widget.pack_info()
            for option in ("padx", "pady", "ipadx", "ipady"):
                value = info[option]
                parts = value if isinstance(value, (tuple, list)) else widget.tk.splitlist(str(value))
                widget.pack_configure(**{option: tuple(round(float(part) * scale) for part in parts)})


def _assets():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "assets"


def _apply_window_icon(window):
    """Tk and native wrapper both receive our icon, including all Toplevels."""
    import tkinter as tk
    assets = _assets()
    if (assets / "app.png").is_file():
        # Byte input avoids Tcl's system-codepage path conversion on some Windows builds.
        image = tk.PhotoImage(data=base64.b64encode((assets / "app.png").read_bytes()), master=window)
        window._brand_image = image
        window.iconphoto(True, image)
    if os.name == "nt" and (assets / "app.ico").is_file():
        try:
            window.iconbitmap(default=str(assets / "app.ico"))
            window.iconbitmap(str(assets / "app.ico"))
        except tk.TclError:
            # iconphoto supplies the Tk default; WM_SETICON uses the Unicode Win32 API.
            pass


def _set_native_icons(window):
    if os.name != "nt" or not (_assets() / "app.ico").is_file():
        return
    from ctypes import wintypes
    user = ctypes.windll.user32
    user.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user.LoadImageW.restype = wintypes.HANDLE
    user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.SendMessageW.restype = wintypes.LPARAM
    icons = getattr(window, "_native_icons", [])
    if not icons:
        for mode, size in ((0, 16), (1, 32)):
            handle = user.LoadImageW(None, str(_assets() / "app.ico"), 1, _px(window, size), _px(window, size), 0x10)
            if handle:
                icons.append((mode, handle))
        window._native_icons = icons
        def cleanup(event):
            if event.widget is window:
                user.DestroyIcon.argtypes = [wintypes.HICON]
                for _, handle in icons:
                    user.DestroyIcon(handle)
                icons.clear()
        window.bind("<Destroy>", cleanup, add=True)
    for mode, handle in icons:
        user.SendMessageW(_native_window(window), 0x80, mode, handle)


def _label(parent, text="", muted=False, **options):
    import tkinter as tk
    values = {"text": text, "bg": parent.cget("bg"),
              "fg": COLORS["muted"] if muted else COLORS["fg"],
              "anchor": "w", "justify": "left"}
    values.update(options)
    return tk.Label(parent, **values)


def _button(parent, text, command, primary=False, **options):
    import tkinter as tk
    values = {"text": text, "command": command, "relief": "flat", "bd": 0,
              "padx": 15, "pady": 9, "cursor": "hand2", "takefocus": True,
              "bg": COLORS["accent"] if primary else COLORS["card"],
              "fg": "#133023" if primary else COLORS["fg"],
              "activebackground": "#baf5ce" if primary else COLORS["hover"],
              "activeforeground": "#133023" if primary else COLORS["fg"],
              "disabledforeground": "#6d8174", "highlightthickness": 1,
              "highlightbackground": COLORS["border"], "highlightcolor": COLORS["accent"]}
    values.update(options)
    return tk.Button(parent, **values)


def _entry(parent, variable):
    import tkinter as tk
    return tk.Entry(parent, textvariable=variable, bg=COLORS["input"], fg=COLORS["fg"],
                    insertbackground=COLORS["accent"], relief="flat", bd=9,
                    highlightthickness=1, highlightbackground=COLORS["border"],
                    highlightcolor=COLORS["accent"], selectbackground=COLORS["hover"],
                    selectforeground=COLORS["fg"], disabledbackground=COLORS["bar"],
                    disabledforeground=COLORS["muted"])


def _center(window, width, height, parent=None):
    window.update_idletasks()
    if parent is not None and parent.winfo_viewable():
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
    else:
        x = (window.winfo_screenwidth() - width) // 2
        y = (window.winfo_screenheight() - height) // 2
    window.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")


class WindowChrome:
    """Tk-only chrome shared by the installer and all its dialogs."""
    def __init__(self, window, title, close, *, minimize=False):
        import tkinter as tk
        self.window, self.close = window, close
        self.drag_offset = None
        _apply_window_icon(window)
        window.overrideredirect(True)
        window.configure(bg=COLORS["border"])
        self.frame = tk.Frame(window, bg=COLORS["bg"])
        self.frame.pack(fill="both", expand=True, padx=1, pady=1)
        self.bar = tk.Frame(self.frame, bg=COLORS["bar"], height=48)
        self.bar.pack(fill="x")
        self.bar.pack_propagate(False)
        if hasattr(window, "_brand_image"):
            window._bar_image = window._brand_image.subsample(max(1, round(256 / _px(window, 28))))
            badge = _label(self.bar, image=window._bar_image, padx=12)
        else:
            badge = _label(self.bar, "✓", font=_font(window, 20), fg=COLORS["accent"], padx=12)
        badge.pack(side="left")
        caption = _label(self.bar, title, font=_font(window, 13))
        caption.pack(side="left", fill="both", expand=True)
        for target in (self.bar, badge, caption):
            target.bind("<ButtonPress-1>", self.start_drag)
            target.bind("<B1-Motion>", self.drag)
            target.bind("<ButtonRelease-1>", lambda _: setattr(self, "drag_offset", None))
        self.close_button = _button(self.bar, "×", close, padx=13, pady=2,
                                    font=_font(window, 23), bg=COLORS["bar"],
                                    activebackground="#a63c43", highlightthickness=0)
        self.close_button.pack(side="right", padx=(2, 7), pady=7)
        if minimize:
            control = _button(self.bar, "—", self.minimize, padx=12, pady=2,
                              bg=COLORS["bar"], highlightthickness=0)
            control.pack(side="right", pady=7)
            window.bind("<Map>", self._mapped, add=True)
        window.protocol("WM_DELETE_WINDOW", close)
        window.bind("<Alt-F4>", self._close_event)
        window.bind("<Escape>", self._close_event)
        window.bind("<Map>", lambda event: window.after_idle(lambda: _set_native_icons(window))
                    if event.widget is window else None, add=True)

    def _close_event(self, _event=None):
        self.close()
        return "break"

    def start_drag(self, event):
        self.drag_offset = (event.x_root - self.window.winfo_x(),
                            event.y_root - self.window.winfo_y())

    def drag(self, event):
        if self.drag_offset is not None:
            x, y = event.x_root - self.drag_offset[0], event.y_root - self.drag_offset[1]
            self.window.geometry(f"+{max(0, x)}+{max(0, y)}")

    def minimize(self):
        self.window.overrideredirect(False)
        self.window.iconify()

    def _mapped(self, event):
        if event.widget is self.window and self.window.state() != "iconic":
            self.window.overrideredirect(True)
            self.window.after_idle(self._taskbar)

    def _taskbar(self):
        # Override-redirect windows need APPWINDOW to remain accessible in Alt+Tab.
        if os.name != "nt" or not self.window.winfo_exists():
            return
        try:
            from ctypes import wintypes
            user = ctypes.windll.user32
            user.GetParent.argtypes = [wintypes.HWND]
            user.GetParent.restype = wintypes.HWND
            hwnd = user.GetParent(self.window.winfo_id()) or self.window.winfo_id()
            get_long = user.GetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user.GetWindowLongW
            set_long = user.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user.SetWindowLongW
            get_long.argtypes, get_long.restype = [wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t
            set_long.argtypes, set_long.restype = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t
            style = get_long(hwnd, -20)
            set_long(hwnd, -20, (style & ~0x80) | 0x40000)
            user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int, wintypes.UINT]
            user.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0027)
        except (OSError, AttributeError):
            pass


class ThemedDialog:
    def __init__(self, parent, title, width=570):
        import tkinter as tk
        self.parent, self.width = parent, width
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title(title)
        self.window.transient(parent)
        self.chrome = WindowChrome(self.window, title, self.close)
        self.body = tk.Frame(self.chrome.frame, bg=COLORS["bg"], padx=24, pady=20)
        self.body.pack(fill="both", expand=True)
        self.closed = False
        self.previous_grab = None

    def show(self, focus=None, height=None):
        _scale_layout(self.window)
        self.window.update_idletasks()
        _center(self.window, _px(self.window, self.width),
                _px(self.window, height) if height else self.window.winfo_reqheight(), self.parent)
        self.window.deiconify()
        self.window.lift()
        self.previous_grab = self.parent.grab_current()
        self.window.grab_set()
        (focus or self.window).focus_set()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.window.grab_release()
        self.window.destroy()
        if self.parent.winfo_exists():
            if self.previous_grab is not None and self.previous_grab.winfo_exists():
                self.previous_grab.grab_set()
            self.parent.lift()
            self.parent.focus_set()

    def wait(self):
        self.window.wait_window()


def themed_message(parent, title, text):
    import tkinter as tk
    dialog = ThemedDialog(parent, title)
    if len(text) < 1100:
        _label(dialog.body, text, wraplength=520).pack(fill="x", pady=(0, 18))
    else:
        content = tk.Text(dialog.body, height=13, width=60, wrap="word", bg=COLORS["input"],
                          fg=COLORS["fg"], insertbackground=COLORS["accent"], relief="flat", bd=10)
        content.insert("1.0", text[:12000])
        content.configure(state="disabled")
        content.pack(fill="both", expand=True, pady=(0, 18))
    control = _button(dialog.body, "确定", dialog.close, primary=True)
    control.pack(anchor="e")
    dialog.window.bind("<Return>", lambda _: dialog.close())
    dialog.show(control)
    dialog.wait()


def themed_input(parent, title, prompt):
    import tkinter as tk
    dialog = ThemedDialog(parent, title, width=500)
    value, result = tk.StringVar(), [None]
    _label(dialog.body, prompt, muted=True, wraplength=450).pack(fill="x", pady=(0, 10))
    field = _entry(dialog.body, value)
    field.pack(fill="x", pady=(0, 16))
    row = tk.Frame(dialog.body, bg=COLORS["bg"])
    row.pack(fill="x")

    def accept():
        if value.get().strip():
            result[0] = value.get().strip()
            dialog.close()

    _button(row, "创建", accept, primary=True).pack(side="right")
    _button(row, "取消", dialog.close).pack(side="right", padx=10)
    field.bind("<Return>", lambda _: accept())
    dialog.show(field)
    dialog.wait()
    return result[0]


class FolderBrowser:
    """A non-recursive, themed folder chooser with asynchronous directory reads."""
    def __init__(self, parent, initial):
        import tkinter as tk
        self.tk = tk
        self.dialog = ThemedDialog(parent, "选择安装位置", width=680)
        self.dialog.chrome.close = self.close
        self.dialog.chrome.close_button.configure(command=self.close)
        self.dialog.window.protocol("WM_DELETE_WINDOW", self.close)
        self.dialog.window.bind("<Escape>", self.close_event)
        self.dialog.window.bind("<Alt-F4>", self.close_event)
        self.current = None
        self.result = None
        self.generation = 0
        self.messages = queue.Queue()
        self.poll_id = None
        body = self.dialog.body
        _label(body, "选择上级文件夹，安装器将在其中创建 TraeCNManager。", muted=True).pack(anchor="w", pady=(0, 12))
        row = tk.Frame(body, bg=COLORS["bg"])
        row.pack(fill="x", pady=(0, 10))
        self.path = tk.StringVar(value=str(initial))
        field = _entry(row, self.path)
        field.pack(side="left", fill="x", expand=True)
        field.bind("<Return>", lambda _: self.load(self.path.get()))
        _button(row, "转到", lambda: self.load(self.path.get())).pack(side="right", padx=(8, 0))
        nav = tk.Frame(body, bg=COLORS["bg"])
        nav.pack(fill="x", pady=(0, 10))
        drive_button = _button(nav, "盘符 ▾", lambda: None)
        drive_button.pack(side="left")
        menu = tk.Menu(drive_button, tearoff=False, bg=COLORS["card"], fg=COLORS["fg"],
                       activebackground=COLORS["hover"], activeforeground=COLORS["fg"], bd=0)
        if os.name == "nt":
            mask = ctypes.windll.kernel32.GetLogicalDrives()
            drives = [f"{chr(65 + index)}:/" for index in range(26) if mask & (1 << index)]
        else:
            drives = ["/"]
        for drive in drives:
            menu.add_command(label=drive, command=lambda value=drive: self.load(value))
        drive_button.configure(command=lambda: menu.tk_popup(drive_button.winfo_rootx(), drive_button.winfo_rooty() + drive_button.winfo_height()))
        _button(nav, "上级文件夹", self.up).pack(side="left", padx=8)
        _button(nav, "新建文件夹", self.new_folder).pack(side="left")
        listing = tk.Frame(body, bg=COLORS["bg"])
        listing.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(listing, height=11, bg=COLORS["input"], fg=COLORS["fg"],
                                 selectbackground=COLORS["hover"], selectforeground=COLORS["fg"],
                                 activestyle="none", exportselection=False, relief="flat", bd=8,
                                 highlightthickness=1, highlightbackground=COLORS["border"])
        self.listbox.pack(side="left", fill="both", expand=True)
        scrollbar = tk.Scrollbar(listing, command=self.listbox.yview, bg=COLORS["bar"],
                                 troughcolor=COLORS["bg"], activebackground=COLORS["hover"], relief="flat", bd=0)
        scrollbar.pack(side="right", fill="y")
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.bind("<Double-Button-1>", lambda _: self.open_selected())
        self.listbox.bind("<Return>", lambda _: self.open_selected())
        self.status = tk.StringVar(value="正在读取当前文件夹…")
        _label(body, textvariable=self.status, muted=True, wraplength=625).pack(fill="x", pady=10)
        footer = tk.Frame(body, bg=COLORS["bg"])
        footer.pack(fill="x")
        self.choose = _button(footer, "选择此文件夹", self.accept, primary=True)
        self.choose.pack(side="right")
        _button(footer, "取消", self.close).pack(side="right", padx=10)
        self.dialog.show(field, height=560)
        self.load(initial, fallback=True)
        self.poll_id = self.dialog.window.after(60, self.poll)

    def load(self, value, fallback=False):
        if self.dialog.closed:
            return
        target = Path(os.path.expandvars(str(value).strip().strip('"'))).expanduser()
        self.generation += 1
        generation = self.generation
        self.choose.configure(state="disabled")
        self.listbox.configure(state="disabled")
        self.status.set("正在读取当前文件夹，不会递归扫描…")

        def worker():
            try:
                selected = target.absolute()
                if fallback:
                    while not selected.is_dir() and selected != selected.parent:
                        selected = selected.parent
                folders = []
                with os.scandir(selected) as entries:
                    for entry in entries:
                        if entry.is_dir(follow_symlinks=False):
                            folders.append(entry.name)
                            if len(folders) >= 5000:
                                break
                self.messages.put((generation, "loaded", (selected, sorted(folders, key=str.casefold))))
            except OSError:
                self.messages.put((generation, "error", "无法读取此目录，请检查路径或权限，也可直接在安装界面输入完整路径。"))

        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        if self.dialog.closed:
            return
        try:
            while True:
                generation, kind, value = self.messages.get_nowait()
                if generation != self.generation:
                    continue
                if kind == "loaded":
                    self.current, folders = value
                    self.path.set(str(self.current))
                    self.listbox.configure(state="normal")
                    self.listbox.delete(0, "end")
                    for folder in folders:
                        self.listbox.insert("end", folder)
                    self.choose.configure(state="normal")
                    self.status.set(f"当前目录有 {len(folders)} 个子文件夹。双击进入，或直接选择当前文件夹。")
                elif kind == "created":
                    self.load(value)
                else:
                    self.status.set(value)
        except queue.Empty:
            pass
        self.poll_id = self.dialog.window.after(60, self.poll)

    def open_selected(self):
        selected = self.listbox.curselection()
        if selected and self.current is not None:
            self.load(self.current / self.listbox.get(selected[0]))

    def up(self):
        if self.current is not None:
            self.load(self.current.parent)

    def new_folder(self):
        if self.current is None or str(self.choose.cget("state")) == "disabled":
            return
        name = themed_input(self.dialog.window, "新建文件夹", "输入文件夹名称（不包含路径分隔符）")
        if not name:
            return
        if name in (".", "..") or any(char in name for char in '\\/:*?"<>|\x00') or name.endswith((".", " ")):
            themed_message(self.dialog.window, "名称无效", "请使用有效的单个文件夹名称。")
            return
        parent, generation = self.current, self.generation

        def worker():
            try:
                destination = parent / name
                destination.mkdir()
                self.messages.put((generation, "created", destination))
            except OSError:
                self.messages.put((generation, "error", "未能创建文件夹，请检查名称、访问权限或是否已存在同名目录。"))

        threading.Thread(target=worker, daemon=True).start()

    def accept(self):
        if self.current is not None and str(self.choose.cget("state")) != "disabled":
            self.result = self.current
            self.close()

    def close(self):
        if self.poll_id is not None:
            self.dialog.window.after_cancel(self.poll_id)
            self.poll_id = None
        self.dialog.close()

    def close_event(self, _event=None):
        self.close()
        return "break"

    def wait(self):
        self.dialog.wait()
        return self.result


class InstallerGUI:
    def __init__(self, directory, shortcut, *, show=True, executor=None):
        import tkinter as tk
        from tkinter import ttk
        from trae_manager import __version__
        self.tk, self.executor = tk, executor or execute
        prepare_windows_display()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("安装 Trae CN 管家")
        _configure_display(self.root)
        self.root.resizable(False, False)
        self.busy = False
        self.closed = False
        self.messages = queue.Queue()
        self.poll_id = None
        self.chrome = WindowChrome(self.root, "Trae CN 管家 · 安装", self.request_close, minimize=True)
        frame = tk.Frame(self.chrome.frame, bg=COLORS["bg"], padx=30, pady=25)
        frame.pack(fill="both", expand=True)
        heading = tk.Frame(frame, bg=COLORS["bg"])
        heading.pack(fill="x")
        _label(heading, "安装 Trae CN 管家", font=_font(self.root, 26)).pack(side="left")
        _label(heading, "v" + __version__, muted=True).pack(side="right", pady=(10, 0))
        _label(frame, "选好位置，即可安装。无需另外下载运行环境。", muted=True).pack(anchor="w", pady=(8, 25))
        _label(frame, "安装位置", font=_font(self.root, 14)).pack(anchor="w")
        row = tk.Frame(frame, bg=COLORS["bg"])
        row.pack(fill="x", pady=(9, 8))
        self.selected = tk.StringVar(value=str(directory))
        self.entry = _entry(row, self.selected)
        self.entry.pack(side="left", fill="x", expand=True)
        self.choose = _button(row, "浏览文件夹…", self.browse)
        self.choose.pack(side="right", padx=(10, 0))
        _label(frame, "支持任意可写盘符，无需管理员权限。", muted=True).pack(anchor="w", pady=(0, 18))
        self.shortcut_value = tk.BooleanVar(value=shortcut)
        self.launch_value = tk.BooleanVar(value=True)
        self.checks = []
        for text, variable in [("创建桌面快捷方式", self.shortcut_value), ("安装完成后打开管家", self.launch_value)]:
            control = tk.Checkbutton(frame, text=text, variable=variable, bg=COLORS["bg"], fg=COLORS["fg"],
                                     selectcolor=COLORS["card"], activebackground=COLORS["bg"],
                                     activeforeground=COLORS["fg"], highlightthickness=0, bd=0,
                                     cursor="hand2", anchor="w", padx=0)
            control.pack(anchor="w", pady=3)
            self.checks.append(control)
        _label(frame, "升级会保留原安装目录 data 文件夹中的账号、设置和备份。", muted=True,
               wraplength=635).pack(anchor="w", pady=(18, 14))
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Installer.Horizontal.TProgressbar", troughcolor=COLORS["card"],
                        background=COLORS["accent"], bordercolor=COLORS["card"],
                        lightcolor=COLORS["accent"], darkcolor=COLORS["accent"], thickness=_px(self.root, 6))
        self.progress = ttk.Progressbar(frame, maximum=100, style="Installer.Horizontal.TProgressbar")
        self.progress.pack(fill="x", ipady=0)
        self.status = tk.StringVar(value="准备就绪")
        _label(frame, textvariable=self.status, muted=True).pack(anchor="w", pady=(10, 18))
        footer = tk.Frame(frame, bg=COLORS["bg"])
        footer.pack(fill="x", side="bottom")
        self.button = _button(footer, "开始安装", self.begin, primary=True, padx=24)
        self.button.pack(side="right")
        self.exit_button = _button(footer, "退出", self.request_close)
        self.exit_button.pack(side="right", padx=12)
        _label(footer, "本地安装 · 当前用户", muted=True).pack(side="left", pady=10)
        self.controls = [self.entry, self.choose, self.button, self.exit_button, *self.checks]
        _scale_layout(self.root)
        self.root.update_idletasks()
        _center(self.root, _px(self.root, 740), max(_px(self.root, 540), self.root.winfo_reqheight()))
        if show:
            self.root.deiconify()
            self.root.after_idle(self.entry.focus_set)
        self.poll_id = self.root.after(80, self.poll)

    def _set_busy(self, busy):
        self.busy = busy
        for control in self.controls:
            control.configure(state="disabled" if busy else "normal")

    def request_close(self):
        if self.busy:
            self.status.set("正在安装，请等待完成后再关闭。")
            return False
        if not self.closed:
            self.closed = True
            if self.poll_id is not None:
                self.root.after_cancel(self.poll_id)
                self.poll_id = None
            self.root.destroy()
        return True

    def message(self, title, text):
        themed_message(self.root, title, text)

    def browse(self):
        if self.busy:
            return
        folder = FolderBrowser(self.root, self.selected.get()).wait()
        if folder:
            self.selected.set(str(folder if folder.name.casefold() == "traecnmanager" else folder / "TraeCNManager"))
            self.entry.focus_set()

    def begin(self):
        if self.busy:
            return
        destination = self.selected.get().strip().strip('"')
        if not destination:
            self.message("请选择目录", "请输入安装目录，或使用浏览文件夹选择位置。")
            self.entry.focus_set()
            return
        self._set_busy(True)
        self.progress["value"] = 0
        self.status.set("正在校验安装包并安装…")
        shortcut = self.shortcut_value.get()

        def worker():
            try:
                result = self.executor(destination, shortcut, lambda value: self.messages.put(("progress", value)))
                self.messages.put(("done", result))
            except Exception as exc:
                self.messages.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        if self.closed:
            return
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "progress":
                    self.progress["value"] = value
                    self.status.set(f"正在安装… {int(value)}%")
                elif kind == "error":
                    self._set_busy(False)
                    self.status.set("安装未完成，可检查后重试。")
                    self.message("安装未完成", value + "\n\n若文件正在使用，请退出旧版管家及其后台签到后再试。")
                    self.entry.focus_set()
                elif kind == "done":
                    self._set_busy(False)
                    self.progress["value"] = 100
                    self.status.set("安装完成")
                    self.message("安装完成", value.get("notice", "完整程序已安装，可以通过桌面快捷方式或安装目录打开。"))
                    if self.launch_value.get():
                        try:
                            subprocess.Popen([str(Path(value["directory"]) / "TraeCNManager.exe")], cwd=value["directory"])
                        except OSError:
                            self.message("程序暂未打开", "安装已完成，但系统未能启动程序。请从安装目录手动打开 TraeCNManager.exe。")
                    self.request_close()
                    return
        except queue.Empty:
            pass
        self.poll_id = self.root.after(80, self.poll)


def gui(directory, shortcut):
    application = InstallerGUI(directory, shortcut)
    application.root.mainloop()
    return 0


def main():
    parser = argparse.ArgumentParser(description="Trae CN 管家完整安装包")
    parser.add_argument("--install-dir", type=Path, default=default_directory())
    parser.add_argument("--silent", action="store_true")
    parser.add_argument("--no-shortcut", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not args.silent:
        return gui(args.install_dir, not args.no_shortcut)
    try:
        result = execute(args.install_dir, not args.no_shortcut)
        code = 0
    except Exception as exc:
        result, code = {"installed": False, "error": str(exc)}, 1
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
