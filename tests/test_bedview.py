from printloop.bedview import BedView, PART_MARGIN, finish_end_gcode, gcode_max_z


class Printer:
    def __init__(self, homed="xyz", z=250.0, busy=None, state="complete"):
        self.homed, self.z, self.busy, self.state, self.sent = homed, z, busy, state, []

    def toolhead(self):
        return {"homed_axes": self.homed, "position": [0, 0, self.z, 0], "axis_maximum": [229, 227, 255, 0]}

    def busy_reason(self):
        return self.busy

    def status(self):
        return type("S", (), {"state": self.state})()

    def gcode(self, script):
        self.sent.append(script)


def view(p, tmp_path):
    return BedView(p, tmp_path / "bed.json", (219, 0))


def test_raise_never_homes_and_skips_when_busy(tmp_path):
    p = Printer(homed="")
    assert "isn't homed" in view(p, tmp_path).raise_for_photo(40) and not p.sent
    p = Printer(busy="printing x.gcode")
    assert "busy" in view(p, tmp_path).raise_for_photo(40) and not p.sent
    assert view(Printer(), tmp_path).raise_for_photo(0) == "disabled"


def test_raise_keeps_gap_above_tallest_known_part(tmp_path):
    p = Printer()
    v = view(p, tmp_path)
    assert v.raise_for_photo(40) is None and "G1 Z40.0" in p.sent[-1] and "X219.0 Y0.0" in p.sent[-1]
    v.remember_part(60)                                  # last finished part was 60 mm tall
    v.raise_for_photo(40)
    assert f"G1 Z{60 + PART_MARGIN:.1f}" in p.sent[-1]
    v.raise_for_photo(40, part_height=300)               # never past the axis
    assert "G1 Z250.0" in p.sent[-1]


def test_lower_only_when_not_printing(tmp_path):
    p = Printer(z=30)
    assert view(p, tmp_path).lower() is None and "G1 Z250.0" in p.sent[-1]
    p = Printer(z=30, state="printing")
    assert view(p, tmp_path).lower() == "printer is printing" and not p.sent
    p = Printer(z=250)
    assert view(p, tmp_path).lower() is None and not p.sent   # already down


def test_end_gcode_keeps_motors_on_and_gcode_height(tmp_path):
    g = finish_end_gcode((219, 0))
    assert "M84" not in g and "END_PRINT" not in g and "max_layer_z + 10" in g
    f = tmp_path / "x.gcode"
    f.write_text("; max_z_height: 12.4\nG1 Z0.2\n")
    assert gcode_max_z(f) == 12.4
    f.write_text("G1 Z0.2 F600\nG1 X1 Z5.6\nG0 Z3\n")
    assert gcode_max_z(f) == 5.6
