"""Messages panel — SMS and WhatsApp conversations via SIP MESSAGE (RFC 3428).

Two tabs: SMS (blue accent) and WhatsApp (green).  Each conversation opens a
ChatWindow Toplevel.  Messages are persisted in SQLite via utils.database.
"""

import time
import tkinter as tk
from datetime import datetime

from gui.theme import get_theme
from utils.database import (
    add_chat_message, get_chats, get_messages, mark_chat_read,
)

# WhatsApp brand green; SMS uses theme accent (blue)
WHATSAPP_GREEN = "#25D366"
WHATSAPP_DARK  = "#128C7E"


def _format_time(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M")
    except Exception:
        return ""


class MessagesPanel(tk.Frame):
    """Left-panel widget with SMS / WhatsApp tabs and per-peer conversation rows."""

    def __init__(self, parent, theme_name="dark", on_send=None, **kwargs):
        self.colors = get_theme(theme_name)
        self.theme_name = theme_name
        super().__init__(parent, bg=self.colors["bg_secondary"], **kwargs)
        self._on_send = on_send          # callback(peer, text, channel) -> bool
        self._chat_windows = {}          # (peer, channel) -> ChatWindow
        self._active_channel = "sms"     # currently visible tab
        self._build()
        self.refresh()

    def _build(self):
        c = self.colors

        # ---- Header ----
        header = tk.Frame(self, bg=c["bg_secondary"])
        header.pack(fill=tk.X, padx=8, pady=(8, 4))
        tk.Label(header, text="Messages", font=("Segoe UI", 11, "bold"),
                 bg=c["bg_secondary"], fg=c["fg"]).pack(side=tk.LEFT)
        add_btn = tk.Label(header, text="+", font=("Segoe UI", 14, "bold"),
                           bg=c["bg_secondary"], fg=c["accent"], cursor="hand2")
        add_btn.pack(side=tk.RIGHT)
        add_btn.bind("<Button-1>", lambda e: self._new_message_dialog())

        # ---- SMS / WhatsApp tabs ----
        tab_bar = tk.Frame(self, bg=c["bg_secondary"])
        tab_bar.pack(fill=tk.X, padx=4)
        self._tab_btns = {}
        for label, ch in [("💬 SMS", "sms"), ("🟢 WhatsApp", "whatsapp")]:
            btn = tk.Label(tab_bar, text=label, font=("Segoe UI", 9, "bold"),
                           bg=c["button_bg"], fg=c["button_fg"],
                           cursor="hand2", padx=10, pady=4)
            btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2, pady=2)
            btn.bind("<Button-1>", lambda e, ch=ch: self._switch_channel(ch))
            self._tab_btns[ch] = btn

        # ---- Scrollable conversation list ----
        list_container = tk.Frame(self, bg=c["bg_secondary"])
        list_container.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))

        canvas = tk.Canvas(list_container, bg=c["bg_secondary"], highlightthickness=0)
        scrollbar = tk.Scrollbar(list_container, orient="vertical",
                                 command=canvas.yview)
        self._list_frame = tk.Frame(canvas, bg=c["bg_secondary"])
        self._list_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=self._list_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas = canvas

        # Activate SMS tab
        self._switch_channel("sms")

    def _switch_channel(self, channel):
        c = self.colors
        self._active_channel = channel
        accent = WHATSAPP_GREEN if channel == "whatsapp" else c["accent"]
        for ch, btn in self._tab_btns.items():
            if ch == channel:
                btn.configure(bg=accent, fg="#ffffff")
            else:
                btn.configure(bg=c["button_bg"], fg=c["button_fg"])
        self.refresh()

    def refresh(self):
        """Reload the conversation list for the active channel."""
        c = self.colors
        for child in self._list_frame.winfo_children():
            child.destroy()

        chats = get_chats(self._active_channel)
        if not chats:
            lbl = "No SMS conversations yet" if self._active_channel == "sms" \
                else "No WhatsApp conversations yet"
            tk.Label(self._list_frame, text=lbl,
                     font=("Segoe UI", 9), bg=c["bg_secondary"],
                     fg=c["fg_dim"]).pack(pady=20)
            return

        for chat in chats:
            self._add_row(chat)

    def _add_row(self, chat):
        c = self.colors
        unread = chat.get("unread", 0) or 0
        accent = WHATSAPP_GREEN if self._active_channel == "whatsapp" else c["accent"]

        row = tk.Frame(self._list_frame, bg=c["bg_secondary"], cursor="hand2")
        row.pack(fill=tk.X, padx=4, pady=2)

        top = tk.Frame(row, bg=c["bg_secondary"])
        top.pack(fill=tk.X)
        name_font = ("Segoe UI", 10, "bold") if unread else ("Segoe UI", 10)
        tk.Label(top, text=chat["peer"], font=name_font,
                 bg=c["bg_secondary"],
                 fg=accent if unread else c["fg"]).pack(side=tk.LEFT)
        tk.Label(top, text=_format_time(chat["last_timestamp"]),
                 font=("Segoe UI", 8),
                 bg=c["bg_secondary"], fg=c["fg_dim"]).pack(side=tk.RIGHT)

        preview = chat["last_body"] or ""
        if len(preview) > 28:
            preview = preview[:28] + "…"
        if chat["last_direction"] == "out":
            preview = "You: " + preview

        bottom = tk.Frame(row, bg=c["bg_secondary"])
        bottom.pack(fill=tk.X)
        tk.Label(bottom, text=preview, font=("Segoe UI", 9),
                 bg=c["bg_secondary"],
                 fg=c["fg"] if unread else c["fg_dim"],
                 anchor=tk.W).pack(side=tk.LEFT, fill=tk.X, expand=True)
        if unread:
            tk.Label(bottom, text=str(unread), font=("Segoe UI", 8, "bold"),
                     bg=c["red"], fg="#ffffff", padx=5).pack(side=tk.RIGHT, padx=2)

        peer = chat["peer"]
        channel = self._active_channel
        for w in [row, top, bottom] + list(top.winfo_children()) + list(bottom.winfo_children()):
            w.bind("<Button-1>", lambda e, p=peer, ch=channel: self.open_chat(p, ch))

    # ---- Public API ----

    def open_chat(self, peer, channel=None):
        channel = channel or self._active_channel
        key = (peer, channel)
        win = self._chat_windows.get(key)
        if win and win.winfo_exists():
            win.deiconify()
            win.lift()
            win.focus_set()
            win.reload()
            return
        win = ChatWindow(
            self, peer, channel=channel, theme_name=self.theme_name,
            on_send=self._send_from_window,
            on_close=lambda k=key: self._chat_windows.pop(k, None),
        )
        self._chat_windows[key] = win
        mark_chat_read(peer, channel)
        self.refresh()

    def on_incoming_message(self, peer, body, timestamp, channel="sms"):
        """Called by the main window when an inbound SIP MESSAGE arrives."""
        add_chat_message(peer, "in", body, timestamp, read=0, message_type=channel)
        self.refresh()
        key = (peer, channel)
        win = self._chat_windows.get(key)
        if win and win.winfo_exists():
            win.reload()
            mark_chat_read(peer, channel)
            self.refresh()

    # ---- Internal ----

    def _send_from_window(self, peer, text, channel):
        if not self._on_send:
            return False
        ok = self._on_send(peer, text, channel)
        if ok:
            add_chat_message(peer, "out", text, time.time(), message_type=channel)
            self.refresh()
            key = (peer, channel)
            win = self._chat_windows.get(key)
            if win and win.winfo_exists():
                win.reload()
        return ok

    def _new_message_dialog(self):
        c = self.colors
        channel = self._active_channel
        dialog = tk.Toplevel(self.winfo_toplevel())
        dialog.title("New Message")
        dialog.geometry("320x140")
        dialog.configure(bg=c["bg"])
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()

        accent = WHATSAPP_GREEN if channel == "whatsapp" else c["accent"]
        ch_label = "WhatsApp" if channel == "whatsapp" else "SMS"
        tk.Label(dialog, text=f"New {ch_label} — To (number):",
                 bg=c["bg"], fg=c["fg"], font=("Segoe UI", 10)).pack(
            anchor=tk.W, padx=12, pady=(12, 2))

        peer_var = tk.StringVar()
        entry = tk.Entry(dialog, textvariable=peer_var,
                         bg=c["bg_input"], fg=c["fg"],
                         font=("Segoe UI", 12), relief=tk.FLAT, bd=4,
                         insertbackground=c["fg"])
        entry.pack(fill=tk.X, padx=12)
        entry.focus_set()

        def _open():
            peer = peer_var.get().strip()
            if peer:
                dialog.destroy()
                self.open_chat(peer, channel)

        entry.bind("<Return>", lambda e: _open())
        btn_row = tk.Frame(dialog, bg=c["bg"])
        btn_row.pack(fill=tk.X, padx=12, pady=10)
        tk.Button(btn_row, text="Open", command=_open,
                  bg=accent, fg="#ffffff",
                  font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=16, pady=4).pack(side=tk.LEFT)
        tk.Button(btn_row, text="Cancel", command=dialog.destroy,
                  bg=c["button_bg"], fg=c["button_fg"],
                  font=("Segoe UI", 10),
                  relief=tk.FLAT, padx=16, pady=4).pack(side=tk.LEFT, padx=(8, 0))


class ChatWindow(tk.Toplevel):
    """Single-peer chat window for one channel (SMS or WhatsApp)."""

    def __init__(self, master, peer, channel="sms", theme_name="dark",
                 on_send=None, on_close=None):
        super().__init__(master)
        self.colors = get_theme(theme_name)
        self._peer = peer
        self._channel = channel
        self._on_send = on_send    # callback(peer, text, channel) -> bool
        self._on_close = on_close
        self._accent = WHATSAPP_GREEN if channel == "whatsapp" else self.colors["accent"]
        self._ch_label = "WhatsApp" if channel == "whatsapp" else "SMS"

        self.title(f"{self._ch_label} — {peer}")
        self.geometry("440x540")
        self.configure(bg=self.colors["bg"])
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._build()
        self.reload()

    def _build(self):
        c = self.colors

        # Top bar with channel badge
        top_bar = tk.Frame(self, bg=c["status_bar"], height=40)
        top_bar.pack(fill=tk.X)
        top_bar.pack_propagate(False)

        tk.Label(top_bar, text=self._peer,
                 font=("Segoe UI", 11, "bold"),
                 bg=c["status_bar"], fg=c["fg"]).pack(side=tk.LEFT, padx=12)

        badge = tk.Label(top_bar, text=f"  {self._ch_label}  ",
                         font=("Segoe UI", 8, "bold"),
                         bg=self._accent, fg="#ffffff")
        badge.pack(side=tk.LEFT, padx=4)

        # Messages area
        msg_container = tk.Frame(self, bg=c["bg"])
        msg_container.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        canvas = tk.Canvas(msg_container, bg=c["bg"], highlightthickness=0)
        scrollbar = tk.Scrollbar(msg_container, orient="vertical",
                                 command=canvas.yview)
        self._msg_frame = tk.Frame(canvas, bg=c["bg"])
        self._msg_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=self._msg_frame, anchor="nw", tags="inner")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure("inner", width=e.width))
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas = canvas

        # Input row
        input_row = tk.Frame(self, bg=c["bg"])
        input_row.pack(fill=tk.X, padx=6, pady=(0, 6))

        self._input_var = tk.StringVar()
        placeholder = "WhatsApp message…" if self._channel == "whatsapp" else "SMS message…"
        self._entry = tk.Entry(input_row, textvariable=self._input_var,
                               bg=c["bg_input"], fg=c["fg"],
                               font=("Segoe UI", 11), relief=tk.FLAT, bd=6,
                               insertbackground=c["fg"])
        self._entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._entry.bind("<Return>", lambda e: self._send())
        self._entry.focus_set()

        send_btn = tk.Label(input_row, text="Send",
                            font=("Segoe UI", 10, "bold"),
                            bg=self._accent, fg="#ffffff",
                            cursor="hand2", padx=14, pady=6)
        send_btn.pack(side=tk.LEFT, padx=(6, 0))
        send_btn.bind("<Button-1>", lambda e: self._send())

    def reload(self):
        c = self.colors
        for w in self._msg_frame.winfo_children():
            w.destroy()

        messages = get_messages(self._peer, message_type=self._channel)
        if not messages:
            tk.Label(self._msg_frame,
                     text=f"(no {self._ch_label} messages yet)",
                     font=("Segoe UI", 9), bg=c["bg"], fg=c["fg_dim"]).pack(pady=20)
        else:
            for m in messages:
                self._render_bubble(m)
        self.after(50, lambda: self._canvas.yview_moveto(1.0))

    def _render_bubble(self, m):
        c = self.colors
        outbound = m["direction"] == "out"
        wrap = tk.Frame(self._msg_frame, bg=c["bg"])
        wrap.pack(fill=tk.X, padx=4, pady=3)

        if outbound:
            bubble = tk.Frame(wrap, bg=self._accent)
            bubble.pack(side=tk.RIGHT, anchor=tk.E)
            fg = "#ffffff"
        else:
            bubble = tk.Frame(wrap, bg=c["bg_secondary"])
            bubble.pack(side=tk.LEFT, anchor=tk.W)
            fg = c["fg"]

        tk.Label(bubble, text=m["body"], font=("Segoe UI", 10),
                 bg=bubble["bg"], fg=fg, wraplength=300, justify=tk.LEFT,
                 padx=10, pady=6).pack(anchor=tk.W if not outbound else tk.E)
        tk.Label(bubble, text=_format_time(m["timestamp"]),
                 font=("Segoe UI", 7),
                 bg=bubble["bg"], fg=fg, padx=10).pack(
            anchor=tk.E, pady=(0, 4))

    def _send(self):
        text = self._input_var.get().strip()
        if not text:
            return
        if self._on_send and self._on_send(self._peer, text, self._channel):
            self._input_var.set("")

    def _close(self):
        if self._on_close:
            try:
                self._on_close()
            except Exception:
                pass
        self.destroy()
