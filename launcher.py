import tkinter as tk
import subprocess
import sys
import os
import webbrowser
import time
import threading

PYTHONW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
if not os.path.exists(PYTHONW):
    PYTHONW = sys.executable

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_URL = "http://127.0.0.1:5000"
LOG_PATH = os.path.join(APP_DIR, "server.log")

class Launcher:
    def __init__(self):
        self.process = None
        self.log_file = None

        self.root = tk.Tk()
        self.root.title("ELD Checker")
        self.root.geometry("300x170")
        self.root.resizable(False, False)
        self.root.configure(bg="#1a1a2e")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Try to keep on top by default (user can move it)
        self.root.attributes("-topmost", True)

        # ── Title bar ───────────────────────────────────────────────
        title_frame = tk.Frame(self.root, bg="#1a1a2e", pady=12)
        title_frame.pack(fill="x", padx=20)

        tk.Label(
            title_frame, text="ELD Checker", font=("Segoe UI", 13, "bold"),
            bg="#1a1a2e", fg="white"
        ).pack(side="left")

        # ── Status row ──────────────────────────────────────────────
        status_frame = tk.Frame(self.root, bg="#1a1a2e")
        status_frame.pack(fill="x", padx=20)

        self.dot = tk.Label(status_frame, text="●", font=("Segoe UI", 14),
                            bg="#1a1a2e", fg="#e53e3e")
        self.dot.pack(side="left")

        self.status_label = tk.Label(
            status_frame, text="Server stopped", font=("Segoe UI", 9),
            bg="#1a1a2e", fg="#a0aec0"
        )
        self.status_label.pack(side="left", padx=(6, 0))

        # ── Buttons ─────────────────────────────────────────────────
        btn_frame = tk.Frame(self.root, bg="#1a1a2e", pady=16)
        btn_frame.pack(fill="x", padx=20)

        self.start_btn = tk.Button(
            btn_frame, text="▶  Start", font=("Segoe UI", 10, "bold"),
            bg="#38a169", fg="white", activebackground="#2f855a",
            activeforeground="white", relief="flat", cursor="hand2",
            width=9, pady=7, command=self.start_server
        )
        self.start_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = tk.Button(
            btn_frame, text="■  Stop", font=("Segoe UI", 10, "bold"),
            bg="#4a5568", fg="#a0aec0", activebackground="#2d3748",
            activeforeground="white", relief="flat", cursor="hand2",
            width=9, pady=7, command=self.stop_server, state="disabled"
        )
        self.stop_btn.pack(side="left", padx=(0, 8))

        # ── Open browser link ────────────────────────────────────────
        self.open_btn = tk.Button(
            self.root, text="Open in Browser →",
            font=("Segoe UI", 8), bg="#1a1a2e", fg="#4a9eff",
            activebackground="#1a1a2e", activeforeground="#90cdf4",
            relief="flat", cursor="hand2", state="disabled",
            command=lambda: webbrowser.open(SERVER_URL)
        )
        self.open_btn.pack(pady=(0, 4))

        self.root.mainloop()

    def start_server(self):
        if self.process is not None:
            return

        self.status_label.config(text="Starting…", fg="#ecc94b")
        self.dot.config(fg="#ecc94b")
        self.start_btn.config(state="disabled")
        self.root.update()

        # Keep the server's output — a crash on startup (bad config, port already
        # taken) is otherwise invisible and the UI would report a false "running".
        self.log_file = open(LOG_PATH, "w", encoding="utf-8")
        self.process = subprocess.Popen(
            [PYTHONW, "app.py"],
            cwd=APP_DIR,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
        )

        # Wait briefly then confirm it's up
        threading.Thread(target=self._wait_for_server, daemon=True).start()

    def _wait_for_server(self):
        import urllib.request
        for _ in range(20):
            if self.process is not None and self.process.poll() is not None:
                self.root.after(0, self._set_failed)   # it exited on its own
                return
            try:
                urllib.request.urlopen(SERVER_URL, timeout=1)
                self.root.after(0, self._set_running)
                return
            except Exception:
                time.sleep(0.4)
        self.root.after(0, self._set_failed)

    def _set_failed(self):
        self.dot.config(fg="#e53e3e")
        self.status_label.config(text="Failed — see server.log", fg="#fc8181")
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled", bg="#4a5568", fg="#a0aec0")
        self.open_btn.config(state="disabled")
        self._close_log()

    def _set_running(self):
        self.dot.config(fg="#38a169")
        self.status_label.config(text=f"Running on port 5000", fg="#68d391")
        self.stop_btn.config(state="normal", bg="#e53e3e", fg="white",
                             activebackground="#c53030")
        self.open_btn.config(state="normal")
        webbrowser.open(SERVER_URL)

    def _close_log(self):
        log = getattr(self, "log_file", None)
        if log is not None and not log.closed:
            log.close()

    def stop_server(self):
        if self.process is None:
            return

        self.process.terminate()
        try:
            self.process.wait(timeout=4)
        except Exception:
            self.process.kill()
        self.process = None
        self._close_log()

        self.dot.config(fg="#e53e3e")
        self.status_label.config(text="Server stopped", fg="#a0aec0")
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled", bg="#4a5568", fg="#a0aec0")
        self.open_btn.config(state="disabled")

    def on_close(self):
        self.stop_server()
        self.root.destroy()


if __name__ == "__main__":
    Launcher()
