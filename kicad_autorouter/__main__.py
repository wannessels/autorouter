"""Command line: route a .kicad_pcb file.

    python -m kicad_autorouter board.kicad_pcb -o routed.kicad_pcb
    python -m kicad_autorouter board.kicad_pcb --nets SDA,SCL --gif thinking.gif
    python -m kicad_autorouter board.kicad_pcb --live          # watch it in a window
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import time

from .kicad_pcb import PcbFile
from .model import RouteSettings
from .problem import build_problem
from .router import STALL_PASSES, Router


def _board_net(name: str, names) -> str:
    """The board's net for a name given without its sheet path (GND for /GND), when only
    one net fits."""
    if name in names:
        return name
    hits = sorted(n for n in names if n.rsplit("/", 1)[-1] == name)
    if len(hits) == 1:
        print(f"Net {name}: using {hits[0]}")
        return hits[0]
    if hits:
        print(f"Net {name} is not on the board; did you mean {', '.join(hits)}?")
    return name


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kicad_autorouter", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", help="input .kicad_pcb")
    ap.add_argument("-o", "--output", help="output .kicad_pcb (default: <board>-routed.kicad_pcb)")
    ap.add_argument("--nets", help="comma-separated nets to route (default: all)")
    ap.add_argument("--include-zone-nets", action="store_true",
                    help="also route nets that have a copper pour (GND etc.)")
    ap.add_argument("--straighten-only", action="store_true",
                    help="do not route: clean up the tracks already on the board (all nets, or --nets)")
    ap.add_argument("--keep-existing", action="store_true",
                    help="straighten only the new routes: leave the copper already on the board exactly as it is")
    ap.add_argument("--pin-gaps", action="store_true",
                    help="experimental: route between neighbouring IC pins where a track just fits")
    ap.add_argument("--tracks-on-planes", action="store_true",
                    help="also route signals on inner layers covered by pours (default: keep planes solid)")
    ap.add_argument("--track-width", type=float)
    ap.add_argument("--clearance", type=float)
    ap.add_argument("--via-diameter", type=float)
    ap.add_argument("--via-drill", type=float)
    ap.add_argument("--grid", type=float, help="grid pitch in mm (default: width + clearance)")
    ap.add_argument("--layers", help="comma-separated copper layers to use (default: all)")
    ap.add_argument("--rip-up", action="store_true",
                    help="remove the existing tracks and vias of the nets to route (--nets, or all) "
                         "and route them again; locked copper stays")
    ap.add_argument("--passes", type=int,
                    help="maximum passes (default 40); a run also stops early after --patience passes "
                         "without progress")
    ap.add_argument("--patience", type=int,
                    help=f"passes without a new low in conflicts before the remaining nets are dropped "
                         f"(default {STALL_PASSES}; a fine grid converges less steadily: try 30)")
    ap.add_argument("--via-cost", type=float)
    ap.add_argument("--no-global", action="store_true",
                    help="skip the coarse global-routing stage (slower on big boards)")
    ap.add_argument("--workers", type=int,
                    help="CPU cores for the first pass (default: automatic; 1 = single core)")
    ap.add_argument("--region-cells", type=int, help="global-routing region size in grid cells (default 6)")
    ap.add_argument("--gif", help="write an animation of the router thinking")
    ap.add_argument("--png", help="write a picture of the final state")
    ap.add_argument("--live", action="store_true", help="show a live matplotlib window")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)          # progress shows up in a log file as it comes

    pcb = PcbFile(a.board)
    board = pcb.board()
    s = pcb.settings_from_project(RouteSettings())
    for k in ("track_width", "clearance", "via_diameter", "via_drill", "grid", "via_cost"):
        v = getattr(a, k)
        if v is not None:
            setattr(s, k, v)
    if a.passes:
        s.max_passes = a.passes
    if a.patience:
        s.stall_passes = a.patience
    if a.no_global:
        s.global_routing = False
    if a.region_cells:
        s.region_cells = a.region_cells
    if a.workers:
        s.workers = a.workers
    if a.layers:
        s.layers = [x.strip() for x in a.layers.split(",")]
    s.skip_zone_nets = not a.include_zone_nets
    s.tracks_on_planes = a.tracks_on_planes
    s.pin_gaps = a.pin_gaps
    pro = os.path.splitext(a.board)[0] + ".kicad_pro"
    if not os.path.exists(pro):
        print(f"Warning: no {os.path.basename(pro)} next to the board, so no net classes: every net gets "
              f"{s.track_width} mm tracks with {s.clearance} mm clearance")
    names = set(board.nets()) | {t.net for t in board.tracks if t.net}
    nets = [_board_net(x.strip(), names) for x in a.nets.split(",")] if a.nets else None

    if a.straighten_only:
        from .restraighten import detect_grid, straighten_existing
        from .router import NetRoute, RouteResult
        s = detect_grid(board, s)
        t0 = time.time()
        res, replaced, fixed = straighten_existing(board, s, nets)
        touched = set(res.nets)
        # the pieces that were redrawn go; everything kept stays in the file as it is
        gone = {id(x) for x in replaced}
        keep_t = [t for t in board.tracks if t.net in touched and id(t) not in gone]
        pcb.remove_items([x for x in replaced if hasattr(x, "x1")], [x for x in replaced if not hasattr(x, "x1")])
        n = pcb.add_routes(res, s)
        out = a.output or a.board.replace(".kicad_pcb", "-straightened.kicad_pcb")
        pcb.save(out)
        old_n = sum(1 for x in replaced if hasattr(x, "x1"))
        print(f"Straightened {len(touched)} nets on a {s.pitch():.3f} mm grid: {old_n} track segments -> "
              f"{sum(len(nr.segments) for nr in res.nets.values())} ({len(keep_t)} off-grid pieces kept) "
              f"in {time.time() - t0:.1f}s; wrote {out}")
        return 0

    from .problem import add_bars_to_result, same_net_bars
    ripped = []
    if a.rip_up:
        # (pour nets the router leaves to their pour keep their copper: nothing would replace it)
        sel = {n for n in (nets or board.nets()) if not (s.skip_zone_nets and n in board.zone_nets)}
        ripped = [x for x in list(board.tracks) + list(board.vias) if x.net in sel and not x.locked]
        gone = {id(x) for x in ripped}
        board = dataclasses.replace(board, tracks=[t for t in board.tracks if id(t) not in gone],
                                    vias=[v for v in board.vias if id(v) not in gone])
        print(f"Rip-up: {len(ripped)} existing track segments and vias of {len(sel)} nets removed")
    bars = same_net_bars(board, s, nets)          # fine-pitch same-net neighbours
    board_plain = board
    if bars:
        board = dataclasses.replace(board, tracks=list(board.tracks) + bars)
    t0 = time.time()
    pb = build_problem(board, s, nets)
    g = pb.grid
    if not a.quiet:
        print(f"Grid {g.nx} x {g.ny} x {g.nl} layers at {g.pitch:.3f} mm; "
              f"{len(pb.nets)} nets to route ({time.time() - t0:.2f}s to build)")

    viz = None
    if a.gif or a.png or a.live:
        if not a.live:
            import matplotlib
            matplotlib.use("Agg")
        from .viz import Visualizer
        viz = Visualizer(board, pb, live=a.live)

    def cb(ev, d):
        if viz is not None:
            viz.callback(ev, d)
        if not a.quiet and ev == "engine":
            print("  search core: compiled" if d["native"] else
                  "  search core: Python (no compiled core for this computer - slower)")
        if not a.quiet and ev == "global_done":
            print(f"  global plan: {d['regions']} regions, {d['passes']} pass(es), "
                  f"{d['overflow']} over-full boundaries ({d['seconds']:.2f}s)")
        if not a.quiet and ev == "pass_end":
            print(f"  pass {d['pass']:2d}: {d['overused']:4d} overused cells, "
                  f"{len(d['conflict_nets']):3d} nets in conflict, {d.get('pour_cuts', 0)} pour cuts, "
                  f"{d['length_mm']:8.1f} mm, {d['vias']:3d} vias  ({d['seconds']:.2f}s)")
        return True

    router = Router(pb, cb, record_exploration=viz is not None)
    router.cleanup_in_run = False             # straightened below (the last step, always)
    if viz is not None:
        viz.attach(router)
    res = router.run()
    add_bars_to_result(res, bars)
    board = board_plain
    from .restraighten import straighten_result
    from .router import NetRoute, RouteResult
    res, old_items = straighten_result(board, res, s, keep_existing=a.keep_existing)
    print(res.summary())

    out = a.output or a.board.replace(".kicad_pcb", "-routed.kicad_pcb")
    old_items = list(old_items or ()) + ripped
    if old_items:
        # existing copper that was re-shaped too goes; what was kept stays in the file as it is
        pcb.remove_items([x for x in old_items if hasattr(x, "x1")], [x for x in old_items if not hasattr(x, "x1")])
    n = pcb.add_routes(res, s)
    pcb.save(out)
    print(f"Wrote {n} items to {out}")
    if board.zone_nets:
        print("Note: the pours in the written file are not refilled. Refill them (B in KiCad) or run "
              "kicad-cli pcb drc --refill-zones before reading DRC.")
    if viz is not None:
        if a.png:
            viz.save_png(a.png)
            print("Wrote", a.png)
        if a.gif:
            viz.save_gif(a.gif)
            print("Wrote", a.gif)
        if a.live:
            import matplotlib.pyplot as plt
            plt.show()
    return 0 if all(nr.complete for nr in res.nets.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
