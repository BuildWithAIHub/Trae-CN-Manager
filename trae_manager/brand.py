"""Original, text-free app mark: account cards with a completion badge."""
from pathlib import Path
import struct

SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">
<rect x="8" y="8" width="240" height="240" rx="58" fill="#172b25"/>
<rect x="45" y="49" width="131" height="145" rx="23" fill="#527a68"/>
<rect x="72" y="69" width="134" height="145" rx="23" fill="#b0f1ca"/>
<circle cx="139" cy="113" r="20" fill="#244b3a"/>
<path d="M105 166c0-30 68-30 68 0v5h-68z" fill="#244b3a"/>
<circle cx="193" cy="194" r="37" fill="#172b25"/>
<path d="m176 194 11 12 23-26" fill="none" stroke="#b0f1ca" stroke-width="11" stroke-linecap="round" stroke-linejoin="round"/>
</svg>'''


def pixmap(size):
    from PySide6.QtCore import Qt, QRectF, QPointF
    from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 256, size / 256)
    painter.setPen(Qt.PenStyle.NoPen)
    for color, rect, radius in [("#172b25", (8, 8, 240, 240), 58),
                                ("#527a68", (45, 49, 131, 145), 23),
                                ("#b0f1ca", (72, 69, 134, 145), 23)]:
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(QRectF(*rect), radius, radius)
    painter.setBrush(QColor("#244b3a"))
    painter.drawEllipse(QPointF(139, 113), 20, 20)
    torso = QPainterPath(QPointF(105, 171))
    torso.lineTo(105, 166)
    torso.cubicTo(105, 136, 173, 136, 173, 166)
    torso.lineTo(173, 171)
    torso.closeSubpath()
    painter.drawPath(torso)
    painter.setBrush(QColor("#172b25"))
    painter.drawEllipse(QPointF(193, 194), 37, 37)
    painter.setPen(QPen(QColor("#b0f1ca"), 11, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    check = QPainterPath(QPointF(176, 194))
    check.lineTo(187, 206)
    check.lineTo(210, 180)
    painter.drawPath(check)
    painter.end()
    return result


def icon():
    from PySide6.QtGui import QIcon
    result = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        result.addPixmap(pixmap(size))
    return result


def write_assets(directory):
    """Write actual multi-resolution ICO, PNG and editable SVG from one mark."""
    from PySide6.QtCore import QBuffer, QIODevice
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    sizes = (16, 24, 32, 48, 64, 128, 256)
    blobs = []
    for size in sizes:
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not pixmap(size).save(buffer, "PNG"):
            raise RuntimeError("App icon encoding failed")
        blobs.append(bytes(buffer.data()))
    offset = 6 + len(sizes) * 16
    entries = []
    for size, blob in zip(sizes, blobs):
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0,
                                   1, 32, len(blob), offset))
        offset += len(blob)
    (directory / "app.ico").write_bytes(struct.pack("<HHH", 0, 1, len(sizes))
                                         + b"".join(entries) + b"".join(blobs))
    (directory / "app.png").write_bytes(blobs[-1])
    (directory / "app.svg").write_text(SVG, encoding="utf-8")
    return directory / "app.ico"
