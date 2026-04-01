"""Contacts panel with favorites filter."""

import tkinter as tk
from gui.theme import get_theme
from models.contact import Contact


class ContactsPanel(tk.Frame):
    """Panel showing contacts with favorites filter and search."""

    def __init__(self, parent, theme_name="dark", on_dial=None, on_change=None, **kwargs):
        self.colors = get_theme(theme_name)
        super().__init__(parent, bg=self.colors["bg_secondary"], **kwargs)
        self._on_dial = on_dial
        self._on_change = on_change  # called when contacts are added/removed/edited
        self._contacts = []  # list of Contact
        self._filter = "all"  # "all" or "favorites"
        self._search_text = ""
        self._ready = False
        self._build()
        self._ready = True

    def _build(self):
        c = self.colors

        # Header
        header = tk.Frame(self, bg=c["bg_secondary"])
        header.pack(fill=tk.X, padx=8, pady=(8, 4))

        tk.Label(header, text="Contacts", font=("Segoe UI", 11, "bold"),
                 bg=c["bg_secondary"], fg=c["fg"]).pack(side=tk.LEFT)

        self.add_btn = tk.Label(header, text="+", font=("Segoe UI", 14, "bold"),
                                bg=c["bg_secondary"], fg=c["accent"], cursor="hand2")
        self.add_btn.pack(side=tk.RIGHT)
        self.add_btn.bind("<Button-1>", lambda e: self._add_dialog())

        # Filter tabs: All | Favorites
        filter_frame = tk.Frame(self, bg=c["bg_secondary"])
        filter_frame.pack(fill=tk.X, padx=8, pady=(0, 4))

        self._filter_buttons = {}
        for label, value in [("All", "all"), ("\u2605 Favorites", "favorites")]:
            btn = tk.Label(filter_frame, text=label, font=("Segoe UI", 9),
                           bg=c["accent"] if value == self._filter else c["button_bg"],
                           fg="#ffffff" if value == self._filter else c["button_fg"],
                           padx=10, pady=2, cursor="hand2")
            btn.pack(side=tk.LEFT, padx=2)
            btn.bind("<Button-1>", lambda e, v=value: self._set_filter(v))
            self._filter_buttons[value] = btn

        # Search bar
        search_frame = tk.Frame(self, bg=c["bg_secondary"])
        search_frame.pack(fill=tk.X, padx=8, pady=(0, 4))

        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._on_search())
        search_entry = tk.Entry(search_frame, textvariable=self._search_var,
                                bg=c["bg_input"], fg=c["fg"], font=("Segoe UI", 9),
                                relief=tk.FLAT, bd=4, insertbackground=c["fg"])
        search_entry.pack(fill=tk.X)
        # Placeholder
        search_entry.insert(0, "Search...")
        search_entry.bind("<FocusIn>", lambda e: self._clear_placeholder(e.widget))
        search_entry.bind("<FocusOut>", lambda e: self._restore_placeholder(e.widget))

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

    def _clear_placeholder(self, widget):
        if widget.get() == "Search...":
            widget.delete(0, tk.END)
            widget.configure(fg=self.colors["fg"])

    def _restore_placeholder(self, widget):
        if not widget.get():
            widget.insert(0, "Search...")
            widget.configure(fg=self.colors["fg_dim"])

    def _on_search(self):
        if not self._ready:
            return
        text = self._search_var.get()
        if text == "Search...":
            text = ""
        self._search_text = text.lower()
        self.refresh()

    def _set_filter(self, value):
        self._filter = value
        c = self.colors
        for v, btn in self._filter_buttons.items():
            if v == value:
                btn.configure(bg=c["accent"], fg="#ffffff")
            else:
                btn.configure(bg=c["button_bg"], fg=c["button_fg"])
        self.refresh()

    def refresh(self):
        """Rebuild the contact list from current data."""
        for widget in self.inner_frame.winfo_children():
            widget.destroy()

        c = self.colors
        filtered = self._contacts
        if self._filter == "favorites":
            filtered = [ct for ct in filtered if ct.favorite]
        if self._search_text:
            filtered = [ct for ct in filtered if
                        self._search_text in ct.name.lower() or
                        self._search_text in ct.number.lower()]

        # Sort: favorites first, then alphabetical
        filtered.sort(key=lambda ct: (not ct.favorite, ct.name.lower()))

        if not filtered:
            msg = "No favorites yet" if self._filter == "favorites" else "No contacts yet"
            tk.Label(self.inner_frame, text=msg, font=("Segoe UI", 10),
                     bg=c["bg_secondary"], fg=c["fg_dim"]).pack(pady=20)
            return

        for contact in filtered:
            self._create_contact_widget(contact)

    def _create_contact_widget(self, contact):
        c = self.colors

        frame = tk.Frame(self.inner_frame, bg=c["bg"], padx=6, pady=4, cursor="hand2")
        frame.pack(fill=tk.X, padx=4, pady=1)

        # Favorite star
        star_color = c["orange"] if contact.favorite else c["fg_dim"]
        star = tk.Label(frame, text="\u2605", font=("Segoe UI", 12),
                        bg=c["bg"], fg=star_color, cursor="hand2")
        star.pack(side=tk.LEFT, padx=(0, 6))
        star.bind("<Button-1>", lambda e, ct=contact: self._toggle_favorite(ct))

        # Name and number
        info = tk.Frame(frame, bg=c["bg"])
        info.pack(side=tk.LEFT, fill=tk.X, expand=True)

        tk.Label(info, text=contact.name, font=("Segoe UI", 10, "bold"),
                 bg=c["bg"], fg=c["fg"], anchor=tk.W).pack(fill=tk.X)
        tk.Label(info, text=contact.number, font=("Segoe UI", 8),
                 bg=c["bg"], fg=c["fg_dim"], anchor=tk.W).pack(fill=tk.X)

        # Delete button
        del_btn = tk.Label(frame, text="\u2715", font=("Segoe UI", 9),
                           bg=c["bg"], fg=c["fg_dim"], cursor="hand2")
        del_btn.pack(side=tk.RIGHT, padx=(4, 0))
        del_btn.bind("<Button-1>", lambda e, ct=contact: self._delete_contact(ct))

        # Click to dial — bind all children
        def _bind_dial(widget, number):
            widget.bind("<Button-1>", lambda e, n=number: self._dial(n))
            for child in widget.winfo_children():
                _bind_dial(child, number)

        _bind_dial(info, contact.number)
        frame.bind("<Button-1>", lambda e, n=contact.number: self._dial(n))

    def _dial(self, number):
        if self._on_dial:
            self._on_dial(number)

    def _toggle_favorite(self, contact):
        contact.favorite = not contact.favorite
        self._notify_change()
        self.refresh()

    def _delete_contact(self, contact):
        self._contacts.remove(contact)
        self._notify_change()
        self.refresh()

    def _notify_change(self):
        if self._on_change:
            self._on_change()

    def _add_dialog(self, edit_contact=None):
        """Show dialog to add or edit a contact."""
        c = self.colors
        dialog = tk.Toplevel(self)
        dialog.title("Edit Contact" if edit_contact else "Add Contact")
        dialog.geometry("300x220")
        dialog.configure(bg=c["bg"])
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()

        tk.Label(dialog, text="Name:", bg=c["bg"], fg=c["fg"],
                 font=("Segoe UI", 10)).pack(anchor=tk.W, padx=15, pady=(15, 2))
        name_var = tk.StringVar(value=edit_contact.name if edit_contact else "")
        name_entry = tk.Entry(dialog, textvariable=name_var, bg=c["bg_input"],
                              fg=c["fg"], font=("Segoe UI", 11), relief=tk.FLAT, bd=4)
        name_entry.pack(fill=tk.X, padx=15)
        name_entry.focus_set()

        tk.Label(dialog, text="Number:", bg=c["bg"], fg=c["fg"],
                 font=("Segoe UI", 10)).pack(anchor=tk.W, padx=15, pady=(8, 2))
        num_var = tk.StringVar(value=edit_contact.number if edit_contact else "")
        tk.Entry(dialog, textvariable=num_var, bg=c["bg_input"],
                 fg=c["fg"], font=("Segoe UI", 11), relief=tk.FLAT, bd=4
                 ).pack(fill=tk.X, padx=15)

        fav_var = tk.BooleanVar(value=edit_contact.favorite if edit_contact else False)
        tk.Checkbutton(dialog, text="\u2605 Favorite", variable=fav_var,
                       bg=c["bg"], fg=c["fg"], selectcolor=c["bg_input"],
                       activebackground=c["bg"], activeforeground=c["fg"],
                       font=("Segoe UI", 10)).pack(anchor=tk.W, padx=15, pady=(8, 0))

        def _save():
            name = name_var.get().strip()
            number = num_var.get().strip()
            if not name or not number:
                return
            if edit_contact:
                edit_contact.name = name
                edit_contact.number = number
                edit_contact.favorite = fav_var.get()
            else:
                self._contacts.append(Contact(
                    name=name, number=number, favorite=fav_var.get()))
            self._notify_change()
            self.refresh()
            dialog.destroy()

        tk.Button(dialog, text="Save", command=_save, bg=c["accent"],
                  fg="#ffffff", font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=20, pady=4).pack(pady=10)

    def get_contacts(self):
        """Return list of dicts for config persistence."""
        return [ct.to_dict() for ct in self._contacts]

    def load_contacts(self, contacts_list):
        """Load contacts from config list of dicts."""
        self._contacts = [Contact.from_dict(d) for d in contacts_list]
        self.refresh()
