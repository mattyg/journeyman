"""Conservative reference-image enhancements for multimodal CAD context."""

from PySide import QtCore, QtGui


_LABEL_HEIGHT = 28
_GAP = 4
_MAX_PANEL_WIDTH = 520
_MAX_PANEL_HEIGHT = 900


def _argb(red, green, blue):
    return (
        (255 << 24)
        | (max(0, min(255, int(red))) << 16)
        | (max(0, min(255, int(green))) << 8)
        | max(0, min(255, int(blue))))


def _channels(pixel):
    return (pixel >> 16) & 255, (pixel >> 8) & 255, pixel & 255


def _scaled_panel(image):
    scale = min(
        1.0,
        _MAX_PANEL_WIDTH / max(1, image.width()),
        _MAX_PANEL_HEIGHT / max(1, image.height()))
    if scale >= 1.0:
        return image.convertToFormat(QtGui.QImage.Format_ARGB32)
    return image.scaled(
        max(1, int(image.width() * scale)),
        max(1, int(image.height() * scale)),
        QtCore.Qt.KeepAspectRatio,
        QtCore.Qt.SmoothTransformation).convertToFormat(
            QtGui.QImage.Format_ARGB32)


def contrast_enhanced(image):
    """Stretch luminance percentiles while retaining the reference's colors."""
    source = image.convertToFormat(QtGui.QImage.Format_ARGB32)
    histogram = [0] * 256
    total = source.width() * source.height()
    for y in range(source.height()):
        for x in range(source.width()):
            red, green, blue = _channels(source.pixel(x, y))
            histogram[(54 * red + 183 * green + 19 * blue) >> 8] += 1
    lower_target = max(1, total // 100)
    upper_target = max(1, total - lower_target)
    cumulative = 0
    low, high = 0, 255
    for value, count in enumerate(histogram):
        cumulative += count
        if cumulative >= lower_target:
            low = value
            break
    cumulative = 0
    for value, count in enumerate(histogram):
        cumulative += count
        if cumulative >= upper_target:
            high = value
            break
    if high <= low + 8:
        low, high = 0, 255
    scale = 255.0 / (high - low)
    result = QtGui.QImage(source.size(), QtGui.QImage.Format_ARGB32)
    for y in range(source.height()):
        for x in range(source.width()):
            red, green, blue = _channels(source.pixel(x, y))
            result.setPixel(
                x, y, _argb(
                    (red - low) * scale,
                    (green - low) * scale,
                    (blue - low) * scale))
    return result


def edge_enhanced(image):
    """Overlay Sobel edges on the original so depth shading remains visible."""
    source = image.convertToFormat(QtGui.QImage.Format_ARGB32)
    width, height = source.width(), source.height()
    gray = [[0] * width for _ in range(height)]
    for y in range(height):
        for x in range(width):
            red, green, blue = _channels(source.pixel(x, y))
            gray[y][x] = (54 * red + 183 * green + 19 * blue) >> 8
    result = source.copy()
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            gx = (
                -gray[y - 1][x - 1] + gray[y - 1][x + 1]
                - 2 * gray[y][x - 1] + 2 * gray[y][x + 1]
                - gray[y + 1][x - 1] + gray[y + 1][x + 1])
            gy = (
                -gray[y - 1][x - 1] - 2 * gray[y - 1][x]
                - gray[y - 1][x + 1] + gray[y + 1][x - 1]
                + 2 * gray[y + 1][x] + gray[y + 1][x + 1])
            strength = min(255, (abs(gx) + abs(gy)) // 3)
            if strength < 36:
                continue
            red, green, blue = _channels(source.pixel(x, y))
            keep = max(0.20, 1.0 - strength / 255.0)
            result.setPixel(
                x, y, _argb(red * keep, green * keep, blue * keep))
    return result


def reference_triptych(image):
    """Return one labelled original/contrast/edge composite image."""
    original = _scaled_panel(image)
    panels = (
        ("ORIGINAL", original),
        ("CONTRAST ENHANCED", contrast_enhanced(original)),
        ("EDGE ENHANCED", edge_enhanced(original)),
    )
    panel_width = original.width()
    width = panel_width * 3 + _GAP * 2
    height = original.height() + _LABEL_HEIGHT
    canvas = QtGui.QImage(width, height, QtGui.QImage.Format_ARGB32)
    canvas.fill(_argb(245, 245, 245))
    painter = QtGui.QPainter(canvas)
    painter.setPen(QtGui.QColor(25, 25, 25))
    font = painter.font()
    font.setBold(True)
    font.setPixelSize(13)
    painter.setFont(font)
    for index, (label, panel) in enumerate(panels):
        left = index * (panel_width + _GAP)
        painter.drawText(
            QtCore.QRect(left, 0, panel_width, _LABEL_HEIGHT),
            QtCore.Qt.AlignCenter, label)
        painter.drawImage(left, _LABEL_HEIGHT, panel)
    painter.end()
    return canvas
