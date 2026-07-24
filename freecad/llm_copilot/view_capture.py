"""Offscreen FreeCAD document rendering without touching the visible view."""
import base64
import binascii
import math
import struct
import zlib
from .history_store import is_internal_object


def changed_object_names(before, after):
    # Thin shim over the single change derivation in document_inspector so
    # rendering and the model feedback agree on what "changed" means.
    from .document_inspector import DocumentDelta
    return DocumentDelta(before, after).changed_names


def _final_shape_objects(doc, names=None):
    wanted = set(names) if names is not None else None
    bodies = {}
    objects = []
    for obj in doc.Objects:
        if is_internal_object(obj):
            continue
        if wanted is not None and obj.Name not in wanted:
            continue
        if wanted is None and not getattr(obj, "Visibility", True):
            continue
        if obj.TypeId == "PartDesign::Body":
            bodies[obj.Name] = obj
            objects.append(obj)
            continue
        try:
            parent = obj.getParentGeoFeatureGroup()
        except Exception:
            parent = None
        if parent is not None and parent.TypeId == "PartDesign::Body":
            if wanted is not None:
                bodies[parent.Name] = parent
            continue
        shape = getattr(obj, "Shape", None)
        try:
            if shape is not None and not shape.isNull():
                objects.append(obj)
        except Exception:
            pass
    objects.extend(body for name, body in bodies.items()
                   if body not in objects)
    unique = []
    seen = set()
    for obj in objects:
        if obj.Name not in seen:
            seen.add(obj.Name)
            unique.append(obj)
    return unique


_OBJECT_PALETTE = (
    (214, 142, 82),   # muted orange
    (79, 155, 163),   # teal
    (132, 166, 105),  # green
    (151, 126, 174),  # violet
    (196, 171, 105),  # sand
    (105, 145, 190),  # blue
)
_NEUTRAL_SURFACE = (205, 199, 188)
_BACKGROUND = (248, 247, 243)
_EDGE_COLOR = (42, 45, 48)


def _object_color(obj, separate):
    if not separate:
        return _NEUTRAL_SURFACE
    # crc32 is deterministic across sessions, unlike Python's salted hash.
    index = binascii.crc32(obj.Name.encode("utf-8")) % len(_OBJECT_PALETTE)
    return _OBJECT_PALETTE[index]


def _geometry(objects, separate_colors=False, include_edges=False):
    triangles = []
    polylines = []
    for obj in objects:
        try:
            shape = obj.Shape
            if shape.isNull():
                continue
            points, facets = shape.tessellate(0.1)
            coords = [(float(p.x), float(p.y), float(p.z)) for p in points]
            color = _object_color(obj, separate_colors)
            triangles.extend(
                (tuple(coords[index] for index in facet), color)
                for facet in facets if len(facet) == 3)
            if include_edges:
                diagonal = float(getattr(shape.BoundBox, "DiagonalLength", 1))
                deflection = max(diagonal / 400.0, 1e-5)
                for edge in shape.Edges:
                    try:
                        samples = edge.discretize(Deflection=deflection)
                    except Exception:
                        try:
                            samples = edge.discretize(Number=32)
                        except Exception:
                            samples = [v.Point for v in edge.Vertexes]
                    line = [
                        (float(p.x), float(p.y), float(p.z))
                        for p in samples]
                    if len(line) >= 2:
                        polylines.append(line)
        except Exception:
            continue
    return triangles, polylines


def _png_bytes(width, height, pixels):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", binascii.crc32(kind + data) & 0xffffffff))
    rows = b"".join(
        b"\x00" + bytes(pixels[y * width * 3:(y + 1) * width * 3])
        for y in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6))
            + chunk(b"IEND", b""))


def _render_pixels(objects, direction, width=256, height=256,
                   technical_edges=True, object_colors=True,
                   depth_shading=True):
    """Render a headless technical illustration from tessellated shapes."""
    triangles, polylines = _geometry(
        objects, separate_colors=object_colors,
        include_edges=technical_edges)
    if not triangles:
        return None

    def normalized(vector):
        length = math.sqrt(sum(v * v for v in vector))
        if length < 1e-15:
            return (0.0, 0.0, 1.0)
        return tuple(v / length for v in vector)

    def cross(a, b):
        return (a[1] * b[2] - a[2] * b[1],
                a[2] * b[0] - a[0] * b[2],
                a[0] * b[1] - a[1] * b[0])

    def dot(a, b):
        return sum(x * y for x, y in zip(a, b))

    forward = normalized(direction)
    world_up = (0, 1, 0) if abs(forward[2]) > 0.95 else (0, 0, 1)
    right = normalized(cross(forward, world_up))
    up = normalized(cross(right, forward))
    projected = []
    all_xy = []
    light_a = normalized((1, -2, 3))
    light_b = normalized((-2, -1, 1))
    light_c = normalized((0, 2, 2))
    all_z = []
    for triangle, base_color in triangles:
        points = [(dot(p, right), dot(p, up), dot(p, forward))
                  for p in triangle]
        normal = normalized(cross(
            tuple(triangle[1][i] - triangle[0][i] for i in range(3)),
            tuple(triangle[2][i] - triangle[0][i] for i in range(3))))
        if depth_shading:
            lighting = (
                0.24
                + 0.48 * abs(dot(normal, light_a))
                + 0.18 * abs(dot(normal, light_b))
                + 0.10 * abs(dot(normal, light_c)))
        else:
            lighting = 0.35 + 0.65 * abs(dot(normal, light_a))
        projected.append((points, base_color, min(1.0, lighting)))
        all_xy.extend((p[0], p[1]) for p in points)
        all_z.extend(p[2] for p in points)
    min_x = min(p[0] for p in all_xy)
    max_x = max(p[0] for p in all_xy)
    min_y = min(p[1] for p in all_xy)
    max_y = max(p[1] for p in all_xy)
    margin = max(12.0, min(width, height) * 0.08)
    scale = min((width - 2 * margin) / max(max_x - min_x, 1e-9),
                (height - 2 * margin) / max(max_y - min_y, 1e-9))

    def screen(point):
        x = margin + (point[0] - min_x) * scale
        y = height - margin - (point[1] - min_y) * scale
        return x, y, point[2]

    pixels = bytearray(_BACKGROUND * (width * height))
    depth = [-float("inf")] * (width * height)
    min_z, max_z = min(all_z), max(all_z)
    z_span = max(max_z - min_z, 1e-9)
    for points, base_color, lighting in projected:
        a, b, c = [screen(p) for p in points]
        denominator = ((b[1] - c[1]) * (a[0] - c[0])
                       + (c[0] - b[0]) * (a[1] - c[1]))
        if abs(denominator) < 1e-12:
            continue
        x0 = max(0, int(math.floor(min(a[0], b[0], c[0]))))
        x1 = min(width - 1, int(math.ceil(max(a[0], b[0], c[0]))))
        y0 = max(0, int(math.floor(min(a[1], b[1], c[1]))))
        y1 = min(height - 1, int(math.ceil(max(a[1], b[1], c[1]))))
        for y in range(y0, y1 + 1):
            py = y + 0.5
            for x in range(x0, x1 + 1):
                px = x + 0.5
                wa = ((b[1] - c[1]) * (px - c[0])
                      + (c[0] - b[0]) * (py - c[1])) / denominator
                wb = ((c[1] - a[1]) * (px - c[0])
                      + (a[0] - c[0]) * (py - c[1])) / denominator
                wc = 1.0 - wa - wb
                if wa < 0 or wb < 0 or wc < 0:
                    continue
                z = wa * a[2] + wb * b[2] + wc * c[2]
                offset = y * width + x
                if z > depth[offset]:
                    depth[offset] = z
                    depth_factor = (
                        0.78 + 0.22 * ((z - min_z) / z_span)
                        if depth_shading else 1.0)
                    factor = min(1.15, lighting * depth_factor)
                    color = tuple(
                        max(0, min(255, round(channel * factor)))
                        for channel in base_color)
                    pixel = offset * 3
                    pixels[pixel:pixel + 3] = bytes(color)

    if technical_edges:
        tolerance = z_span * 0.008 + 1e-9

        def draw_point(x, y, z):
            ix, iy = int(round(x)), int(round(y))
            for oy in (-1, 0, 1):
                py = iy + oy
                if py < 0 or py >= height:
                    continue
                for ox in (-1, 0, 1):
                    px = ix + ox
                    if px < 0 or px >= width:
                        continue
                    offset = py * width + px
                    if z + tolerance >= depth[offset]:
                        pixel = offset * 3
                        pixels[pixel:pixel + 3] = bytes(_EDGE_COLOR)

        for line in polylines:
            projected_line = [
                screen((dot(p, right), dot(p, up), dot(p, forward)))
                for p in line]
            for start, end in zip(projected_line, projected_line[1:]):
                steps = max(
                    1, int(math.ceil(max(
                        abs(end[0] - start[0]),
                        abs(end[1] - start[1])))))
                for step in range(steps + 1):
                    amount = step / steps
                    draw_point(
                        start[0] + (end[0] - start[0]) * amount,
                        start[1] + (end[1] - start[1]) * amount,
                        start[2] + (end[2] - start[2]) * amount)
    return pixels


_VIEWS = (
    ("front", (0, -1, 0)),
    ("back", (0, 1, 0)),
    ("left", (-1, 0, 0)),
    ("right", (1, 0, 0)),
    ("top", (0, 0, -1)),
    ("bottom", (0, 0, 1)),
    ("isometric", (1, -1, -1)),
)


def _render_montage(objects, tile_size=256, columns=3, **render_options):
    """Render all side views into one PNG contact sheet."""
    rows = math.ceil(len(_VIEWS) / columns)
    width, height = tile_size * columns, tile_size * rows
    canvas = bytearray([238]) * (width * height * 3)
    rendered = False
    for index, (_label, direction) in enumerate(_VIEWS):
        tile = _render_pixels(
            objects, direction, tile_size, tile_size, **render_options)
        if tile is None:
            continue
        rendered = True
        tile_x = (index % columns) * tile_size
        tile_y = (index // columns) * tile_size
        for y in range(tile_size):
            source = y * tile_size * 3
            target = ((tile_y + y) * width + tile_x) * 3
            canvas[target:target + tile_size * 3] = \
                tile[source:source + tile_size * 3]
    if not rendered:
        return ""
    # Dark separators make the individual camera views unambiguous.
    separator = bytes((95, 105, 120))
    for x in range(tile_size, width, tile_size):
        for y in range(height):
            offset = (y * width + x) * 3
            canvas[offset:offset + 3] = separator
    for y in range(tile_size, height, tile_size):
        start = y * width * 3
        for x in range(width):
            offset = start + x * 3
            canvas[offset:offset + 3] = separator
    return base64.b64encode(_png_bytes(width, height, canvas)).decode("ascii")


def capture(app, changed_names=(), strategy="global_and_changed",
            max_isolated=4, technical_edges=True, object_colors=True,
            depth_shading=True):
    """Return labelled base64 PNGs rendered from independent shape scenes."""
    doc = getattr(app, "ActiveDocument", None)
    if doc is None:
        return []
    images = []
    if strategy in ("global", "global_and_changed"):
        objects = _final_shape_objects(doc)
        data = _render_montage(
            objects, technical_edges=technical_edges,
            object_colors=object_colors, depth_shading=depth_shading)
        if data:
            images.append({
                "label": (
                    "Full document contact sheet — row 1: front, back, left; "
                    "row 2: right, top, bottom; row 3: isometric"),
                "data": data,
            })
    if strategy in ("changed", "global_and_changed"):
        changed = _final_shape_objects(doc, changed_names)
        for obj in changed[:max(0, max_isolated)]:
            data = _render_montage(
                [obj], technical_edges=technical_edges,
                object_colors=object_colors, depth_shading=depth_shading)
            if data:
                images.append({
                    "label": (
                        f"Changed element — {obj.Label} ({obj.Name}); "
                        "row 1: front, back, left; row 2: right, top, bottom; "
                        "row 3: isometric"),
                    "data": data,
                })
    return images
