#!/usr/bin/env python3
"""
Preservica MODS Batch Editor
============================

A small desktop tool for batch-editing MODS metadata held in Preservica.

Workflow
    1. Connect to Preservica.
    2. Pick a folder and one or more MODS fields, then export them to a CSV.
    3. Clean the CSV in OpenRefine (or any spreadsheet tool).
    4. Load the edited CSV, preview every change, then apply.

The list of fields comes from your Preservica MODS CSV template
(MODS_template.csv, kept next to this script): row 1 holds the MODS path of
each column and row 2 holds its label.

Only the elements you change are touched. The rest of each MODS record is
left exactly as it is, and the original XML is saved before anything is
written back.

Requirements:  pip install pyPreservica lxml
Run:           python preservica_mods_editor.py
"""
from __future__ import annotations

import csv
import difflib
import json
import queue
import re
import threading
import traceback
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path

from lxml import etree

APP_TITLE = "Preservica MODS Batch Editor"
DEFAULT_SEP = "|"
ID_COLS = ["entity_ref", "entity_type", "title", "mods_status"]
SETTINGS_FILE = Path.home() / ".preservica_mods_editor.json"
MAX_PREVIEW_ROWS = 5000

# How a NEW value is added for fields that sit inside a wrapper element.
#   REPEAT_WRAPPERS  - every new value gets its own new wrapper
#                      (one <subject> per topic, one <titleInfo> per title ...)
#   ALIGNED_WRAPPERS - the value goes into the first existing wrapper that does
#                      not have this sub-element yet, otherwise a new wrapper
#                      (so a new Role lands in the <name> that has no role)
#   anything else    - the existing wrapper is reused
#                      (<originInfo>, <physicalDescription>, <location>)
REPEAT_WRAPPERS = {"subject", "titleInfo", "language"}
ALIGNED_WRAPPERS = {"name", "relatedItem"}

STEP_RE = re.compile(r"^ns:([A-Za-z_][\w.\-]*)((?:\[@[\w:.\-]+='[^']*'\])*)$")
PRED_RE = re.compile(r"\[@([\w:.\-]+)='([^']*)'\]")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


# ----------------------------------------------------------------------------
# Template / field definitions
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Field:
    label: str          # e.g. "Subject (Topical)"
    namespace: str      # e.g. "http://www.loc.gov/mods/v3"
    xpath: str          # e.g. "//ns:mods/ns:subject/ns:topic"
    steps: tuple        # (("mods", ()), ("subject", ()), ("topic", ()))


def _parse_steps(xpath: str) -> tuple:
    steps = []
    for raw in xpath.lstrip("/").split("/"):
        m = STEP_RE.match(raw.strip())
        if not m:
            raise ValueError(f"Cannot understand this path in the template: {xpath}")
        steps.append((m.group(1), tuple(PRED_RE.findall(m.group(2)))))
    if len(steps) < 2:
        raise ValueError(f"Path is too short to be a field: {xpath}")
    return tuple(steps)


def load_template(path) -> list:
    """Read a Preservica CSV metadata template and return its fields."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in csv.reader(fh) if r]
    if len(rows) < 2 or not rows[0] or rows[0][0].strip() != "PRESERVICA_CSV_TEMPLATE":
        raise ValueError("This does not look like a Preservica CSV template "
                         "(the first cell should be PRESERVICA_CSV_TEMPLATE).")
    fields, seen = [], set()
    for i in range(1, len(rows[0])):
        cell = rows[0][i].strip()
        if not cell:
            continue
        namespace, _, xpath = cell.partition(";")
        label = rows[1][i].strip() if i < len(rows[1]) else ""
        if not xpath or not label:
            raise ValueError(f"Template column {i + 1} is missing a path or a label.")
        if label in seen or label in ID_COLS:
            raise ValueError(f"Template label is used twice or is reserved: {label}")
        seen.add(label)
        fields.append(Field(label, namespace.strip(), xpath.strip(), _parse_steps(xpath.strip())))
    if not fields:
        raise ValueError("No fields found in the template.")
    if len({f.namespace for f in fields}) != 1:
        raise ValueError("The template mixes more than one metadata schema; use one schema per template.")
    return fields


# ----------------------------------------------------------------------------
# XML helpers
# ----------------------------------------------------------------------------

def _q(ns, tag):
    return f"{{{ns}}}{tag}"


def _is_el(node):
    return isinstance(node.tag, str)


def _matches(el, ns, step):
    tag, preds = step
    return _is_el(el) and el.tag == _q(ns, tag) and all(el.get(a) == v for a, v in preds)


def _own_text(el):
    return (el.text or "").strip()


def _is_nested(el):
    """True for an element whose content is held in child elements, not its own text."""
    return any(_is_el(c) for c in el) and not _own_text(el)


def _display_text(el):
    if _is_nested(el):
        return " ".join(t.strip() for t in el.itertext() if t.strip())
    return _own_text(el)


def _step_xpath(step):
    tag, preds = step
    return f"ns:{tag}" + "".join(f"[@{a}='{v}']" for a, v in preds)


def parse_xml(xml_text: str):
    return etree.fromstring(xml_text.encode("utf-8"))


def find_record_root(root, field: Field):
    """The element the template paths start from (the <mods> element)."""
    target = _q(field.namespace, field.steps[0][0])
    if root.tag == target:
        return root
    return root.find(f".//{target}")


def field_elements(root, field: Field) -> list:
    return root.xpath(field.xpath, namespaces={"ns": field.namespace})


def field_values(root, field: Field) -> list:
    return [_display_text(e) for e in field_elements(root, field)]


def _make(ns, step, parent=None):
    tag, preds = step
    el = etree.SubElement(parent, _q(ns, tag)) if parent is not None else etree.Element(_q(ns, tag))
    for a, v in preds:
        el.set(a, v)
    return el


def _build_chain(ns, steps, value):
    top = cur = _make(ns, steps[0])
    for s in steps[1:]:
        cur = _make(ns, s, cur)
    cur.text = value
    return top, cur


def _first_match(parent, ns, step):
    found = [c for c in parent if _matches(c, ns, step)]
    if not found:
        return None
    if not step[1]:  # a path without a type: prefer an element without attributes
        plain = [c for c in found if not c.attrib]
        if plain:
            return plain[0]
    return found[0]


def _ensure_path(ns, parent, steps):
    cur = parent
    for s in steps:
        nxt = _first_match(cur, ns, s)
        cur = nxt if nxt is not None else _make(ns, s, cur)
    return cur


def _top_under(record, el):
    while el.getparent() is not None and el.getparent() is not record:
        el = el.getparent()
    return el


def _add_value(record, field: Field, value, after=None, before=None):
    """Create a new element for `value`. Returns the new leaf element."""
    ns, steps = field.namespace, field.steps[1:]
    head = steps[0][0]
    if len(steps) > 1 and head not in REPEAT_WRAPPERS:
        if head in ALIGNED_WRAPPERS:
            rel = "/".join(_step_xpath(s) for s in steps[1:])
            for wrapper in record:
                if _matches(wrapper, ns, steps[0]) and not wrapper.xpath(rel, namespaces={"ns": ns}):
                    leaf = _make(ns, steps[-1], _ensure_path(ns, wrapper, steps[1:-1]))
                    leaf.text = value
                    return leaf
        else:
            existing = _first_match(record, ns, steps[0])
            if existing is not None:
                container = _ensure_path(ns, existing, steps[1:-1])
                leaf = _make(ns, steps[-1])
                leaf.text = value
                if after is not None and after.getparent() is container:
                    after.addnext(leaf)
                else:
                    container.append(leaf)
                return leaf
    top, leaf = _build_chain(ns, steps, value)
    a = _top_under(record, after) if after is not None else None
    b = _top_under(record, before) if before is not None else None
    if a is not None and a.getparent() is record:
        a.addnext(top)
    elif b is not None and b.getparent() is record:
        b.addprevious(top)
    else:
        record.append(top)
    return leaf


def _remove_element(record, el):
    parent = el.getparent()
    parent.remove(el)
    # tidy up wrappers that are now completely empty
    while parent is not None and parent is not record and len(parent) == 0 and not _own_text(parent):
        grand = parent.getparent()
        if grand is None:
            break
        grand.remove(parent)
        parent = grand


# ----------------------------------------------------------------------------
# Planning changes
# ----------------------------------------------------------------------------

@dataclass
class Change:
    label: str
    action: str      # edit | add | remove | blocked | skipped
    old: str = ""
    new: str = ""
    note: str = ""

    @property
    def effective(self):
        return self.action in ("edit", "add", "remove")


@dataclass
class RecordPlan:
    ref: str
    etype: str = "asset"
    title: str = ""
    status: str = "nochange"   # change | nochange | skip | error | done | failed
    message: str = ""
    changes: list = dc_field(default_factory=list)
    original_xml: str | None = None
    new_xml: str | None = None
    creates_record: bool = False


def apply_field(root, record, field: Field, new_values: list, allow_remove: bool) -> list:
    """Change one field inside a parsed record so it holds `new_values`."""
    els = field_elements(root, field)
    current = [_display_text(e) for e in els]
    if current == new_values:
        return []
    last_preds = {a for a, _ in field.steps[-1][1]}
    changes, to_remove = [], []
    placed = {}                       # index in new_values -> element holding it
    matcher = difflib.SequenceMatcher(a=current, b=new_values, autojunk=False)
    opcodes = matcher.get_opcodes()

    removed_idx = set()
    for tag, i1, i2, j1, j2 in opcodes:
        if tag != "equal":
            pairs = min(i2 - i1, j2 - j1)
            removed_idx.update(range(i1 + pairs, i2))
    survivors = [e for i, e in enumerate(els) if i not in removed_idx or not allow_remove]

    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            for k in range(i2 - i1):
                placed[j1 + k] = els[i1 + k]
            continue
        pairs = min(i2 - i1, j2 - j1)
        for k in range(pairs):
            el, new = els[i1 + k], new_values[j1 + k]
            placed[j1 + k] = el
            if _is_nested(el):
                changes.append(Change(field.label, "skipped", current[i1 + k], new,
                                      "value is held in sub-elements; edit this one in Preservica"))
                continue
            extras = [f"{k2}={v}" for k2, v in el.attrib.items() if k2 not in last_preds]
            note = ("keeps attributes: " + "; ".join(extras)) if extras else ""
            el.text = new
            changes.append(Change(field.label, "edit", current[i1 + k], new, note))
        for k in range(i1 + pairs, i2):
            if allow_remove:
                to_remove.append(els[k])
                changes.append(Change(field.label, "remove", current[k], ""))
            else:
                changes.append(Change(field.label, "blocked", current[k], "",
                                      "removal not applied; tick 'Allow removing values' to permit it"))
        for k in range(j1 + pairs, j2):
            after = placed.get(k - 1)
            before = survivors[0] if (after is None and survivors) else None
            placed[k] = _add_value(record, field, new_values[k], after=after, before=before)
            changes.append(Change(field.label, "add", "", new_values[k]))
    for el in to_remove:
        _remove_element(record, el)
    return changes


def plan_record(plan: RecordPlan, field_values_pairs: list, allow_remove: bool) -> RecordPlan:
    """Work out the new XML for one record. `plan.original_xml` may be None (no MODS yet)."""
    if not field_values_pairs:
        return plan
    first = field_values_pairs[0][0]
    if plan.original_xml:
        root = parse_xml(plan.original_xml)
        record = find_record_root(root, first)
        if record is None:
            plan.status, plan.message = "error", "The metadata record has no <mods> element."
            return plan
    else:
        root = record = etree.Element(_q(first.namespace, first.steps[0][0]), nsmap={None: first.namespace})
        plan.creates_record = True
    for fld, values in field_values_pairs:
        plan.changes.extend(apply_field(root, record, fld, values, allow_remove))
    if any(c.effective for c in plan.changes):
        plan.status = "change"
        plan.new_xml = etree.tostring(root, encoding="unicode")
        if plan.creates_record:
            plan.message = "No MODS record yet: a new one will be created."
    else:
        plan.status = "nochange"
        plan.creates_record = False
    return plan


def split_cell(cell: str, sep: str) -> list:
    return [v.strip() for v in (cell or "").split(sep) if v.strip()]


# ----------------------------------------------------------------------------
# Talking to Preservica (through a pyPreservica EntityAPI client)
# ----------------------------------------------------------------------------

def _etype_name(entity) -> str:
    return "folder" if getattr(entity.entity_type, "name", "") == "FOLDER" else "asset"


def _fetch(client, etype: str, ref: str):
    return client.folder(ref) if etype == "folder" else client.asset(ref)


def _schema_count(entity, namespace) -> int:
    return list((entity.metadata or {}).values()).count(namespace)


class Cancelled(Exception):
    pass


def export_csv(client, folder_ref, fields, out_path, sep=DEFAULT_SEP, recursive=True,
               include_folders=False, progress=None, cancel=None) -> dict:
    """Write the chosen fields of every asset in a folder to a CSV."""
    namespace = fields[0].namespace
    walker = client.all_descendants(folder_ref) if recursive else client.descendants(folder_ref)
    stats = {"rows": 0, "no_mods": 0, "multiple": 0, "sep_clash": 0}
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(ID_COLS + [f.label for f in fields])
        for light in walker:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            etype = _etype_name(light)
            if getattr(light.entity_type, "name", "") not in ("ASSET", "FOLDER"):
                continue
            if etype == "folder" and not include_folders:
                continue
            entity = _fetch(client, etype, light.reference)
            count = _schema_count(entity, namespace)
            cells, status = [""] * len(fields), "ok"
            if count == 0:
                status = "no MODS record"
                stats["no_mods"] += 1
            else:
                if count > 1:
                    status = "multiple MODS records (will be skipped)"
                    stats["multiple"] += 1
                root = parse_xml(client.metadata_for_entity(entity, namespace))
                for i, fld in enumerate(fields):
                    values = [v for v in field_values(root, fld) if v]
                    if any(sep in v for v in values):
                        stats["sep_clash"] += 1
                    cells[i] = sep.join(values)
            writer.writerow([entity.reference, etype, entity.title or "", status] + cells)
            stats["rows"] += 1
            if progress:
                progress(f"Exported {stats['rows']} records...")
    return stats


def build_plans(client, csv_path, all_fields, sep=DEFAULT_SEP, allow_remove=False,
                progress=None, cancel=None):
    """Compare an edited CSV with what is in Preservica now. Nothing is written."""
    by_label = {f.label: f for f in all_fields}
    namespace = all_fields[0].namespace
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = [(h or "").strip().lstrip("﻿") for h in (reader.fieldnames or [])]
        reader.fieldnames = headers
        rows = list(reader)
    if "entity_ref" not in headers:
        raise ValueError("The CSV has no 'entity_ref' column. Use a CSV exported by this tool.")
    columns = [h for h in headers if h in by_label]
    ignored = [h for h in headers if h and h not in by_label and h not in ID_COLS]
    if not columns:
        raise ValueError("None of the CSV columns match a field in the template.")

    plans, seen = [], set()
    for n, row in enumerate(rows, start=2):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        ref = (row.get("entity_ref") or "").strip()
        etype = (row.get("entity_type") or "asset").strip().lower() or "asset"
        plan = RecordPlan(ref=ref, etype=etype, title=(row.get("title") or "").strip())
        plans.append(plan)
        if not ref:
            plan.status = "error"
            plan.message = (f"CSV row {n} has no entity_ref. If you split multi-valued cells in "
                            "OpenRefine, join them again before exporting.")
            continue
        if ref in seen:
            plan.status, plan.message = "error", f"CSV row {n}: this entity_ref appears more than once."
            continue
        seen.add(ref)
        try:
            entity = _fetch(client, etype, ref)
            plan.title = entity.title or plan.title
            count = _schema_count(entity, namespace)
            if count > 1:
                plan.status = "skip"
                plan.message = "Has more than one MODS record; edit this one in Preservica."
                continue
            plan.original_xml = client.metadata_for_entity(entity, namespace) if count else None
            pairs = [(by_label[c], split_cell(row.get(c), sep)) for c in columns]
            plan_record(plan, pairs, allow_remove)
        except Cancelled:
            raise
        except Exception as ex:  # one bad record should not stop the batch
            plan.status, plan.message = "error", f"{type(ex).__name__}: {ex}"
        if progress:
            progress(f"Checked {len(plans)} of {len(rows)} records...")
    return plans, ignored


def _norm(xml_text):
    return (xml_text or "").strip()


def apply_plans(client, plans, run_dir, namespace, progress=None, cancel=None) -> dict:
    """Write the planned changes to Preservica, saving a backup and a log."""
    run_dir = Path(run_dir)
    (run_dir / "backup").mkdir(parents=True, exist_ok=True)
    (run_dir / "after").mkdir(parents=True, exist_ok=True)
    todo = [p for p in plans if p.status == "change"]
    stats = {"updated": 0, "created": 0, "skipped": 0, "failed": 0}
    with open(run_dir / "changes_log.csv", "w", newline="", encoding="utf-8") as fh:
        log = csv.writer(fh)
        log.writerow(["time", "entity_ref", "title", "field", "action", "old_value", "new_value", "note", "result"])
        for i, plan in enumerate(todo, start=1):
            if cancel is not None and cancel.is_set():
                break
            result = ""
            try:
                entity = _fetch(client, plan.etype, plan.ref)
                count = _schema_count(entity, namespace)
                live = client.metadata_for_entity(entity, namespace) if count else None
                if count > 1 or _norm(live) != _norm(plan.original_xml):
                    plan.status = "skip"
                    plan.message = "Changed in Preservica since the preview; run the preview again."
                    stats["skipped"] += 1
                    result = "skipped: " + plan.message
                else:
                    if live:
                        (run_dir / "backup" / f"{plan.ref}.xml").write_text(live, encoding="utf-8")
                    if plan.creates_record:
                        client.add_metadata(entity, namespace, plan.new_xml)
                        stats["created"] += 1
                        result = "created"
                    else:
                        client.update_metadata(entity, namespace, plan.new_xml)
                        stats["updated"] += 1
                        result = "updated"
                    (run_dir / "after" / f"{plan.ref}.xml").write_text(plan.new_xml, encoding="utf-8")
                    plan.status = "done"
            except Exception as ex:
                plan.status, plan.message = "failed", f"{type(ex).__name__}: {ex}"
                stats["failed"] += 1
                result = "FAILED: " + plan.message
            stamp = datetime.now().isoformat(timespec="seconds")
            for c in plan.changes:
                log.writerow([stamp, plan.ref, plan.title, c.label, c.action, c.old, c.new, c.note,
                              result if c.effective else "not applied"])
            fh.flush()
            if progress:
                progress(f"Applied {i} of {len(todo)} records...")
    return stats


def summarise(plans) -> dict:
    out = {"change": 0, "nochange": 0, "skip": 0, "error": 0, "edit": 0, "add": 0, "remove": 0,
           "blocked": 0, "skipped": 0, "new_records": 0}
    for p in plans:
        if p.status in out:
            out[p.status] += 1
        if p.status == "change" and p.creates_record:
            out["new_records"] += 1
        for c in p.changes:
            out[c.action] = out.get(c.action, 0) + 1
    return out


# ----------------------------------------------------------------------------
# The window
# ----------------------------------------------------------------------------

def run_gui(client=None, template_path=None):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(APP_TITLE)
            self.geometry("1080x780")
            self.minsize(900, 640)
            self.client = client
            self.fields = []
            self.plans = []
            self.plan_csv = None
            self.busy = False
            self.cancel = threading.Event()
            self.q = queue.Queue()
            self.settings = self._load_settings()
            self._build()
            start = template_path or self.settings.get("template") or str(Path(__file__).with_name("MODS_template.csv"))
            if Path(start).exists():
                self._load_template(start)
            else:
                self.log("Choose your MODS template CSV to fill the field list.")
            if self.client is not None:
                self.conn_status.set("Connected")
            self.after(100, self._poll)

        # ---- settings -------------------------------------------------------
        def _load_settings(self):
            try:
                return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except Exception:
                return {}

        def _save_settings(self):
            data = {"server": self.server.get().strip(), "username": self.username.get().strip(),
                    "tenant": self.tenant.get().strip(), "template": self.template.get().strip(),
                    "folder": self.folder.get().strip()}
            try:  # the password is never saved
                SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except Exception:
                pass

        # ---- layout ---------------------------------------------------------
        def _build(self):
            pad = {"padx": 6, "pady": 3}
            s = self.settings

            # 1. connection
            box = ttk.LabelFrame(self, text="1. Connect to Preservica")
            box.pack(fill="x", padx=10, pady=(10, 4))
            self.server = tk.StringVar(value=s.get("server", ""))
            self.username = tk.StringVar(value=s.get("username", ""))
            self.password = tk.StringVar()
            self.tenant = tk.StringVar(value=s.get("tenant", ""))
            self.conn_status = tk.StringVar(value="Not connected")
            ttk.Label(box, text="Server").grid(row=0, column=0, sticky="e", **pad)
            ttk.Entry(box, textvariable=self.server, width=24).grid(row=0, column=1, **pad)
            ttk.Label(box, text="Username").grid(row=0, column=2, sticky="e", **pad)
            ttk.Entry(box, textvariable=self.username, width=22).grid(row=0, column=3, **pad)
            ttk.Label(box, text="Password").grid(row=0, column=4, sticky="e", **pad)
            ttk.Entry(box, textvariable=self.password, width=16, show="•").grid(row=0, column=5, **pad)
            ttk.Label(box, text="Tenant (optional)").grid(row=0, column=6, sticky="e", **pad)
            ttk.Entry(box, textvariable=self.tenant, width=8).grid(row=0, column=7, **pad)
            self.btn_connect = ttk.Button(box, text="Connect", command=self.on_connect)
            self.btn_connect.grid(row=0, column=8, **pad)
            ttk.Label(box, textvariable=self.conn_status, foreground="#555").grid(row=1, column=1, columnspan=8, sticky="w", padx=6, pady=(0, 4))

            # 2. export
            box = ttk.LabelFrame(self, text="2. Choose records and fields, then export a CSV")
            box.pack(fill="x", padx=10, pady=4)
            box.columnconfigure(1, weight=1)
            self.template = tk.StringVar()
            self.folder = tk.StringVar(value=s.get("folder", ""))
            self.recursive = tk.BooleanVar(value=True)
            self.include_folders = tk.BooleanVar(value=False)
            self.sep = tk.StringVar(value=DEFAULT_SEP)
            ttk.Label(box, text="Field template").grid(row=0, column=0, sticky="e", **pad)
            ttk.Entry(box, textvariable=self.template, state="readonly").grid(row=0, column=1, columnspan=3, sticky="ew", **pad)
            ttk.Button(box, text="Change...", command=self.on_pick_template).grid(row=0, column=4, sticky="w", **pad)
            ttk.Label(box, text="Folder reference").grid(row=1, column=0, sticky="e", **pad)
            ttk.Entry(box, textvariable=self.folder).grid(row=1, column=1, sticky="ew", **pad)
            ttk.Checkbutton(box, text="Include subfolders", variable=self.recursive).grid(row=1, column=2, sticky="w", **pad)
            ttk.Checkbutton(box, text="Include folder records", variable=self.include_folders).grid(row=1, column=3, sticky="w", **pad)
            sepbox = ttk.Frame(box)
            sepbox.grid(row=1, column=4, sticky="w", **pad)
            ttk.Label(sepbox, text="Value separator").pack(side="left")
            ttk.Entry(sepbox, textvariable=self.sep, width=3).pack(side="left", padx=4)

            ttk.Label(box, text="Field to edit").grid(row=2, column=0, sticky="e", **pad)
            row = ttk.Frame(box)
            row.grid(row=2, column=1, columnspan=4, sticky="ew", **pad)
            self.field_choice = tk.StringVar()
            self.field_combo = ttk.Combobox(row, textvariable=self.field_choice, state="readonly", width=38, height=20)
            self.field_combo.pack(side="left")
            ttk.Button(row, text="Add field", command=self.on_add_field).pack(side="left", padx=6)
            ttk.Button(row, text="Add all", command=self.on_add_all).pack(side="left")

            ttk.Label(box, text="Selected fields").grid(row=3, column=0, sticky="ne", **pad)
            row = ttk.Frame(box)
            row.grid(row=3, column=1, columnspan=4, sticky="ew", **pad)
            self.chosen = tk.Listbox(row, height=5, selectmode="extended", exportselection=False)
            self.chosen.pack(side="left", fill="x", expand=True)
            sb = ttk.Scrollbar(row, orient="vertical", command=self.chosen.yview)
            sb.pack(side="left", fill="y")
            self.chosen.configure(yscrollcommand=sb.set)
            col = ttk.Frame(row)
            col.pack(side="left", padx=8)
            ttk.Button(col, text="Remove", command=self.on_remove_field).pack(fill="x", pady=1)
            ttk.Button(col, text="Clear", command=lambda: self.chosen.delete(0, "end")).pack(fill="x", pady=1)
            self.btn_export = ttk.Button(col, text="Export CSV...", command=self.on_export)
            self.btn_export.pack(fill="x", pady=(8, 1))

            # 3. import
            box = ttk.LabelFrame(self, text="3. Load the edited CSV, preview, then apply")
            box.pack(fill="both", expand=True, padx=10, pady=4)
            row = ttk.Frame(box)
            row.pack(fill="x", padx=6, pady=4)
            self.allow_remove = tk.BooleanVar(value=False)
            self.btn_preview = ttk.Button(row, text="Load edited CSV and preview...", command=self.on_preview)
            self.btn_preview.pack(side="left")
            ttk.Checkbutton(row, text="Allow removing values", variable=self.allow_remove).pack(side="left", padx=12)
            self.btn_apply = ttk.Button(row, text="Apply changes to Preservica", command=self.on_apply, state="disabled")
            self.btn_apply.pack(side="right")
            self.btn_stop = ttk.Button(row, text="Stop", command=self.cancel.set, state="disabled")
            self.btn_stop.pack(side="right", padx=6)
            self.summary = tk.StringVar(value="No preview yet. Nothing is written until you press Apply.")
            ttk.Label(box, textvariable=self.summary, foreground="#333").pack(fill="x", padx=8)

            wrap = ttk.Frame(box)
            wrap.pack(fill="both", expand=True, padx=6, pady=4)
            cols = ("title", "field", "action", "old", "new", "note")
            self.tree = ttk.Treeview(wrap, columns=cols, show="headings", height=9)
            for cid, text, width in (("title", "Record", 190), ("field", "Field", 150), ("action", "Action", 70),
                                     ("old", "Current value", 230), ("new", "New value", 230), ("note", "Note", 520)):
                self.tree.heading(cid, text=text)
                self.tree.column(cid, width=width, anchor="w", stretch=False)
            ysb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
            xsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
            self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
            wrap.rowconfigure(0, weight=1)
            wrap.columnconfigure(0, weight=1)
            self.tree.grid(row=0, column=0, sticky="nsew")
            ysb.grid(row=0, column=1, sticky="ns")
            xsb.grid(row=1, column=0, sticky="ew")
            self.tree.tag_configure("add", foreground="#1a6b2d")
            self.tree.tag_configure("remove", foreground="#a11")
            self.tree.tag_configure("problem", foreground="#a15c00")

            # log
            box = ttk.LabelFrame(self, text="Activity")
            box.pack(fill="x", padx=10, pady=(4, 10))
            self.logbox = tk.Text(box, height=5, wrap="word", state="disabled")
            self.logbox.pack(fill="x", padx=6, pady=4)
            self.status = tk.StringVar(value="Ready")
            ttk.Label(box, textvariable=self.status, foreground="#555").pack(fill="x", padx=8, pady=(0, 4))

        # ---- helpers --------------------------------------------------------
        def log(self, text):
            self.logbox.configure(state="normal")
            self.logbox.insert("end", f"{datetime.now():%H:%M:%S}  {text}\n")
            self.logbox.see("end")
            self.logbox.configure(state="disabled")

        def _set_busy(self, busy, status=None):
            self.busy = busy
            state = "disabled" if busy else "normal"
            for b in (self.btn_connect, self.btn_export, self.btn_preview):
                b.configure(state=state)
            self.btn_stop.configure(state="normal" if busy else "disabled")
            can_apply = (not busy) and any(p.status == "change" for p in self.plans)
            self.btn_apply.configure(state="normal" if can_apply else "disabled")
            self.status.set(status or ("Working..." if busy else "Ready"))

        def _run(self, label, work, done):
            if self.busy:
                return
            self.cancel.clear()
            self._set_busy(True, label)

            def target():
                try:
                    self.q.put(("done", done, work()))
                except Cancelled:
                    self.q.put(("fail", "Stopped.", ""))
                except Exception as ex:
                    self.q.put(("fail", f"{type(ex).__name__}: {ex}", traceback.format_exc()))

            threading.Thread(target=target, daemon=True).start()

        def _progress(self, text):
            self.q.put(("progress", text, None))

        def _poll(self):
            try:
                while True:
                    kind, a, b = self.q.get_nowait()
                    if kind == "progress":
                        self.status.set(a)
                    elif kind == "done":
                        self._set_busy(False)
                        a(b)
                    elif kind == "fail":
                        self._set_busy(False)
                        self.log(a)
                        if a != "Stopped.":
                            messagebox.showerror(APP_TITLE, a)
            except queue.Empty:
                pass
            self.after(100, self._poll)

        def _need_client(self):
            if self.client is None:
                messagebox.showinfo(APP_TITLE, "Connect to Preservica first.")
                return False
            return True

        def _load_template(self, path):
            try:
                self.fields = load_template(path)
            except Exception as ex:
                messagebox.showerror(APP_TITLE, f"Could not read the template:\n{ex}")
                return
            self.template.set(str(path))
            self.field_combo.configure(values=[f.label for f in self.fields])
            if self.fields:
                self.field_combo.current(0)
            self.chosen.delete(0, "end")
            self.log(f"Loaded {len(self.fields)} fields from {Path(path).name}.")

        def _separator(self):
            return self.sep.get() or DEFAULT_SEP

        # ---- actions --------------------------------------------------------
        def on_connect(self):
            server, user = self.server.get().strip(), self.username.get().strip()
            pw, tenant = self.password.get(), self.tenant.get().strip()
            server = re.sub(r"^https?://", "", server).strip("/")

            def work():
                from pyPreservica import EntityAPI
                if user:
                    return EntityAPI(username=user, password=pw, tenant=tenant or None, server=server)
                return EntityAPI()  # falls back to credentials.properties / environment variables

            def done(api):
                self.client = api
                self.conn_status.set("Connected")
                self.log(f"Connected to {server or 'Preservica'}.")
                self._save_settings()

            self._run("Connecting...", work, done)

        def on_pick_template(self):
            path = filedialog.askopenfilename(title="Choose the MODS template CSV",
                                              filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
            if path:
                self._load_template(path)
                self._save_settings()

        def on_add_field(self):
            label = self.field_choice.get()
            if label and label not in self.chosen.get(0, "end"):
                self.chosen.insert("end", label)

        def on_add_all(self):
            self.chosen.delete(0, "end")
            for f in self.fields:
                self.chosen.insert("end", f.label)

        def on_remove_field(self):
            for i in reversed(self.chosen.curselection()):
                self.chosen.delete(i)

        def on_export(self):
            if not self._need_client():
                return
            found = UUID_RE.findall(self.folder.get())
            if not found:
                messagebox.showinfo(APP_TITLE, "Paste the folder's reference (its UUID) or its Preservica URL.")
                return
            folder_ref = found[-1]
            labels = list(self.chosen.get(0, "end"))
            if not labels:
                messagebox.showinfo(APP_TITLE, "Add at least one field to edit.")
                return
            fields = [f for f in self.fields if f.label in labels]
            out = filedialog.asksaveasfilename(title="Save export as", defaultextension=".csv",
                                               initialfile=f"mods_export_{datetime.now():%Y%m%d_%H%M}.csv",
                                               filetypes=[("CSV files", "*.csv")])
            if not out:
                return
            sep, rec, inc = self._separator(), self.recursive.get(), self.include_folders.get()
            self._save_settings()

            def work():
                folder = self.client.folder(folder_ref)
                stats = export_csv(self.client, folder_ref, fields, out, sep, rec, inc, self._progress, self.cancel)
                return folder.title, stats

            def done(res):
                title, st = res
                self.log(f"Exported {st['rows']} records from '{title}' to {out}")
                if st["no_mods"]:
                    self.log(f"  {st['no_mods']} records have no MODS record yet (cells left blank).")
                if st["multiple"]:
                    self.log(f"  {st['multiple']} records have more than one MODS record and will be skipped on apply.")
                if st["sep_clash"]:
                    self.log(f"  WARNING: {st['sep_clash']} cells contain the separator '{sep}' inside a value. "
                             "Choose a different separator and export again.")

            self._run("Exporting...", work, done)

        def on_preview(self):
            if not self._need_client():
                return
            path = filedialog.askopenfilename(title="Choose the edited CSV",
                                              filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
            if not path:
                return
            sep, allow = self._separator(), self.allow_remove.get()

            def work():
                return build_plans(self.client, path, self.fields, sep, allow, self._progress, self.cancel)

            def done(res):
                self.plans, ignored = res
                self.plan_csv = path
                if ignored:
                    self.log("Columns ignored (not in the template): " + ", ".join(ignored))
                self._show_plans()
                self._set_busy(False)

            self._run("Comparing with Preservica...", work, done)

        def _show_plans(self):
            self.tree.delete(*self.tree.get_children())
            shown = 0
            for p in self.plans:
                if p.status in ("error", "skip", "failed"):
                    self.tree.insert("", "end", values=(p.title or p.ref, "", p.status, "", "", p.message), tags=("problem",))
                    shown += 1
                for c in p.changes:
                    if shown >= MAX_PREVIEW_ROWS:
                        break
                    tag = c.action if c.action in ("add", "remove") else ("problem" if not c.effective else "")
                    self.tree.insert("", "end", values=(p.title or p.ref, c.label, c.action, c.old, c.new, c.note),
                                     tags=(tag,) if tag else ())
                    shown += 1
            s = summarise(self.plans)
            text = (f"{s['change']} records will change ({s['edit']} edits, {s['add']} additions, "
                    f"{s['remove']} removals) | {s['nochange']} unchanged | "
                    f"{s['skip'] + s['error']} skipped or with problems")
            if s["new_records"]:
                text += f" | new MODS records: {s['new_records']}"
            if s["blocked"]:
                text += f" | removals blocked: {s['blocked']}"
            if shown >= MAX_PREVIEW_ROWS:
                text += f" | showing the first {MAX_PREVIEW_ROWS} rows"
            self.summary.set(text)
            self.log("Preview ready. " + text)

        def on_apply(self):
            if not self._need_client():
                return
            s = summarise(self.plans)
            if not s["change"]:
                return
            if not messagebox.askyesno(
                    APP_TITLE,
                    f"Write changes to {s['change']} records in Preservica?\n\n"
                    f"{s['edit']} edits, {s['add']} additions, {s['remove']} removals.\n\n"
                    "The original XML of every record is saved first.", icon="warning"):
                return
            run_dir = Path(self.plan_csv).parent / "mods_editor_runs" / f"{datetime.now():%Y%m%d_%H%M%S}"
            namespace = self.fields[0].namespace

            def work():
                return apply_plans(self.client, self.plans, run_dir, namespace, self._progress, self.cancel)

            def done(st):
                self.log(f"Finished: {st['updated']} updated, {st['created']} created, "
                         f"{st['skipped']} skipped, {st['failed']} failed.")
                self.log(f"Backups and log saved in {run_dir}")
                self._show_plans()
                self._set_busy(False)
                if st["failed"] or st["skipped"]:
                    messagebox.showwarning(APP_TITLE, "Some records were not updated. See the list and the log file.")

            self._run("Writing to Preservica...", work, done)

    app = App()
    return app


if __name__ == "__main__":
    run_gui().mainloop()
