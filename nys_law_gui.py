#!/usr/bin/env python3
import re
import os
import threading
import tempfile
import subprocess
import shutil
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
    if "items" in root:
        for item in root.get("items", []):
            law_api.walk_documents(item, [], lines)
    else:
        law_api.walk_documents(root, [], lines)
    if not lines:
        return "No SECTION documents found."
    return "\n".join(lines)


def collect_sections_from_data(data):
    sections = []

    def walk(node):
        if node is None:
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        if "items" in node and isinstance(node.get("items"), list):
            for item in node.get("items", []):
                walk(item)
        if node.get("docType") == "SECTION":
            location_id = node.get("locationId", "")
            title = node.get("title") or ""
            sections.append((location_id, title))
        docs = node.get("documents")
        if docs:
            walk(docs)

    walk(data.get("result", {}).get("documents"))
    return sections


def extract_sections_from_lines(text):
    sections = []
    path_lookup = {}
    for line in text.splitlines():
        match = re.search(r"\(locationId=([^)]+)\)\s*$", line)
        location_id = ""
        if match:
            location_id = match.group(1).strip()
        else:
            marker = "locationId="
            idx = line.rfind(marker)
            if idx != -1:
                remainder = line[idx + len(marker) :]
                end = remainder.find(")")
                location_id = remainder[:end].strip() if end != -1 else remainder.strip()
        if not location_id:
            continue
        parts = line.rsplit("::", 1)
        label = parts[1] if len(parts) == 2 else line
        label = re.sub(r"\s*\(locationId=.*\)\s*$", "", label).strip()
        if label.startswith("SECTION "):
            label = label[len("SECTION ") :].strip()
        sections.append((location_id, label))
        path_lookup[location_id] = line
    return sections, path_lookup


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
        self._law_name_lookup = {}
        self._statute_lookup = []
        self._statute_path_lookup = {}

        self._build_ui()

    def _build_ui(self):
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(5, weight=1)
        self.rowconfigure(7, weight=0)

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

        law_row = ttk.Frame(self)
        law_row.grid(row=2, column=0, columnspan=3, sticky="ew")
        law_row.columnconfigure(1, weight=65)
        law_row.columnconfigure(4, weight=35)

        ttk.Label(law_row, text="Law").grid(row=0, column=0, sticky="w")
        self.law_combo = ttk.Combobox(
            law_row,
            textvariable=self.law_id_var,
            state="readonly",
            width=28,
            values=[],
        )
        self.law_combo.grid(row=0, column=1, sticky="ew", padx=5)
        self.law_combo.bind(
            "<<ComboboxSelected>>", lambda _evt: self._run_list_statutes()
        )
        ttk.Button(law_row, text="Refresh Laws", command=self._run_load_laws).grid(
            row=0, column=2, sticky="w"
        )
        ttk.Label(law_row, text="Statue").grid(row=0, column=3, sticky="w", padx=(10, 0))
        statute_entry = ttk.Entry(law_row, textvariable=self.location_id_var, width=20)
        statute_entry.grid(row=0, column=4, sticky="ew", padx=5)
        statute_entry.bind("<Return>", lambda _evt: self._run_statute_text())

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=3, column=0, columnspan=3, sticky="w", pady=(5, 0))
        self.list_laws_btn = ttk.Button(
            btn_frame, text="List Laws", command=self._run_list_laws
        )
        self.list_laws_btn.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.list_statutes_btn = ttk.Button(
            btn_frame,
            text="List Statutes",
            command=lambda: self._run_list_statutes(show_output=True),
        )
        self.list_statutes_btn.grid(row=0, column=1, sticky="w", padx=(0, 8))
        self.statute_text_btn = ttk.Button(
            btn_frame, text="Statute Text", command=self._run_statute_text
        )
        self.statute_text_btn.grid(row=0, column=2, sticky="w", padx=(0, 8))
        search_frame = ttk.Frame(self)
        search_frame.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        search_frame.columnconfigure(1, weight=1)
        ttk.Label(search_frame, text="Find in statute").grid(
            row=0, column=0, sticky="w"
        )
        self.statute_search_var = tk.StringVar()
        statute_search_entry = ttk.Entry(
            search_frame, textvariable=self.statute_search_var
        )
        statute_search_entry.grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Button(search_frame, text="Find", command=self._find_in_statute).grid(
            row=0, column=2, sticky="w", padx=(0, 6)
        )
        ttk.Button(search_frame, text="Prev", command=self._prev_statute_match).grid(
            row=0, column=3, sticky="w", padx=(0, 6)
        )
        ttk.Button(search_frame, text="Next", command=self._next_statute_match).grid(
            row=0, column=4, sticky="w", padx=(0, 6)
        )
        ttk.Button(search_frame, text="Clear", command=self._clear_statute_highlights).grid(
            row=0, column=5, sticky="w"
        )
        statute_search_entry.bind("<Return>", lambda _evt: self._find_in_statute())

        print_row = ttk.Frame(self)
        print_row.grid(row=7, column=0, columnspan=4, sticky="e", pady=(6, 0), padx=(0, 5))
        self.print_btn = ttk.Button(print_row, text="Print", command=self._run_print)
        self.print_btn.grid(row=0, column=0, sticky="e", padx=(0, 8))
        self.save_pdf_btn = ttk.Button(print_row, text="Save PDF", command=self._run_save_pdf)
        self.save_pdf_btn.grid(row=0, column=1, sticky="e")

        statutes_header = ttk.Frame(self)
        statutes_header.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        statutes_header.columnconfigure(1, weight=1)
        ttk.Label(statutes_header, text="Statutes").grid(row=0, column=0, sticky="w")
        self.statute_list_search_var = tk.StringVar()
        statute_list_search_entry = ttk.Entry(
            statutes_header, textvariable=self.statute_list_search_var
        )
        statute_list_search_entry.grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Button(
            statutes_header, text="Find", command=self._find_in_statute_list
        ).grid(row=0, column=2, sticky="w", padx=(0, 6))
        ttk.Button(
            statutes_header, text="Prev", command=self._prev_statute_list_match
        ).grid(row=0, column=3, sticky="w", padx=(0, 6))
        ttk.Button(
            statutes_header, text="Next", command=self._next_statute_list_match
        ).grid(row=0, column=4, sticky="w", padx=(0, 6))
        ttk.Button(
            statutes_header, text="Clear", command=self._clear_statute_list_highlights
        ).grid(row=0, column=5, sticky="w")
        statute_list_search_entry.bind(
            "<Return>", lambda _evt: self._find_in_statute_list()
        )
        self.paned = ttk.Panedwindow(self, orient="vertical")
        self.paned.grid(row=5, column=0, columnspan=4, sticky="nsew", pady=(5, 0))

        list_frame = ttk.Frame(self.paned)
        self.statute_list = tk.Listbox(list_frame, height=8)
        self.statute_list.pack(side="left", fill="both", expand=True)
        statute_scroll = ttk.Scrollbar(list_frame, command=self.statute_list.yview)
        statute_scroll.pack(side="right", fill="y")
        self.statute_list.configure(yscrollcommand=statute_scroll.set)
        self.statute_list.bind("<<ListboxSelect>>", self._on_statute_select)

        output_frame = ttk.Frame(self.paned)
        self.output = tk.Text(output_frame, wrap="word", height=20)
        self.output.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(output_frame, command=self.output.yview)
        scrollbar.pack(side="right", fill="y")
        self.output.configure(yscrollcommand=scrollbar.set)

        self.paned.add(list_frame, weight=1)
        self.paned.add(output_frame, weight=2)

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
        if hasattr(self, "statute_list"):
            self.statute_list.configure(state="disabled" if busy else "normal")

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
            self.master.after(0, lambda: self._set_busy(True))
            try:
                func()
            except SystemExit as exc:
                self.master.after(
                    0, lambda exc=exc: messagebox.showerror("Error", str(exc))
                )
            except Exception as exc:
                self.master.after(
                    0, lambda exc=exc: messagebox.showerror("Error", str(exc))
                )
            finally:
                self.master.after(0, lambda: self._set_busy(False))

        threading.Thread(target=task, daemon=True).start()

    def _run_list_laws(self):
        key = self._get_key()

        def work():
            data = law_api.api_get("/laws", key)
            output = format_laws(data)
            self.master.after(0, lambda: self._set_output(output))

        self._run_worker(work)

    def _run_load_laws(self):
        key = self._get_key()

        def work():
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
                if law_id:
                    self._law_name_lookup[law_id] = name
            def update():
                self._law_lookup = lookup
                self.law_combo.configure(values=values)
                if values:
                    self.law_combo.set(values[0])
                    self.law_id_var.set(values[0])
                    self._run_list_statutes()

            self.master.after(0, update)

        self._run_worker(work)

    def _run_list_statutes(self, show_output=False):
        law_id = self._resolve_law_id()
        if not law_id:
            raise ValueError("Law ID is required.")
        key = self._get_key()

        def work():
            data = law_api.api_get("/laws/{0}".format(law_id), key)
            output = format_statutes(data)
            sections, path_lookup = extract_sections_from_lines(output)
            if not sections:
                sections = collect_sections_from_data(data)
            if not path_lookup:
                path_lookup = {}
            def update():
                self.statute_list.configure(state="normal")
                self._statute_lookup = sections
                self._statute_path_lookup = path_lookup
                self.statute_list.delete(0, "end")
                for location_id, title in sections:
                    title = title.strip()
                    if title.startswith("{0} - ".format(location_id)):
                        title = title[len(location_id) + 3 :]
                    label = "{0} - {1}".format(location_id, title)
                    self.statute_list.insert("end", label)

                if show_output:
                    self._set_output(output)
                elif not sections:
                    self._set_output("No SECTION documents found for that law.")

            self.master.after(0, update)

        self._run_worker(work)

    def _fetch_statute_text(self, law_id, location_id, key):
        data = law_api.api_get(
            "/laws/{0}/{1}".format(law_id, location_id),
            key,
            params={"full": "true"},
        )
        title, text = law_api.get_text_from_doc(data)
        text = law_api.strip_html(text)
        text = law_api.normalize_statute_text(text)
        text = law_api.format_statute_text(text)
        text = law_api.apply_marker_indents(text)
        if law_id.upper() == "PEN":
            text = law_api.apply_pen_last_sentence_break(text)
        return title, text

    def _run_statute_text(self):
        law_id = self._resolve_law_id()
        location_id = self._resolve_location_id()
        if not law_id or not location_id:
            raise ValueError("Law ID and Location ID are required.")
        key = self._get_key()

        def work():
            title, text = self._fetch_statute_text(law_id, location_id, key)
            output = "{0}\n\n{1}".format(title, text).strip()
            def update():
                self._set_output(output)
                self._select_statute_in_list(location_id)

            self.master.after(0, update)

        self._run_worker(work)

    def _run_save_pdf(self):
        law_id = self._resolve_law_id()
        location_id = self._resolve_location_id()
        if not law_id or not location_id:
            raise ValueError("Law ID and Location ID are required.")
        key = self._get_key()
        title = self._get_statute_title(location_id)
        suffix = location_id
        if title:
            suffix = "{0}_{1}".format(location_id, self._sanitize_filename(title))
        default_name = "{0}_{1}.pdf".format(law_id, suffix)
        path = filedialog.asksaveasfilename(
            title="Save PDF",
            defaultextension=".pdf",
            initialfile=default_name,
            filetypes=[("PDF", "*.pdf")],
        )
        if not path:
            return

        def work():
            title, text = self._fetch_statute_text(law_id, location_id, key)
            header_lines = self._build_pdf_header(law_id, location_id)
            highlight_term = self.statute_search_var.get().strip()
            law_api.write_pdf(
                path,
                title,
                text,
                header_lines=header_lines,
                highlight_term=highlight_term if highlight_term else None,
            )
            self.master.after(
                0, lambda: messagebox.showinfo("Saved", "PDF written to {0}".format(path))
            )

        self._run_worker(work)

    def _run_print(self):
        law_id = self._resolve_law_id()
        location_id = self._resolve_location_id()
        if not law_id or not location_id:
            raise ValueError("Law ID and Location ID are required.")
        key = self._get_key()
        if not shutil.which("lp") and not shutil.which("lpr"):
            raise ValueError("No system print command found (lp or lpr).")

        def work():
            title, text = self._fetch_statute_text(law_id, location_id, key)
            header_lines = self._build_pdf_header(law_id, location_id)
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                pdf_path = tmp.name
            try:
                law_api.write_pdf(
                    pdf_path,
                    title,
                    text,
                    header_lines=header_lines,
                    highlight_term=self.statute_search_var.get().strip() or None,
                )
                if shutil.which("lp"):
                    subprocess.check_call(["lp", pdf_path])
                else:
                    subprocess.check_call(["lpr", pdf_path])
                self.master.after(0, lambda: messagebox.showinfo("Printed", "Print job sent."))
            finally:
                try:
                    os.unlink(pdf_path)
                except Exception:
                    pass

        self._run_worker(work)

    def _get_selected_location_id(self):
        selection = self.statute_list.curselection()
        if not selection:
            return ""
        index = int(selection[0])
        if index < len(self._statute_lookup):
            return self._statute_lookup[index][0]
        return ""

    def _resolve_law_id(self):
        law_key = self.law_id_var.get().strip()
        law_id = self._law_lookup.get(law_key)
        if law_id:
            return law_id
        if " - " in law_key:
            return law_key.split(" - ", 1)[0].strip()
        return law_key

    def _resolve_location_id(self):
        location_id = self.location_id_var.get().strip()
        if not location_id:
            location_id = self._get_selected_location_id()
        return location_id

    def _on_statute_select(self, _event):
        location_id = self._get_selected_location_id()
        if location_id:
            self.location_id_var.set(location_id)
            self._run_statute_text()

    def _select_statute_in_list(self, location_id):
        for idx, item in enumerate(self._statute_lookup):
            if item[0] == location_id:
                self.statute_list.selection_clear(0, "end")
                self.statute_list.selection_set(idx)
                self.statute_list.see(idx)
                break

    def _get_statute_title(self, location_id):
        for item in self._statute_lookup:
            if item[0] == location_id:
                return item[1]
        return ""

    def _sanitize_filename(self, name):
        cleaned = re.sub(r"[^\w\- ]+", "", name)
        cleaned = re.sub(r"\s+", "_", cleaned).strip("_")
        return cleaned[:60]

    def _find_in_statute(self):
        term = self.statute_search_var.get().strip()
        self.output.tag_remove("search_match", "1.0", "end")
        self.output.tag_remove("search_current", "1.0", "end")
        if not term:
            self._statute_matches = []
            self._statute_match_index = -1
            return
        start = "1.0"
        self._statute_matches = []
        while True:
            pos = self.output.search(term, start, stopindex="end", nocase=True)
            if not pos:
                break
            end = "{0}+{1}c".format(pos, len(term))
            self.output.tag_add("search_match", pos, end)
            self._statute_matches.append((pos, end))
            start = end
        self.output.tag_config("search_match", background="#ffe08a")
        self.output.tag_config("search_current", background="#ffbf66")
        if self._statute_matches:
            self._statute_match_index = 0
            self._show_statute_match(self._statute_match_index)
        else:
            self._statute_match_index = -1

    def _show_statute_match(self, index):
        if not self._statute_matches:
            return
        index = max(0, min(index, len(self._statute_matches) - 1))
        self._statute_match_index = index
        self.output.tag_remove("search_current", "1.0", "end")
        start, end = self._statute_matches[index]
        self.output.tag_add("search_current", start, end)
        self.output.see(start)

    def _next_statute_match(self):
        if not getattr(self, "_statute_matches", None):
            self._find_in_statute()
        if not self._statute_matches:
            return
        next_index = (self._statute_match_index + 1) % len(self._statute_matches)
        self._show_statute_match(next_index)

    def _prev_statute_match(self):
        if not getattr(self, "_statute_matches", None):
            self._find_in_statute()
        if not self._statute_matches:
            return
        prev_index = (self._statute_match_index - 1) % len(self._statute_matches)
        self._show_statute_match(prev_index)

    def _clear_statute_highlights(self):
        self.output.tag_remove("search_match", "1.0", "end")
        self.output.tag_remove("search_current", "1.0", "end")
        self._statute_matches = []
        self._statute_match_index = -1

    def _find_in_statute_list(self):
        term = self.statute_list_search_var.get().strip().lower()
        self.statute_list.selection_clear(0, "end")
        if not term:
            self._statute_list_matches = []
            self._statute_list_match_index = -1
            return
        matches = []
        for idx in range(self.statute_list.size()):
            label = self.statute_list.get(idx).lower()
            if term in label:
                matches.append(idx)
        self._statute_list_matches = matches
        if matches:
            self._statute_list_match_index = 0
            self._show_statute_list_match(self._statute_list_match_index)
        else:
            self._statute_list_match_index = -1

    def _show_statute_list_match(self, index):
        if not self._statute_list_matches:
            return
        index = max(0, min(index, len(self._statute_list_matches) - 1))
        self._statute_list_match_index = index
        match_index = self._statute_list_matches[index]
        self.statute_list.selection_clear(0, "end")
        self.statute_list.selection_set(match_index)
        self.statute_list.see(match_index)

    def _next_statute_list_match(self):
        if not getattr(self, "_statute_list_matches", None):
            self._find_in_statute_list()
        if not self._statute_list_matches:
            return
        next_index = (self._statute_list_match_index + 1) % len(
            self._statute_list_matches
        )
        self._show_statute_list_match(next_index)

    def _prev_statute_list_match(self):
        if not getattr(self, "_statute_list_matches", None):
            self._find_in_statute_list()
        if not self._statute_list_matches:
            return
        prev_index = (self._statute_list_match_index - 1) % len(
            self._statute_list_matches
        )
        self._show_statute_list_match(prev_index)

    def _clear_statute_list_highlights(self):
        self.statute_list.selection_clear(0, "end")
        self._statute_list_matches = []
        self._statute_list_match_index = -1

    def _build_pdf_header(self, law_id, location_id):
        header = []
        law_name = self._law_name_lookup.get(law_id, "")
        if law_name:
            header.append("Law: {0} ({1})".format(law_name, law_id))
        elif law_id:
            header.append("Law ID: {0}".format(law_id))

        line = self._statute_path_lookup.get(location_id, "")
        if line:
            chapter = self._extract_part(line, r"CHAPTER\s+([^/]+)")
            title = self._extract_part(line, r"TITLE\s+([^/]+)")
            article = self._extract_part(line, r"ARTICLE\s+([^:]+)")
            section = self._extract_part(line, r"SECTION\s+([^-]+)")
            section_title = self._extract_part(
                line, r"SECTION\s+[^-]+-\s*(.*)\s*\(locationId="
            )
            if chapter:
                header.append("Chapter: {0}".format(chapter))
            if title:
                header.append("Title: {0}".format(title))
            if article:
                header.append("Article: {0}".format(article))
            if section:
                section_line = "Section: {0}".format(section)
                if section_title:
                    section_line = "{0} - {1}".format(section_line, section_title)
                header.append(section_line)
        return header

    def _extract_part(self, line, pattern):
        match = re.search(pattern, line)
        return match.group(1).strip() if match else ""

    def _open_search_window(self):
        window = tk.Toplevel(self.master)
        window.title("Search")
        window.transient(self.master)

        query_var = tk.StringVar()
        scope_var = tk.StringVar(value="statutes")

        ttk.Label(window, text="Search for").grid(row=0, column=0, sticky="w", padx=10, pady=10)
        ttk.Entry(window, textvariable=query_var, width=40).grid(
            row=0, column=1, sticky="ew", padx=10, pady=10
        )

        scope_frame = ttk.Frame(window)
        scope_frame.grid(row=1, column=0, columnspan=2, sticky="w", padx=10)
        ttk.Radiobutton(
            scope_frame, text="Statutes list", variable=scope_var, value="statutes"
        ).grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Radiobutton(
            scope_frame, text="Statute text", variable=scope_var, value="text"
        ).grid(row=0, column=1, sticky="w", padx=(0, 10))
        ttk.Radiobutton(
            scope_frame, text="Both", variable=scope_var, value="both"
        ).grid(row=0, column=2, sticky="w")

        results = tk.Listbox(window, height=12)
        results.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=10, pady=10)
        results_scroll = ttk.Scrollbar(window, command=results.yview)
        results_scroll.grid(row=2, column=2, sticky="ns", pady=10)
        results.configure(yscrollcommand=results_scroll.set)

        def search_statutes(term_lower):
            hits = []
            for location_id, title in self._statute_lookup:
                label = "{0} - {1}".format(location_id, title)
                if term_lower in label.lower():
                    hits.append(("statute", location_id, label))
            return hits

        def search_text(term_lower):
            hits = []
            text = self.output.get("1.0", "end-1c")
            text_lower = text.lower()
            start = 0
            while True:
                idx = text_lower.find(term_lower, start)
                if idx == -1:
                    break
                snippet_start = max(0, idx - 40)
                snippet_end = min(len(text), idx + 40)
                snippet = text[snippet_start:snippet_end].replace("\n", " ")
                hits.append(("text", idx, snippet))
                start = idx + len(term_lower)
            return hits

        def run_search(_event=None):
            term = query_var.get().strip()
            results.delete(0, "end")
            if not term:
                return
            term_lower = term.lower()
            combined = []
            scope = scope_var.get()
            if scope in ("statutes", "both"):
                combined.extend(search_statutes(term_lower))
            if scope in ("text", "both"):
                combined.extend(search_text(term_lower))
            for item in combined:
                if item[0] == "statute":
                    results.insert("end", "STATUTE: {0}".format(item[2]))
                else:
                    results.insert("end", "TEXT: ...{0}...".format(item[2]))

        def on_result_select(_event):
            selection = results.curselection()
            if not selection:
                return
            index = int(selection[0])
            scope = scope_var.get()
            term = query_var.get().strip()
            if not term:
                return
            term_lower = term.lower()
            combined = []
            if scope in ("statutes", "both"):
                combined.extend(search_statutes(term_lower))
            if scope in ("text", "both"):
                combined.extend(search_text(term_lower))
            if index >= len(combined):
                return
            item = combined[index]
            if item[0] == "statute":
                location_id = item[1]
                self.location_id_var.set(location_id)
                self._run_statute_text()

        ttk.Button(window, text="Find", command=run_search).grid(
            row=3, column=0, sticky="w", padx=10, pady=(0, 10)
        )
        ttk.Button(window, text="Close", command=window.destroy).grid(
            row=3, column=1, sticky="e", padx=10, pady=(0, 10)
        )

        results.bind("<Double-Button-1>", on_result_select)
        window.columnconfigure(1, weight=1)
        window.rowconfigure(2, weight=1)


def main():
    root = tk.Tk()
    root.geometry("1200x850")
    app = App(root)
    app._run_load_laws()
    root.mainloop()


if __name__ == "__main__":
    main()
