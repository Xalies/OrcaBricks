# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "OrcaBricks Prime Tower"
# description = "Rebuilds the prime tower as stackable, LEGO(R) compatible building bricks so purge filament becomes something useful."
# author = "Sonny Gilbert"
# version = "0.6.6"
# ///
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field

PLUGIN_VERSION = "0.6.6"


DEFAULTS: dict = {
    "enabled": True,
    "allow_mixed_materials": False,
    "brick_sizes": "2x4,2x3,2x2,1x4,1x3,1x2,1x1",
    "brick_style": "auto",
    "allow_medium_bricks": True,
    "allow_plates": True,
    "allow_tiles": True,
    "allow_slopes": True,
    "allow_round": True,
    "finish_last_brick": True,
    "line_width": 0.45,
    "brick_speed": 40.0,
    "first_layer_speed": 20.0,
    "travel_speed": 150.0,
    "max_purge_speed": 150.0,
    "brick_gap": 1.0,
    "breakaway_gap": 0.25,
    "solid_spacer_cap": False,
    "stud_clearance": 0.09,
    "stud_diameter": 4.8,
    "spacer_clearance": 0.6,
    "brim_loops": 3,
    "max_hidden_flow": 1.5,
    "footprint_grow": 0.0,
    "z_lift": 0.6,
}

PITCH = 8.0
BRICK_H = 9.6
MEDIUM_BRICK_H = 6.4
PLATE_H = 3.2
DECK_T = 1.0
STUD_H = 1.7
STUD_TIP_RADIUS = 0.2
EDGE = 0.1
SOCKET_D = 2.0

FILL_MIN = 0.15
SPACER_MIN = 0.25
IFACE_D = 0.5
SCAFFOLD_D = 0.15
SOCK_MIN = 0.3
INTERNAL_KINDS = {"fill", "socket", "spacer", "scaffold"}
LINK_MAX = 2.5
NO_LINK = {"stud", "tube", "pin", "hole"}


def scaffold_paths(cells, z, t, d, w, along_y):
    paths = []
    for ci, b in enumerate(cells):
        x0, y0, x1, y1 = b.outer
        x0, y0, x1, y1 = x0 + 0.3, y0 + 0.3, x1 - 0.3, y1 - 0.3
        pp = clipped_lines(x0 + w, y0 + w, x1 - w, y1 - w, w / max(d, 0.05), along_y, [], z, t,
                           "scaffold", bead_w=w)
        pp.append(Path(rect_loop(x0 + w / 2, y0 + w / 2, x1 - w / 2, y1 - w / 2), z, w, t, "scaffold"))
        for q in pp:
            q.grp = 1000 + ci
        paths += pp
    return paths


def load_settings(raw: dict | None) -> dict:
    s = dict(DEFAULTS)
    for k, v in (raw or {}).items():
        if k not in DEFAULTS:
            continue
        d = DEFAULTS[k]
        try:
            if isinstance(d, bool):
                s[k] = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(d, int):
                s[k] = int(v)
            elif isinstance(d, float):
                s[k] = float(v)
            else:
                s[k] = str(v)
        except (TypeError, ValueError):
            pass
    return s


def parse_sizes(text: str) -> list[tuple[int, int]]:
    out = []
    for part in re.split(r"[,\s;]+", text or ""):
        m = re.fullmatch(r"(\d+)\s*[xX*]\s*(\d+)", part.strip())
        if m:
            a, b = sorted((int(m.group(1)), int(m.group(2))))
            if 1 <= a <= 8 and 1 <= b <= 16 and (a, b) not in out:
                out.append((a, b))
    out.sort(key=lambda ab: -ab[0] * ab[1])
    return out or [(2, 4)]


class Skip(Exception):
    pass


CMD_RE = re.compile(r"^\s*([GMT])(\d+)(?:\.(\d+))?", re.I)
WORD_RE = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)")
LAYER_TAG_RE = re.compile(r"^;\s*(LAYER_CHANGE|CHANGE_LAYER)\s*$")
Z_TAG_RE = re.compile(r"^;\s*(?:Z|Z_HEIGHT)\s*:\s*([-+]?[\d.]+)")
H_TAG_RE = re.compile(r"^;\s*(?:HEIGHT|LAYER_HEIGHT)\s*:\s*([-+]?[\d.]+)")
FEATURE_RE = re.compile(r"^;\s*(TYPE|FEATURE)\s*:\s*(.+?)\s*$")
WT_START_RE = re.compile(r"^;\s*WIPE_TOWER_START\b")
WT_END_RE = re.compile(r"^;\s*WIPE_TOWER_END\b")
WT_BRIM_START_RE = re.compile(r"^;\s*WIPE_TOWER_BRIM_START\b")
WT_BRIM_END_RE = re.compile(r"^;\s*WIPE_TOWER_BRIM_END\b")
CUSTOM_START_RE = re.compile(r"^\s*M620\s+S", re.I)
CUSTOM_END_RE = re.compile(r"^\s*M621\s+S", re.I)
CP_START_RE = re.compile(r"^;\s*CP TOOLCHANGE START")
CP_END_RE = re.compile(r"^;\s*CP TOOLCHANGE END")
CONFIG_START_RE = re.compile(r"^;\s*CONFIG_BLOCK_START")
CONFIG_RE = re.compile(r"^;\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$")
TOWER_FEATURES = {"prime tower", "wipe tower"}
MARK = "; BRICKTOWER"


@dataclass
class Cmd:
    letter: str = ""
    num: int = -1
    words: dict = field(default_factory=dict)

    @property
    def is_move(self):
        return self.letter == "G" and self.num in (0, 1, 2, 3)


_WORD_LETTERS = set("XYZEFIJRSPKL")


def parse_cmd(line: str) -> Cmd | None:
    code = line.split(";", 1)[0]
    if not code:
        return None
    c0 = code[0]
    if c0 in "Gg":
        toks = code.split()
        head = toks[0][1:] if toks else ""
        if head.isdigit():
            words = {}
            try:
                for tok in toks[1:]:
                    L = tok[0].upper()
                    if L in _WORD_LETTERS:
                        words[L] = float(tok[1:])
                    else:
                        raise ValueError
                return Cmd("G", int(head), words)
            except (ValueError, IndexError):
                pass
    elif c0 not in "MmTt \t":
        return None
    m = CMD_RE.match(code)
    if not m:
        return None
    letter = m.group(1).upper()
    num = int(m.group(2))
    rest = code[m.end():]
    words = {}
    if letter != "T":
        for k, v in WORD_RE.findall(rest):
            try:
                words[k.upper()] = float(v)
            except ValueError:
                pass
    return Cmd(letter, num, words)


def arc_length(sx, sy, ex, ey, w, clockwise):
    if "I" in w or "J" in w:
        cx, cy = sx + w.get("I", 0.0), sy + w.get("J", 0.0)
        r = math.hypot(sx - cx, sy - cy)
        a0 = math.atan2(sy - cy, sx - cx)
        a1 = math.atan2(ey - cy, ex - cx)
        sweep = a0 - a1 if clockwise else a1 - a0
        while sweep <= 1e-9:
            sweep += 2 * math.pi
        return r * sweep
    return math.hypot(ex - sx, ey - sy)


class Tracker:

    def __init__(self, per_tool=False):
        self.x = self.y = self.z = 0.0
        self.e = 0.0
        self.f = 3000.0
        self.abs_xyz = True
        self.abs_e = True
        self.tool = 0
        self.per_tool = per_tool
        self._ret = {}
        self.fw_retracted = False

    @property
    def retract(self):
        return self._ret.get(self.tool if self.per_tool else 0, 0.0)

    @retract.setter
    def retract(self, v):
        self._ret[self.tool if self.per_tool else 0] = v

    def feed(self, cmd: Cmd | None):
        if cmd is None:
            return None
        L, n, w = cmd.letter, cmd.num, cmd.words
        if L == "T":
            self.tool = n
            return None
        if L == "M":
            if n == 82:
                self.abs_e = True
            elif n == 83:
                self.abs_e = False
            return None
        if L != "G":
            return None
        if n == 90:
            self.abs_xyz = True
        elif n == 91:
            self.abs_xyz = False
        elif n == 92:
            if "E" in w:
                self.e = w["E"]
            for a in ("X", "Y", "Z"):
                if a in w:
                    setattr(self, a.lower(), w[a])
        elif n == 10:
            self.fw_retracted = True
        elif n == 11:
            self.fw_retracted = False
        elif n in (0, 1, 2, 3):
            sx, sy, sz = self.x, self.y, self.z
            if self.abs_xyz:
                ex, ey, ez = w.get("X", sx), w.get("Y", sy), w.get("Z", sz)
            else:
                ex, ey, ez = sx + w.get("X", 0.0), sy + w.get("Y", 0.0), sz + w.get("Z", 0.0)
            de = 0.0
            if "E" in w:
                de = (w["E"] - self.e) if self.abs_e else w["E"]
                self.e = w["E"] if self.abs_e else self.e + w["E"]
            if "F" in w:
                self.f = w["F"]
            has_xy = "X" in w or "Y" in w
            if n in (2, 3):
                length = arc_length(sx, sy, ex, ey, w, n == 2)
            else:
                length = math.hypot(ex - sx, ey - sy)
            self.x, self.y, self.z = ex, ey, ez
            eff = refill = 0.0
            if de < 0:
                self.retract += de
            elif de > 0:
                refill = min(de, -self.retract)
                self.retract += refill
                eff = de - refill
            return {"sx": sx, "sy": sy, "sz": sz, "ex": ex, "ey": ey, "ez": ez, "de": de,
                    "eff": eff, "refill": refill,
                    "f": self.f, "len": length, "has_xy": has_xy, "has_z": "Z" in w,
                    "has_e": "E" in w, "arc": n in (2, 3)}
        return None


@dataclass
class Brick:
    x: float
    y: float
    nx: int
    ny: int
    shape: str = "rect"
    axis: str = "y"
    slot: int = -1

    @property
    def outer(self):
        return (self.x + EDGE, self.y + EDGE, self.x + PITCH * self.nx - EDGE, self.y + PITCH * self.ny - EDGE)

    def studs(self):
        return [(self.x + PITCH * (i + 0.5), self.y + PITCH * (j + 0.5))
                for i in range(self.nx) for j in range(self.ny)]

    def top_studs(self):
        if self.shape == "slope":
            if self.axis == "y":
                return [(sx, sy) for (sx, sy) in self.studs() if sy > self.y + PITCH]
            return [(sx, sy) for (sx, sy) in self.studs() if sx > self.x + PITCH]
        return self.studs()


def theme_pieces(cells, theme):
    out = []
    for b in cells:
        if theme == "round":
            used = set()
            for i in range(0, b.nx - 1, 2):
                for j in range(0, b.ny - 1, 2):
                    out.append(Brick(b.x + PITCH * i, b.y + PITCH * j, 2, 2, "round"))
                    used |= {(i, j), (i + 1, j), (i, j + 1), (i + 1, j + 1)}
            for i in range(b.nx):
                for j in range(b.ny):
                    if (i, j) not in used:
                        out.append(Brick(b.x + PITCH * i, b.y + PITCH * j, 1, 1, "round"))
        elif theme == "slope" and b.ny == 2:
            out.append(Brick(b.x, b.y, b.nx, 2, "slope", "y"))
        elif theme == "slope" and b.nx == 2:
            out.append(Brick(b.x, b.y, 2, b.ny, "slope", "x"))
        else:
            out.append(Brick(b.x, b.y, b.nx, b.ny))
    return out


@dataclass
class Path:
    pts: list
    z: float
    w: float
    t: float
    kind: str
    grp: int = 0


def order_paths(paths):
    out = []
    i = 0
    while i < len(paths):
        j = i
        while j < len(paths) and paths[j].grp == paths[i].grp:
            j += 1
        run = paths[i:j]
        i = j
        holes, outer, lines_, other = [], [], [], []
        for q in run:
            if q.kind == "hole":
                holes.append(q)
            elif q.kind in ("wall", "deck_edge", "brim"):
                outer.append(q)
            elif len(q.pts) == 2:
                lines_.append(q)
            elif q.kind not in NO_LINK and len(q.pts) > 3 and q.pts[0] == q.pts[-1]:
                lines_.append(q)
            else:
                other.append(q)
        if len(lines_) > 2 and any(len(q.pts) == 2 for q in lines_):
            first = max((q for q in lines_ if len(q.pts) == 2),
                        key=lambda q: math.hypot(q.pts[1][0] - q.pts[0][0], q.pts[1][1] - q.pts[0][1]))
            rest = [q for q in lines_ if q is not first]
            chain = [first]
            cx, cy = first.pts[1]
            while rest:
                bi, bd, at = 0, 1e18, 0
                for k, q in enumerate(rest):
                    pts = q.pts
                    cand = (0, 1) if len(pts) == 2 else range(len(pts) - 1)
                    for v in cand:
                        ax, ay = pts[v]
                        d = (ax - cx) * (ax - cx) + (ay - cy) * (ay - cy)
                        if d < bd:
                            bi, bd, at = k, d, v
                q = rest.pop(bi)
                if at:
                    if len(q.pts) == 2:
                        q = Path([q.pts[1], q.pts[0]], q.z, q.w, q.t, q.kind, q.grp)
                    else:
                        ring = q.pts[:-1]
                        ring = ring[at:] + ring[:at]
                        q = Path(ring + [ring[0]], q.z, q.w, q.t, q.kind, q.grp)
                chain.append(q)
                cx, cy = q.pts[-1]
            lines_ = chain
        if holes:
            out += outer + lines_ + other + holes
        else:
            out += lines_ + other + outer
    return out


def circle(cx, cy, r, start_angle=0.0):
    n = max(12, int(2 * math.pi * r / 0.35))
    return [(cx + r * math.cos(start_angle + 2 * math.pi * k / n),
             cy + r * math.sin(start_angle + 2 * math.pi * k / n)) for k in range(n + 1)]


def rect_loop(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def ring_paths(cx, cy, r_in, r_out, w, z, t, kind):
    paths = []
    width = r_out - r_in
    if width <= 0:
        return paths
    if r_in <= 1e-6:
        n = max(1, int(round(r_out / w)))
        lw = r_out / n
        for k in range(n):
            r = r_out - lw * (k + 0.5)
            if r < lw * 0.6:
                d = max(r, 0.15)
                paths.append(Path([(cx - d, cy), (cx + d, cy)], z, lw, t, kind))
            else:
                paths.append(Path(circle(cx, cy, r, k * 0.7), z, lw, t, kind))
        paths.reverse()
        return paths
    n = max(1, int(round(width / w)))
    lw = width / n
    for k in range(n):
        r = r_in + lw * (k + 0.5)
        paths.append(Path(circle(cx, cy, r, k * 0.7), z, lw, t, kind))
    return paths


def zigzag(x0, y0, x1, y1, w, along_y, z, t, kind):
    if x1 - x0 < w * 0.5 or y1 - y0 < w * 0.5:
        return []
    pts = []
    if along_y:
        n = max(1, int(round((x1 - x0) / w)))
        sp = (x1 - x0) / n
        for k in range(n):
            x = x0 + sp * (k + 0.5)
            ya, yb = (y0, y1) if k % 2 == 0 else (y1, y0)
            pts += [(x, ya), (x, yb)]
    else:
        n = max(1, int(round((y1 - y0) / w)))
        sp = (y1 - y0) / n
        for k in range(n):
            y = y0 + sp * (k + 0.5)
            xa, xb = (x0, x1) if k % 2 == 0 else (x1, x0)
            pts += [(xa, y), (xb, y)]
    return [Path(pts, z, sp, t, kind)]


def clipped_lines(x0, y0, x1, y1, w, along_y, holes, z, t, kind, bead_w=None, aligned=()):
    paths = []
    if x1 - x0 < 0.3 or y1 - y0 < 0.3:
        return paths
    low, high, start, end = (x0, x1, y0, y1) if along_y else (y0, y1, x0, x1)
    count = max(1, int(round((high - low) / w)))
    spacing = (high - low) / count
    bw = bead_w or spacing
    positions = [low + spacing * (k + 0.5) for k in range(count)]
    if aligned:
        positions = sorted([v for v in positions if all(abs(v - p) >= bw for p in aligned)] + list(aligned))
    for k, pos in enumerate(positions):
        cuts = []
        for cx, cy, r in holes:
            cross, center = (cx, cy) if along_y else (cy, cx)
            delta = abs(pos - cross)
            if delta < r:
                half = math.sqrt(r * r - delta * delta)
                cuts.append((center - half, center + half))
        segments = _subtract(start, end, cuts)
        if k % 2:
            segments = [(b, a) for a, b in reversed(segments)]
        for a, b in segments:
            if abs(b - a) >= bw * 0.8:
                points = [(pos, a), (pos, b)] if along_y else [(a, pos), (b, pos)]
                paths.append(Path(points, z, bw, t, kind))
    return paths


def _subtract(a, b, cuts):
    segs = [(a, b)]
    for (c0, c1) in sorted(cuts):
        nxt = []
        for (s0, s1) in segs:
            if c1 <= s0 or c0 >= s1:
                nxt.append((s0, s1))
                continue
            if c0 > s0:
                nxt.append((s0, c0))
            if c1 < s1:
                nxt.append((c1, s1))
        segs = nxt
    return segs


def pack_bricks(w: float, h: float, sizes, gap: float):
    memo = {}

    def solve(W, H):
        key = (round(W, 2), round(H, 2))
        if key in memo:
            return memo[key]
        best = (0, 0, [])
        for (a, b) in sizes:
            for (nx, ny) in ((b, a), (a, b)):
                cw, ch = PITCH * nx, PITCH * ny
                cx = int((W + gap + 1e-9) // (cw + gap))
                cy = int((H + gap + 1e-9) // (ch + gap))
                if cx < 1 or cy < 1:
                    continue
                cells = [(i * (cw + gap), j * (ch + gap), nx, ny) for i in range(cx) for j in range(cy)]
                used_w = cx * (cw + gap)
                used_h = cy * (ch + gap)
                right = solve(W - used_w, used_h - gap) if W - used_w > PITCH else (0, 0, [])
                top = solve(W, H - used_h) if H - used_h > PITCH else (0, 0, [])
                items = cells + [(x + used_w, y, p, q) for (x, y, p, q) in right[2]] \
                              + [(x, y + used_h, p, q) for (x, y, p, q) in top[2]]
                studs = sum(p * q for (_, _, p, q) in items)
                score = (studs, -len(items))
                if score > (best[0], best[1]):
                    best = (studs, -len(items), items)
        memo[key] = best
        return best

    return solve(w, h)[2]


@dataclass
class LayerSpec:
    idx: int
    z: float
    t: float
    region: str
    deck_n: int
    first: bool
    elem_first: bool
    spacer: bool
    spacer_z: float
    elem: int
    kind: str
    v: float = 0.0
    spacer_top: bool = False
    spacer_last: bool = False
    spacer_ring_clearance: float | None = None
    spacer_cap: bool = False
    stud_tip_inset: float = 0.0


def plan_layers(layers, last_tower, max_layer, settings, gap, layer_cost=None):
    allow_plates = settings["allow_plates"]
    notes = []
    specs = {}
    elements = []

    def simulate(start_idx, base, H, stud_h=STUD_H):
        out = []
        i = start_idx
        top = base
        deck_n = 0
        n_in_elem = 0
        while i <= max_layer:
            Z, t = layers[i]
            if i == 0:
                z = Z
            elif n_in_elem == 0:
                z = base + t
            else:
                z = top + t
                v_next_mid = (top + z) / 2 - base
                if z > Z + 1e-6 and n_in_elem >= 2 and v_next_mid < H - DECK_T - t:
                    z = max(Z, top + t * 0.9)
            mid = (max(top, base) + z) / 2 - base
            if mid >= H + stud_h:
                return i, out
            if mid < H - DECK_T:
                region = "cavity"
            elif mid < H:
                region = "deck"
            else:
                region = "studs"
            spec = LayerSpec(i, z, z - max(top, base) if n_in_elem else (z if i == 0 else t),
                             region, deck_n, i == 0, n_in_elem == 0, False, 0.0, -1, "", mid)
            if region == "studs":
                rise = max(0.0, min(STUD_TIP_RADIUS, mid - (H + stud_h - STUD_TIP_RADIUS)))
                spec.stud_tip_inset = STUD_TIP_RADIUS - math.sqrt(STUD_TIP_RADIUS ** 2 - rise ** 2)
            out.append(spec)
            if region == "deck":
                deck_n += 1
            top = z
            n_in_elem += 1
            i += 1
        if out and top - base >= H + stud_h - 0.05:
            return max_layer + 1, out
        return None, out

    def legacy_choice(i, base):
        brick_end, brick_specs = simulate(i, base, BRICK_H)
        plate_end, plate_specs = simulate(i, base, PLATE_H) if allow_plates else (None, None)
        kind, end, sp = "brick", brick_end, brick_specs
        if allow_plates and plate_specs:
            if (plate_end is not None and plate_end > last_tower) or brick_end is None:
                kind, end, sp = "plate", plate_end, plate_specs
        if kind == "plate" and settings.get("allow_tiles") and (end is None or end > last_tower):
            tile_end, tile_specs = simulate(i, base, PLATE_H, stud_h=0.0)
            if tile_end is not None and tile_end > last_tower:
                kind, end, sp = "tile", tile_end, tile_specs
        return kind, end, sp

    def prepare(kind, end, sp):
        has_next = end is not None and end <= last_tower
        for s in sp:
            s.kind = kind
            if s.region == "studs" and (has_next or s.idx <= last_tower):
                s.spacer = True
                s.spacer_z = s.z + gap
        spacer_layers = [s for s in sp if s.spacer]
        if spacer_layers and has_next:
            last = spacer_layers[-1]
            last.spacer_top = True
            last.spacer_last = True
            last.spacer_cap = settings.get("solid_spacer_cap", False) and gap > 0
            if gap >= last.t - 1e-6:
                width = settings["line_width"]
                inner = max(settings["stud_clearance"], 0.1)
                outer = min(settings["spacer_clearance"], PITCH / 2 - EDGE - settings["stud_diameter"] / 2 - width)
                if outer >= inner:
                    for remaining, s in enumerate(reversed(spacer_layers)):
                        s.spacer_ring_clearance = min(outer, inner + remaining * width / 3)
        if spacer_layers:
            spacer_layers[0].spacer_top = True
        return kind, end, sp

    def next_base(sp):
        last = sp[-1]
        return last.z + 2 * gap + (last.t if last.spacer_cap else 0.0)

    if layer_cost is None:
        choices = []
        i, base = 0, 0.0
        while i <= last_tower:
            kind, end, sp = prepare(*legacy_choice(i, base))
            if not sp:
                break
            choices.append((kind, end, sp))
            if end is None or end > last_tower:
                break
            i, base = end, next_base(sp)
    else:
        heights = [("brick", BRICK_H, STUD_H), ("medium brick", MEDIUM_BRICK_H, STUD_H)]
        if allow_plates:
            heights.append(("plate", PLATE_H, STUD_H))
            if settings["allow_tiles"]:
                heights.append(("tile", PLATE_H, 0.0))

        pending = {0: {0.0: ((0.0, 0.0, 0, 0), None)}}
        best = None
        for i in range(last_tower + 1):
            states = pending.pop(i, {})
            for base, (score, prefix) in sorted(states.items(), key=lambda item: item[1][0])[:3]:
                for kind, height, studs in heights:
                    end, sp = simulate(i, base, height, studs)
                    if not sp or (kind == "tile" and (end is None or end <= last_tower)):
                        continue
                    choice = prepare(kind, end, sp)
                    overload, material = map(sum, zip(*(layer_cost(s) for s in sp)))
                    next_score = (score[0] + overload, score[1] + material,
                                  score[2] + sum(s.idx > last_tower for s in sp), score[3] + 1)
                    node = (choice, prefix)
                    if end is None or end > last_tower:
                        rank = (next_score[0], end is None, *next_score[1:])
                        if best is None or rank < best[0]:
                            best = (rank, node)
                    else:
                        candidates = pending.setdefault(end, {})
                        base_above = next_base(sp)
                        if base_above not in candidates or next_score < candidates[base_above][0]:
                            candidates[base_above] = (next_score, node)
        choices = []
        node = best[1] if best else None
        while node is not None:
            choice, node = node
            choices.append(choice)
        choices.reverse()

    for e_idx, (kind, end, sp) in enumerate(choices):
        elements.append({"kind": kind, "start": sp[0].idx, "end": end, "complete": end is not None})
        for s in sp:
            s.elem = e_idx
            specs[s.idx] = s
        if end is None:
            why = "print ended" if max_layer == len(layers) - 1 else "'finish last brick' is off"
            notes.append(f"top {kind} is unfinished ({why})")
    return specs, elements, notes


def layer_paths(spec: LayerSpec, bricks: list[Brick], st: dict, style=("tubes", 0.0), spacer_d=1.0,
                scaffold=(), socket_support=()) -> list[Path]:
    w = st["line_width"]
    c = st["stud_clearance"]
    stud_r = st["stud_diameter"] / 2
    z, t = spec.z, spec.t
    paths: list[Path] = []
    socket, fill = style[0], style[1]
    sock_d = style[2] if len(style) > 2 else 1.0

    if scaffold and not spec.spacer and spec.region != "studs":
        paths += scaffold_paths(scaffold, z, t, SCAFFOLD_D, w, spec.idx % 2 == 0)

    def _piece(b):
        paths = []
        if b.shape == "round":
            paths += _round_piece(b, spec, style, w, c, stud_r, z, t)
            return paths
        x0, y0, x1, y1 = b.outer
        if b.shape == "slope":
            lip = BRICK_H - (PITCH - EDGE)
            shift = min(max(0.0, spec.v - lip), PITCH - EDGE - w)
            if b.axis == "y":
                y0 += shift
            else:
                x0 += shift
        if spec.region == "cavity":
            wall = (PITCH / 2 - stud_r - c) - EDGE
            n = max(1, int(round(wall / w)))
            lw = wall / n
            for k in reversed(range(n)):
                d = lw * (k + 0.5)
                paths.append(Path(rect_loop(x0 + d, y0 + d, x1 - d, y1 - d), z, lw, t, "wall"))
            ix0, iy0, ix1, iy1 = x0 + wall + w * 0.25, y0 + wall + w * 0.25, x1 - wall - w * 0.25, y1 - wall - w * 0.25
            along = spec.idx % 2 == 0
            if spec.v < SOCKET_D and socket == "holes":
                hr = stud_r + c
                studs_in = [(sx, sy) for (sx, sy) in b.studs() if x0 < sx < x1 and y0 < sy < y1]
                for (sx, sy) in studs_in:
                    paths += ring_paths(sx, sy, hr, hr + w, w, z, t, "hole")
                holes = [(sx, sy, hr + w * 0.85) for (sx, sy) in studs_in]
                paths += clipped_lines(ix0, iy0, ix1, iy1, w / max(sock_d, 0.05), along, holes, z, t, "socket",
                                       bead_w=w)
                return paths
            def fits(cx, cy, r):
                return cx - r >= ix0 - 0.3 and cy - r >= iy0 - 0.3
            holes = []
            if b.nx >= 2 and b.ny >= 2:
                r_out = (PITCH * math.sqrt(2) - 2 * stud_r) / 2 - c
                for i in range(1, b.nx):
                    for j in range(1, b.ny):
                        if fits(b.x + PITCH * i, b.y + PITCH * j, r_out):
                            paths += ring_paths(b.x + PITCH * i, b.y + PITCH * j, stud_r, r_out, w, z, t, "tube")
                            holes.append((b.x + PITCH * i, b.y + PITCH * j, r_out + w * 0.3))
            elif b.nx == 1 and b.ny >= 2:
                for j in range(1, b.ny):
                    r = PITCH / 2 - stud_r - c
                    if fits(b.x + PITCH / 2, b.y + PITCH * j, r):
                        paths += ring_paths(b.x + PITCH / 2, b.y + PITCH * j, 0, r, w, z, t, "pin")
                        holes.append((b.x + PITCH / 2, b.y + PITCH * j, r + w * 0.3))
            elif b.ny == 1 and b.nx >= 2:
                for i in range(1, b.nx):
                    r = PITCH / 2 - stud_r - c
                    if fits(b.x + PITCH * i, b.y + PITCH / 2, r):
                        paths += ring_paths(b.x + PITCH * i, b.y + PITCH / 2, 0, r, w, z, t, "pin")
                        holes.append((b.x + PITCH * i, b.y + PITCH / 2, r + w * 0.3))
            if spec.v >= SOCKET_D and fill > 0:
                paths += clipped_lines(ix0, iy0, ix1, iy1, w / fill, along, holes, z, t, "fill", bead_w=w)
        elif spec.region == "deck":
            d = w / 2
            paths.append(Path(rect_loop(x0 + d, y0 + d, x1 - d, y1 - d), z, w, t, "deck_edge"))
            short_is_y = (x1 - x0) >= (y1 - y0)
            along_y = short_is_y if spec.deck_n % 2 == 0 else not short_is_y
            paths += zigzag(x0 + w, y0 + w, x1 - w, y1 - w, w, along_y, z, t, "deck")
        else:
            for (sx, sy) in b.top_studs():
                paths += ring_paths(sx, sy, 0, stud_r - spec.stud_tip_inset, w, z, t, "stud")
        return paths

    for bi, b in enumerate(bricks):
        pp = _piece(b)
        pp.sort(key=lambda q: 1 if q.kind in ("wall", "deck_edge") else 0)
        for q in pp:
            q.grp = bi + 1
        paths += pp

    n_brim = st.get("_brim_n", st["brim_loops"])
    footing = list(bricks) + list(scaffold)
    if spec.first and n_brim > 0 and footing:
        ux0 = min(b.outer[0] for b in footing)
        uy0 = min(b.outer[1] for b in footing)
        ux1 = max(b.outer[2] for b in footing)
        uy1 = max(b.outer[3] for b in footing)
        for k in range(n_brim):
            d = w * (k + 0.5)
            paths.append(Path(rect_loop(ux0 - d, uy0 - d, ux1 + d, uy1 + d), z, w, t, "brim", -1))

    if spec.spacer and (bricks or scaffold):
        sz = spec.spacer_z
        d = w / 2
        sd = min(1.0, max(spacer_d, 0.05))
        studs = [(sx, sy) for b in bricks for (sx, sy) in b.top_studs()]
        sp_paths = []
        ring_clearance = spec.spacer_ring_clearance
        if ring_clearance is None and spec.spacer_last and sz - t >= z - 1e-6:
            ring_clearance = max(c, 0.1)
        if ring_clearance is not None:
            r0 = stud_r + ring_clearance
            for (sx, sy) in studs:
                sp_paths += ring_paths(sx, sy, r0, r0 + w, w, sz, t, "spacer")
            holes = [(sx, sy, r0 + w) for (sx, sy) in studs]
        else:
            holes = [(sx, sy, stud_r + st["spacer_clearance"]) for (sx, sy) in studs]
        along_y = spec.idx % 2 == 0
        cap_paths = []
        cap_z = sz + t
        for (rx0, ry0, rx1, ry1) in [b.outer for b in bricks] + [s.outer for s in scaffold]:
            x0, y0, x1, y1 = rx0 + w, ry0 + w, rx1 - w, ry1 - w
            aligned = sorted({x if along_y else y for x, y in socket_support
                              if x0 <= x <= x1 and y0 <= y <= y1}) if spec.spacer_last else ()
            sp_paths += clipped_lines(x0, y0, x1, y1, w / sd, along_y, holes, sz, t,
                                      "spacer", bead_w=w, aligned=aligned)
            sp_paths.append(Path(rect_loop(rx0 + d, ry0 + d, rx1 - d, ry1 - d), sz, w, t, "spacer"))
            if spec.spacer_cap:
                cap_paths.append(Path(rect_loop(rx0 + d, ry0 + d, rx1 - d, ry1 - d),
                                      cap_z, w, t, "spacer_cap"))
                cap_paths += zigzag(x0, y0, x1, y1, w, not along_y, cap_z, t, "spacer_cap")
        for q in sp_paths:
            q.grp = -2
        for q in cap_paths:
            q.grp = -3
        paths += sp_paths + cap_paths
    return paths


def _disc_lines(cx, cy, R, spacing, along_y, holes, z, t, kind, bead_w):
    paths = []
    if R <= bead_w * 0.5:
        return paths
    n = max(1, int(round(2 * R / spacing)))
    sp = 2 * R / n
    for k in range(n):
        off = -R + sp * (k + 0.5)
        h = math.sqrt(max(R * R - off * off, 0.0))
        p = (cx + off) if along_y else (cy + off)
        cuts = []
        for (hx, hy, r) in holes:
            d = abs(p - (hx if along_y else hy))
            if d < r:
                q = math.sqrt(r * r - d * d)
                c0 = hy if along_y else hx
                cuts.append((c0 - q, c0 + q))
        base = cy if along_y else cx
        segs = _subtract(base - h, base + h, cuts)
        if k % 2:
            segs = [(b, a) for (a, b) in reversed(segs)]
        for a, b in segs:
            if abs(b - a) >= bead_w * 0.8:
                pts = [(p, a), (p, b)] if along_y else [(a, p), (b, p)]
                paths.append(Path(pts, z, bead_w, t, kind))
    return paths


def _round_piece(p, spec, style, w, c, stud_r, z, t):
    paths = []
    n = p.nx
    cx, cy = p.x + PITCH * n / 2, p.y + PITCH * n / 2
    R = PITCH * n / 2 - EDGE - 0.1
    wall = (PITCH / 2 - stud_r - c) - EDGE
    along = spec.idx % 2 == 0
    fill = style[1]
    hr = stud_r + c
    if spec.region == "cavity":
        if spec.v < SOCKET_D:
            if n == 1:
                paths += ring_paths(cx, cy, hr, R, w, z, t, "hole")
            else:
                holes = [(sx, sy, hr) for (sx, sy) in p.studs()]
                paths += _disc_lines(cx, cy, R - w * 0.5, w, along, holes, z, t, "socket", w)
            return paths
        if n == 1:
            paths += ring_paths(cx, cy, 0 if fill > 0 else R - wall, R, w, z, t, "fill" if fill > 0 else "wall")
        else:
            paths += ring_paths(cx, cy, R - wall, R, w, z, t, "wall")
            r_tube = (PITCH * math.sqrt(2) - 2 * stud_r) / 2 - c
            paths += ring_paths(cx, cy, stud_r, r_tube, w, z, t, "tube")
            if fill > 0:
                paths += _disc_lines(cx, cy, R - wall - w * 0.25, w / fill, along,
                                     [(cx, cy, r_tube + w * 0.3)], z, t, "fill", w)
    elif spec.region == "deck":
        paths += _disc_lines(cx, cy, R - w, w, spec.deck_n % 2 == 0, [], z, t, "deck", w)
        paths += ring_paths(cx, cy, R - w, R, w, z, t, "deck_edge")
    else:
        for (sx, sy) in p.top_studs():
            paths += ring_paths(sx, sy, 0, stud_r - spec.stud_tip_inset, w, z, t, "stud")
    return paths


def bead_area(w, t):
    if w <= t:
        return math.pi * (w / 2) ** 2
    return t * (w - t) + math.pi * (t / 2) ** 2


class LayerJob:

    def __init__(self, paths, fil_area, k=1.0):
        self.segs = []
        self.meta = [(p.kind, p.grp) for p in paths]
        self.last_pid = None
        for pid, p in enumerate(paths):
            epm = bead_area(p.w, p.t) / fil_area * (k if p.kind in INTERNAL_KINDS else 1.0)
            for (a, b) in zip(p.pts, p.pts[1:]):
                L = math.hypot(b[0] - a[0], b[1] - a[1])
                if L > 1e-6:
                    self.segs.append((pid, a[0], a[1], b[0], b[1], L, epm, p.z))
        self.si = 0
        self.frac = 0.0

    def capacity(self):
        return sum(s[5] * s[6] for s in self.segs)

    def done(self):
        return self.si >= len(self.segs)

    def current_epm(self):
        return self.segs[self.si][6] if not self.done() else 1.0

    def pour(self, em, E, feed):
        while E > 1e-7 and not self.done():
            pid, x0, y0, x1, y1, L, epm, z = self.segs[self.si]
            px, py = x0 + (x1 - x0) * self.frac, y0 + (y1 - y0) * self.frac
            soft = False
            if self.last_pid is not None and self.last_pid != pid:
                (ka, ga), (kb, gb) = self.meta[self.last_pid], self.meta[pid]
                soft = ga == gb and ka not in NO_LINK and kb not in NO_LINK
            em.goto_start(px, py, z, soft)
            path_feed = min(feed, em.cap_f) if self.meta[pid][0] == "spacer_cap" else feed
            self.last_pid = pid
            rem_len = L * (1 - self.frac)
            rem_e = rem_len * epm
            if E >= rem_e - 1e-9:
                em.extrude(x1, y1, rem_e, path_feed)
                E -= rem_e
                self.si += 1
                self.frac = 0.0
            else:
                l = E / epm
                self.frac += l / L
                em.extrude(x0 + (x1 - x0) * self.frac, y0 + (y1 - y0) * self.frac, E, path_feed)
                E = 0.0
        return E

    def finish(self, em, feed):
        self.pour(em, float("inf"), feed)


class Emitter:
    def __init__(self, out, st):
        self.out = out
        self.st = st
        self.x = self.y = self.z = 0.0
        self.new_e = 0.0
        self.abs_e = False
        self.switched_abs = False
        self.need_entry = True
        self.tower_top = 0.0
        self.rel_xyz = False
        self.travel_f = st.get("_travel_f") or st["travel_speed"] * 60
        self.cap_f = st["first_layer_speed"] * 60
        self.xy_known = True
        self.last_seg = None
        self.tool = 0
        self.retracted = False
        self.stream_ret = False
        self.min_hop = 0.0

    def w(self, s):
        self.out.append(s + "\n")

    def begin(self, orig: Tracker):
        self.x, self.y, self.z = orig.x, orig.y, orig.z
        self.abs_e = orig.abs_e
        self.new_e = orig.e
        self.need_entry = True
        self.xy_known = True
        self.last_seg = None
        self.rel_xyz = not orig.abs_xyz
        self.switched_abs = False
        self.tool = orig.tool
        self.retracted = False
        self.stream_ret = orig.retract < -1e-6 or orig.fw_retracted

    def _r(self, key, default):
        arr = self.st.get("_ret", {}).get(key) or [default]
        return arr[self.tool] if 0 <= self.tool < len(arr) else arr[0]

    def settle(self):
        if self.retracted:
            speed = self._r("despeed", 0) or self._r("speed", 30)
            self._e(self._r("len", 0.8), speed * 60)
            self.retracted = False

    def ensure_abs(self):
        if self.rel_xyz and not self.switched_abs:
            self.w("G90")
            self.switched_abs = True

    def restore_rel(self):
        if self.switched_abs:
            self.w("G91")
            self.switched_abs = False

    def goto_start(self, x, y, z, soft=False):
        same_xy = self.xy_known and abs(x - self.x) < 1e-4 and abs(y - self.y) < 1e-4
        if same_xy and abs(z - self.z) < 1e-5 and not self.need_entry:
            return
        self.ensure_abs()
        self.last_seg = None
        if self.need_entry:
            if (not same_xy and not self.retracted and not self.stream_ret and self._r("len", 0.8) > 0
                    and (not self.xy_known
                         or math.hypot(x - self.x, y - self.y) > self._r("min_travel", 1.0))):
                self._e(-self._r("len", 0.8), self._r("speed", 30) * 60)
                self.retracted = True
            safe = max(self.z, z, self.tower_top + self.st["z_lift"])
            self.w(f"G1 Z{safe:.3f} F{self.travel_f:.0f}")
            self.z = safe
            if not same_xy:
                self.w(f"G1 X{x:.3f} Y{y:.3f} F{self.travel_f:.0f}")
            self.w(f"G1 Z{z:.3f} F{self.travel_f:.0f}")
            self.need_entry = False
        else:
            dist = math.hypot(x - self.x, y - self.y)
            long_travel = dist > self._r("min_travel", 1.0) and not (soft and dist <= LINK_MAX)
            if long_travel and not self.retracted and not self.stream_ret and self._r("len", 0.8) > 0:
                self._e(-self._r("len", 0.8), self._r("speed", 30) * 60)
                self.retracted = True
            hop = max(self._r("zhop", 0.0) if long_travel else 0.0, self.min_hop)
            top = max(self.z, z) + hop
            if not same_xy and top < self.tower_top - 1e-5:
                top = self.tower_top + max(hop, 0.15)
            if top > self.z + 1e-5:
                self.w(f"G1 Z{top:.3f} F{self.travel_f:.0f}")
            if not same_xy:
                self.w(f"G1 X{x:.3f} Y{y:.3f} F{self.travel_f:.0f}")
            if abs(top - z) > 1e-5:
                self.w(f"G1 Z{z:.3f} F{self.travel_f:.0f}")
        self.x, self.y, self.z = x, y, z
        self.xy_known = True

    def wipe(self, length, de, feed):
        if self.last_seg is None:
            self._e(de, feed)
            return
        dx, dy = self.last_seg[0] - self.x, self.last_seg[1] - self.y
        d = math.hypot(dx, dy)
        if d < 0.05:
            self._e(de, feed)
            return
        k = min(length, d) / d
        self.ensure_abs()
        self.x, self.y = self.x + dx * k, self.y + dy * k
        if self.abs_e:
            self.new_e += de
            de = self.new_e
        self.w(f"G1 X{self.x:.3f} Y{self.y:.3f} E{de:.5f} F{feed:.0f}")

    def extrude(self, x, y, de, feed):
        self.settle()
        self.ensure_abs()
        if self.abs_e:
            self.new_e += de
            e = self.new_e
        else:
            e = de
        self.w(f"G1 X{x:.3f} Y{y:.3f} E{e:.5f} F{feed:.0f}")
        self.last_seg = (self.x, self.y)
        self.x, self.y = x, y
        self.tower_top = max(self.tower_top, self.z)

    def e_only(self, de, feed):
        self.settle()
        self._e(de, feed)

    def _e(self, de, feed):
        if self.abs_e:
            self.new_e += de
            self.w(f"G1 E{self.new_e:.5f} F{feed:.0f}")
        else:
            self.w(f"G1 E{de:.5f} F{feed:.0f}")

    def sync_e(self, orig_e):
        if self.abs_e and abs(self.new_e - orig_e) > 1e-6:
            self.w(f"G92 E{orig_e:.5f}")
        self.new_e = orig_e


def material_family(t: str) -> str:
    u = re.sub(r"[^A-Z0-9]", "", (t or "").upper())
    rules = [("PLA", "PLA"), ("PETG", "PETG"), ("PCTG", "PETG"), ("PET", "PETG"), ("ABS", "ABS/ASA"),
             ("ASA", "ABS/ASA"), ("TPU", "TPU"), ("TPE", "TPU"), ("FLEX", "TPU"), ("PVA", "SOLUBLE"),
             ("BVOH", "SOLUBLE"), ("HIPS", "HIPS"), ("PPA", "PA"), ("PA", "PA"), ("NYLON", "PA"),
             ("PC", "PC"), ("PP", "PP"), ("PE", "PE")]
    for prefix, fam in rules:
        if u.startswith(prefix):
            return fam
    return u or "UNKNOWN"


@dataclass
class Block:
    start: int
    end: int
    layer: int
    e_in: float = 0.0
    last_extrusion: int = -1
    last_print: int = -1
    empty: bool = False
    pre: bool = False


def in_rect(r, x, y):
    return r[0] <= x <= r[2] and r[1] <= y <= r[3]


def seg_hits_rect(r, x0, y0, x1, y1):
    if in_rect(r, x0, y0) or in_rect(r, x1, y1):
        return True
    t0, t1 = 0.0, 1.0
    dx, dy = x1 - x0, y1 - y0
    for p, q in ((-dx, x0 - r[0]), (dx, r[2] - x0), (-dy, y0 - r[1]), (dy, r[3] - y0)):
        if abs(p) < 1e-12:
            if q < 0:
                return False
        else:
            tt = q / p
            if p < 0:
                t0 = max(t0, tt)
            else:
                t1 = min(t1, tt)
            if t0 > t1:
                return False
    return True


def scaf_rects(lower, upper):
    low = {b.slot: b for b in lower}
    out = []
    for u in upper:
        l = low.get(u.slot)
        if l is None:
            out.append(Brick(u.x, u.y, u.nx, u.ny))
            continue
        if u.nx > l.nx:
            out.append(Brick(u.x + PITCH * l.nx, u.y, u.nx - l.nx, u.ny))
        if u.ny > l.ny:
            out.append(Brick(u.x, u.y + PITCH * l.ny, min(l.nx, u.nx), u.ny - l.ny))
    return out


def choose_brick_count(bricks, specs, by_layer, block_moves, last_tower, st, fil_area, tight=1.0):
    budget = sum(mv["eff"] for mv in block_moves.values() if mv["has_xy"] and not mv["custom"])
    sample = {}

    def key_of(s):
        return ("socket" if s.region == "cavity" and s.v < SOCKET_D else s.region, s.spacer, s.spacer_top,
                s.first, s.spacer_last, s.spacer_ring_clearance, s.spacer_cap, round(s.stud_tip_inset, 5))

    for li in sorted(specs):
        s = specs[li]
        sample.setdefault(key_of(s), s)

    def measure(s, cells, key):
        base = layer_paths(s, cells, st, ("tubes", 0.0), IFACE_D if s.spacer_top else SPACER_MIN)
        full_style = ("holes", 1.0) if key[0] in ("socket", "cavity") else ("tubes", 0.0)
        full = layer_paths(s, cells, st, full_style, 1.0)
        c_lo = LayerJob(base, fil_area).capacity() / max(s.t, 1e-6)
        c_hi = LayerJob(full, fil_area, st["max_hidden_flow"]).capacity() / max(s.t, 1e-6)
        return c_lo, max(c_hi, c_lo)

    table = {}

    def piece_caps(nx, ny, key):
        k = (nx, ny, key)
        if k not in table:
            table[k] = measure(sample[key], [Brick(0.0, 0.0, nx, ny)], key)
        return table[k]

    def caps(cells):
        lo, hi = {}, {}
        for key, s in sample.items():
            if s.first:
                lo[key], hi[key] = measure(s, cells, key)
            else:
                lo[key] = sum(piece_caps(b.nx, b.ny, key)[0] for b in cells)
                hi[key] = sum(piece_caps(b.nx, b.ny, key)[1] for b in cells)
        return lo, hi

    opts = sorted({o for (a, b) in parse_sizes(st["brick_sizes"]) for o in ((a, b), (b, a))})
    ref = next((k for k in sample if k[0] == "socket" and not k[3]), None) or \
        next((k for k in sample if not k[3]), None) or next(iter(sample))
    state = [(b.nx, b.ny) for b in bricks]
    seq = [list(state)]
    while any(state):
        best = None
        for i, cur in enumerate(state):
            if cur is None:
                continue
            c0 = piece_caps(cur[0], cur[1], ref)[0]
            smaller = [o for o in opts if o[0] <= cur[0] and o[1] <= cur[1] and o[0] * o[1] < cur[0] * cur[1]]
            for o in smaller + [None]:
                loss = c0 - (piece_caps(o[0], o[1], ref)[0] if o else 0.0)
                rank = (loss, -(o[0] * o[1] if o else 0), i)
                if best is None or rank < best[0]:
                    best = (rank, i, o)
        state[best[1]] = best[2]
        seq.append(list(state))
    seq.reverse()
    sets = [[Brick(bricks[i].x, bricks[i].y, s[0], s[1], slot=i) for i, s in enumerate(stt) if s] for stt in seq]

    N = len(sets) - 1
    n_el = max(s.elem for s in specs.values()) + 1
    cells_of = dict(enumerate(sets))
    cap_of = {n: caps(cells_of[n]) for n in range(1, N + 1)}
    by_elem = {}
    for li, s in specs.items():
        by_elem.setdefault(s.elem, []).append(li)

    def elem_cost(e, n):
        if n == 0:
            return True, 0.0, 0.0
        lo, hi = cap_of[n]
        est = finish = 0.0
        for li in by_elem.get(e, []):
            s = specs[li]
            need = sum(b.e_in for b in by_layer.get(li, []))
            k = key_of(s)
            if need > hi[k] * s.t + 1e-6:
                return False, 0.0, 0.0
            use = max(need, lo[k] * s.t)
            est += use
            if li > last_tower:
                finish += use
        return True, est, finish

    need_n = []
    for e in range(n_el):
        m = next((n for n in range(1, N + 1) if elem_cost(e, n)[0]), N)
        need_n.append(m)

    w = st["line_width"]
    t_ref = min(s.t for s in specs.values())

    def scaf_rate(cells):
        if not cells:
            return 0.0
        return LayerJob(scaffold_paths(cells, 0.0, t_ref, SCAFFOLD_D, w, True), fil_area).capacity() / t_ref

    def plan_cost(counts):
        est = finish = 0.0
        for e, c in enumerate(counts):
            _, a, f = elem_cost(e, c)
            above = max(counts[e + 1:], default=0)
            if above > c:
                extra = scaf_rate(scaf_rects(cells_of[c], cells_of[above]))
                a += extra * sum(specs[li].t for li in by_elem.get(e, []))
            est += a
            finish += f
        return est, finish

    def scaffold_holds(e, counts):
        above = max(counts[e + 1:], default=0)
        if above == 0:
            return False
        cells = cells_of[above]
        rate = LayerJob(scaffold_paths(cells, 0.0, t_ref, SCAFFOLD_D, w, True), fil_area,
                        st["max_hidden_flow"]).capacity() / t_ref
        return all(sum(b.e_in for b in by_layer.get(li, [])) <= rate * specs[li].t
                   for li in by_elem.get(e, []))

    def within(counts):
        est, finish = plan_cost(counts)
        return est <= (budget + finish) * tight

    chosen = None
    for n in range(N, 0, -1):
        counts = [max(m, n) for m in need_n]
        if within(counts):
            chosen = counts
            break
    if chosen is None:
        chosen = list(need_n)
        purge_of = {e: sum(sum(b.e_in for b in by_layer.get(li, [])) for li in by_elem.get(e, []))
                    for e in range(n_el)}
        for e in sorted(range(n_el - 1), key=lambda q: purge_of[q]):
            if within(chosen):
                break
            trial = chosen[:]
            trial[e] = 0
            if scaffold_holds(e, trial):
                chosen = trial
    if os.environ.get("BRICKTOWER_DEBUG"):
        print(f"budget {budget:.0f} mm, bricks available {N}", file=sys.stderr)
        for e in range(n_el):
            purge = sum(sum(b.e_in for b in by_layer.get(li, [])) for li in by_elem.get(e, []))
            worst = max(by_elem.get(e, [0]), key=lambda li: sum(b.e_in for b in by_layer.get(li, [])))
            row = " ".join(f"n{n}:{'ok' if elem_cost(e, n)[0] else '--'}/{elem_cost(e, n)[1]:.0f}"
                           for n in range(1, N + 1))
            print(f"  level {e}: purge {purge:.0f} mm, peak layer {sum(b.e_in for b in by_layer.get(worst, [])):.1f} mm "
                  f"({key_of(specs[worst])[0]}), needs {need_n[e]}, uses {chosen[e]} | {row}", file=sys.stderr)
    elem_cells = [cells_of[c] for c in chosen]
    scaf_cells = []
    for e, c in enumerate(chosen):
        above = max(chosen[e + 1:], default=0)
        scaf_cells.append(scaf_rects(cells_of[c], cells_of[above]) if above > c else [])
    return elem_cells, scaf_cells


def style_name(sty):
    sock, fill = sty
    parts = ["hollow" if fill == 0 else ("solid" if fill >= 1 else f"{fill*100:.0f}% filled")]
    if sock == "holes":
        parts.append("socket plate")
    return ", ".join(parts)


def elem_label(e):
    if e.get("pieces", 1) == 0:
        return "scaffold"
    theme = e.get("theme", "rect")
    if theme == "slope":
        return "slope"
    if theme == "round":
        return "round " + e["kind"]
    return e["kind"]


def _describe(bricks, elements, styles_used):
    kinds = {}
    for b in bricks:
        key = f"{min(b.nx, b.ny)}x{max(b.nx, b.ny)}"
        kinds[key] = kinds.get(key, 0) + 1
    elem_desc = ", ".join(f"{elem_label(e)} ({style_name(styles_used[n])}{'' if e['complete'] else ', unfinished'})"
                          for n, e in enumerate(elements))
    brick_desc = ", ".join(f"{n} x {k}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]))
    return brick_desc, elem_desc


def piece_counts(elements):
    counts = {}
    for e in elements:
        if e.get("pieces", 0) == 0:
            continue
        lab = elem_label(e)
        counts[lab] = counts.get(lab, 0) + e.get("pieces", 0)
    total = sum(counts.values())
    parts = ", ".join(f"{n} {lab}{'s' if n != 1 else ''}" for lab, n in counts.items())
    return total, parts


def preview_piece_levels(elements, elem_cells):
    levels = []
    for level, (element, cells) in enumerate(zip(elements, elem_cells), 1):
        counts = {}
        for piece in theme_pieces(cells, element["theme"]):
            kind = element["kind"]
            if piece.shape == "slope":
                kind = "45° slope"
            elif piece.shape == "round":
                kind = "round " + kind
            width, length = sorted((piece.nx, piece.ny))
            label = f"{width}×{length} {kind}"
            counts[label] = counts.get(label, 0) + 1
        if counts:
            unfinished = " (unfinished)" if not element["complete"] else ""
            levels.append((level, [f"{count} × {label}{unfinished}" for label, count in counts.items()]))
    return levels


def process_gcode(text: str, raw_settings: dict | None = None, config_lookup=None):
    st = load_settings(raw_settings)
    if not st["enabled"]:
        raise Skip("disabled in settings")
    lines = text.splitlines(keepends=True)
    if MARK in text:
        raise Skip("file was already processed")

    cfg = {}
    for ln in lines:
        if ln.startswith(";") and "=" in ln:
            m = CONFIG_RE.match(ln)
            if m:
                cfg.setdefault(m.group(1), m.group(2))
    def conf(key, default=None):
        if key in cfg:
            return cfg[key]
        if config_lookup:
            try:
                v = config_lookup(key)
                if v is not None:
                    return str(v)
            except Exception:
                pass
        return default

    per_tool = str(conf("single_extruder_multi_material", "1")).strip() == "0"

    def flist(key, default):
        try:
            vals = [float(x) for x in re.split(r"[;,]", conf(key, "") or "") if x.strip()]
        except ValueError:
            vals = []
        return vals or [default]
    st["_ret"] = {"len": flist("retraction_length", 0.8), "speed": flist("retraction_speed", 30.0),
                  "despeed": flist("deretraction_speed", 0.0), "min_travel": flist("retraction_minimum_travel", 1.0),
                  "zhop": flist("z_hop", 0.0)}
    try:
        tv = float(re.split(r"[;,]", conf("travel_speed", "") or "")[0])
        st["_travel_f"] = tv * 60 if tv > 0 else None
    except ValueError:
        st["_travel_f"] = None
    try:
        slicer_brim = float(str(conf("prime_tower_brim_width", "0")).split(",")[0])
    except ValueError:
        slicer_brim = 0.0
    if st["brim_loops"] > 0:
        st["_brim_n"] = max(st["brim_loops"], int(round(max(slicer_brim, 0.0) / st["line_width"])))
    else:
        st["_brim_n"] = 0

    orig = Tracker(per_tool)
    feature = ""
    tag_tower = False
    tag_brim = False
    in_custom = False
    layer = -1
    layers: list[list] = []
    blocks: list[Block] = []
    cur_block: Block | None = None
    tools_used = set()
    style_bbl = False
    tower_pts = []
    brim_lines = set()
    block_moves = {}
    custom_lines = set()
    cp_lines = set()
    in_cp = False
    kinds = bytearray(len(lines))

    for idx, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith(";"):
            if LAYER_TAG_RE.match(s):
                layer += 1
                layers.append([None, None, idx, None])
                feature = ""
            elif layer >= 0 and (m := Z_TAG_RE.match(s)) and layers[layer][0] is None:
                layers[layer][0] = float(m.group(1))
            elif layer >= 0 and (m := H_TAG_RE.match(s)) and layers[layer][1] is None:
                layers[layer][1] = float(m.group(1))
            elif (m := FEATURE_RE.match(s)):
                feature = m.group(2).strip().lower()
                if m.group(1).upper() == "FEATURE":
                    style_bbl = True
            elif WT_START_RE.match(s):
                tag_tower = True
            elif WT_BRIM_START_RE.match(s):
                tag_brim = True
            elif WT_BRIM_END_RE.match(s):
                tag_brim = False
            elif CP_START_RE.match(s):
                in_cp = True
            elif CP_END_RE.match(s):
                in_cp = False
        if CUSTOM_START_RE.match(s):
            in_custom = True
        cmd = parse_cmd(s)
        if cmd and cmd.letter == "T":
            tools_used.add(cmd.num)
        tower_region = tag_tower or tag_brim or feature in TOWER_FEATURES
        if WT_END_RE.match(s):
            tower_region = True
        mv = orig.feed(cmd)
        if in_custom:
            pass
        elif not s or (s.startswith(";") and not LAYER_TAG_RE.match(s)):
            kinds[idx] = 1
        elif mv is not None:
            if mv["de"] == 0:
                kinds[idx] = 4 if mv["has_xy"] else (3 if mv["has_z"] else 1)
            elif mv["de"] > 0 and not mv["has_xy"] and not mv["has_z"]:
                kinds[idx] = 2
        elif cmd is not None and cmd.letter == "M" and cmd.num == 73:
            kinds[idx] = 1
        if mv and layer >= 0 and mv["has_xy"] and layers[layer][3] is None and not tower_region and not in_custom:
            layers[layer][3] = idx
        if layer >= 0 and tower_region:
            if cur_block is None or cur_block.layer != layer:
                cur_block = Block(idx, idx + 1, layer)
                blocks.append(cur_block)
            cur_block.end = idx + 1
            if tag_brim:
                brim_lines.add(idx)
            if in_cp:
                cp_lines.add(idx)
            if mv:
                mv["custom"] = in_custom
                mv["brim"] = tag_brim
                mv["cp"] = in_cp
                block_moves[idx] = mv
                if mv["has_xy"] and mv["de"] > 0 and not in_custom and not tag_brim and layer >= 1:
                    tower_pts.append((mv["ex"], mv["ey"]))
            if in_custom:
                custom_lines.add(idx)
        else:
            cur_block = None
        if in_custom:
            custom_lines.add(idx)
        if CUSTOM_END_RE.match(s):
            in_custom = False
        if WT_END_RE.match(s):
            tag_tower = False
            cur_block = cur_block

    if not blocks:
        raise Skip("no prime tower found in this G-code")

    zs = []
    prev = 0.0
    for L in layers:
        if L[0] is None:
            L[0] = prev + (L[1] or 0.2)
        if L[1] is None:
            L[1] = L[0] - prev if L[0] > prev else 0.2
        zs.append((L[0], L[1]))
        prev = L[0]

    ftypes = [x.strip() for x in re.split(r"[;,]", conf("filament_type", "") or "") if x.strip()]
    used = sorted(tools_used) or [0]
    fams = sorted({material_family(ftypes[t]) for t in used if t < len(ftypes)})
    if not st["allow_mixed_materials"] and len(fams) > 1:
        raise Skip("print mixes filament families that may not bond (" + " + ".join(fams) +
                   "); tick 'Allow mixed materials' to build bricks anyway")

    rot = conf("wipe_tower_rotation_angle", "0") or "0"
    try:
        if abs(float(rot.split(",")[0])) > 1e-3:
            raise Skip("rotated prime towers are not supported yet (set rotation to 0)")
    except ValueError:
        pass

    try:
        fil_d = [float(x) for x in re.split(r"[;,]", conf("filament_diameter", "1.75")) if x.strip()]
    except ValueError:
        fil_d = [1.75]
    fil_area = math.pi * (fil_d[0] / 2) ** 2

    if not tower_pts:
        tower_pts = [(block_moves[i]["ex"], block_moves[i]["ey"]) for i in block_moves
                     if block_moves[i]["de"] > 0 and block_moves[i]["has_xy"] and not block_moves[i]["custom"]
                     and not block_moves[i]["brim"]]
    if not tower_pts:
        raise Skip("could not find prime tower extrusions")
    xs = sorted(p[0] for p in tower_pts)
    ys = sorted(p[1] for p in tower_pts)
    g = st["footprint_grow"]
    foot = (xs[0] - g, ys[0] - g, xs[-1] + g, ys[-1] + g)
    fw, fh = foot[2] - foot[0], foot[3] - foot[1]
    sizes = parse_sizes(st["brick_sizes"])
    cells = pack_bricks(fw + 2 * EDGE, fh + 2 * EDGE, sizes, st["brick_gap"])
    if not cells:
        raise Skip(f"prime tower footprint {fw:.1f} x {fh:.1f} mm is too small for a "
                   f"{min(sizes, key=lambda s: s[0]*s[1])[0]}x{min(sizes, key=lambda s: s[0]*s[1])[1]} brick "
                   "(raise prime tower width or footprint_grow)")
    px1 = max(x + PITCH * nx for (x, y, nx, ny) in cells)
    py1 = max(y + PITCH * ny for (x, y, nx, ny) in cells)
    ox = foot[0] - EDGE + (fw + 2 * EDGE - px1) / 2
    oy = foot[1] - EDGE + (fh + 2 * EDGE - py1) / 2
    bricks = [Brick(ox + x, oy + y, nx, ny) for (x, y, nx, ny) in cells]
    brim = st["_brim_n"] * st["line_width"]
    guard = (min(b.outer[0] for b in bricks) - brim - 1.0, min(b.outer[1] for b in bricks) - brim - 1.0,
             max(b.outer[2] for b in bricks) + brim + 1.0, max(b.outer[3] for b in bricks) + brim + 1.0)
    claim = (min(foot[0], guard[0]) - 2.0, min(foot[1], guard[1]) - 2.0,
             max(foot[2], guard[2]) + 2.0, max(foot[3], guard[3]) + 2.0)

    has_cp = bool(cp_lines)

    def classify(mv):
        if mv["custom"]:
            return "keep"
        if mv["has_xy"]:
            if mv["brim"] or in_rect(claim, mv["ex"], mv["ey"]):
                if mv["de"] < 0:
                    return "retract"
                if mv["de"] == 0:
                    return "drop"
                required = not mv["brim"] and (mv["cp"] or not has_cp)
                return "pour" if required else "filler"
            return "keep"
        if mv["has_e"] and not mv["has_z"]:
            return "keep"
        if mv["has_z"]:
            if mv["ez"] > mv["sz"] + 1e-6:
                return "keep"
            return "zdrop" if in_rect(claim, mv["sx"], mv["sy"]) else "keep"
        return "keep"

    tag_end_re = re.compile(r"^;\s*(WIPE_TOWER_END|WIPE_TOWER_BRIM_END|CP .* END)\b")
    for b in blocks:
        last_rel = None
        for i in range(b.start, b.end):
            mv = block_moves.get(i)
            if mv and classify(mv) == "pour" and mv["eff"] > 0:
                b.e_in += mv["eff"]
                b.last_extrusion = i
            if mv and mv["has_xy"] and mv["de"] > 0 and classify(mv) in ("pour", "filler"):
                b.last_print = i
            if (mv and classify(mv) != "keep") or tag_end_re.match(lines[i].strip()):
                last_rel = i
        if last_rel is not None:
            b.end = last_rel + 1
        b.empty = last_rel is None
    blocks = [b for b in blocks if not b.empty]
    prev_end = 0
    for b in sorted(blocks, key=lambda q: q.start):
        k, first, n = b.start - 1, b.start, 0
        while k >= prev_end and n < 400 and kinds[k] in (1, 2, 3, 4):
            if kinds[k] != 1:
                first = k
            k -= 1
            n += 1
        if first < b.start:
            b.start = first
            b.pre = True
        prev_end = b.end

    last_tower = max(b.layer for b in blocks if b.e_in > 0) if any(b.e_in > 0 for b in blocks) else 0
    n_layers = len(zs)
    max_layer = n_layers - 1 if st["finish_last_brick"] else last_tower
    min_t = min(t for (_, t) in zs[: last_tower + 1]) if zs else 0.2
    gap = max(0.0, min(st["breakaway_gap"], 1.5 * min_t))

    by_layer = {}
    for b in blocks:
        by_layer.setdefault(b.layer, []).append(b)
    budget_e = sum(mv["eff"] for mv in block_moves.values() if mv["has_xy"] and not mv["custom"])
    mode = str(st["brick_style"]).strip().lower()
    purge_by_layer = {li: sum(b.e_in for b in group) for li, group in by_layer.items()}

    def need_of(li):
        return purge_by_layer.get(li, 0.0)

    def cap_of(paths, k=1.0):
        return LayerJob(paths, fil_area, k).capacity()

    capacities = {}

    def height_layer_cost(spec):
        socket_layer = spec.region == "cavity" and spec.v < SOCKET_D
        key = (spec.region, socket_layer, round(spec.t, 6), spec.idx % 2, spec.deck_n % 2,
               spec.first, spec.spacer, spec.spacer_top, spec.spacer_last, spec.spacer_ring_clearance,
               spec.spacer_cap, round(spec.stud_tip_inset, 5),
               gap >= spec.t - 1e-6)
        if key not in capacities:
            hollow = ("holes", 1.0) if mode == "solid" else ("tubes", 0.0)
            full = hollow if mode in ("authentic", "solid") else ("holes", 1.0)
            min_paths = layer_paths(spec, bricks, st, hollow, IFACE_D if spec.spacer_top else SPACER_MIN)
            max_paths = layer_paths(spec, bricks, st, full, 1.0)
            capacities[key] = cap_of(min_paths), cap_of(max_paths, st["max_hidden_flow"])
        low, high = capacities[key]
        need = need_of(spec.idx)
        return max(0.0, need - high), max(need, low)

    def solve_density(spec, pieces, socket, which, lo, scaf, socket_support=()):
        need = need_of(spec.idx)
        build = {"fill": lambda d: layer_paths(spec, pieces, st, (socket, d), 1.0, scaf),
                 "spacer": lambda d: layer_paths(spec, pieces, st, (socket, 0.0), d, scaf, socket_support),
                 "socket": lambda d: layer_paths(spec, pieces, st, (socket, 0.0, d), 1.0, scaf)}[which]
        p_lo = build(lo)
        c_lo = cap_of(p_lo)
        if need <= c_lo + 1e-9:
            return p_lo, lo
        p_hi = build(1.0)
        c_hi = cap_of(p_hi)
        if c_hi <= c_lo + 1e-9:
            return p_hi, 1.0
        d = lo + (1.0 - lo) * (need - c_lo) / (c_hi - c_lo)
        if d >= 0.97:
            return p_hi, 1.0
        d = min(1.0, max(lo, d * 1.03))
        return build(d), d

    def build_plan(schedule):
        specs, elements, _ = schedule
        n_el = len(elements)
        elem_cells, scaf_cells = choose_brick_count(bricks, specs, by_layer, block_moves, last_tower, st, fil_area)

        def plan_element(e_idx, pieces, socket_support):
            e_layers = sorted(li for li, s in specs.items() if s.elem == e_idx)
            scaf = scaf_cells[e_idx]
            socket = "holes" if mode == "solid" else "tubes"
            sock_layers = [li for li in e_layers if specs[li].region == "cavity" and specs[li].v < SOCKET_D]
            if socket == "tubes" and mode != "authentic":
                for li in sock_layers:
                    if need_of(li) > cap_of(layer_paths(specs[li], pieces, st, ("tubes", 0.0), 1.0, scaf)) + 1e-9:
                        socket = "holes"
                        break
            res = {}
            dens = {}
            for li in e_layers:
                spec = specs[li]
                if spec.region == "cavity" and spec.v >= SOCKET_D and mode != "authentic":
                    if mode == "solid":
                        paths, d = layer_paths(spec, pieces, st, (socket, 1.0), 1.0, scaf), 1.0
                    else:
                        paths, d = solve_density(spec, pieces, socket, "fill", 0.0, scaf)
                    dens[li] = d
                elif spec.region == "cavity" and spec.v < SOCKET_D and socket == "holes" and mode != "solid":
                    paths, _ = solve_density(spec, pieces, socket, "socket", SOCK_MIN, scaf)
                elif spec.spacer:
                    paths, _ = solve_density(spec, pieces, socket, "spacer",
                                             IFACE_D if spec.spacer_top else SPACER_MIN, scaf, socket_support)
                else:
                    paths = layer_paths(spec, pieces, st, (socket, 0.0), IFACE_D if spec.spacer_top else SPACER_MIN, scaf)
                res[li] = paths
            if any(d > 0 for d in dens.values()):
                for li, d in dens.items():
                    if d < FILL_MIN:
                        res[li] = layer_paths(specs[li], pieces, st, (socket, FILL_MIN), 1.0, scaf)
                        dens[li] = FILL_MIN
            out = {}
            k_worst = (1.0, e_layers[0])
            for li in e_layers:
                paths = res[li]
                need = need_of(li)
                cap = cap_of(paths)
                k = 1.0
                if need > cap + 1e-9:
                    ci = cap_of([p for p in paths if p.kind in INTERNAL_KINDS])
                    k = 1.0 + (need - cap) / ci if ci > 0 else float("inf")
                out[li] = (paths, k)
                if k > k_worst[0]:
                    k_worst = (k, li)
            avg_d = sum(dens.values()) / len(dens) if dens else 0.0
            return out, (socket, avg_d), k_worst

        top_e = n_el - 1
        top_spacer = any(s.spacer for s in specs.values() if s.elem == top_e)
        allow_top_slope = n_el > 1 and elements[-1]["kind"] == "brick" and st["allow_slopes"] and not top_spacer
        first_round = max(1, n_el - max(1, n_el // 3)) if st["allow_round"] else n_el

        def choose_shape(e_idx, rounds_above, socket_support):
            cells = elem_cells[e_idx]
            if e_idx == top_e and allow_top_slope and any(b.nx == 2 or b.ny == 2 for b in cells):
                pieces = theme_pieces(cells, "slope")
                plan = plan_element(e_idx, pieces, socket_support)
                if plan[2][0] <= st["max_hidden_flow"]:
                    return "slope", pieces, plan, set()
            if cells and e_idx >= first_round and (e_idx == top_e or rounds_above):
                pieces = theme_pieces(cells, "round")
                footprints = {(b.x, b.y, b.nx, b.ny) for b in pieces}
                if rounds_above <= footprints:
                    plan = plan_element(e_idx, pieces, socket_support)
                    if plan[2][0] <= st["max_hidden_flow"]:
                        return "round", pieces, plan, footprints
            pieces = theme_pieces(cells, "rect")
            return "rect", pieces, plan_element(e_idx, pieces, socket_support), set()

        tight = 1.0
        for attempt in range(3):
            if attempt:
                elem_cells, scaf_cells = choose_brick_count(bricks, specs, by_layer, block_moves, last_tower, st,
                                                            fil_area, tight)
            chosen = []
            rounds_above = set()
            socket_support = set()
            for e_idx in reversed(range(n_el)):
                theme, pieces, planned, rounds_above = choose_shape(e_idx, rounds_above, socket_support)
                chosen.append((theme, len(pieces), planned))
                first_paths, _ = planned[0][elements[e_idx]["start"]]
                socket_support = set()
                for path in first_paths:
                    if path.kind in ("tube", "pin"):
                        points = path.pts[:-1] if len(path.pts) > 2 else path.pts
                        socket_support.add(tuple(round(sum(axis) / len(points), 4) for axis in zip(*points)))
            jobs = {}
            worst = (1.0, -1)
            styles_used = []
            for e_idx, (theme, piece_count, (out, sty, k_e)) in enumerate(reversed(chosen)):
                jobs.update(out)
                styles_used.append(sty)
                elements[e_idx]["theme"] = theme
                elements[e_idx]["pieces"] = piece_count
                if k_e[0] > worst[0]:
                    worst = k_e
            used = {li: LayerJob(p, fil_area, k).capacity() for li, (p, k) in jobs.items()}
            total = sum(used.values())
            allowed = budget_e + sum(c for li, c in used.items() if li > last_tower)
            if total <= allowed * 1.01:
                break
            tight *= allowed / total
        return jobs, elem_cells, styles_used, worst, total

    legacy = plan_layers(zs, last_tower, max_layer, st, gap)
    if not legacy[0]:
        raise Skip("nothing to plan")
    selected = legacy
    alternate = None
    if st["allow_medium_bricks"]:
        alternate = plan_layers(zs, last_tower, max_layer, st, gap, height_layer_cost)
    compare = alternate is not None and alternate[0] and alternate[1] != legacy[1]
    result = build_plan(legacy)
    if compare:
        trial = build_plan(alternate)

        def rank(schedule, plan):
            _, _, _, (peak, _), total = plan
            return (max(0.0, peak - st["max_hidden_flow"]),
                    not schedule[1][-1]["complete"], total, peak)

        if rank(alternate, trial) < rank(legacy, result):
            selected, result = alternate, trial
    specs, elements, notes = selected
    jobs, elem_cells, styles_used, worst, _ = result

    def purge_layer_report(li, capacity):
        spec = specs[li]
        phase = {"cavity": "brick body", "deck": "top deck", "studs": "studs"}[spec.region]
        if spec.region == "cavity" and spec.v < SOCKET_D:
            phase = "stud socket"
        if spec.spacer:
            phase += " and spacer"
        return (f"slicer Z={zs[li][0]:.2f} mm ({phase}) needs {need_of(li) * fil_area:.1f} mm^3. "
                f"The planned paths can hold {capacity * fil_area:.1f} mm^3 "
                f"at the {st['max_hidden_flow']:.0%} hidden-line flow limit.")

    if os.environ.get("BRICKTOWER_DEBUG"):
        for e_idx, el in enumerate(elements):
            ls = sorted(li for li, s in specs.items() if s.elem == e_idx)
            ks = [(li, jobs[li][1]) for li in ls]
            kmax = max(ks, key=lambda q: q[1])
            print(f"elem {e_idx} {el['kind']}/{el.get('theme')} layers {ls[0]}-{ls[-1]} "
                  f"Z {zs[ls[0]][0]:.2f}-{zs[ls[-1]][0]:.2f} style {styles_used[e_idx]} worst k {kmax[1]:.2f} "
                  f"at Z {zs[kmax[0]][0]:.2f} ({specs[kmax[0]].region}, v={specs[kmax[0]].v:.2f}, "
                  f"need {sum(b.e_in for b in by_layer.get(kmax[0], [])):.1f} mm)", file=sys.stderr)
    if worst[0] > st["max_hidden_flow"]:
        li = worst[1]
        if li not in jobs:
            raise Skip("Max flow on hidden lines must be at least 100%")
        capacity = cap_of(jobs[li][0], st["max_hidden_flow"])
        raise Skip(f"The layer at {purge_layer_report(li, capacity)} Make the prime tower bigger (wider tower, or a larger purge-line "
                   "spacing such as 'wipe_tower_extra_spacing'), lower flush volumes, or raise 'Max flow on hidden lines'")

    unmapped = [b.layer for b in blocks if b.e_in > 0 and b.layer not in specs]
    if unmapped:
        raise Skip(f"tower layers {unmapped[:5]} fall outside the brick plan")

    inject_at = {}
    for li in specs:
        if li not in by_layer:
            fl = layers[li][3]
            if fl is None:
                raise Skip(f"no place to print brick layer {li}")
            inject_at[fl] = li
    last_block_of_layer = {}
    for b in blocks:
        if b.layer in specs:
            last_block_of_layer[b.layer] = b

    out: list[str] = []
    em = Emitter(out, st)
    orig = Tracker(per_tool)
    tower_tag = "; FEATURE: Prime tower" if style_bbl else ";TYPE:Prime tower"
    cur_feature_line = None
    block_at = {b.start: b for b in blocks}
    injected_layers = 0
    brick_f = st["brick_speed"] * 60
    first_f = st["first_layer_speed"] * 60
    max_purge_f = st["max_purge_speed"] * 60
    live_jobs = {}

    def get_job(li):
        sp_ = specs[li]
        em.min_hop = (max(0.0, gap - sp_.t) + 0.15) if sp_.spacer else 0.0
        if li not in live_jobs:
            paths, k = jobs[li]
            live_jobs[li] = LayerJob(order_paths(paths), fil_area, k)
        return live_jobs[li]

    def stream_retracted():
        return orig.retract < -1e-6 or orig.fw_retracted

    def at_brick(job):
        if job is None or job.done() or not em.need_entry:
            return
        _, x0, y0, x1, y1, _, _, z = job.segs[job.si]
        em.goto_start(x0 + (x1 - x0) * job.frac, y0 + (y1 - y0) * job.frac, z)

    def next_abs_travel(idx):
        if not orig.abs_xyz:
            return None
        for k in range(idx, min(idx + 400, N)):
            c = parse_cmd(lines[k].strip())
            if c is None:
                continue
            if c.letter == "G" and c.num in (0, 1):
                w = c.words
                if "X" in w and "Y" in w:
                    return None if "E" in w else (w["X"], w["Y"])
                if "X" in w or "Y" in w:
                    return None
                continue
            if c.letter == "G" and c.num in (2, 3, 91, 92, 28):
                return None
        return None

    def end_block(next_idx, slope_len=0.0, slope_f=0.0):
        em.restore_rel()
        safe = max(em.z, em.tower_top + st["z_lift"])
        dest = next_abs_travel(next_idx)
        if dest is not None:
            em.settle()
            dist = math.hypot(dest[0] - em.x, dest[1] - em.y)
            if (slope_len > 0.5 and em.xy_known and dist > slope_len + 1.0 and orig.z > em.z + 1e-6
                    and em.z >= em.tower_top - 1e-6 and orig.z >= em.tower_top + 0.25 - 1e-6):
                k = slope_len / dist
                em.x, em.y, em.z = em.x + (dest[0] - em.x) * k, em.y + (dest[1] - em.y) * k, orig.z
                em.w(f"G1 X{em.x:.3f} Y{em.y:.3f} Z{em.z:.3f} F{slope_f or em.travel_f:.0f}")
            else:
                em.w(f"G1 Z{safe:.3f} F{em.travel_f:.0f}")
                em.z = safe
        else:
            dest_out = not in_rect(guard, orig.x, orig.y)
            far = math.hypot(orig.x - em.x, orig.y - em.y) > em._r("min_travel", 1.0)
            if dest_out and far and not stream_retracted() and not em.retracted and em._r("len", 0.8) > 0:
                em._e(-em._r("len", 0.8), em._r("speed", 30) * 60)
                em.retracted = True
            elif not dest_out:
                em.settle()
            em.w(f"G1 Z{safe:.3f} F{em.travel_f:.0f}")
            em.z = safe
            if orig.abs_xyz:
                em.w(f"G1 X{orig.x:.3f} Y{orig.y:.3f} F{em.travel_f:.0f}")
            else:
                em.w("G90")
                em.w(f"G1 X{orig.x:.3f} Y{orig.y:.3f} F{em.travel_f:.0f}")
                em.w("G91")
            if dest_out and orig.abs_xyz:
                em.w(f"G1 Z{orig.z:.3f} F{em.travel_f:.0f}")
                em.z = orig.z
            em.settle()
        em.sync_e(orig.e)
        em.w(f"G1 F{orig.f:.0f}")
        em.w(f"{MARK} END z_restore={orig.z:.4f} top={em.tower_top:.4f}")

    E_ONLY_RE = re.compile(r"^\s*G1\s+E(-?\d*\.?\d+)\s*(F[\d.]+)?\s*(;.*)?$")

    def tidy_block(start, job):
        if em.abs_e:
            return
        seg = out[start:]
        code = [k for k, ln in enumerate(seg) if ln.strip() and
                (not ln.lstrip().startswith(";") or "WIPE_TOWER" in ln or "; CP " in ln)]
        drop = set()
        for a, b2 in zip(code, code[1:]):
            if a in drop:
                continue
            ma, mb = E_ONLY_RE.match(seg[a].strip()), E_ONLY_RE.match(seg[b2].strip())
            if ma and mb and float(ma.group(1)) < 0 and abs(float(ma.group(1)) + float(mb.group(1))) < 1e-6:
                drop |= {a, b2}
        if job is None:
            rest = [k for k in code if k not in drop]
            eo = [(k, E_ONLY_RE.match(seg[k].strip())) for k in rest]
            moves = any(re.match(r"^\s*G[0-3]\s+.*[XY]", seg[k]) for k in rest)
            if not moves and abs(sum(float(m.group(1)) for _, m in eo if m)) < 1e-6:
                drop |= {k for k, m in eo if m}
        if drop:
            out[start:] = [ln for k, ln in enumerate(seg) if k not in drop]

    def complete(li, retract_level, fw_retracted):
        job = get_job(li)
        if job.done():
            return
        em.stream_ret = fw_retracted or retract_level < -1e-6
        at_brick(job)
        em.stream_ret = False
        if fw_retracted:
            em.w("G11")
        elif retract_level < -1e-6:
            em.e_only(-retract_level, 2400)
        job.finish(em, first_f if (li == 0 or specs[li].elem_first) else brick_f)
        if fw_retracted:
            em.w("G10")
        elif retract_level < -1e-6:
            em.e_only(retract_level, 2400)
        em.stream_ret = fw_retracted or retract_level < -1e-6

    i = 0
    N = len(lines)
    while i < N:
        if i in inject_at:
            li = inject_at[i]
            em.begin(orig)
            em.w(f"{MARK} BEGIN injected layer={li}")
            em.w(tower_tag)
            complete(li, orig.retract, orig.fw_retracted)
            if cur_feature_line:
                em.w(cur_feature_line.rstrip("\n"))
            end_block(i)
            injected_layers += 1
        if i in block_at:
            b = block_at[i]
            job = get_job(b.layer) if b.layer in specs else None
            em.begin(orig)
            em.w(f"{MARK} BEGIN layer={b.layer}")
            out_start = len(out)
            is_last = last_block_of_layer.get(b.layer) is b
            has_t = any(lines[j].lstrip()[:1] in "Tt" and parse_cmd(lines[j].strip()) is not None
                        and parse_cmd(lines[j].strip()).letter == "T" for j in range(b.start, b.end))
            approach = job is not None and (not has_t or b.pre)
            print_now = False
            slope_len, slope_f = 0.0, 0.0
            for j in range(b.start, b.end):
                ln = lines[j]
                s = ln.strip()
                cmd = parse_cmd(s)
                if FEATURE_RE.match(s):
                    cur_feature_line = ln
                e_before = orig.e
                was_ret = stream_retracted()
                mv = orig.feed(cmd)
                if mv is not None:
                    mv["custom"] = j in custom_lines
                    mv["brim"] = j in brim_lines
                    mv["cp"] = j in cp_lines
                    action = classify(mv)
                    if action == "pour" and job is None:
                        action = "filler"
                    em.stream_ret = was_ret
                    if action == "keep" and not mv["custom"] and job is not None and not job.done():
                        if approach and mv["de"] == 0 and (mv["has_xy"] or (mv["has_z"] and mv["ez"] < mv["sz"])):
                            em.need_entry = True
                            continue
                        if not mv["has_xy"] and not mv["has_z"] and mv["de"] > 0:
                            at_brick(job)
                            approach = False
                            if is_last and b.last_extrusion < 0 and not stream_retracted():
                                print_now = True
                    if action in ("pour", "filler") and mv["refill"] > 1e-9:
                        at_brick(job)
                    if action == "pour":
                        approach = False
                    if (action == "keep" and mv["has_z"] and not mv["has_xy"] and orig.abs_xyz
                            and mv["ez"] < em.tower_top + 0.05 and in_rect(guard, em.x, em.y)):
                        em.need_entry = True
                        em.stream_ret = stream_retracted()
                        continue
                    if action == "keep":
                        em.settle()
                        em.restore_rel()
                        em.sync_e(e_before)
                        out.append(ln)
                        em.new_e = orig.e
                        if orig.abs_xyz:
                            if mv["has_xy"]:
                                em.x, em.y = mv["ex"], mv["ey"]
                            if mv["has_z"]:
                                em.z = mv["ez"]
                        else:
                            em.x += mv["ex"] - mv["sx"]
                            em.y += mv["ey"] - mv["sy"]
                            em.z += mv["ez"] - mv["sz"]
                        if mv["has_xy"] or mv["has_z"]:
                            em.need_entry = True
                        if mv["has_xy"] and orig.abs_xyz and "X" in cmd.words and "Y" in cmd.words:
                            em.xy_known = True
                        if print_now:
                            print_now = False
                            complete(b.layer, 0.0, False)
                    elif action == "filler":
                        if mv["refill"] > 1e-9:
                            em.e_only(mv["refill"], 1800)
                    elif action == "pour":
                        t_min = mv["len"] / max(mv["f"], 1.0)
                        rate = mv["de"] / t_min if t_min > 0 else 2400.0
                        if mv["refill"] > 1e-9:
                            em.e_only(mv["refill"], min(max(rate, 60.0), 9000.0))
                        em.stream_ret = False
                        if mv["eff"] > 1e-9:
                            feed = min(max(rate / job.current_epm(), 300.0), max_purge_f)
                            if b.layer == 0 or specs[b.layer].elem_first:
                                feed = min(feed, first_f)
                            left = job.pour(em, mv["eff"], feed)
                            if left > 1e-4:
                                em.e_only(left, 1200)
                        if is_last and j == b.last_extrusion:
                            complete(b.layer, 0.0, False)
                    elif action == "retract":
                        t_min = mv["len"] / max(mv["f"], 1.0)
                        rate = abs(mv["de"]) / t_min if t_min > 0 else mv["f"]
                        if (mv["has_xy"] and b.last_print >= 0 and j > b.last_print and job is not None
                                and not em.need_entry and not em.retracted):
                            em.wipe(mv["len"], mv["de"], mv["f"])
                        else:
                            em.e_only(mv["de"], min(max(rate, 60.0), 9000.0))
                    elif (action == "drop" and mv["has_z"] and mv["ez"] > mv["sz"] + 1e-6
                          and b.last_print >= 0 and j > b.last_print):
                        slope_len += mv["len"]
                        slope_f = mv["f"]
                    em.stream_ret = stream_retracted()
                    continue
                if s and not s.startswith(";"):
                    em.settle()
                if cmd and cmd.letter == "G" and cmd.num == 92 and "E" in cmd.words:
                    out.append(ln)
                    em.new_e = orig.e
                    continue
                if cmd and cmd.letter == "T":
                    em.tool = cmd.num
                    safe = em.tower_top + st["z_lift"]
                    if em.z < safe - 1e-6:
                        em.ensure_abs()
                        em.w(f"G1 Z{safe:.3f} F{em.travel_f:.0f}")
                        em.z = safe
                    em.restore_rel()
                    em.need_entry = True
                    em.xy_known = False
                    approach = job is not None and not job.done()
                out.append(ln)
            if is_last and b.last_extrusion < 0 and job is not None:
                complete(b.layer, orig.retract, orig.fw_retracted)
            tidy_block(out_start, job)
            end_block(b.end, slope_len, slope_f)
            i = b.end
            continue
        ln = lines[i]
        s = ln.strip()
        if FEATURE_RE.match(s):
            cur_feature_line = ln
        orig.feed(parse_cmd(s))
        out.append(ln)
        i += 1

    final = guard_pass(out, guard, st)

    brick_desc, elem_desc = _describe(max(elem_cells, key=len), elements, styles_used)
    try:
        dens_g = float(re.split(r"[;,]", conf("filament_density", "1.24") or "1.24")[0]) / 1000.0
    except ValueError:
        dens_g = 1.24 / 1000.0
    to_g = fil_area * dens_g
    tower_g = sum(mv["eff"] for mv in block_moves.values() if mv["has_xy"] and not mv["custom"]) * to_g
    total_e = finish_e = 0.0
    limiting = (0.0, None, 0.0)
    limit_k = st["max_hidden_flow"]
    for li, (paths, k) in jobs.items():
        job = LayerJob(paths, fil_area)
        base = job.capacity()
        hidden = sum(length * epm for pid, _, _, _, _, length, epm, _ in job.segs
                     if job.meta[pid][0] in INTERNAL_KINDS)
        used = base + hidden * (k - 1.0)
        total_e += used
        if li > last_tower:
            finish_e += used
        need = need_of(li)
        if need > 0:
            available = base + hidden * (limit_k - 1.0)
            utilization = need / available if available > 0 else float("inf")
            if utilization > limiting[0]:
                limiting = (utilization, li, available)
    brick_g, finish_g = total_e * to_g, finish_e * to_g
    capped_joints = sum(any(p.kind == "spacer_cap" for p in paths) for paths, _ in jobs.values())
    cap_report = (f"Solid spacer cap at {capped_joints} {'joint' if capped_joints == 1 else 'joints'}."
                  if capped_joints else "")
    _, limiting_li, available = limiting
    purge_report = ("The tightest purge layer at " + purge_layer_report(limiting_li, available)
                    if limiting_li is not None else "No purge was required by the brick layers.")
    finish_report = f"The top piece used {finish_g:.2f} g after the final purge layer."
    grams = (f"filament: bricks {brick_g:.1f} g vs normal tower {tower_g:.1f} g"
             + (f" ({finish_g:.1f} g of it finishes the top piece)" if finish_g >= 0.1 else ""))
    msg = (f"Brick tower: {brick_desc} per level; stack = {elem_desc}. {grams}"
           + (f"; {injected_layers} layers added" if injected_layers else "")
           + (f"; worst layer over-extruded {worst[0]*100-100:.0f}%" if worst[0] > 1.0 else "")
           + ("; " + "; ".join(notes) if notes else "")
           + f". {purge_report} {finish_report}"
           + (f" {cap_report}" if cap_report else ""))
    header = (f"{MARK} v{PLUGIN_VERSION}: {msg}\n"
              f"{MARK} footprint X{foot[0]:.2f}..{foot[2]:.2f} Y{foot[1]:.2f}..{foot[3]:.2f}\n")
    insert_at = 0
    for k, ln in enumerate(final[:200]):
        if "HEADER_BLOCK_END" in ln:
            insert_at = k + 1
            break
    else:
        for k, ln in enumerate(final[:5000]):
            if ln.strip() and not ln.lstrip().startswith(";"):
                insert_at = k
                break
    final.insert(insert_at, header)
    pieces, counts = piece_counts(elements)
    unfinished = sum(e.get("pieces", 0) for e in elements if not e["complete"])
    short = (f"Brick tower built: {pieces} pieces ({counts}; {len(elements)} levels)"
             + (f", {unfinished} unfinished on top" if unfinished else "")
             + f". Bricks use {brick_g:.1f} g of filament; the normal tower would use {tower_g:.1f} g"
             + (f" ({finish_g:.1f} g extra finishes the top piece)." if finish_g >= 0.1 else ".")
             + (f" {cap_report}" if cap_report else ""))
    banner = ("; ==================== BRICK PRIME TOWER ====================\n"
              f"; {short}\n"
              f"; {purge_report}\n"
              f"; {finish_report}\n"
              f"; Stack, bottom to top: {elem_desc}\n"
              "; ===========================================================\n")
    end_at = None
    last_layer = max((k for k, ln in enumerate(final) if LAYER_TAG_RE.match(ln.strip())), default=None)
    if last_layer is not None:
        for k in range(len(final) - 1, last_layer, -1):
            m = FEATURE_RE.match(final[k].strip())
            if m and m.group(2).strip().lower() == "custom":
                end_at = k
                break
    if end_at is None:
        end_at = next((k for k, ln in enumerate(final) if CONFIG_START_RE.match(ln.strip())), len(final))
    final.insert(end_at, banner)
    info = {"short": short,
            "preview_levels": preview_piece_levels(elements, elem_cells),
            "preview_html": build_preview(bricks, specs, jobs, elements, styles_used, style_name, short,
                                          purge_report, finish_report)}
    return "".join(final), msg, info


KIND_COLORS = {"wall": "#4a7bd0", "tube": "#4a7bd0", "pin": "#4a7bd0", "socket": "#8e6cd1", "hole": "#8e6cd1",
               "fill": "#e3a33b", "deck": "#3aa17e", "deck_edge": "#3aa17e", "stud": "#d9534f", "spacer": "#9aa0a6",
               "spacer_cap": "#9aa0a6", "brim": "#b8b8b8"}


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_preview(bricks, specs, jobs, elements, styles_used, style_name, headline, purge_report, finish_report):
    if not bricks or not specs:
        return ""
    by_elem = {}
    for li in sorted(specs):
        by_elem.setdefault(specs[li].elem, []).append(specs[li])

    def pick(pred):
        for li in sorted(specs):
            if pred(specs[li]):
                return specs[li]
        return None

    views = []
    sock = pick(lambda s: s.region == "cavity" and s.v < SOCKET_D and not s.first
                and styles_used[s.elem][0] == "holes")
    if sock:
        views.append(("Socket plate (studs from below go in here)", sock))
    cav = pick(lambda s: s.region == "cavity" and s.v >= SOCKET_D + 1 and styles_used[s.elem][0] == "tubes"
               and styles_used[s.elem][1] == 0) or pick(lambda s: s.region == "cavity" and s.v >= SOCKET_D + 1)
    if cav:
        views.append(("Inside the brick: walls and tubes" + ("" if styles_used[cav.elem][1] == 0 else " + fill"), cav))
    deck = pick(lambda s: s.region == "deck")
    if deck:
        views.append(("Top deck", deck))
    stud = pick(lambda s: s.region == "studs" and s.spacer) or pick(lambda s: s.region == "studs")
    if stud:
        views.append(("Studs" + (" inside the breakaway spacer" if stud.spacer else ""), stud))

    pad = 2.0
    x0 = min(b.outer[0] for b in bricks) - pad
    y0 = min(b.outer[1] for b in bricks) - pad
    x1 = max(b.outer[2] for b in bricks) + pad
    y1 = max(b.outer[3] for b in bricks) + pad
    w, h = x1 - x0, y1 - y0
    svgs = []
    sections = [(title, spec.z, [p for p in jobs[spec.idx][0] if p.kind not in ("brim", "spacer_cap")])
                for title, spec in views]
    cap = next((paths for paths, _ in jobs.values() if any(p.kind == "spacer_cap" for p in paths)), None)
    if cap:
        paths = [p for p in cap if p.kind == "spacer_cap"]
        sections.append(("Solid spacer cap", paths[0].z, paths))
    for title, z, paths in sections:
        lines = []
        for p in paths:
            pts = " ".join(f"{x - x0:.2f},{y1 - y:.2f}" for (x, y) in p.pts)
            lines.append(f'<polyline points="{pts}" fill="none" stroke="{KIND_COLORS.get(p.kind, "#888")}" '
                         f'stroke-width="{p.w * 0.9:.2f}" stroke-linecap="round" stroke-linejoin="round"/>')
        svgs.append(f'<figure><svg viewBox="0 0 {w:.1f} {h:.1f}" role="img" aria-label="{_esc(title)}">'
                    + "".join(lines) + f'</svg><figcaption>{_esc(title)}<br><small>Z {z:.2f} mm</small>'
                    '</figcaption></figure>')

    rows = []
    zmax = max(s.z for s in specs.values())
    H = 260.0
    sc = H / max(zmax, 1.0)
    bw = 120.0
    for e_idx, el in enumerate(elements):
        ss = by_elem.get(e_idx, [])
        if not ss:
            continue
        zb = min(s.z - s.t for s in ss)
        zt = max(s.z for s in ss)
        body_top = max((s.z for s in ss if s.region != "studs"), default=zt)
        yb, yt, ybody = H - zb * sc, H - zt * sc, H - body_top * sc
        sty = styles_used[e_idx]
        col = "#3aa17e" if sty[1] == 0 else ("#e3a33b" if sty[1] < 1 else "#d9534f")
        rows.append(f'<rect x="10" y="{ybody:.1f}" width="{bw}" height="{max(yb - ybody, 1):.1f}" rx="2" fill="{col}" opacity="0.85"/>')
        if zt > body_top:
            for k in range(4):
                rows.append(f'<rect x="{18 + k * 28}" y="{yt:.1f}" width="16" height="{max(ybody - yt, 1):.1f}" fill="{col}" opacity="0.85"/>')
        label = f"{elem_label(el)}: {style_name(sty)}" + ("" if el["complete"] else " (unfinished)")
        rows.append(f'<text x="{bw + 22}" y="{(yb + ybody) / 2 + 4:.1f}" class="lbl">{_esc(label)}</text>')
    stack = (f'<svg viewBox="0 0 420 {H + 10:.0f}" class="stack" role="img" aria-label="Brick stack">'
             + "".join(rows) + "</svg>")
    legend = "".join(f'<span><i style="background:{c}"></i>{k}</span>' for k, c in
                     (("walls / tubes", "#4a7bd0"), ("socket plate", "#8e6cd1"), ("fill", "#e3a33b"),
                      ("deck", "#3aa17e"), ("studs", "#d9534f"), ("spacer", "#9aa0a6")))
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Brick Prime Tower</title><style>
:root{{--bg:#ffffff;--fg:#1f2328;--muted:#656d76;--card:#f6f8fa;--line:#d0d7de}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1e1f22;--fg:#e6e6e6;--muted:#9aa0a6;--card:#2a2b2f;--line:#3a3b40}}}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:13px system-ui,sans-serif}}
h1{{font-size:15px;margin:0 0 4px}}p{{margin:0 0 12px;color:var(--muted)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}}
figure{{margin:0;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px}}
figure svg{{width:100%;height:auto;display:block}}figcaption{{margin-top:6px}}small{{color:var(--muted)}}
.legend{{display:flex;flex-wrap:wrap;gap:12px;margin:12px 0}}.legend i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}}
.stackbox{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;margin-top:12px}}
.stack{{width:100%;max-width:520px;height:auto}}.lbl{{fill:var(--fg);font-size:11px}}
</style></head><body>
<h1>{_esc(headline)}</h1>
<p>{_esc(purge_report).replace('mm^3', 'mm&sup3;')}<br>{_esc(finish_report)}</p>
<p>These are the toolpaths written into the exported G-code. OrcaSlicer's normal preview still shows the original tower; drag the exported .gcode into OrcaSlicer to see it as printed.</p>
<div class="legend">{legend}</div>
<div class="grid">{''.join(svgs)}</div>
<div class="stackbox"><b>Stack (bottom to top)</b>{stack}</div>
</body></html>"""


def guard_pass(out_lines, rect, st):
    res = []
    trk = Tracker()
    phys_z = 0.0
    lifted = False
    virt_z = 0.0
    in_gen = False
    tower_top = None
    lift = st["z_lift"]
    trigger = 0.25
    tf = st.get("_travel_f") or st["travel_speed"] * 60

    def abs_z(z, f_after):
        if trk.abs_xyz:
            res.append(f"G1 Z{z:.3f} F{tf:.0f}\n")
        else:
            res.append(f"G90\nG1 Z{z:.3f} F{tf:.0f}\nG91\n")
        if abs(f_after - tf) > 0.5:
            res.append(f"G1 F{f_after:.0f}\n")

    for ln in out_lines:
        s = ln.strip()
        if s.startswith(MARK):
            if " BEGIN" in s:
                in_gen = True
                lifted = False
            elif " END" in s:
                in_gen = False
                m = re.search(r"z_restore=([-\d.]+)", s)
                t = re.search(r"top=([-\d.]+)", s)
                if t:
                    tower_top = max(tower_top or 0.0, float(t.group(1)))
                virt_z = float(m.group(1)) if m else phys_z
                lifted = abs(virt_z - phys_z) > 1e-6
                trk.z = phys_z
            res.append(ln)
            continue
        cmd = parse_cmd(s)
        if in_gen:
            mv = trk.feed(cmd)
            if mv:
                phys_z = trk.z
            res.append(ln)
            continue
        if cmd is None or not cmd.is_move:
            trk.feed(cmd)
            res.append(ln)
            continue
        sx, sy = trk.x, trk.y
        f_prev = trk.f
        trk.z = virt_z if lifted else phys_z
        mv = trk.feed(cmd)
        tx, ty, tz = mv["ex"], mv["ey"], mv["ez"]
        printing = mv["de"] > 0 and mv["has_xy"]
        if tower_top is None:
            res.append(ln)
            phys_z = virt_z = tz
            continue
        if printing:
            if lifted:
                abs_z(virt_z, f_prev)
                lifted = False
            res.append(ln)
            phys_z = virt_z = tz
            continue
        hits = seg_hits_rect(rect, sx, sy, tx, ty)
        need = tower_top + trigger
        cur_z = phys_z
        if hits and min(cur_z, tz) < need:
            safe = max(tower_top + lift, tz)
            if cur_z < safe - 1e-6:
                abs_z(safe, f_prev)
                phys_z = safe
            if mv["has_z"]:
                code = ln.split(";", 1)[0]
                code = re.sub(r"\s*Z\s*[-+]?(?:\d+\.?\d*|\.\d+)", "", code, flags=re.I)
                if not re.fullmatch(r"\s*G[0-3]\s*(F[-+\d.]+)?\s*", code, flags=re.I):
                    res.append(code.rstrip() + "\n")
            else:
                res.append(ln)
            virt_z = tz
            lifted = abs(virt_z - phys_z) > 1e-6
            if lifted and not in_rect(rect, tx, ty):
                abs_z(virt_z, trk.f)
                phys_z = virt_z
                lifted = False
            trk.z = phys_z
            continue
        if lifted and in_rect(rect, sx, sy) and not mv["has_xy"]:
            res.append(ln)
            if mv["has_z"]:
                phys_z = virt_z = tz
                lifted = False
            trk.z = phys_z
            continue
        if lifted:
            abs_z(virt_z, f_prev)
            phys_z = virt_z
            lifted = False
        res.append(ln)
        phys_z = virt_z = tz
        trk.z = phys_z
    return res


CONFIG_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>
:root{--bg:#ffffff;--fg:#1f2328;--muted:#656d76;--card:#f6f8fa;--line:#d0d7de;--on:#2f7d4f;--field:#ffffff}
@media (prefers-color-scheme:dark){:root{--bg:#2b2b2b;--fg:#e2e2e2;--muted:#a0a0a0;--card:#333438;--line:#46474c;--on:#5cc489;--field:#3a3a3a}}
body{font:13px system-ui,sans-serif;margin:14px;color:var(--fg);background:var(--bg)}
h3{margin:18px 0 4px;font-size:13px}p.hint{margin:0 0 8px;color:var(--muted)}
.row{display:flex;gap:8px;align-items:center;margin:6px 0}.sub{color:var(--muted);margin:0 0 6px 26px}
.piece-list,.size-list{border-bottom:1px solid var(--line);max-width:640px}
.piece{display:grid;grid-template-columns:72px minmax(0,1fr) 58px;gap:12px;align-items:center;
  min-height:44px;padding:3px 8px;border-top:1px solid var(--line)}
label.piece{cursor:pointer}
label.piece:hover{background:var(--card)}
.piece:focus-within,.size:focus-within{outline:2px solid var(--on);outline-offset:-2px}
.piece input,.piece-fixed{grid-column:3;grid-row:1;justify-self:center}
.piece input,.size input{width:16px;height:16px;margin:0;color-scheme:light dark;accent-color:var(--on);cursor:pointer}
.piece svg{grid-column:1;grid-row:1;width:72px;height:42px;color:var(--on);fill:currentColor}
.piece input:not(:checked) ~ svg{color:var(--muted)}
.piece svg .top{opacity:.34}.piece svg .front{opacity:.72}
.piece-copy{grid-column:2;grid-row:1}
.piece-copy small,.piece-fixed{display:block;color:var(--muted);font-size:12px;line-height:1.4}
.piece-fixed{font-weight:600;white-space:nowrap}
@media (max-width:360px){.piece{grid-template-columns:56px minmax(0,1fr) 54px;gap:8px}.piece svg{width:56px}}
.size-list{display:grid;grid-template-columns:repeat(9,minmax(0,1fr));border-top:1px solid var(--line)}
.size{display:grid;grid-template-columns:auto auto;grid-template-rows:30px auto;justify-content:center;
  align-items:center;column-gap:6px;padding:6px 0 8px;cursor:pointer}
.size:hover{background:var(--card)}
.size input{grid-column:1;grid-row:2}
.size-name{grid-column:2;grid-row:2;font-weight:600;font-variant-numeric:tabular-nums}
.footprint{--stud-pitch:10px;grid-column:1/-1;grid-row:1;justify-self:center;display:grid;grid-auto-rows:var(--stud-pitch);place-items:center;
  color:var(--muted);background:var(--card);outline:1px solid currentColor;outline-offset:-1px}
.footprint i{width:7px;height:7px;border-radius:50%;background:currentColor}
.size input:checked ~ .footprint{color:var(--on)}
@media (max-width:600px){.size-list{grid-template-columns:repeat(5,minmax(0,1fr))}}
@media (max-width:420px){.size-list{grid-template-columns:repeat(3,minmax(0,1fr))}}
details{margin-top:16px}summary{cursor:pointer;font-weight:600}
input[type=number],select{background:var(--field);color:var(--fg);border:1px solid var(--line);border-radius:4px;padding:3px 5px}
input[type=number]{width:78px}
button{margin-top:16px;padding:6px 18px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
#last-preview{margin:0;flex:none}
#last-preview:disabled{color:var(--muted);cursor:default}
.preview-controls{flex-wrap:wrap;margin:12px 0}
#preview-source{color:var(--muted);overflow-wrap:anywhere}
#preview-pieces{max-width:640px;padding:0;margin:0 0 12px;list-style:none;border-bottom:1px solid var(--line)}
#preview-pieces li{display:grid;grid-template-columns:auto minmax(0,1fr);gap:12px;
  padding:8px 0;border-top:1px solid var(--line);line-height:1.4}
#preview-pieces li span:first-child{color:var(--muted);font-variant-numeric:tabular-nums}
#s{margin-left:10px;color:var(--muted)}
</style></head><body>
<label class="row"><input type="checkbox" id="enabled"> <b>Build bricks from the prime tower</b></label>
<label class="row"><input type="checkbox" id="allow_mixed_materials"> Allow mixed materials</label>
<p class="sub">Off: the normal tower is kept when the print mixes filaments that may not stick to each other (for example PLA + PETG, or soluble supports).</p>

<div class="row preview-controls">
 <button type="button" id="last-preview" aria-controls="preview-pieces" aria-describedby="preview-source" aria-expanded="false" disabled>Last brick preview</button>
 <span id="preview-source">Export G-code with OrcaBricks enabled to see a preview here.</span>
</div>
<ul id="preview-pieces" aria-label="Brick pieces by level, bottom to top" hidden></ul>

<h3 id="piece-types">Piece types</h3>
<p class="hint" id="piece-hint">Choose the heights and shapes OrcaBricks can use to hold the tower's purge.</p>
<div class="piece-list" role="group" aria-labelledby="piece-types" aria-describedby="piece-hint">
 <div class="piece">
  <svg viewBox="0 0 78 42" aria-hidden="true" focusable="false">
   <path class="front" d="M9 18H55V36H9Z"/><path d="M55 18L69 10V28L55 36Z"/>
   <path class="top" d="M9 18L23 10H69L55 18Z"/>
   <path d="M23 11H35V14A6 2.5 0 0 1 23 14Z M45 11H57V14A6 2.5 0 0 1 45 14Z"/>
   <ellipse class="front" cx="29" cy="11" rx="6" ry="2.5"/><ellipse class="front" cx="51" cy="11" rx="6" ry="2.5"/>
  </svg>
  <div class="piece-copy"><b>Brick</b><small>9.6 mm body. Fallback when another shape can't hold the purge.</small></div>
  <span class="piece-fixed">Always on</span>
 </div>
 <label class="piece">
  <input type="checkbox" id="allow_medium_bricks" aria-labelledby="medium-name" aria-describedby="medium-desc">
  <svg viewBox="0 0 78 42" aria-hidden="true" focusable="false">
   <path class="front" d="M9 24H55V36H9Z"/><path d="M55 24L69 16V28L55 36Z"/>
   <path class="top" d="M9 24L23 16H69L55 24Z"/>
   <path d="M23 17H35V20A6 2.5 0 0 1 23 20Z M45 17H57V20A6 2.5 0 0 1 45 20Z"/>
   <ellipse class="front" cx="29" cy="17" rx="6" ry="2.5"/><ellipse class="front" cx="51" cy="17" rx="6" ry="2.5"/>
  </svg>
  <span class="piece-copy"><b id="medium-name">Medium brick</b><small id="medium-desc">6.4 mm body. As tall as two plates, with studs on top.</small></span>
 </label>
 <label class="piece">
  <input type="checkbox" id="allow_plates" aria-labelledby="plate-name" aria-describedby="plate-desc">
  <svg viewBox="0 0 78 42" aria-hidden="true" focusable="false">
   <path class="front" d="M9 30H55V36H9Z"/><path d="M55 30L69 22V28L55 36Z"/>
   <path class="top" d="M9 30L23 22H69L55 30Z"/>
   <path d="M23 23H35V26A6 2.5 0 0 1 23 26Z M45 23H57V26A6 2.5 0 0 1 45 26Z"/>
   <ellipse class="front" cx="29" cy="23" rx="6" ry="2.5"/><ellipse class="front" cx="51" cy="23" rx="6" ry="2.5"/>
  </svg>
  <span class="piece-copy"><b id="plate-name">Plate</b><small id="plate-desc">3.2 mm body. The shortest studded piece.</small></span>
 </label>
 <label class="piece">
  <input type="checkbox" id="allow_tiles" aria-labelledby="tile-name" aria-describedby="tile-desc">
  <svg viewBox="0 0 78 42" aria-hidden="true" focusable="false">
   <path class="front" d="M9 30H55V36H9Z"/><path d="M55 30L69 22V28L55 36Z"/>
   <path class="top" d="M9 30L23 22H69L55 30Z"/>
  </svg>
  <span class="piece-copy"><b id="tile-name">Tile</b><small id="tile-desc">Smooth top. Can replace the top plate.</small></span>
 </label>
 <label class="piece">
  <input type="checkbox" id="allow_slopes" aria-labelledby="slope-name" aria-describedby="slope-desc">
  <svg viewBox="0 0 78 42" aria-hidden="true" focusable="false">
   <path class="front" d="M9 36V32L23 18H55V36Z"/><path d="M55 18L69 10V28L55 36Z"/>
   <path opacity=".48" d="M9 32L23 24L37 10L23 18Z"/><path class="top" d="M23 18L37 10H69L55 18Z"/>
   <path d="M45 11H57V14A6 2.5 0 0 1 45 14Z"/><ellipse class="front" cx="51" cy="11" rx="6" ry="2.5"/>
  </svg>
  <span class="piece-copy"><b id="slope-name">45&deg; slope</b><small id="slope-desc">Roof piece. Can replace the top brick.</small></span>
 </label>
 <label class="piece">
  <input type="checkbox" id="allow_round" aria-labelledby="round-name" aria-describedby="round-desc">
  <svg viewBox="0 0 78 42" aria-hidden="true" focusable="false">
   <path class="front" d="M19 11A20 7 0 0 0 59 11V29A20 7 0 0 1 19 29Z"/>
   <ellipse class="top" cx="39" cy="11" rx="20" ry="7"/>
   <path d="M33 6H45V9A6 2.5 0 0 1 33 9Z"/><ellipse class="front" cx="39" cy="6" rx="6" ry="2.5"/>
  </svg>
  <span class="piece-copy"><b id="round-name">Round</b><small id="round-desc">Round 2&times;2 and 1&times;1 pieces for upper tower levels.</small></span>
 </label>
</div>

<h3 id="size-title">Piece sizes</h3>
<p class="hint" id="size-hint">Choose at least one size. OrcaBricks tries the largest selected sizes first.</p>
<div class="size-list" id="sizes" role="group" aria-labelledby="size-title" aria-describedby="size-hint"></div>

<h3>Options</h3>
<label class="row"><input type="checkbox" id="finish_last_brick"> Keep printing the top piece after the last colour change</label>
<label class="row">Brick style <select id="brick_style"><option value="auto">Auto</option><option value="authentic">Authentic (always hollow)</option><option value="solid">Solid</option></select></label>
<p class="sub" style="margin-left:0">Auto keeps bricks hollow when the purge fits, and fills them above the stud socket when it doesn't.</p>

<details><summary>Fit &amp; print (advanced)</summary>
<label class="row"><input type="number" step="0.01" id="stud_clearance"> Stud fit clearance (mm)</label>
<label class="row"><input type="number" step="0.01" id="stud_diameter"> Stud diameter (mm)</label>
<label class="row"><input type="number" step="0.01" id="breakaway_gap"> Breakaway gap between levels (mm)</label>
<label class="row"><input type="checkbox" id="solid_spacer_cap"> Solid spacer cap (experimental)</label>
<label class="row"><input type="number" step="0.01" id="line_width"> Line width (mm)</label>
<label class="row"><input type="number" step="1" id="brick_speed"> Brick speed (mm/s)</label>
<label class="row"><input type="number" step="1" id="first_layer_speed"> First layer speed (mm/s)</label>
<label class="row"><input type="number" step="1" id="brim_loops"> Minimum brim loops</label>
<label class="row"><input type="number" step="0.05" id="max_hidden_flow"> Max flow on hidden inner lines (1.5 = 150%)</label>
<label class="row"><input type="number" step="0.5" id="footprint_grow"> Grow footprint (mm, only if the area is clear)</label>
</details>
<button id="save">Save</button><span id="s"></span>
<script>
const D = __DEFAULTS__;
const lastPreview = __LAST_PREVIEW__;
if (lastPreview) {
  const [filename, levels] = lastPreview;
  const button = document.getElementById('last-preview');
  const list = document.getElementById('preview-pieces');
  document.getElementById('preview-source').textContent = filename || 'Last successful brick plan';
  for (const [level, pieces] of levels) {
    const row = document.createElement('li');
    const number = document.createElement('span');
    const names = document.createElement('span');
    number.textContent = `Level ${level}`;
    names.textContent = pieces.join(', ');
    row.append(number, names);
    list.appendChild(row);
  }
  button.disabled = false;
  button.addEventListener('click', () => {
    list.hidden = !list.hidden;
    button.setAttribute('aria-expanded', String(!list.hidden));
  });
}
const SIZES = ["2x6","2x4","2x3","2x2","1x6","1x4","1x3","1x2","1x1"];
const ids = Object.keys(D);
function parse(c){ if(typeof c==='string'){try{return JSON.parse(c||'{}')}catch(e){return {}}} return c||{}; }
const box = document.getElementById('sizes');
for (const size of SIZES) {
  const [rows, columns] = size.split('x').map(Number);
  const label = document.createElement('label');
  label.className = 'size';
  label.innerHTML = `<input type="checkbox" data-size="${size}">
    <span class="footprint" aria-hidden="true" style="grid-template-columns:repeat(${columns},var(--stud-pitch))">${'<i></i>'.repeat(rows * columns)}</span>
    <span class="size-name">${rows}&times;${columns}</span>`;
  box.appendChild(label);
}
box.addEventListener('change', e => {
  if (!box.querySelector(':checked')) e.target.checked = true;
});
function fill(c){
  for(const k of ids){ const el=document.getElementById(k); if(!el) continue; const v=(k in c)?c[k]:D[k];
    if(el.type==='checkbox') el.checked=!!v; else el.value=v; }
  const want = String(('brick_sizes' in c)?c.brick_sizes:D.brick_sizes).toLowerCase().replace(/\s/g,'').split(/[,;]+/);
  document.querySelectorAll('[data-size]').forEach(i=>{ const s=i.dataset.size; i.checked = want.includes(s) || want.includes(s.split('x').reverse().join('x')); });
  if(!box.querySelector(':checked')) box.querySelector('[data-size="2x4"]').checked = true; }
function read(){ const c={}; for(const k of ids){ const el=document.getElementById(k); if(!el){c[k]=D[k];continue;}
    if(el.type==='checkbox') c[k]=el.checked; else if(typeof D[k]==='number') c[k]=parseFloat(el.value); else c[k]=el.value; }
  c.brick_sizes = [...document.querySelectorAll('[data-size]:checked')].map(i=>i.dataset.size).join(',');
  return c; }
Promise.resolve(window.orca && window.orca.getConfig ? window.orca.getConfig() : '{}').then(c=>fill(parse(c))).catch(()=>fill({}));
document.getElementById('save').onclick=()=>{ const s=document.getElementById('s');
  Promise.resolve(window.orca.saveConfig(JSON.stringify(read()))).then(()=>s.textContent='Saved').catch(e=>s.textContent='Save failed: '+e); };
</script></body></html>"""


def _run_on_file(path: str, raw_cfg: dict | None, config_lookup=None):
    with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as f:
        text = f.read()
    try:
        new_text, msg, info = process_gcode(text, raw_cfg, config_lookup)
    except Skip as e:
        if MARK in text:
            m = "Brick tower: this G-code already contains the brick tower."
            return True, m, {"short": m, "preview_html": ""}
        m = f"Brick tower NOT built: {e}. The normal prime tower was kept."
        return False, m, {"short": m, "preview_html": ""}
    tmp = path + ".bricktmp"
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(new_text)
    os.replace(tmp, path)
    return True, msg, info


def _notify(built: bool, text: str, html: str):
    try:
        ui = orca.host.ui
        L = ui.NotificationLevel
        if built and html:
            def show():
                try:
                    try:
                        ui.create_window(html=html, title="Brick Prime Tower", width=820, height=760)
                    except TypeError:
                        ui.create_window(html=html, title="Brick Prime Tower")
                except Exception:
                    pass
                return True
            ui.push_notification(L.RegularNotificationLevel, text, hyper_text="Show bricks", on_click=show)
        else:
            ui.push_notification(L.RegularNotificationLevel if built else L.WarningNotificationLevel, text)
        print("[Brick Prime Tower] notification shown: " + text, file=sys.stderr)
    except Exception as e:
        print(f"[Brick Prime Tower] could not show notification: {e!r}", file=sys.stderr)


try:
    import orca
except ImportError:
    orca = None

if orca is not None:
    _last_brick_preview: tuple[str, list[tuple[int, list[str]]]] | None = None

    class BrickPrimeTower(orca.slicing.SlicingPipelineCapabilityBase):
        def get_name(self):
            return "Brick Prime Tower"

        def get_default_config(self):
            return dict(DEFAULTS)

        def has_config_ui(self):
            return True

        def get_config_ui(self):
            preview = json.dumps(_last_brick_preview).replace("<", r"\u003c")
            return CONFIG_HTML.replace("__DEFAULTS__", json.dumps(DEFAULTS)).replace("__LAST_PREVIEW__", preview)

        def _config(self):
            try:
                return json.loads(self.get_config() or "{}")
            except Exception:
                return {}

        def execute(self, ctx):
            global _last_brick_preview
            if ctx.step != orca.slicing.Step.psGCodePostProcess or not ctx.gcode_path:
                return orca.ExecutionResult.success()
            lookup = getattr(ctx, "config_value", None)
            try:
                built, msg, info = _run_on_file(ctx.gcode_path, self._config(), lookup)
            except Exception as e:
                built = False
                msg = f"Brick tower NOT built (internal error: {e!r}). The normal prime tower was kept."
                info = {"short": msg, "preview_html": ""}
            levels = info.get("preview_levels")
            if built and levels:
                filename = os.path.basename(getattr(ctx, "output_name", "") or "")
                _last_brick_preview = (filename, levels)
            _notify(built, info["short"], info["preview_html"])
            return orca.ExecutionResult.success(msg)

    @orca.plugin
    class BrickPrimeTowerPlugin(orca.base):
        def register_capabilities(self):
            orca.register_capability(BrickPrimeTower)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Rewrite an OrcaSlicer prime tower into stackable bricks.")
    ap.add_argument("gcode", help="G-code file (edited in place unless -o is given)")
    ap.add_argument("-o", "--output", help="write here instead of editing in place")
    ap.add_argument("--config", help="JSON file with settings")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override one setting")
    ap.add_argument("--preview", metavar="HTML", help="also write the brick preview page here")
    a = ap.parse_args(argv)
    raw = {}
    if a.config:
        with open(a.config, encoding="utf-8") as f:
            raw.update(json.load(f))
    for kv in a.set:
        k, _, v = kv.partition("=")
        raw[k.strip()] = v.strip()
    if a.output:
        with open(a.gcode, "r", encoding="utf-8", errors="surrogateescape", newline="") as f:
            text = f.read()
        try:
            new_text, msg, info = process_gcode(text, raw)
        except Skip as e:
            print(f"Brick tower skipped: {e}")
            return 0
        with open(a.output, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(new_text)
        print(msg)
    else:
        _, msg, info = _run_on_file(a.gcode, raw)
        print(msg)
    if a.preview and info.get("preview_html"):
        with open(a.preview, "w", encoding="utf-8") as f:
            f.write(info["preview_html"])
    return 0


if __name__ == "__main__" and orca is None:
    sys.exit(main())
