#!/usr/bin/env python3
import argparse
import json
import os
import sys
import textwrap
import urllib.parse
import urllib.request
import re
from html.parser import HTMLParser

BASE_URL = "https://legislation.nysenate.gov/api/3"


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super(_HTMLStripper, self).__init__()
        self._chunks = []

    def handle_data(self, data):
        if data:
            self._chunks.append(data)

    def get_data(self):
        return "".join(self._chunks)


def strip_html(text):
    stripper = _HTMLStripper()
    stripper.feed(text)
    return stripper.get_data()


def load_api_key(key, key_file):
    if key:
        return key.strip()
    if key_file and os.path.exists(key_file):
        with open(key_file, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                return content
    env_key = os.environ.get("NYS_LAW_API_KEY")
    if env_key:
        return env_key.strip()
    raise SystemExit(
        "Missing API key. Provide --key, --key-file, or set NYS_LAW_API_KEY."
    )


def api_get(path, key, params=None):
    query = dict(params or {})
    query["key"] = key
    url = "{0}{1}?{2}".format(BASE_URL, path, urllib.parse.urlencode(query))
    try:
        with urllib.request.urlopen(url) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            "API request failed: {0} {1}".format(exc.code, exc.reason)
        )
    except urllib.error.URLError as exc:
        raise SystemExit("API request failed: {0}".format(exc.reason))
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit("API response was not valid JSON.")


def print_laws(data):
    items = data.get("result", {}).get("items", [])
    if not items:
        print("No laws returned.")
        return
    for item in items:
        law_id = item.get("lawId", "")
        name = item.get("name", "")
        law_type = item.get("lawType", "")
        chapter = item.get("chapter", "")
        print("{0}\t{1}\t{2}\t{3}".format(law_id, name, law_type, chapter))


def walk_documents(doc, ancestors, out_lines):
    doc_type = doc.get("docType", "")
    title = doc.get("title") or ""
    doc_level = doc.get("docLevelId", "")
    location_id = doc.get("locationId", "")

    label_parts = [p for p in [doc_type, doc_level] if p]
    label = " ".join(label_parts).strip()
    if label:
        label = "{0} - ".format(label)
    path = " / ".join([p for p in ancestors if p])

    if doc_type == "SECTION":
        line = "{0} :: {1}{2} (locationId={3})".format(
            path, label, title, location_id
        )
        out_lines.append(line.strip(" /"))

    for child in doc.get("documents", {}).get("items", []):
        next_ancestors = ancestors + ["{0} {1}".format(doc_type, doc_level).strip()]
        walk_documents(child, next_ancestors, out_lines)


def list_statutes(data):
    root = data.get("result", {}).get("documents")
    if not root:
        print("No documents returned for that law.")
        return
    lines = []
    walk_documents(root, [], lines)
    if not lines:
        print("No SECTION documents found.")
        return
    for line in lines:
        print(line)


def get_text_from_doc(data):
    result = data.get("result", {})
    title = result.get("title") or result.get("docType") or "Document"
    text = result.get("text") or ""
    if not text:
        # Some responses embed the document under documents/items
        docs = result.get("documents", {}).get("items", [])
        if docs:
            title = docs[0].get("title") or title
            text = docs[0].get("text") or ""
    return title, text


def wrap_text(text, width=90):
    lines = []
    for paragraph in text.splitlines():
        if not paragraph.strip():
            lines.append("")
            continue
        lines.append(textwrap.fill(paragraph.strip(), width=width))
    return "\n".join(lines).strip()


def normalize_statute_text(text):
    # Remove literal "\n" sequences and collapse whitespace to single spaces.
    cleaned = text.replace("\\n", " ")
    return " ".join(cleaned.split()).strip()


def marker_indent(marker):
    raw = marker.strip()
    has_paren = raw.startswith("(") and raw.endswith(")")
    clean = raw.strip("()").rstrip(".")
    if re.match(r"^\d+-[A-Za-z]+$", clean):
        level = 0
    elif re.match(r"^\d+$", clean):
        level = 3 if has_paren else 0
    elif re.match(r"^[ivxlcdm]+$", clean, re.IGNORECASE):
        level = 2
    elif re.match(r"^[A-Z]+$", clean):
        level = 4 if has_paren else 1
    elif re.match(r"^[a-z]+$", clean):
        level = 1
    else:
        level = 0
    return "  " * level


def apply_marker_indents(text):
    lines = []
    pattern = re.compile(
        r"^\s*(\(?\d+[)\.]|\(?[A-Za-z]+[)\.]|\d+-[A-Za-z][)\.]?)\b"
    )
    for line in text.splitlines():
        match = pattern.match(line)
        if not match:
            lines.append(line)
            continue
        marker = match.group(1)
        indent = marker_indent(marker)
        lines.append(indent + line.strip())
    return "\n".join(lines)


def format_statute_text(text):
    # Break lines at subsection markers only when preceded by a period, comma, semicolon, or colon,
    # and optionally the word "and" or "or" after punctuation.

    pattern = re.compile(
        r"([.,;:])\s+((?:and|or)\s+)?((?:\(?\d+[)\.]|\(?[A-Za-z]+[)\.]|\d+-[A-Za-z][)\.]?))\s+",
        re.IGNORECASE,
    )
    def repl(match):
        punct = match.group(1)
        conj = match.group(2) or ""
        marker = match.group(3)
        indent = marker_indent(marker)
        conj = conj.strip()
        if conj:
            return "{0} {1}\n{2}{3} ".format(punct, conj, indent, marker)
        return "{0}\n{1}{2} ".format(punct, indent, marker)

    return pattern.sub(repl, text).strip()


def write_pdf(path, title, body, header_lines=None):
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
    except Exception:
        raise SystemExit(
            "PDF output requires reportlab. Install with: pip install reportlab"
        )

    c = canvas.Canvas(path, pagesize=letter)
    width, height = letter
    margin = 54
    y = height - margin
    c.setFont("Times-Bold", 14)
    for wrapped in wrap_text(title, width=80).splitlines():
        if y < margin:
            c.showPage()
            y = height - margin
            c.setFont("Times-Bold", 14)
        c.drawString(margin, y, wrapped)
        y -= 18
    y -= 6
    if header_lines:
        c.setFont("Times-Roman", 11)
        for line in header_lines:
            for wrapped in wrap_text(line, width=100).splitlines():
                if y < margin:
                    c.showPage()
                    y = height - margin
                    c.setFont("Times-Roman", 11)
                c.drawString(margin, y, wrapped)
                y -= 14
        y -= 6
    c.setFont("Times-Roman", 11)
    for line in _wrap_preserve_indent(body, width=100):
        if y < margin:
            c.showPage()
            y = height - margin
            c.setFont("Times-Roman", 11)
        c.drawString(margin, y, line)
        y -= 14
    c.save()


def _wrap_preserve_indent(text, width=100):
    lines = []
    for paragraph in text.splitlines():
        if not paragraph.strip():
            lines.append("")
            continue
        leading = len(paragraph) - len(paragraph.lstrip(" "))
        indent = " " * leading
        content = paragraph.lstrip(" ")
        wrapped = textwrap.fill(
            content,
            width=width,
            initial_indent=indent,
            subsequent_indent=indent,
        )
        lines.extend(wrapped.splitlines())
    return lines


def cmd_list_laws(args):
    key = load_api_key(args.key, args.key_file)
    data = api_get("/laws", key)
    print_laws(data)


def cmd_structure(args):
    key = load_api_key(args.key, args.key_file)
    data = api_get("/laws/{0}".format(args.law_id), key)
    list_statutes(data)


def cmd_statute(args):
    key = load_api_key(args.key, args.key_file)
    data = api_get(
        "/laws/{0}/{1}".format(args.law_id, args.location_id),
        key,
        params={"full": "true"},
    )
    title, text = get_text_from_doc(data)
    if args.strip_html:
        text = strip_html(text)
    text = normalize_statute_text(text)
    text = format_statute_text(text)
    text = apply_marker_indents(text)
    output = "{0}\n\n{1}".format(title, text).strip()
    print(output)
    if args.pdf:
        write_pdf(args.pdf, title, text)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Query the NYS Law API for laws, statute structure, and text."
    )
    parser.add_argument("--key", help="API key value (overrides --key-file).")
    parser.add_argument(
        "--key-file",
        default="api_key.txt",
        help="Path to file containing the API key.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list-laws", help="List all laws.")
    list_parser.set_defaults(func=cmd_list_laws)

    structure_parser = subparsers.add_parser(
        "list-statutes",
        help="List SECTION statutes for a law.",
    )
    structure_parser.add_argument("law_id", help="Three-letter law ID (e.g. EDN).")
    structure_parser.set_defaults(func=cmd_structure)

    statute_parser = subparsers.add_parser(
        "statute-text",
        help="Get the text of a statute by location ID.",
    )
    statute_parser.add_argument("law_id", help="Three-letter law ID (e.g. EDN).")
    statute_parser.add_argument(
        "location_id",
        help="Location ID of the statute (e.g. 100, A2).",
    )
    statute_parser.add_argument(
        "--no-strip-html",
        dest="strip_html",
        action="store_false",
        help="Do not remove HTML tags from text.",
    )
    statute_parser.add_argument(
        "--pdf",
        help="Optional path to write statute text to a PDF file.",
    )
    statute_parser.set_defaults(func=cmd_statute, strip_html=True)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
