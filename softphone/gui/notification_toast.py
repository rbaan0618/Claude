"""Frameless bottom-right toast notification for incoming messages.

Appears for TOAST_DURATION milliseconds, plays a system sound, and can be
clicked to open the relevant conversation.  No third-party dependencies —
uses only tkinter and winsound (both bundled with Python on Windows).
"""

import tkinter as tk
import winsound

TOAST_DURATION = 6000   # ms before auto-dismiss
TOAST_WIDTH    = 320
TOAST_HEIGHT   = 76
TOAST_MARGIN   = 18     # px from screen edge
TASKBAR_HEIGHT = 48     # approximate Windows taskbar height

WHATSAPP_GREEN = "#25D366"


class NotificationToast(tk.Toplevel):
    """Single-message toast that slides up from the bottom-right corner."""

    def __init__(self, master, peer, body, channel, colors, on_click=None):
        super().__init__(master)
        self._on_click = on_click

        accent   = WHATSAPP_GREEN if channel == "whatsapp" else colors["accent"]
        bg       = colors["status_bar"]   # darkest panel bg — looks clean
        fg       = colors["fg"]
        fg_dim   = colors["fg_dim"]
        ch_label = "WhatsApp" if channel == "whatsapp" else "SMS"

        # ── Window chrome ────────────────────────────────────────────────────
        self.overrideredirect(True)          # no title bar
        self.attributes("-topmost", True)    # float above everything
        self.configure(bg=bg)

        # Position: bottom-right, above taskbar
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x  = sw - TOAST_WIDTH  - TOAST_MARGIN
        y  = sh - TOAST_HEIGHT - TOAST_MARGIN - TASKBAR_HEIGHT
        self.geometry(f"{TOAST_WIDTH}x{TOAST_HEIGHT}+{x}+{y}")

        # ── Layout ───────────────────────────────────────────────────────────
        # Coloured left accent bar
        tk.Frame(self, bg=accent, width=5).pack(side=tk.LEFT, fill=tk.Y)

        # Main content area
        content = tk.Frame(self, bg=bg, padx=10, pady=8)
        content.pack(fill=tk.BOTH, expand=True)

        # Header row: channel label + close button
        hdr = tk.Frame(content, bg=bg)
        hdr.pack(fill=tk.X)

        tk.Label(hdr,
                 text=f"  {ch_label}  ",
                 font=("Segoe UI", 8, "bold"),
                 bg=accent, fg="#ffffff").pack(side=tk.LEFT)

        tk.Label(hdr,
                 text=peer,
                 font=("Segoe UI", 9, "bold"),
                 bg=bg, fg=fg).pack(side=tk.LEFT, padx=(8, 0))

        close_btn = tk.Label(hdr, text="✕",
                             font=("Segoe UI", 9),
                             bg=bg, fg=fg_dim, cursor="hand2")
        close_btn.pack(side=tk.RIGHT)
        close_btn.bind("<Button-1>", lambda e: self._dismiss())

        # Message preview
        preview = body[:70] + ("…" if len(body) > 70 else "")
        msg_lbl = tk.Label(content,
                           text=preview,
                           font=("Segoe UI", 9),
                           bg=bg, fg=fg_dim,
                           anchor=tk.W, justify=tk.LEFT,
                           wraplength=TOAST_WIDTH - 30)
        msg_lbl.pack(fill=tk.X, pady=(4, 0))

        # ── Interactions ─────────────────────────────────────────────────────
        for widget in [self, content, hdr, msg_lbl]:
            widget.bind("<Button-1>", self._clicked)
        # Don't let click on close bubble up to _clicked
        close_btn.bind("<Button-1>", lambda e: (self._dismiss(), "break"),
                       add=False)

        # ── Auto-dismiss ─────────────────────────────────────────────────────
        self._dismiss_id = self.after(TOAST_DURATION, self._dismiss)

        # ── Sound ────────────────────────────────────────────────────────────
        try:
            # MB_ICONASTERISK = the Windows "asterisk" / information chime
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _clicked(self, event=None):
        self._dismiss()
        if self._on_click:
            try:
                self._on_click()
            except Exception:
                pass

    def _dismiss(self, event=None):
        try:
            self.after_cancel(self._dismiss_id)
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass
