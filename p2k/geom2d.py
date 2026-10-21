"""Small 2-D geometry kit for the copper connectivity checks (stdlib only, mm).

A copper item is a Shape: a core (a segment, or a filled polygon with optional holes) grown
by a radius r. That covers everything on this board exactly:
  track      segment, r = width / 2          via / round flash   point (segment of length 0), r
  pad        polygon, r = 0                  Proteus pour        polygon with holes, r = stroke / 2
(Proteus strokes every pour region and every clear hole with the pour's edge width, so its
copper is (outer grown by w/2) minus (holes shrunk by w/2).)

touches(a, b, eps): do the two copper shapes overlap or touch (gap <= eps)?
"""
import math


class Shape:
    __slots__ = ("kind", "pts", "holes", "r", "bbox", "_egrid")

    def __init__(self, kind, pts, r=0.0, holes=()):
        self.kind = kind            # "seg" or "poly"
        self.pts = pts              # seg: [(x1, y1), (x2, y2)]; poly: closed or open ring
        if kind == "poly" and pts[0] == pts[-1]:
            self.pts = pts[:-1]
        self.holes = [h[:-1] if h[0] == h[-1] else h for h in holes]
        self.r = r
        xs = [p[0] for p in self.pts]
        ys = [p[1] for p in self.pts]
        self.bbox = (min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r)
        self._egrid = None

    def edges(self):
        if self.kind == "seg":
            return [(self.pts[0], self.pts[-1])]
        p = self.pts
        return [(p[i - 1], p[i]) for i in range(len(p))]

    def edges_near(self, bbox):
        """Outer edges whose bbox meets `bbox` (grid-accelerated for big polygons)."""
        if self.kind == "seg" or len(self.pts) < 24:
            return [e for e in self.edges() if _bbox_hit(_ebox(e), bbox)]
        if self._egrid is None:
            self._egrid = _EdgeGrid(self.edges(), 1.0)
        return self._egrid.query(bbox)


def seg(x1, y1, x2, y2, r):
    return Shape("seg", [(x1, y1), (x2, y2)], r)


def poly(pts, r=0.0, holes=()):
    return Shape("poly", list(pts), r, holes)


def _ebox(e):
    (ax, ay), (bx, by) = e
    return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))


def _bbox_hit(a, b, m=0.0):
    return a[0] - m <= b[2] and b[0] - m <= a[2] and a[1] - m <= b[3] and b[1] - m <= a[3]


def bbox_hit(a, b, m=0.0):
    return _bbox_hit(a, b, m)


class _EdgeGrid:
    def __init__(self, edges, cell):
        self.cell = cell
        self.cells = {}
        for e in edges:
            x0, y0, x1, y1 = _ebox(e)
            for i in range(int(math.floor(x0 / cell)), int(math.floor(x1 / cell)) + 1):
                for j in range(int(math.floor(y0 / cell)), int(math.floor(y1 / cell)) + 1):
                    self.cells.setdefault((i, j), []).append(e)

    def query(self, bbox):
        c = self.cell
        seen, out = set(), []
        for i in range(int(math.floor(bbox[0] / c)), int(math.floor(bbox[2] / c)) + 1):
            for j in range(int(math.floor(bbox[1] / c)), int(math.floor(bbox[3] / c)) + 1):
                for e in self.cells.get((i, j), ()):
                    if id(e) not in seen:
                        seen.add(id(e))
                        out.append(e)
        return out


def point_in_ring(x, y, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _pt_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def seg_seg(a, b, c, d):
    """Distance between segments ab and cd."""
    (ax, ay), (bx, by), (cx, cy), (dx, dy) = a, b, c, d
    d1 = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    d2 = (bx - ax) * (dy - ay) - (by - ay) * (dx - ax)
    d3 = (dx - cx) * (ay - cy) - (dy - cy) * (ax - cx)
    d4 = (dx - cx) * (by - cy) - (dy - cy) * (bx - cx)
    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return 0.0
    return min(_pt_seg(ax, ay, cx, cy, dx, dy), _pt_seg(bx, by, cx, cy, dx, dy),
               _pt_seg(cx, cy, ax, ay, bx, by), _pt_seg(dx, dy, ax, ay, bx, by))


def _boundary_dist(a, b, limit):
    """Min distance between the core boundaries of a and b (outer rings only), or
    a value > limit when they are further apart than limit."""
    best = limit + 1.0
    ea = a.edges_near(_grow(b.bbox, limit - b.r)) if a.kind == "poly" else a.edges()
    for e in ea:
        box = _grow(_ebox(e), limit)
        for f in b.edges_near(box):
            if _bbox_hit(box, _ebox(f)):
                d = seg_seg(e[0], e[1], f[0], f[1])
                if d < best:
                    best = d
                    if best == 0.0:
                        return 0.0
    return best


def _grow(b, m):
    return (b[0] - m, b[1] - m, b[2] + m, b[3] + m)


def _core_inside(a, ring):
    x, y = a.pts[0]
    return point_in_ring(x, y, ring)


def core_dist_le(a, b, limit):
    """Is the distance between the filled cores of a and b (holes ignored) <= limit?"""
    if b.kind == "poly" and _core_inside(a, b.pts):
        return True
    if a.kind == "poly" and _core_inside(b, a.pts):
        return True
    return _boundary_dist(a, b, limit) <= limit


def _inside_hole(a, hole, margin):
    """Is shape a (core grown by a.r) entirely inside `hole` shrunk by `margin`?"""
    if not _core_inside(a, hole):
        return False
    h = Shape("poly", hole)
    return _boundary_dist(a, h, a.r + margin) > a.r + margin


def touches(a, b, eps=1e-4):
    if not _bbox_hit(a.bbox, b.bbox, eps):
        return False
    if not core_dist_le(a, b, a.r + b.r + eps):
        return False
    for big, small in ((a, b), (b, a)):
        for h in big.holes:
            hb = _bbox_of(h)
            if _bbox_hit(hb, small.bbox) and _inside_hole(small, h, big.r - eps):
                return False
    return True


def _bbox_of(ring):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return (min(xs), min(ys), max(xs), max(ys))


def ring_area(ring):
    s = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i - 1]
        x2, y2 = ring[i]
        s += x1 * y2 - x2 * y1
    return s / 2.0


class Grid:
    """Bucket items by bbox for candidate-pair queries."""

    def __init__(self, cell=2.0):
        self.cell = cell
        self.cells = {}

    def add(self, key, bbox):
        c = self.cell
        for i in range(int(math.floor(bbox[0] / c)), int(math.floor(bbox[2] / c)) + 1):
            for j in range(int(math.floor(bbox[1] / c)), int(math.floor(bbox[3] / c)) + 1):
                self.cells.setdefault((i, j), []).append(key)

    def query(self, bbox):
        c = self.cell
        out = set()
        for i in range(int(math.floor(bbox[0] / c)), int(math.floor(bbox[2] / c)) + 1):
            for j in range(int(math.floor(bbox[1] / c)), int(math.floor(bbox[3] / c)) + 1):
                out.update(self.cells.get((i, j), ()))
        return out


class UnionFind:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, i):
        p = self.p
        while p[i] != i:
            p[i] = p[p[i]]
            i = p[i]
        return i

    def union(self, i, j):
        i, j = self.find(i), self.find(j)
        if i != j:
            self.p[max(i, j)] = min(i, j)

    def groups(self):
        out = {}
        for i in range(len(self.p)):
            out.setdefault(self.find(i), []).append(i)
        return out
