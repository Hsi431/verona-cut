"""Deterministic generated inputs shared by packing benchmarks and tests."""
import json
import math
from pathlib import Path

from PySide6.QtGui import QColor, QImage

from verona.raster_import import _png_base64


def polygon_item(name, points, **extra):
    points = list(points)
    if points[0] != points[-1]:
        points.append(points[0])
    return {"name": name, "cut": True, "contours": [points], **extra}


def rectangle(name, width, height, x=0, y=0, **extra):
    return polygon_item(name, [(x, y), (x+width, y), (x+width, y+height), (x, y+height)], **extra)


def circle(name="circle", radius=10, x=0, y=0, sides=48):
    return polygon_item(name, [(x+radius+radius*math.cos(2*math.pi*i/sides),
                                y+radius+radius*math.sin(2*math.pi*i/sides))
                               for i in range(sides)])


def star(name="star", radius=13, inner=6, sides=5, x=0, y=0):
    return polygon_item(name, [(x+radius+(radius if i % 2 == 0 else inner)*math.cos(-math.pi/2+math.pi*i/sides),
                                y+radius+(radius if i % 2 == 0 else inner)*math.sin(-math.pi/2+math.pi*i/sides))
                               for i in range(sides*2)])


def image_rectangle(name="printed sticker", width=20, height=12, bleed=None):
    image = QImage(24, 16, QImage.Format.Format_RGBA8888)
    image.fill(QColor("#df3366"))
    item = rectangle(name, width, height, 0, 0, image_data=_png_base64(image),
                     image_transform=[0, 0, width/24, 0, 0, height/16],
                     source_color=0xFFDF3366)
    item["image_clip"] = item["contours"]
    if bleed:
        item["bleed"] = {"width_mm": bleed, "color": "#df3366"}
    return item


def _demo_item():
    path = Path(__file__).resolve().parents[2] / "examples" / "raster-contour-demo.jdesign"
    return json.loads(path.read_text(encoding="utf-8"))["items"][0]


def scenarios():
    frame = {"print_cut": {"enabled": True, "paper_width": 120, "paper_height": 120,
                           "bounds": [10, 10, 110, 110]}}
    large_frame = {"print_cut": {"enabled": True, "paper_width": 300, "paper_height": 300,
                                 "bounds": [10, 10, 290, 290]}}
    return [
        ("same-size circles", [circle()], frame,
         {"fill": True, "shape_aware": True, "allow_rotate": True, "gap_mm": 1}),
        ("mixed rectangles", [rectangle("wide", 30, 18), rectangle("tall", 14, 27),
                              rectangle("small", 18, 16)], frame,
         {"fill": True, "allow_rotate": True, "gap_mm": 1}),
        ("L and star interlock", [polygon_item("L", [(0,0),(7,0),(7,18),(18,18),(18,25),(0,25)]),
                                  star()], frame,
         {"fill": True, "shape_aware": True, "allow_rotate": True, "gap_mm": 1}),
        ("raster contour demo", [_demo_item()], large_frame,
         {"fill": True, "shape_aware": True, "allow_rotate": True, "gap_mm": 1}),
        ("Print & Cut marks", [image_rectangle()], frame,
         {"fill": True, "shape_aware": True, "allow_rotate": True, "gap_mm": 2}),
        ("printed bleed", [image_rectangle(width=18, height=14, bleed=3)], frame,
         {"fill": True, "shape_aware": True, "allow_rotate": True, "gap_mm": 1}),
    ]
