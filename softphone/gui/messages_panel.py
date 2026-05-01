"""Messages (SIP MESSAGE / RFC 3428) panel.

Shows a list of conversations. Clicking a peer opens a chat window as a
separate Toplevel dialog. Inbound messages are received via a callback
from the SIP handler and flashed on the peer list.
"""

import time
import tkinter as tk
from datetime import datetime

from gui.theme import get_theme
from utils.database import (
    add_chat_message, get_chats, get_messages, mark_chat_read,
)


def _format_time(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M")
    except Exception:
        return ""


def _format_date(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


class MessagesPanel(tk.Frame):
    """Conversations list panel that launches chat windows on click."""

    def __init__(self, parent, theme_name="dark", on_send=None, **kwargs):
        self.colors = get_theme(theme_name)
        self.theme_name = theme_name
        super().__init__(parent, bg=self.colors["bg_secondary"], **kwargs)
        self._on_send = on_send  # callback(peer, text) -> bool
        self._chat_windows = {}  # peer -> ChatWindow
        self._build()
        self.refresh()

    def _build(self):
        c = self.colors

        header = tk.Frame(self, bg=c["bg_secondary"])
        header.pack(fill=tk.X, padx=8, pady=(8, 4))

        tk.Label(header, text="Messages", font=("Segoe UI", 11, "bold"),
                 bg=c["bg_secondary"], fg=c["fg"]).pack(side=tk.LEFT)

        add_btn = tk.Label(header, text="+", font=("Segoe UI", 14, "bold"),
                           bg=c["bg_secondary"], fg=c["accent"], cursor="hand2")
        add_btn.pack(side=tk.RIGHT)
        add_btn.bind("<Button-1>", lambda e: self._new_message_dialog())

        # Scrollable list of conversations
        list_container = tk.Frame(self, bg=c["bg_secondary"])
        list_container.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))

        canvas = tk.Canvas(list_container, bg=c["bg_secondary"],
                           highlightthickness=0)
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

    def refresh(self):
        """Re-read the chat list from the database and rebuild the list."""
        c = self.colors
        for child in self._list_frame.winfo_children():
            child.destroy()

        chats = get_chats()
        if not chats:
            tk.Label(self._list_frame, text="No conversations",
                     font=("Segoe UI", 9), bg=c["bg_secondary"],
                     fg=c["fg_dim"]).pack(pady=20)
            return

        for chat in chats:
            self._add_row(chat)

    def _add_row(self, chat):
        c = self.colors
        unread = chat.get("unread", 0) or 0

        row = tk.Frame(self._list_frame, bg=c["bg_secondary"], cursor="hand2")
        row.pack(fill=tk.X, padx=4, pady=2)

        top = tk.Frame(row, bg=c["bg_secondary"])
        top.pack(fill=tk.X)

        name_font = ("Segoe UI", 10, "bold") if unread else ("Segoe UI", 10)
        tk.Label(top, text=chat["peer"], font=name_font,
                 bg=c["bg_secondary"],
                 fg=c["accent"] if unread else c["fg"]).pack(side=tk.LEFT)

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
            badge = tk.Label(bottom, text=str(unread), font=("Segoe UI", 8, "bold"),
                             bg=c["red"], fg="#ffffff", padx=5)
            badge.pack(side=tk.RIGHT, padx=2)

        # Click handlers on every child widget
        peer = chat["peer"]
        for w in (row, top, bottom) + tuple(top.winfo_children()) + tuple(bottom.winfo_children()):
            w.bind("<Button-1>", lambda e, p=peer: self.open_chat(p))

    # ---- Public API ----

    def open_chat(self, peer):
        """Open (or focus) a chat window for the given peer."""
        win = self._chat_windows.get(peer)
        if win and win.winfo_exists():
            win.deiconify()
            win.lift()
            win.focus_set()
            win.reload()
            return

        win = ChatWindow(self, peer, theme_name=self.theme_name,
                         on_send=self._send_from_window,
                         on_close=lambda p=peer: self._chat_windows.pop(p, None))
        self._chat_windows[peer] = win
        mark_chat_read(peer)
        self.refresh()

    def on_incoming_message(self, peer, body, timestamp):
        """Called by the main window when a MESSAGE arrives."""
        add_chat_message(peer, "in", body, timestamp, read=0)
        self.refresh()
        win = self._chat_windows.get(peer)
        if win and win.winfo_exists():
            win.reload()
            mark_chat_read(peer)
            self.refresh()

    # ---- Internal ----

    def _send_from_window(self, peer, text):
        """Invoked by a ChatWindow when the user sends a message."""
        if not self._on_send:
            return False
        ok = self._on_send(peer, text)
        if ok:
            add_chat_message(peer, "out", text, time.time())
            self.refresh()
            win = self._chat_windows.get(peer)
            if win and win.winfo_exists():
                win.reload()
        return ok

    def _new_message_dialog(self):
        c = self.colors
        dialog = tk.Toplevel(self.winfo_toplevel())
        dialog.title("New Message")
        dialog.geometry("300x120")
        dialog.configure(bg=c["bg"])
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()

        tk.Label(dialog, text="To (number or SIP user):",
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
                self.open_chat(peer)

        entry.bind("<Return>", lambda e: _open())

        btn_row = tk.Frame(dialog, bg=c["bg"])
        btn_row.pack(fill=tk.X, padx=12, pady=10)
        tk.Button(btn_row, text="Open", command=_open,
                  bg=c["accent"], fg="#ffffff",
                  font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=16, pady=4).pack(side=tk.LEFT)
        tk.Button(btn_row, text="Cancel", command=dialog.destroy,
                  bg=c["button_bg"], fg=c["button_fg"],
                  font=("Segoe UI", 10),
                  relief=tk.FLAT, padx=16, pady=4).pack(side=tk.LEFT, padx=(8, 0))


class ChatWindow(tk.Toplevel):
    """A Toplevel chat window for a single peer conversation."""

    def __init__(self, master, peer, theme_name="dark", on_send=None, on_close=None):
        super().__init__(master)
        self.colors = get_theme(theme_name)
        self._peer = peer
        self._on_send = on_send
        self._on_close = on_close

        self.title(f"Chat — {peer}")
        self.geometry("420x520")
        self.configure(bg=self.colors["bg"])
        self.protocol("WM_DELETE_WINDOW", self._close)

        self._build()
        self.reload()

    def _build(self):
        c = self.colors

        header = tk.Frame(self, bg=c["status_bar"], height=36)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text=self._peer, font=("Segoe UI", 11, "bold"),
                 bg=c["status_bar"], fg=c["fg"]).pack(side=tk.LEFT, padx=12)

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
        canvas.create_window((0, 0), window=self._msg_frame, anchor="nw",
                             tags="inner")
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
        self._entry = tk.Entry(input_row, textvariable=self._input_var,
                               bg=c["bg_input"], fg=c["fg"],
                               font=("Segoe UI", 11), relief=tk.FLAT, bd=6,
                               insertbackground=c["fg"])
        self._entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._entry.bind("<Return>", lambda e: self._send())
        self._entry.focus_set()

        send_btn = tk.Label(input_row, text="Send",
                            font=("Segoe UI", 10, "bold"),
                            bg=c["accent"], fg="#ffffff", cursor="hand2",
                            padx=14, pady=6)
        send_btn.pack(side=tk.LEFT, padx=(6, 0))
        send_btn.bind("<Button-1>", lambda e: self._send())

    def reload(self):
        c = self.colors
        for w in self._msg_frame.winfo_children():
            w.destroy()

        messages = get_messages(self._peer)
        if not messages:
            tk.Label(self._msg_frame, text="(no messages yet)",
                     font=("Segoe UI", 9), bg=c["bg"], fg=c["fg_dim"]).pack(pady=20)
        else:
            for m in messages:
                self._render_bubble(m)

        # Scroll to bottom
        self.after(50, lambda: self._canvas.yview_moveto(1.0))

    def _render_bubble(self, m):
        c = self.colors
        outbound = m["direction"] == "out"
        wrap = tk.Frame(self._msg_frame, bg=c["bg"])
        wrap.pack(fill=tk.X, padx=4, pady=3)

        if outbound:
            bubble = tk.Frame(wrap, bg=c["accent"])
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
        if self._on_send and self._on_send(self._peer, text):
            self._input_var.set("")

    def _close(self):
        if self._on_close:
            try:
                self._on_close()
            except Exception:
                pass
        self.destroy()
