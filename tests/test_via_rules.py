"""Via rules: drill to copper (hole clearance), net-class clearance for vias, a net's own vias."""
import math
import os
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
sys.path[:0] = [ROOT, os.path.join(ROOT, "examples"), HERE]

from synthetic import rect_outline  # noqa: E402

from kicad_autorouter import Board, Pad, RouteSettings  # noqa: E402
from kicad_autorouter.kicad_pcb import PcbFile  # noqa: E402
from kicad_autorouter.model import Via  # noqa: E402
from kicad_autorouter.problem import BLOCKED, FREE, build_problem  # noqa: E402
from kicad_autorouter.restraighten import drop_dead_ends  # noqa: E402
from kicad_autorouter.router import NetRoute, RouteResult, Router  # noqa: E402

ROUTED = os.path.join(ROOT, "examples", "demo_routed.kicad_pcb")
# a small via ring: the drill's hole clearance reaches further than the ring's clearance
SMALL_VIA = dict(track_width=0.2, clearance=0.15, via_diameter=0.45, via_drill=0.3, hole_clearance=0.3)


def smd(net, x, y, ref):
    return Pad(net, x, y, 1.0, 1.0, "rect", 0, {"F.Cu"}, ref=ref)


def two_nets():
    return Board(["F.Cu", "B.Cu"], rect_outline(0, 0, 30, 20),
                 [smd("A", 3, 3, "J1.1"), smd("A", 27, 17, "J2.1"), smd("B", 15, 10, "U1.1"),
                  smd("B", 3, 17, "J1.2")])


def cells_near(g, dist, reach):
    return [(x, y) for y in range(g.ny) for x in range(g.nx) if dist(*g.center(x, y)) < reach]


def test_no_via_of_another_net_with_its_drill_within_hole_clearance_of_a_pad():
    s = RouteSettings(**SMALL_VIA)
    b = two_nets()
    pb = build_problem(b, s)
    g, a = pb.grid, pb.net_ids["A"]
    pad = b.pads[2]
    near = cells_near(g, pad.distance, s.via_drill / 2 + s.hole_clearance)
    assert len(near) > len(cells_near(g, pad.distance, s.via_diameter / 2 + s.clearance))
    assert all(pb.via_owner[y * g.nx + x] not in (a, FREE) for x, y in near)


def test_no_track_of_another_net_within_hole_clearance_of_an_existing_via_drill():
    s = RouteSettings(**SMALL_VIA)
    b = two_nets()
    b.vias.append(Via("B", 15.0, 5.0, 0.45, 0.4))
    pb = build_problem(b, s)
    g, a = pb.grid, pb.net_ids["A"]
    near = cells_near(g, lambda x, y: math.hypot(x - 15.0, y - 5.0), 0.4 / 2 + s.hole_clearance + s.track_width / 2)
    for x, y in near:
        for l in range(g.nl):
            assert pb.owner[g.idx(x, y, l)] != a and pb.owner[g.idx(x, y, l)] != FREE


def test_via_footprint_keeps_the_hole_clearance():
    s = RouteSettings(**SMALL_VIA)
    pb = build_problem(two_nets(), s)
    reach = s.via_drill / 2 + s.hole_clearance + s.track_width / 2
    p = pb.grid.pitch
    assert all((dx, dy) in pb.via_footprint for dx in range(-3, 4) for dy in range(-3, 4)
               if math.hypot(dx, dy) * p < reach - 1e-9)


def test_vias_of_a_net_class_keep_its_clearance_from_other_copper():
    s = RouteSettings(net_rules={"A": (0.25, 0.45)})
    b = two_nets()
    pb = build_problem(b, s)
    g = pb.grid
    job = next(j for j in pb.nets if j.name == "A")
    a = job.id
    bad = set(job.via_bad)
    pad = b.pads[2]
    near = cells_near(g, pad.distance, 0.45 + s.via_diameter / 2)
    assert any(pb.via_owner[y * g.nx + x] in (a, FREE) for x, y in near)       # the shared map allows these
    assert all(pb.via_owner[y * g.nx + x] not in (a, FREE) or y * g.nx + x in bad for x, y in near)
    assert len(job.via_fp) > len(pb.via_footprint)


def test_the_last_step_merges_stacked_vias_and_drops_dead_ends():
    b = Board(["F.Cu", "B.Cu"], rect_outline(0, 0, 20, 20),
              [Pad("A", 2, 2, 1, 1, "rect", 0, {"F.Cu"}, ref="J1.1"),
               Pad("A", 10, 2, 1, 1, "rect", 0, {"B.Cu"}, ref="J2.1")])
    nr = NetRoute("A", complete=True)
    nr.segments = [(2, 2, 6, 2, "F.Cu"), (6, 2, 10, 2, "B.Cu"),
                   (6, 2, 6, 5, "F.Cu"),                          # a stub that ends in nothing
                   (2, 2, 2, 6, "F.Cu")]                          # ends on a via that joins one layer
    nr.vias = [(6, 2), (6.0, 2.0), (2, 6)]
    res = RouteResult({"A": nr}, [], True, [], {})
    drop_dead_ends(b, res, [], [])
    assert nr.segments == [(2, 2, 6, 2, "F.Cu"), (6, 2, 10, 2, "B.Cu")]
    assert nr.vias == [(6, 2)]


def test_a_via_down_to_its_pour_is_no_dead_end():
    ring = [(0, 0), (20, 0), (20, 20), (0, 20)]
    b = Board(["F.Cu", "B.Cu"], rect_outline(0, 0, 20, 20), [Pad("GND", 2, 2, 1, 1, "rect", 0, {"F.Cu"}, ref="C1.2")],
              zone_nets={"GND"}, zone_layers={"GND": {"B.Cu"}}, zone_outlines={"GND": [({"B.Cu"}, [ring])]})
    nr = NetRoute("GND", segments=[(2, 2, 3, 3, "F.Cu")], vias=[(3, 3)], complete=True, widths=[0.3])
    drop_dead_ends(b, RouteResult({"GND": nr}, [], True, [], {}), [], [])
    assert nr.segments == [(2, 2, 3, 3, "F.Cu")] and nr.vias == [(3, 3)] and nr.widths == [0.3]


def test_a_net_keeps_its_own_vias_apart_while_it_routes():
    s = RouteSettings()
    pb = build_problem(two_nets(), s)
    r = Router(pb)
    g = pb.grid
    job = next(j for j in pb.nets if j.name == "A")
    col = next(y * g.nx + x for y in range(3, g.ny - 3) for x in range(3, g.nx - 3)
               if all(pb.via_owner[(y + dy) * g.nx + x + dx] == FREE for dx in (-1, 0, 1) for dy in (-1, 0, 1)))
    job.paths = [[col, g.plane + col]]                          # a via at (x, y)
    before = bytes(memoryview(pb.via_owner).cast("B"))
    close, reopen = r._own_via_spacing(job)
    close()
    assert pb.via_owner[col] != BLOCKED and pb.via_owner[col + 1] == BLOCKED
    reopen()
    assert bytes(memoryview(pb.via_owner).cast("B")) == before


def test_a_via_drill_is_read_from_the_file():
    vias = PcbFile(ROUTED).board().vias
    assert vias and all(v.drill > 0 for v in vias)
