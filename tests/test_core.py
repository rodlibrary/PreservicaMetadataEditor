import csv, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, str(Path(__file__).parent))
import preservica_mods_editor as m
from fake import FakeClient, uid, NS
from lxml import etree

APP = Path(sys.argv[1])
fields = m.load_template(APP / "MODS_template.csv")
F = {f.label: f for f in fields}
assert len(fields) == 31, len(fields)
print("template fields:", len(fields))
X = lambda xml, p: etree.fromstring(xml.encode()).xpath(p, namespaces={"ns": NS})

tmp = Path(tempfile.mkdtemp())
c = FakeClient()

# ---- export ---------------------------------------------------------------
out = tmp / "export.csv"
st = m.export_csv(c, uid(10), fields, out)
rows = list(csv.DictReader(open(out, encoding="utf-8")))
print("export stats:", st)
assert st == {"rows": 4, "no_mods": 1, "multiple": 1, "sep_clash": 0}
r1 = rows[0]
assert r1["Subject (Topical)"] == "Student movments|Demonstrations|College students"
assert r1["Name"] == "Smith, Jon|University News Bureau" and r1["Role"] == "photographer"
assert r1["Repository"] == "University Archives" and r1["Call Number"] == "RS 39/2/20"
assert r1["Note"] == "Café scene on verso"
assert rows[1]["Title"] == "Commencement" and rows[1]["Subject (Name)"] == "Doe, Jane"
assert rows[1]["Collection"] == "Photo Collection"
assert rows[2]["mods_status"] == "no MODS record"
st2 = m.export_csv(c, uid(10), fields, tmp / "flat.csv", recursive=False, include_folders=True)
assert st2["rows"] == 3      # 2 assets + the subfolder record
print("export OK")

# ---- untouched round trip => no changes -----------------------------------
plans, ignored = m.build_plans(c, out, fields)
s = m.summarise(plans)
assert s["change"] == 0 and s["nochange"] == 3 and s["skip"] == 1, s
print("round trip with no edits: no changes  OK")

# ---- edits ----------------------------------------------------------------
def write(path, rows, extra_cols=()):
    cols = list(rows[0].keys()) + list(extra_cols)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:   # BOM, as Excel would save
        w = csv.DictWriter(fh, fieldnames=cols); w.writeheader(); w.writerows(rows)

ed = [dict(r) for r in rows]
a1, a2, a3, a4 = ed
a1["Subject (Topical)"] = "Demonstrations|College students|Student movements|Civil rights"  # drop 1st, add 2
a1["Role"] = "photographer|publisher"                 # second role -> the name without a role
a1["Date Created"] = "1969-05-01|1969-05-02"          # reuse originInfo
a1["Archival Resource Key (ARK)"] = "ark:/12345/abc"  # new typed identifier
a1["Title"] = "Campus protest on the Main Quad"       # plain edit
a1["Genre"] = "Photographs"                           # edit, keeps authority attr
a1["URL (Finding Aid)"] = "https://example.edu/fa/39-2-20"
a2["Subject (Name)"] = "Doe, Jane A."                 # nested -> must be skipped, not clobbered
a2["Collection"] = "University Photograph Collection"
a2["URL (Finding Aid)"] = "https://example.edu/fa/photo"   # goes into the host relatedItem
a3["Title"] = "Untitled scan"; a3["Subject (Topical)"] = "Buildings|Campus"; a3["Name"] = "Lee, Ann"; a3["Role"] = "creator"
a4["Title"] = "should be skipped"
ed.append({**{k: "" for k in a1}, "Title": "orphan row"})                       # no ref
ed.append({**a2, "Title": "duplicate"})                                         # duplicate ref
for r in ed: r["OpenRefine note"] = "x"
write(tmp / "edited.csv", ed)

plans, ignored = m.build_plans(c, tmp / "edited.csv", fields, allow_remove=False)
assert ignored == ["OpenRefine note"]
p1, p2, p3, p4, p5, p6 = plans
for p in plans:
    print(f"\n[{p.status}] {p.title} {p.message}")
    for ch in p.changes: print(f"    {ch.action:8} {ch.label:28} {ch.old!r} -> {ch.new!r} {('('+ch.note+')') if ch.note else ''}")
assert p4.status == "skip" and p5.status == "error" and p6.status == "error"
assert [c_.action for c_ in p1.changes if c_.label == "Subject (Topical)"].count("blocked") == 1
# with removal blocked the misspelt subject is still there
assert "Student movments" in X(p1.new_xml, "//ns:subject/ns:topic/text()")

plans, _ = m.build_plans(c, tmp / "edited.csv", fields, allow_remove=True)
p1, p2, p3 = plans[:3]
topics = X(p1.new_xml, "//ns:subject/ns:topic")
assert [t.text for t in topics] == ["Demonstrations", "College students", "Student movements", "Civil rights"], [t.text for t in topics]
# surviving subjects keep their OWN URIs (no shifting), new ones have none
assert topics[0].get("valueURI").endswith("sh85034149") and topics[1].get("valueURI").endswith("sh85026282")
assert topics[2].get("valueURI") is None and topics[2].getparent().get("authority") is None
assert len(X(p1.new_xml, "//ns:subject")) == 5                       # 4 topical + 1 geographic, empty wrapper pruned
names = X(p1.new_xml, "//ns:mods/ns:name")
assert len(names) == 2 and X(p1.new_xml, "//ns:name[2]/ns:role/ns:roleTerm/text()") == ["publisher"]
assert X(p1.new_xml, "//ns:name[1]/ns:role/ns:roleTerm/@authority") == ["marcrelator"]
assert len(X(p1.new_xml, "//ns:originInfo")) == 1 and X(p1.new_xml, "//ns:originInfo/ns:dateCreated/text()") == ["1969-05-01", "1969-05-02"]
assert X(p1.new_xml, "//ns:identifier[@type='ark']/text()") == ["ark:/12345/abc"]
assert X(p1.new_xml, "//ns:genre/@authority") == ["aat"] and X(p1.new_xml, "//ns:genre/text()") == ["Photographs"]
assert X(p1.new_xml, "//ns:relatedItem/ns:location/ns:url/text()") == ["https://example.edu/fa/39-2-20"]
# untouched parts are intact
assert X(p1.new_xml, "//ns:abstract/text()") == ["Students gather; speakers address the crowd."]
assert X(p1.new_xml, "//ns:mods/@version") == ["3.7"] and "xlink" in p1.new_xml
assert X(p1.new_xml, "//ns:dateCreated[1]/@keyDate") == ["yes"]
# A2: nested subject name untouched; URL lands in the existing host relatedItem
assert X(p2.new_xml, "//ns:subject/ns:name/ns:namePart/text()") == ["Doe, Jane"]
assert any(ch.action == "skipped" for ch in p2.changes)
assert len(X(p2.new_xml, "//ns:relatedItem")) == 1
assert X(p2.new_xml, "//ns:relatedItem[@type='host']/ns:location/ns:url/text()") == ["https://example.edu/fa/photo"]
assert X(p2.new_xml, "//ns:relatedItem/ns:titleInfo/ns:title/text()") == ["University Photograph Collection"]
assert p2.new_xml.startswith("<mods:mods")                            # prefix style preserved
# A3: brand-new record
assert p3.creates_record and X(p3.new_xml, "/ns:mods/ns:name/ns:role/ns:roleTerm/text()") == ["creator"]
assert len(X(p3.new_xml, "//ns:subject")) == 2 and len(X(p3.new_xml, "//ns:name")) == 1
print("\nplanning OK")

# ---- apply ----------------------------------------------------------------
before_a1 = c.store[uid(1)].docs[0]
run = tmp / "run1"
st = m.apply_plans(c, plans, run, NS)
print("apply stats:", st, c.calls)
assert st == {"updated": 2, "created": 1, "skipped": 0, "failed": 0}
assert (run / "backup" / f"{uid(1)}.xml").read_text(encoding="utf-8") == before_a1
assert not (run / "backup" / f"{uid(3)}.xml").exists()
log = list(csv.DictReader(open(run / "changes_log.csv", encoding="utf-8")))
assert any(r["result"] == "created" for r in log) and any(r["result"] == "not applied" for r in log)
assert len(c.store[uid(4)].docs) == 2 and "should be skipped" not in c.store[uid(4)].docs[0]
# what Preservica now holds, re-exported, matches the edited CSV
m.export_csv(c, uid(10), fields, tmp / "after.csv")
after = list(csv.DictReader(open(tmp / "after.csv", encoding="utf-8")))
for col in ("Subject (Topical)", "Role", "Date Created", "Title", "Genre", "Archival Resource Key (ARK)", "Note", "Abstract"):
    assert after[0][col] == a1[col], (col, after[0][col], a1[col])
assert after[2]["Title"] == "Untitled scan" and after[2]["mods_status"] == "ok"
# running the same CSV again is a no-op apart from the deliberately skipped nested value
plans2, _ = m.build_plans(c, tmp / "edited.csv", fields, allow_remove=True)
assert m.summarise(plans2)["change"] == 0, m.summarise(plans2)
print("apply OK, second run is a no-op")

# ---- stale preview is refused ---------------------------------------------
a1["Title"] = "Another title"; write(tmp / "edited2.csv", [a1])
plans3, _ = m.build_plans(c, tmp / "edited2.csv", fields)
c.store[uid(1)].docs[0] = c.store[uid(1)].docs[0].replace("Illinois", "Illinois (State)")   # someone else edits
st = m.apply_plans(c, plans3, tmp / "run2", NS)
assert st["skipped"] == 1 and st["updated"] == 0 and "Another title" not in c.store[uid(1)].docs[0]
print("stale preview refused OK")

# ---- separator clash warning ----------------------------------------------
c.store[uid(2)].docs[0] = c.store[uid(2)].docs[0].replace("Commencement ceremonies", "A|B")
assert m.export_csv(c, uid(10), [F["Subject (Topical)"]], tmp / "clash.csv")["sep_clash"] == 1
print("ALL CORE TESTS PASSED")
