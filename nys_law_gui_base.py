#!/usr/bin/env python3
import threading
import tkinter as tk
from tkinter import filedialog
from tkinter import messagebox
from tkinter import ttk

import nys_law_cli as law_api


def format_laws(data):
    items = data.get("result", {}).get("items", [])
    if not items:
        return "No laws returned."
    lines = []
    for item in items:
        law_id = item.get("lawId", "")
        name = item.get("name", "")
        law_type = item.get("lawType", "")
        chapter = item.get("chapter", "")
        lines.append("{0}\t{1}\t{2}\t{3}".format(law_id, name, law_type, chapter))
    return "\n".join(lines)


def format_statutes(data):
    root = data.get("result", {}).get("documents")
    if not root:
        return "No documents returned for that law."
    lines = []
    law_api.walk_documents(root, [], lines)
    if not lines:
        return "No SECTION documents found."
    return "\n".join(lines)


class App(ttk.Frame):
    def __init__(self, master):
        ttk.Frame.__init__(self, master, padding=10)
        self.master.title("NYS Law Lookup")
        self.grid(sticky="nsew")

        self.api_key_var = tk.StringVar()
        self.api_key_file_var = tk.StringVar(value="api_key.txt")
        self.law_id_var = tk.StringVar()
        self.location_id_var = tk.StringVar()
        self._law_lookup = {}

        self._build_ui()

    def _build_ui(self):
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(4, weight=1)

        ttk.Label(self, text="API Key (optional)").grid(row=0, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.api_key_var, width=40).grid(
            row=0, column=1, sticky="ew", padx=5
        )

        ttk.Label(self, text="API Key File").grid(row=1, column=0, sticky="w")
        key_file_entry = ttk.Entry(self, textvariable=self.api_key_file_var, width=40)
        key_file_entry.grid(row=1, column=1, sticky="ew", padx=5)
        ttk.Button(self, text="Browse", command=self._browse_key_file).grid(
            row=1, column=2, sticky="w"
        )

        ttk.Label(self, text="Law").grid(row=2, column=0, sticky="w")
        self.law_combo = ttk.Combobox(
            self,
            textvariable=self.law_id_var,
            state="readonly",
            width=40,
            values=[],
        )
        self.law_combo.grid(row=2, column=1, sticky="ew", padx=5)
        ttk.Button(self, text="Refresh Laws", command=self._run_load_laws).grid(
            row=2, column=2, sticky="w"
        )

        ttk.Label(self, text="Location ID").grid(row=3, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.location_id_var, width=20).grid(
            row=3, column=1, sticky="w", padx=5
        )

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=2, column=2, rowspan=2, sticky="n")
        self.list_laws_btn = ttk.Button(
            btn_frame, text="List Laws", command=self._run_list_laws
        )
        self.list_laws_btn.grid(row=0, column=0, sticky="ew", pady=2)
        self.list_statutes_btn = ttk.Button(
            btn_frame, text="List Statutes", command=self._run_list_statutes
        )
        self.list_statutes_btn.grid(row=1, column=0, sticky="ew", pady=2)
        self.statute_text_btn = ttk.Button(
            btn_frame, text="Statute Text", command=self._run_statute_text
        )
        self.statute_text_btn.grid(row=2, column=0, sticky="ew", pady=2)
        self.save_pdf_btn = ttk.Button(
            btn_frame, text="Save PDF", command=self._run_save_pdf
        )
        self.save_pdf_btn.grid(row=3, column=0, sticky="ew", pady=2)

        self.output = tk.Text(self, wrap="word", height=20)
        self.output.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
        scrollbar = ttk.Scrollbar(self, command=self.output.yview)
        scrollbar.grid(row=4, column=3, sticky="ns", pady=(10, 0))
        self.output.configure(yscrollcommand=scrollbar.set)

    def _browse_key_file(self):
        path = filedialog.askopenfilename(title="Select API Key File")
        if path:
            self.api_key_file_var.set(path)

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for btn in (
            self.list_laws_btn,
            self.list_statutes_btn,
            self.statute_text_btn,
            self.save_pdf_btn,
        ):
            btn.configure(state=state)
        if hasattr(self, "law_combo"):
            self.law_combo.configure(state="disabled" if busy else "readonly")

    def _set_output(self, text):
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)

    def _get_key(self):
        return law_api.load_api_key(
            self.api_key_var.get().strip() or None,
            self.api_key_file_var.get().strip() or None,
        )

    def _run_worker(self, func):
        def task():
            self._set_busy(True)
            try:
                func()
            except Exception as exc:
                messagebox.showerror("Error", str(exc))
            finally:
                self._set_busy(False)

        threading.Thread(target=task, daemon=True).start()

    def _run_list_laws(self):
        def work():
            key = self._get_key()
            data = law_api.api_get("/laws", key)
            self._set_output(format_laws(data))

        self._run_worker(work)

    def _run_load_laws(self):
        def work():
            key = self._get_key()
            data = law_api.api_get("/laws", key)
            items = data.get("result", {}).get("items", [])
            values = []
            lookup = {}
            for item in items:
                law_id = item.get("lawId", "")
                name = item.get("name", "")
                label = "{0} - {1}".format(law_id, name)
                values.append(label)
                lookup[label] = law_id
            self._law_lookup = lookup
            self.law_combo.configure(values=values)
            if values:
                self.law_combo.set(values[0])
                self.law_id_var.set(values[0])

        self._run_worker(work)

    def _run_list_statutes(self):
        def work():
            law_key = self.law_id_var.get().strip()
            law_id = self._law_lookup.get(law_key, law_key)
            if not law_id:
                raise ValueError("Law ID is required.")
            key = self._get_key()
            data = law_api.api_get("/laws/{0}".format(law_id), key)
            self._set_output(format_statutes(data))

        self._run_worker(work)

    def _get_statute_text(self):
        law_key = self.law_id_var.get().strip()
        law_id = self._law_lookup.get(law_key, law_key)
        location_id = self.location_id_var.get().strip()
        if not law_id or not location_id:
            raise ValueError("Law ID and Location ID are required.")
        key = self._get_key()
        data = law_api.api_get(
            "/laws/{0}/{1}".format(law_id, location_id),
            key,
            params={"full": "true"},
        )
        title, text = law_api.get_text_from_doc(data)
        text = law_api.strip_html(text)
        text = law_api.normalize_statute_text(text)
        text = law_api.format_statute_text(text)
        text = law_api.wrap_text(text, width=90)
        return title, text

    def _run_statute_text(self):
        def work():
            title, text = self._get_statute_text()
            output = "{0}\n\n{1}".format(title, text).strip()
            self._set_output(output)

        self._run_worker(work)

    def _run_save_pdf(self):
        def work():
            title, text = self._get_statute_text()
            path = filedialog.asksaveasfilename(
                title="Save PDF",
                defaultextension=".pdf",
                filetypes=[("PDF", "*.pdf")],
            )
            if path:
                law_api.write_pdf(path, title, text)
                messagebox.showinfo("Saved", "PDF written to {0}".format(path))

        self._run_worker(work)


def main():
    root = tk.Tk()
    app = App(root)
    app._run_load_laws()
    root.mainloop()


if __name__ == "__main__":
    main()
