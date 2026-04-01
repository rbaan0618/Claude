"""Dialpad widget with number display and call controls."""

import tkinter as tk
from gui.theme import get_theme


class Dialpad(tk.Frame):
    """Telephone-style dialpad with display and call buttons."""

    def __init__(self, parent, theme_name="dark", on_call=None, on_hangup=None,
                 on_dtmf=None, on_answer=None, **kwargs):
        self.colors = get_theme(theme_name)
        super().__init__(parent, bg=self.colors["bg"], **kwargs)
        self._on_call = on_call
        self._on_hangup = on_hangup
        self._on_dtmf = on_dtmf
        self._on_answer = on_answer
        self._build()

    def _build(self):
        c = self.colors

        # Number display
        display_frame = tk.Frame(self, bg=c["bg"])
        display_frame.pack(fill=tk.X, padx=10, pady=(10, 5))

        self.number_var = tk.StringVar()
        self.display = tk.Entry(
            display_frame, textvariable=self.number_var,
            font=("Segoe UI", 20), justify=tk.CENTER,
            bg=c["bg_input"], fg=c["fg"], insertbackground=c["fg"],
            relief=tk.FLAT, bd=8
        )
        self.display.pack(fill=tk.X)
        self.display.bind("<Return>", lambda e: self._dial())

        # Caller info label (shown during incoming calls)
        self.caller_info_var = tk.StringVar()
        self.caller_info = tk.Label(
            self, textvariable=self.caller_info_var,
            font=("Segoe UI", 11), bg=c["bg"], fg=c["accent"],
        )
        self.caller_info.pack(pady=(0, 5))

        # Call timer
        self.timer_var = tk.StringVar(value="")
        self.timer_label = tk.Label(
            self, textvariable=self.timer_var,
            font=("Segoe UI", 12, "bold"), bg=c["bg"], fg=c["green"],
        )
        self.timer_label.pack()

        # Dialpad grid
        pad_frame = tk.Frame(self, bg=c["bg"])
        pad_frame.pack(padx=10, pady=5)

        buttons = [
            ("1", ""), ("2", "ABC"), ("3", "DEF"),
            ("4", "GHI"), ("5", "JKL"), ("6", "MNO"),
            ("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ"),
            ("*", ""), ("0", "+"), ("#", ""),
        ]

        for i, (digit, letters) in enumerate(buttons):
            row, col = divmod(i, 3)
            btn = tk.Frame(pad_frame, bg=c["dialpad_bg"], cursor="hand2")
            btn.grid(row=row, column=col, padx=3, pady=3)

            d_label = tk.Label(btn, text=digit, font=("Segoe UI", 18, "bold"),
                               bg=c["dialpad_bg"], fg=c["dialpad_fg"],
                               width=3, cursor="hand2")
            d_label.pack()

            if letters:
                l_label = tk.Label(btn, text=letters, font=("Segoe UI", 7),
                                   bg=c["dialpad_bg"], fg=c["fg_dim"],
                                   cursor="hand2")
                l_label.pack()
            else:
                spacer = tk.Frame(btn, bg=c["dialpad_bg"], height=12)
                spacer.pack()

            for widget in (btn, d_label):
                widget.bind("<Button-1>", lambda e, d=digit: self._press(d))
                widget.bind("<Enter>", lambda e, b=btn: b.configure(bg=c["dialpad_active"]))
                widget.bind("<Leave>", lambda e, b=btn: b.configure(bg=c["dialpad_bg"]))

        # Backspace button
        backspace_btn = tk.Label(
            pad_frame, text="\u232b", font=("Segoe UI", 16),
            bg=c["bg"], fg=c["fg_dim"], cursor="hand2", width=3
        )
        backspace_btn.grid(row=0, column=3, padx=3, pady=3)
        backspace_btn.bind("<Button-1>", lambda e: self._backspace())

        # Call control buttons
        ctrl_frame = tk.Frame(self, bg=c["bg"])
        ctrl_frame.pack(fill=tk.X, padx=10, pady=10)

        self.call_btn = tk.Label(
            ctrl_frame, text="\u260e  Call", font=("Segoe UI", 13, "bold"),
            bg=c["green"], fg="#1e1e2e", cursor="hand2",
            padx=20, pady=8, relief=tk.FLAT
        )
        self.call_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 5))
        self.call_btn.bind("<Button-1>", lambda e: self._dial())

        self.hangup_btn = tk.Label(
            ctrl_frame, text="\u2716  Hangup", font=("Segoe UI", 13, "bold"),
            bg=c["red"], fg="#1e1e2e", cursor="hand2",
            padx=20, pady=8, relief=tk.FLAT
        )
        self.hangup_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(5, 0))
        self.hangup_btn.bind("<Button-1>", lambda e: self._hangup())

        # Answer button (hidden by default)
        self.answer_btn = tk.Label(
            ctrl_frame, text="\u2714  Answer", font=("Segoe UI", 13, "bold"),
            bg=c["green"], fg="#1e1e2e", cursor="hand2",
            padx=20, pady=8, relief=tk.FLAT
        )
        # answer_btn is packed dynamically during incoming calls

        # Mid-call controls
        mid_frame = tk.Frame(self, bg=c["bg"])
        mid_frame.pack(fill=tk.X, padx=10, pady=(0, 5))

        self._mid_buttons = {}
        for text, cmd in [("Hold", "hold"), ("Transfer", "transfer"), ("Mute", "mute")]:
            btn = tk.Label(
                mid_frame, text=text, font=("Segoe UI", 10),
                bg=c["button_bg"], fg=c["button_fg"], cursor="hand2",
                padx=12, pady=4
            )
            btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
            btn.bind("<Button-1>", lambda e, c=cmd: self._mid_call_action(c))
            self._mid_buttons[cmd] = btn

    def _press(self, digit):
        self.number_var.set(self.number_var.get() + digit)
        if self._on_dtmf:
            self._on_dtmf(digit)

    def _backspace(self):
        current = self.number_var.get()
        self.number_var.set(current[:-1])

    def _dial(self):
        number = self.number_var.get().strip()
        if number and self._on_call:
            self._on_call(number)

    def _hangup(self):
        if self._on_hangup:
            self._on_hangup()

    def _mid_call_action(self, action):
        # Propagated to main window
        self.event_generate(f"<<MidCall-{action}>>")

    def show_incoming(self, caller):
        """Show incoming call UI."""
        self.caller_info_var.set(f"Incoming: {caller}")
        self.answer_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(5, 0),
                             before=self.hangup_btn)
        self.answer_btn.bind("<Button-1>", lambda e: self._answer())

    def hide_incoming(self):
        """Hide incoming call UI."""
        self.caller_info_var.set("")
        self.answer_btn.pack_forget()

    def _answer(self):
        self.hide_incoming()
        if self._on_answer:
            self._on_answer()

    def set_mute_active(self, active):
        """Toggle mute button color — red when active."""
        c = self.colors
        btn = self._mid_buttons.get("mute")
        if btn:
            if active:
                btn.configure(bg=c["red"], fg="#ffffff")
            else:
                btn.configure(bg=c["button_bg"], fg=c["button_fg"])

    def set_hold_active(self, active):
        """Toggle hold button color — red when active."""
        c = self.colors
        btn = self._mid_buttons.get("hold")
        if btn:
            if active:
                btn.configure(bg=c["red"], fg="#ffffff")
            else:
                btn.configure(bg=c["button_bg"], fg=c["button_fg"])

    def set_number(self, number):
        self.number_var.set(number)
