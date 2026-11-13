import csv, subprocess, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, str(Path(__file__).parent))
import preservica_mods_editor as m
from fake import FakeClient, uid
from tkinter import filedialog, messagebox

m.SETTINGS_FILE = Path(tempfile.mkdtemp()) / "settings.json"
tmp = Path(tempfile.mkdtemp()); shots = Path(sys.argv[2])
errors = []
messagebox.showerror = lambda *a, **k: errors.append(a)
messagebox.showinfo = lambda *a, **k: errors.append(a)
messagebox.showwarning = lambda *a, **k: None
messagebox.askyesno = lambda *a, **k: True

c = FakeClient()
app = m.run_gui(client=c)

def pump(until_idle=True, secs=0.4):
    end = time.time() + secs
    while time.time() < end or (until_idle and app.busy):
        app.update(); time.sleep(0.02)

def shot(name):
    pump(False, 0.5)
    subprocess.run(["import", "-window", "root", str(shots / name)], check=True)

pump(False)
assert len(app.field_combo["values"]) == 31
app.folder.set(f"https://example.preservica.com/explorer/explorer.html#properties:SO&{uid(10)}")
for label in ("Title", "Subject (Topical)", "Name", "Role", "Date Created"):
    app.field_choice.set(label); app.on_add_field()
app.field_choice.set("Title"); app.on_add_field()            # duplicate ignored
assert app.chosen.size() == 5
out = tmp / "export.csv"
filedialog.asksaveasfilename = lambda **k: str(out)
app.on_export(); pump()
rows = list(csv.DictReader(open(out, encoding="utf-8")))
assert len(rows) == 4 and list(rows[0])[4:] == ["Title", "Name", "Role", "Subject (Topical)", "Date Created"], list(rows[0])
shot("1_after_export.png")

rows[0]["Subject (Topical)"] = "Student movements|Demonstrations|College students|Civil rights"
rows[0]["Title"] = "Campus protest on the Main Quad"
rows[1]["Subject (Topical)"] = ""
rows[2]["Title"] = "Untitled scan"; rows[2]["Name"] = "Lee, Ann"; rows[2]["Role"] = "creator"
with open(tmp / "edited.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
filedialog.askopenfilename = lambda **k: str(tmp / "edited.csv")
app.on_preview(); pump()
assert str(app.btn_apply["state"]) == "normal"
print("summary:", app.summary.get())
shot("2_preview.png")
app.on_apply(); pump()
print("calls:", c.calls)
assert str(app.btn_apply["state"]) == "disabled" and len(c.calls) == 2
runs = list((tmp / "mods_editor_runs").iterdir())
assert (runs[0] / "changes_log.csv").exists() and len(list((runs[0] / "backup").iterdir())) == 1
shot("3_after_apply.png")
assert not errors, errors
# not-connected guard
app.client = None; app.on_export(); assert errors
print("GUI TEST PASSED")
app.destroy()
