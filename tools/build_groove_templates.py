"""Build RICARDO FEEL groove-template MIDI clips from his timing/velocity DNA.

One .mid per (role × 1/2 bars) under store/exports/grooves/. One-time manual step
turns them into native Live grooves (instructions printed). Undo = delete the folder.

Run: .venv/bin/python tools/build_groove_templates.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
import core  # noqa: E402


def main():
    agg = core._aggregate()
    roles = sorted(set(agg.get("velocity_by_role", {})) & set(agg.get("timing_by_role", {})))
    if not roles:
        print("no DNA in style_profile.json — run analyze/style_profile.py first")
        return
    built = []
    for role in roles:
        for bars in (1, 2):
            notes = core.build_groove_template_notes(role=role, bars=bars, seed=0)
            name = f"grooves/RICARDO FEEL {role.upper()} {bars}BAR.mid"
            res = core.export_midi(notes, name, tempo=120.0)
            tb = agg["timing_by_role"][role]
            built.append((name, len(notes), tb["offgrid_frac"]))
            print(f"  {os.path.basename(res['path']):36s} {len(notes):3d} notes  "
                  f"(offgrid {tb['offgrid_frac']:.0%}, dev_std {tb['dev_std_beats']:.3f})")
    print(f"\n{len(built)} templates -> store/exports/grooves/")
    print("\nONE-TIME manual step (API cannot fill the groove pool):")
    print("  1. Drag each .mid into any Live track (they're one clip each).")
    print("  2. Right-click the clip -> Extract Groove(s). The groove lands in the pool")
    print("     named after the clip (RICARDO FEEL LEAD 1BAR etc.).")
    print("  3. Save your default Set template so the pool persists in new projects.")
    print("Then any clip gets your feel from the Groove chooser — sidekick or not.")


if __name__ == "__main__":
    main()
