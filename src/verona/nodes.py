"""Editable line/cubic paths in document mm; polylines remain the cut geometry."""
import copy
import math

from .i18n import tr

TOLERANCE_MM = .025


def validate_path(path):
    if not isinstance(path, dict) or set(path) != {"closed", "nodes"} or type(path["closed"]) is not bool:
        raise ValueError(tr("節點路徑需包含 nodes 與 closed。"))
    nodes = path["nodes"]
    if not isinstance(nodes, list) or not (3 if path["closed"] else 2) <= len(nodes) <= 10000:
        raise ValueError(tr("開放路徑至少 2 點、封閉路徑至少 3 點，最多 10000 點。"))
    result = []
    for node in nodes:
        if not isinstance(node, dict) or "point" not in node or set(node)-{"point", "in", "out"}:
            raise ValueError(tr("節點需有 point，可選 in／out 曲線把手。"))
        safe = {}
        for key in ("point", "in", "out"):
            point = node.get(key)
            if point is None and key != "point":
                safe[key] = None
                continue
            if (not isinstance(point, (list, tuple)) or len(point) != 2
                    or any(isinstance(v, bool) or not isinstance(v, (int, float))
                           or not math.isfinite(v) or abs(v) > 10000 for v in point)):
                raise ValueError(tr("節點座標需為有限毫米數值，絕對值不超過 10000。"))
            safe[key] = tuple(float(v) for v in point)
        result.append(safe)
    return {"closed": path["closed"], "nodes": result}


def editable_path(item, index):
    if type(index) is not int or not 0 <= index < len(item["contours"]):
        raise ValueError("Choose a current contour index.")
    saved = item.get("node_paths", {}).get(str(index))
    if saved is not None:
        return copy.deepcopy(saved)
    contour = item["contours"][index]
    closed = len(contour) > 2 and contour[0] == contour[-1]
    return {"closed": closed, "nodes": [{"point": p, "in": None, "out": None}
            for p in (contour[:-1] if closed else contour)]}


def _middle(a, b):
    return tuple((x+y)/2 for x, y in zip(a, b))


def _split(a, b, c, d):
    ab, bc, cd = _middle(a, b), _middle(b, c), _middle(c, d)
    abc, bcd = _middle(ab, bc), _middle(bc, cd)
    mid = _middle(abc, bcd)
    return (a, ab, abc, mid), (mid, bcd, cd, d)


def flatten_path(path):
    nodes = path["nodes"]
    result = [nodes[0]["point"]]

    def cubic(a, b, c, d, depth=0):
        if len(result) >= 200000:
            raise ValueError(tr("曲線細分超過上限，請縮小曲線或簡化節點。"))
        dx, dy = d[0]-a[0], d[1]-a[1]
        length2 = dx*dx+dy*dy
        def distance(p):
            t = max(0., min(1., ((p[0]-a[0])*dx+(p[1]-a[1])*dy)/length2)) if length2 else 0.
            return math.dist(p, (a[0]+t*dx, a[1]+t*dy))
        if max(distance(b), distance(c)) <= TOLERANCE_MM:
            result.append(d)
        elif depth >= 20 or len(result) > 200000:
            raise ValueError(tr("曲線細分超過上限，請縮小曲線或簡化節點。"))
        else:
            left, right = _split(a, b, c, d)
            cubic(*left, depth+1)
            cubic(*right, depth+1)

    for i in range(len(nodes) if path["closed"] else len(nodes)-1):
        a, d = nodes[i], nodes[(i+1) % len(nodes)]
        if a.get("out") is None and d.get("in") is None:
            result.append(d["point"])
        else:
            cubic(a["point"], a.get("out") or a["point"], d.get("in") or d["point"], d["point"])
    return result


def edit_path(item, index, path):
    if item.get("registration_guide"):
        raise ValueError("Registration guides cannot be node edited.")
    original = editable_path(item, index)
    safe = validate_path(path)
    if safe == original:
        return copy.deepcopy(item)
    result = copy.deepcopy(item)
    result["contours"][index] = flatten_path(safe)
    result.setdefault("node_paths", {})[str(index)] = safe
    return result


def move_node(path, index, point):
    node = path["nodes"][index]
    delta = tuple(v-old for v, old in zip(point, node["point"]))
    for key in ("in", "out"):
        if node.get(key) is not None:
            node[key] = tuple(v+d for v, d in zip(node[key], delta))
    node["point"] = tuple(point)


def insert_node(path, index):
    nodes = path["nodes"]
    if not path["closed"] and index == len(nodes)-1:
        raise ValueError(tr("請選擇一段路徑的起點再插入。"))
    a, d = nodes[index], nodes[(index+1) % len(nodes)]
    if a.get("out") is None and d.get("in") is None:
        new = {"point": _middle(a["point"], d["point"]), "in": None, "out": None}
    else:
        left, right = _split(a["point"], a.get("out") or a["point"], d.get("in") or d["point"], d["point"])
        a["out"], d["in"] = left[1], right[2]
        new = {"point": left[3], "in": left[2], "out": right[1]}
    nodes.insert(index+1, new)


def validate_saved_paths(item, raw):
    if not isinstance(raw, dict) or len(raw) > len(item["contours"]):
        raise ValueError("Invalid saved node paths.")
    safe = {}
    for key, path in raw.items():
        if not isinstance(key, str) or not key.isdecimal() or str(int(key)) != key or int(key) >= len(item["contours"]):
            raise ValueError("Invalid saved node contour index.")
        value = validate_path(path)
        contour = flatten_path(value)
        actual = item["contours"][int(key)]
        if len(contour) != len(actual) or any(math.dist(a, b) > 1e-8 for a, b in zip(contour, actual)):
            raise ValueError("Saved node paths disagree with cutting geometry.")
        safe[key] = value
    return safe
