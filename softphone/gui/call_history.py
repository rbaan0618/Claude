"""Call history panel widget."""

import tkinter as tk
from gui.theme import get_theme
from utils.database import get_call_history, delete_call_record, clear_call_history
from datetime import datetime


class CallHistoryPanel(tk.Frame):
    """Panel showing call history with filtering."""

    DIRECTION_ICONS = {
        "outbound": "\u2197",   # arrow upper-right
        "inbound": "\u2199",    # arrow lower-left
    }
    STATUS_COLORS_KEY = {
        "answered": "green",
        "missed": "red",
        "rejected": "orange",
        "failed": "red",
    }

    def __init__(self, parent, theme_name="dark", on_redial=None, **kwargs):
        self.colors = get_theme(theme_name)
        super().__init__(parent, bg=self.colors["bg_secondary"], **kwargs)
        self._on_redial = on_redial
        self._filter = None  # None = all, 'inbound', 'outbound'
        self._build()
        self.refresh()

    def _build(self):
        c = self.colors

        # Header with filter tabs
        header = tk.Frame(self, bg=c["bg_secondary"])
        header.pack(fill=tk.X, padx=8, pady=(8, 4))

        tk.Label(header, text="Call History", font=("Segoe UI", 11, "bold"),
                 bg=c["bg_secondary"], fg=c["fg"]).pack(side=tk.LEFT)

        # Clear button
        clear_btn = tk.Label(header, text="Clear", font=("Segoe UI", 9),
                             bg=c["bg_secondary"], fg=c["red"], cursor="hand2")
        clear_btn.pack(side=tk.RIGHT, padx=(8, 0))
        clear_btn.bind("<Button-1>", lambda e: self._clear_all())

        # Filter tabs
        filter_frame = tk.Frame(self, bg=c["bg_secondary"])
        filter_frame.pack(fill=tk.X, padx=8, pady=(0, 4))

        self._filter_buttons = {}
        for label, value in [("All", None), ("Received", "inbound"), ("Dialed", "outbound")]:
            btn = tk.Label(filter_frame, text=label, font=("Segoe UI", 9),
                           bg=c["button_bg"] if value != self._filter else c["accent"],
                           fg=c["button_fg"] if value != self._filter else "#ffffff",
                           padx=10, pady=2, cursor="hand2")
            btn.pack(side=tk.LEFT, padx=2)
            btn.bind("<Button-1>", lambda e, v=value: self._set_filter(v))
            self._filter_buttons[value] = btn

        # Scrollable list
        self.canvas = tk.Canvas(self, bg=c["bg_secondary"], highlightthickness=0)
        self.scrollbar = tk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.inner_frame = tk.Frame(self.canvas, bg=c["bg_secondary"])

        self.inner_frame.bind("<Configure>",
                              lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.inner_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def refresh(self):
        """Reload call history from database."""
        # Clear existing widgets
        for widget in self.inner_frame.winfo_children():
            widget.destroy()

        records = get_call_history(limit=200, direction_filter=self._filter)
        c = self.colors

        if not records:
            tk.Label(self.inner_frame, text="No calls yet",
                     font=("Segoe UI", 10), bg=c["bg_secondary"],
                     fg=c["fg_dim"]).pack(pady=20)
            return

        for record in records:
            self._create_record_widget(record)

    def _create_record_widget(self, record):
        c = self.colors

        frame = tk.Frame(self.inner_frame, bg=c["bg"], padx=8, pady=4,
                         cursor="hand2")
        frame.pack(fill=tk.X, padx=4, pady=1)

        # Direction icon
        direction = record.get("direction", "outbound")
        icon = self.DIRECTION_ICONS.get(direction, "?")
        status = record.get("status", "answered")
        icon_color = c.get(self.STATUS_COLORS_KEY.get(status, "fg_dim"), c["fg_dim"])

        tk.Label(frame, text=icon, font=("Segoe UI", 14),
                 bg=c["bg"], fg=icon_color).pack(side=tk.LEFT, padx=(0, 6))

        # Info column
        info = tk.Frame(frame, bg=c["bg"])
        info.pack(side=tk.LEFT, fill=tk.X, expand=True)

        name = record.get("remote_name") or record.get("remote_number", "Unknown")
        number = record.get("remote_number", "")
        protocol = record.get("protocol", "SIP")

        tk.Label(info, text=name, font=("Segoe UI", 10, "bold"),
                 bg=c["bg"], fg=c["fg"], anchor=tk.W).pack(fill=tk.X)

        detail_text = f"{number}  ({protocol})"
        tk.Label(info, text=detail_text, font=("Segoe UI", 8),
                 bg=c["bg"], fg=c["fg_dim"], anchor=tk.W).pack(fill=tk.X)

        # Right column: time and duration
        right = tk.Frame(frame, bg=c["bg"])
        right.pack(side=tk.RIGHT)

        started = record.get("started_at", "")
        try:
            dt = datetime.fromisoformat(started)
            time_str = dt.strftime("%H:%M")
            date_str = dt.strftime("%b %d")
        except (ValueError, TypeError):
            time_str = ""
            date_str = ""

        tk.Label(right, text=time_str, font=("Segoe UI", 10),
                 bg=c["bg"], fg=c["fg"]).pack(anchor=tk.E)
        tk.Label(right, text=date_str, font=("Segoe UI", 8),
                 bg=c["bg"], fg=c["fg_dim"]).pack(anchor=tk.E)

        duration = record.get("duration_seconds", 0)
        if duration > 0:
            mins, secs = divmod(duration, 60)
            tk.Label(right, text=f"{mins}:{secs:02d}",
                     font=("Segoe UI", 8), bg=c["bg"],
                     fg=c["fg_dim"]).pack(anchor=tk.E)

        # Click to redial
        for w in (frame, info):
            w.bind("<Button-1>",
                   lambda e, n=number: self._redial(n))

        # Right-click to delete
        frame.bind("<Button-3>",
                   lambda e, rid=record["id"]: self._delete_record(rid))

    def _set_filter(self, direction_filter):
        self._filter = direction_filter
        c = self.colors
        for value, btn in self._filter_buttons.items():
            if value == direction_filter:
                btn.configure(bg=c["accent"], fg="#ffffff")
            else:
                btn.configure(bg=c["button_bg"], fg=c["button_fg"])
        self.refresh()

    def _redial(self, number):
        if number and self._on_redial:
            self._on_redial(number)

    def _delete_record(self, record_id):
        delete_call_record(record_id)
        self.refresh()

    def _clear_all(self):
        clear_call_history()
        self.refresh()
