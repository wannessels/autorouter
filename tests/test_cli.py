"""The command line: net names, rip-up."""
import os
import shutil
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from kicad_autorouter.__main__ import _board_net, main  # noqa: E402
from kicad_autorouter.kicad_pcb import PcbFile  # noqa: E402
from kicad_autorouter.problem import net_status  # noqa: E402

ROUTED = os.path.join(ROOT, "examples", "demo_routed.kicad_pcb")


def test_net_name_without_its_sheet_path():
    names = {"/GND", "/cam/SDA", "/disp/SDA", "+3V3"}
    assert _board_net("GND", names) == "/GND"
    assert _board_net("+3V3", names) == "+3V3"
    assert _board_net("SDA", names) == "SDA"            # two nets fit: left as given, and skipped


def _segments(path, net):
    return sorted((round(t.x1, 4), round(t.y1, 4), round(t.x2, 4), round(t.y2, 4), t.layer)
                  for t in PcbFile(path).board().tracks if t.net == net)


def test_rip_up_routes_the_net_again_in_place_of_its_old_copper(tmp_path):
    board = str(tmp_path / "b.kicad_pcb")
    shutil.copy(ROUTED, board)
    old = _segments(board, "D1")
    assert old

    ripped = str(tmp_path / "ripped.kicad_pcb")
    assert main([board, "-o", ripped, "--rip-up", "--nets", "D1", "--workers", "1", "-q"]) == 0

    pcb = PcbFile(board)                           # the same board with D1's copper deleted by hand
    m = pcb.board()
    pcb.remove_items([t for t in m.tracks if t.net == "D1"], [v for v in m.vias if v.net == "D1"])
    bare = str(tmp_path / "bare.kicad_pcb")
    pcb.save(bare)
    fresh = str(tmp_path / "fresh.kicad_pcb")
    assert main([bare, "-o", fresh, "--nets", "D1", "--workers", "1", "-q"]) == 0

    assert _segments(ripped, "D1") == _segments(fresh, "D1")
    assert net_status(PcbFile(ripped).board())["D1"] == "routed"
    assert _segments(ripped, "D2") == _segments(board, "D2")
