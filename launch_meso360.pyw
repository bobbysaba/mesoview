"""
meso360 Launch Dialog
Double-click (Windows) or run via launch_meso360.command (macOS) to start the supervisor.
Runs supervisor.py inside the meso360 conda environment.
"""
from __future__ import annotations

import subprocess
import sys
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

REPO_DIR = Path(__file__).parent
ICON_ICO = REPO_DIR / 'meso360.ico'
ICON_PNG = REPO_DIR / 'meso360.png'


def _conda_python_cmd() -> list:
    """Return a command prefix that runs Python in the meso360 conda env.

    Strategy: find python.exe directly inside the env rather than using
    'conda run -n meso360', because conda resolves env names by the first
    match in environments.txt — which may not be the env where packages
    are actually installed (e.g. mamba user envs vs miniforge envs).
    """
    import os
    exe = 'python.exe' if sys.platform == 'win32' else 'bin/python'

    # Build candidate list — checked in order, first match wins
    candidates: list[Path] = []

    # 1. All envs registered in conda/mamba environments.txt files
    if sys.platform == 'win32':
        for env_txt in [
            Path(os.environ.get('USERPROFILE', '')) / '.conda' / 'environments.txt',
            Path(os.environ.get('USERPROFILE', '')) / '.mamba' / 'environments.txt',
        ]:
            if env_txt.exists():
                for line in env_txt.read_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith('#') and 'meso360' in line:
                        candidates.append(Path(line) / 'python.exe')

    # 2. Well-known install locations
    candidates += [
        Path.home() / '.local' / 'share' / 'mamba' / 'envs' / 'meso360' / exe,
        Path.home() / 'miniforge3'  / 'envs' / 'meso360' / exe,
        Path.home() / 'anaconda3'   / 'envs' / 'meso360' / exe,
        Path.home() / 'miniconda3'  / 'envs' / 'meso360' / exe,
        Path('C:/ProgramData/miniforge3/envs/meso360/python.exe'),
        Path('C:/ProgramData/anaconda3/envs/meso360/python.exe'),
        Path('/opt/homebrew/Caskroom/miniforge/base/envs/meso360/bin/python'),
        Path('/opt/conda/envs/meso360/bin/python'),
    ]

    for p in candidates:
        if p.exists():
            return [str(p)]

    # Last resort: conda run (may pick the wrong env if multiple meso360 envs exist)
    import shutil
    conda = shutil.which('conda')
    if conda:
        return [conda, 'run', '--no-capture-output', '-n', 'meso360', 'python']

    return [sys.executable]


class LaunchDialog:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('meso360')
        self.root.resizable(False, False)
        self._icon_ref = None   # keep PhotoImage alive
        self._set_icon()
        self._build_ui()
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        x = (self.root.winfo_screenwidth()  - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f'+{x}+{y}')

    def _set_icon(self):
        if sys.platform == 'darwin':
            # iconbitmap doesn't work on macOS; iconphoto does
            if ICON_PNG.exists():
                try:
                    img = tk.PhotoImage(file=str(ICON_PNG))
                    self.root.iconphoto(True, img)
                    self._icon_ref = img
                except Exception:
                    pass
        else:
            if ICON_ICO.exists():
                try:
                    self.root.iconbitmap(str(ICON_ICO))
                except Exception:
                    pass

    def _build_ui(self):
        PAD = 18
        frame = ttk.Frame(self.root, padding=PAD)
        frame.grid(row=0, column=0)

        row = 0

        # Logo image
        self._img = None
        if ICON_PNG.exists():
            try:
                raw = tk.PhotoImage(file=str(ICON_PNG))
                w, h = raw.width(), raw.height()
                factor = max(w // 72, h // 72, 1)
                self._img = raw.subsample(factor, factor)
                ttk.Label(frame, image=self._img).grid(
                    row=row, column=0, columnspan=2, pady=(0, 10))
                row += 1
            except Exception:
                pass

        ttk.Label(frame, text='meso360', font=('Segoe UI', 15, 'bold')).grid(
            row=row, column=0, columnspan=2)
        row += 1
        ttk.Label(frame, text='Mesonet 360° Supervisor', foreground='gray').grid(
            row=row, column=0, columnspan=2, pady=(2, 14))
        row += 1

        self._test_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text='Test mode  (replay test_data/test.txt at 1 Hz)',
                        variable=self._test_var).grid(
            row=row, column=0, columnspan=2, sticky='w', pady=(0, 18))
        row += 1

        btn = ttk.Frame(frame)
        btn.grid(row=row, column=0, columnspan=2)
        ttk.Button(btn, text='Launch', command=self._launch, width=13).grid(
            row=0, column=0, padx=5)
        ttk.Button(btn, text='Cancel', command=self.root.destroy, width=13).grid(
            row=0, column=1, padx=5)

        self.root.bind('<Return>', lambda _e: self._launch())
        self.root.bind('<Escape>', lambda _e: self.root.destroy())

    def _launch(self):
        cmd = _conda_python_cmd() + [str(REPO_DIR / 'supervisor.py')]
        if self._test_var.get():
            cmd.append('--test')
        log_path = REPO_DIR / 'launch.log'
        try:
            if sys.platform == 'win32':
                with open(log_path, 'w') as log_fh:
                    log_fh.write(f'cmd: {cmd}\n')
                    log_fh.flush()
                subprocess.Popen(cmd, cwd=str(REPO_DIR),
                                 creationflags=subprocess.CREATE_NEW_CONSOLE,
                                 stdout=open(log_path, 'a'),
                                 stderr=subprocess.STDOUT)
            else:
                subprocess.Popen(cmd, cwd=str(REPO_DIR), start_new_session=True)
        except Exception as exc:
            messagebox.showerror('Launch failed', str(exc))
            return
        messagebox.showinfo('meso360 launching',
                            f'Supervisor starting.\n\nIf it crashes, check:\n{log_path}')
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == '__main__':
    LaunchDialog().run()
