"""Dedicated in-call view — replaces the dialpad while a call is active."""

import tkinter as tk
import threading
try:
    import winsound as _winsound
    _WINSOUND = True
except ImportError:
    _WINSOUND = False

from gui.theme import get_theme

_DTMF_FREQ = {
    "1": 697, "2": 697, "3": 697,
    "4": 770, "5": 770, "6": 770,
    "7": 852, "8": 852, "9": 852,
    "*": 941, "0": 941, "#": 941,
}


class InCallView(tk.Frame):
    """Full-panel in-call screen.

    Shows large caller info, call state label, timer, and prominent
    mute/hold/transfer/keypad/hangup controls. Used in place of the
    `Dialpad` widget while a call is active.
    """

    def __init__(self, parent, theme_name="dark",
                 on_hangup=None, on_answer=None, on_hold=None,
                 on_mute=None, on_transfer=None, on_dtmf=None,
                 on_complete_transfer=None, on_cancel_transfer=None,
                 **kwargs):
        self.colors = get_theme(theme_name)
        super().__init__(parent, bg=self.colors["bg"], **kwargs)

        self._on_hangup = on_hangup
        self._on_answer = on_answer
        self._on_hold = on_hold
        self._on_mute = on_mute
        self._on_transfer = on_transfer
        self._on_dtmf = on_dtmf
        self._on_complete_transfer = on_complete_transfer
        self._on_cancel_transfer = on_cancel_transfer

        self._keypad_visible = False
        self._build()

    def _build(self):
        c = self.colors

        # ---- Caller info block ----
        info = tk.Frame(self, bg=c["bg"])
        info.pack(fill=tk.X, padx=20, pady=(30, 10))

        self.name_var = tk.StringVar(value="")
        tk.Label(info, textvariable=self.name_var,
                 font=("Segoe UI", 18, "bold"),
                 bg=c["bg"], fg=c["fg"]).pack()

        self.number_var = tk.StringVar(value="")
        tk.Label(info, textvariable=self.number_var,
                 font=("Segoe UI", 13),
                 bg=c["bg"], fg=c["fg_dim"]).pack(pady=(2, 0))

        self.state_var = tk.StringVar(value="")
        self.state_label = tk.Label(self, textvariable=self.state_var,
                                    font=("Segoe UI", 12, "bold"),
                                    bg=c["bg"], fg=c["accent"])
        self.state_label.pack(pady=(8, 0))

        self.timer_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self.timer_var,
                 font=("Segoe UI", 22, "bold"),
                 bg=c["bg"], fg=c["green"]).pack(pady=(4, 10))

        # ---- Consultation sub-panel (attended transfer) ----
        self.consult_frame = tk.Frame(self, bg=c["bg_secondary"])
        self.consult_var = tk.StringVar(value="")
        tk.Label(self.consult_frame, textvariable=self.consult_var,
                 font=("Segoe UI", 10, "bold"),
                 bg=c["bg_secondary"], fg=c["orange"]).pack(pady=(8, 4))

        consult_btns = tk.Frame(self.consult_frame, bg=c["bg_secondary"])
        consult_btns.pack(pady=(0, 8))

        complete_btn = tk.Label(consult_btns, text="\u2714  Complete Transfer",
                                font=("Segoe UI", 10, "bold"),
                                bg=c["green"], fg="#1e1e2e", cursor="hand2",
                                padx=14, pady=5)
        complete_btn.pack(side=tk.LEFT, padx=4)
        complete_btn.bind("<Button-1>",
                          lambda e: self._on_complete_transfer and self._on_complete_transfer())

        cancel_btn = tk.Label(consult_btns, text="\u2716  Cancel",
                              font=("Segoe UI", 10, "bold"),
                              bg=c["red"], fg="#1e1e2e", cursor="hand2",
                              padx=14, pady=5)
        cancel_btn.pack(side=tk.LEFT, padx=4)
        cancel_btn.bind("<Button-1>",
                        lambda e: self._on_cancel_transfer and self._on_cancel_transfer())

        # consult_frame is hidden by default — packed via show_consultation()

        # ---- Collapsible DTMF keypad ----
        self.keypad_frame = tk.Frame(self, bg=c["bg"])
        # Not packed initially — toggled via _toggle_keypad()

        for i, digit in enumerate(["1", "2", "3", "4", "5", "6",
                                    "7", "8", "9", "*", "0", "#"]):
            row, col = divmod(i, 3)
            btn = tk.Label(self.keypad_frame, text=digit,
                           font=("Segoe UI", 14, "bold"),
                           bg=c["dialpad_bg"], fg=c["dialpad_fg"],
                           cursor="hand2", width=4, pady=6)
            btn.grid(row=row, column=col, padx=3, pady=3)
            btn.bind("<Button-1>", lambda e, d=digit: self._press_dtmf(d))
            btn.bind("<Enter>", lambda e, b=btn: b.configure(bg=c["dialpad_active"]))
            btn.bind("<Leave>", lambda e, b=btn: b.configure(bg=c["dialpad_bg"]))

        # ---- Mid-call action row (mute / hold / transfer / keypad) ----
        actions = tk.Frame(self, bg=c["bg"])
        actions.pack(fill=tk.X, padx=20, pady=(10, 6))

        self._action_buttons = {}
        for label, key, handler in [
            ("Mute", "mute", lambda: self._on_mute and self._on_mute()),
            ("Hold", "hold", lambda: self._on_hold and self._on_hold()),
            ("Transfer", "transfer", lambda: self._on_transfer and self._on_transfer()),
            ("Keypad", "keypad", self._toggle_keypad),
        ]:
            btn = tk.Label(actions, text=label, font=("Segoe UI", 10, "bold"),
                           bg=c["button_bg"], fg=c["button_fg"], cursor="hand2",
                           padx=10, pady=8)
            btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=3)
            btn.bind("<Button-1>", lambda e, h=handler: h())
            self._action_buttons[key] = btn

        # ---- Hangup / Answer button row ----
        bottom = tk.Frame(self, bg=c["bg"])
        bottom.pack(fill=tk.X, padx=20, pady=(6, 20), side=tk.BOTTOM)

        self.answer_btn = tk.Label(
            bottom, text="\u2714  Answer", font=("Segoe UI", 14, "bold"),
            bg=c["green"], fg="#1e1e2e", cursor="hand2",
            padx=20, pady=10
        )
        self.answer_btn.bind("<Button-1>", lambda e: self._on_answer and self._on_answer())
        # answer_btn is packed dynamically when an incoming call is shown

        self.hangup_btn = tk.Label(
            bottom, text="\u2716  Hangup", font=("Segoe UI", 14, "bold"),
            bg=c["red"], fg="#1e1e2e", cursor="hand2",
            padx=20, pady=10
        )
        self.hangup_btn.pack(side=tk.LEFT, expand=True, fill=tk.X)
        self.hangup_btn.bind("<Button-1>",
                             lambda e: self._on_hangup and self._on_hangup())

        self._bottom = bottom

    # ---- Public API ----

    def set_caller(self, name, number):
        self.name_var.set(name or number or "")
        self.number_var.set(number if name else "")

    def set_state(self, text):
        self.state_var.set(text)

    def set_timer(self, text):
        self.timer_var.set(text)

    def show_incoming(self, caller_display):
        self.name_var.set(caller_display)
        self.number_var.set("")
        self.set_state("Incoming call")
        self.timer_var.set("")
        # Pack answer button before hangup
        self.answer_btn.pack(side=tk.LEFT, expand=True, fill=tk.X,
                             padx=(0, 6), before=self.hangup_btn)

    def hide_incoming(self):
        try:
            self.answer_btn.pack_forget()
        except Exception:
            pass

    def show_consultation(self, target):
        self.consult_var.set(f"Consulting: {target}")
        # Pack the consult frame just below the timer
        if not self.consult_frame.winfo_ismapped():
            self.consult_frame.pack(fill=tk.X, padx=20, pady=(0, 8),
                                    after=self.state_label)

    def clear_consultation(self):
        self.consult_var.set("")
        if self.consult_frame.winfo_ismapped():
            self.consult_frame.pack_forget()

    def set_mute_active(self, active):
        c = self.colors
        btn = self._action_buttons.get("mute")
        if not btn:
            return
        if active:
            btn.configure(bg=c["red"], fg="#ffffff")
        else:
            btn.configure(bg=c["button_bg"], fg=c["button_fg"])

    def set_hold_active(self, active):
        c = self.colors
        btn = self._action_buttons.get("hold")
        if not btn:
            return
        if active:
            btn.configure(bg=c["red"], fg="#ffffff")
        else:
            btn.configure(bg=c["button_bg"], fg=c["button_fg"])

    def reset(self):
        """Reset all transient state — called when leaving the in-call view."""
        self.name_var.set("")
        self.number_var.set("")
        self.state_var.set("")
        self.timer_var.set("")
        self.consult_var.set("")
        self.hide_incoming()
        self.clear_consultation()
        self.set_mute_active(False)
        self.set_hold_active(False)
        if self._keypad_visible:
            self._toggle_keypad()

    # ---- Internal ----

    def _toggle_keypad(self):
        c = self.colors
        btn = self._action_buttons.get("keypad")
        if self._keypad_visible:
            self.keypad_frame.pack_forget()
            self._keypad_visible = False
            if btn:
                btn.configure(bg=c["button_bg"], fg=c["button_fg"])
        else:
            # Pack above the bottom (hangup) row
            self.keypad_frame.pack(pady=(4, 4), before=self._bottom)
            self._keypad_visible = True
            if btn:
                btn.configure(bg=c["accent"], fg="#1e1e2e")

    def _press_dtmf(self, digit):
        if _WINSOUND:
            freq = _DTMF_FREQ.get(str(digit), 941)
            threading.Thread(target=_winsound.Beep, args=(freq, 60), daemon=True).start()
        if self._on_dtmf:
            self._on_dtmf(digit)
