"""Shared descriptions and availability rules for editable Verona commands."""

from .editor_actions import boolean_indices, editable_indices
from .grouping import selection_units


def _parameter(name, kind, description, minimum=None, maximum=None, choices=None):
    value = {"name": name, "type": kind, "description": description}
    if minimum is not None:
        value["minimum"] = minimum
    if maximum is not None:
        value["maximum"] = maximum
    if choices is not None:
        value["choices"] = list(choices)
    return value


_NUMBER = lambda name, description, low, high: _parameter(name, "number", description, low, high)
_BOOLEAN = lambda name, description: _parameter(name, "boolean", description)
_STRING = lambda name, description: _parameter(name, "string", description)


COMMANDS = (
    {"id": "edit.duplicate", "label_zh": "複製物件", "label_en": "Duplicate object",
     "menu": ["編輯"], "shortcut": "Ctrl+D", "action": "duplicate",
     "defaults": {"dx_mm": 5, "dy_mm": 5},
     "parameters": [_NUMBER("dx_mm", "複製後的水平位移（mm）", -300, 300),
                    _NUMBER("dy_mm", "複製後的垂直位移（mm）", -300, 300)],
     "selection": (1, None), "kind": "editable"},
    {"id": "edit.delete", "label_zh": "刪除物件", "label_en": "Delete object",
     "menu": ["編輯"], "shortcut": "Delete", "action": "delete", "parameters": [],
     "selection": (1, None), "kind": "editable"},
    {"id": "edit.transform", "label_zh": "變形", "label_en": "Transform",
     "menu": ["變形"], "shortcut": "", "action": "transform",
     "parameters": [_NUMBER("dx_mm", "水平位移（mm）", -300, 300),
                    _NUMBER("dy_mm", "垂直位移（mm）", -300, 300),
                    _NUMBER("scale", "等比例縮放倍數", .001, 1000),
                    _NUMBER("rotation_deg", "旋轉角度", -360, 360),
                    _BOOLEAN("mirror_x", "水平鏡像"), _BOOLEAN("mirror_y", "垂直鏡像")],
     "selection": (1, None), "kind": "editable"},
    {"id": "edit.set_cut", "label_zh": "設定切割", "label_en": "Set Cutting",
     "menu": ["圖層"], "shortcut": "", "action": "set_cut",
     "parameters": [_BOOLEAN("enabled", "是否啟用切割")],
     "selection": (1, None), "kind": "editable"},
    {"id": "edit.group", "label_zh": "群組", "label_en": "Group", "menu": ["編輯"],
     "shortcut": "Ctrl+G", "action": "group", "parameters": [],
     "selection": (2, None), "kind": "group"},
    {"id": "edit.ungroup", "label_zh": "解散群組", "label_en": "Ungroup", "menu": ["編輯"],
     "shortcut": "Ctrl+Shift+G", "action": "ungroup", "parameters": [],
     "selection": (1, None), "kind": "ungroup"},
    {"id": "arrange.bring_forward", "label_zh": "上移一層", "label_en": "Bring forward one level",
     "menu": ["編輯", "排列順序"], "shortcut": "Ctrl+]", "action": "layer_order",
     "fixed": {"direction": 1}, "parameters": [], "selection": (1, None), "kind": "editable"},
    {"id": "arrange.send_backward", "label_zh": "下移一層", "label_en": "Send backward one level",
     "menu": ["編輯", "排列順序"], "shortcut": "Ctrl+[", "action": "layer_order",
     "fixed": {"direction": -1}, "parameters": [], "selection": (1, None), "kind": "editable"},
    {"id": "arrange.bring_to_front", "label_zh": "移至最上層", "label_en": "Bring to front",
     "menu": ["編輯", "排列順序"], "shortcut": "Ctrl+Shift+]", "action": "layer_order",
     "fixed": {"direction": 2}, "parameters": [], "selection": (1, None), "kind": "editable"},
    {"id": "arrange.send_to_back", "label_zh": "移至最下層", "label_en": "Send to back",
     "menu": ["編輯", "排列順序"], "shortcut": "Ctrl+Shift+[", "action": "layer_order",
     "fixed": {"direction": -2}, "parameters": [], "selection": (1, None), "kind": "editable"},
    {"id": "path.weld", "label_zh": "焊接", "label_en": "Weld", "menu": ["編輯", "路徑運算"],
     "shortcut": "Ctrl+Shift+U", "action": "boolean", "fixed": {"mode": "weld"},
     "parameters": [], "selection": (2, None), "kind": "boolean"},
    {"id": "path.subtract", "label_zh": "減去上層", "label_en": "Minus Front",
     "menu": ["編輯", "路徑運算"], "shortcut": "", "action": "boolean",
     "fixed": {"mode": "subtract"}, "parameters": [], "selection": (2, None), "kind": "boolean"},
    {"id": "path.intersect", "label_zh": "交集", "label_en": "Intersect",
     "menu": ["編輯", "路徑運算"], "shortcut": "", "action": "boolean",
     "fixed": {"mode": "intersect"}, "parameters": [], "selection": (2, None), "kind": "boolean"},
    {"id": "path.exclude", "label_zh": "排除重疊", "label_en": "Exclude overlap",
     "menu": ["編輯", "路徑運算"], "shortcut": "", "action": "boolean",
     "fixed": {"mode": "exclude"}, "parameters": [], "selection": (2, None), "kind": "boolean"},
    {"id": "align.left", "label_zh": "靠左", "label_en": "Left", "menu": ["對齊與均分"],
     "shortcut": "", "action": "align", "fixed": {"mode": "left"}, "parameters": [],
     "selection": (2, None), "kind": "align"},
    {"id": "align.hcenter", "label_zh": "水平置中", "label_en": "Center H",
     "menu": ["對齊與均分"], "shortcut": "", "action": "align", "fixed": {"mode": "hcenter"},
     "parameters": [], "selection": (2, None), "kind": "align"},
    {"id": "align.right", "label_zh": "靠右", "label_en": "Right", "menu": ["對齊與均分"],
     "shortcut": "", "action": "align", "fixed": {"mode": "right"}, "parameters": [],
     "selection": (2, None), "kind": "align"},
    {"id": "align.top", "label_zh": "靠上", "label_en": "Top", "menu": ["對齊與均分"],
     "shortcut": "", "action": "align", "fixed": {"mode": "top"}, "parameters": [],
     "selection": (2, None), "kind": "align"},
    {"id": "align.vcenter", "label_zh": "垂直置中", "label_en": "Center V",
     "menu": ["對齊與均分"], "shortcut": "", "action": "align", "fixed": {"mode": "vcenter"},
     "parameters": [], "selection": (2, None), "kind": "align"},
    {"id": "align.bottom", "label_zh": "靠下", "label_en": "Bottom", "menu": ["對齊與均分"],
     "shortcut": "", "action": "align", "fixed": {"mode": "bottom"}, "parameters": [],
     "selection": (2, None), "kind": "align"},
    {"id": "align.distribute_h", "label_zh": "水平均分", "label_en": "Distribute Horizontally",
     "menu": ["對齊與均分"], "shortcut": "", "action": "align", "fixed": {"mode": "distribute_h"},
     "parameters": [], "selection": (3, None), "kind": "align"},
    {"id": "align.distribute_v", "label_zh": "垂直均分", "label_en": "Distribute Vertically",
     "menu": ["對齊與均分"], "shortcut": "", "action": "align", "fixed": {"mode": "distribute_v"},
     "parameters": [], "selection": (3, None), "kind": "align"},
    {"id": "path.link_cut", "label_zh": "將選取切線附到圖片", "label_en": "Attach selected cut paths to image",
     "menu": ["編輯"], "shortcut": "", "action": "link_cut",
     "parameters": [_parameter("contour_indices", "integer[]", "要移轉的來源輪廓索引；省略時全部移轉", 0)],
     "selection": (2, 2), "kind": "link_cut"},
    {"id": "path.edit", "label_zh": "編輯節點與曲線…", "label_en": "Edit nodes and curves…",
     "menu": ["編輯"], "shortcut": "", "action": "edit_path",
     "parameters": [_parameter("contour_index", "integer", "輪廓索引", 0),
                    _parameter("path", "object", "包含 nodes 與 closed 的路徑資料")],
     "selection": (1, 1), "kind": "vector_one"},
    {"id": "print.vector_style", "label_zh": "設定向量列印…", "label_en": "Set vector printing…",
     "menu": ["編輯"], "shortcut": "", "action": "vector_print",
     "parameters": [_STRING("fill", "填色 #RRGGBB 或 null"), _STRING("stroke", "線條 #RRGGBB 或 null"),
                    _NUMBER("width_mm", "列印線寬（mm）", .01, 5)],
     "selection": (1, None), "kind": "vector"},
    {"id": "print.bleed", "label_zh": "設定列印出血…", "label_en": "Set printed bleed…",
     "menu": ["編輯"], "shortcut": "", "action": "bleed",
     "parameters": [_NUMBER("width_mm", "外側出血寬度（mm）", 0, 5), _STRING("color", "#RRGGBB 或 source"),
                    _NUMBER("inner_mm", "內側色帶寬度（mm）", 0, 1),
                    _parameter("mode", "string", "出血方式", choices=("solid", "edge"))],
     "selection": (1, None), "kind": "image"},
    {"id": "arrange.pack", "label_zh": "自動排版…", "label_en": "Automatic layout…",
     "menu": ["編輯"], "shortcut": "", "action": "pack_objects",
     "parameters": [_parameter("copies_per_unit", "integer[]", "各物件份數，依索引順序", 1, 1000),
                    _NUMBER("gap_mm", "物件間距（mm）", 0, 50), _NUMBER("margin_mm", "工作區留邊（mm）", 0, 150),
                    _BOOLEAN("allow_rotate", "允許旋轉"),
                    _BOOLEAN("shape_aware", "依實際輪廓計算佔用區"), _BOOLEAN("fill", "繼續填滿剩餘範圍"),
                    _parameter("rotation_step_deg", "integer", "輪廓旋轉角度間隔(度;需 allow_rotate 與 shape_aware)", 5, 90),
                    _NUMBER("time_limit_seconds", "搜尋時間上限(秒)", .1, 60),
                    _BOOLEAN("new_page_for_unplaced", "放不下的份數開新分頁")],
     "selection": (1, None), "kind": "editable"},
    {"id": "arrange.fill_area", "label_zh": "填滿切割範圍", "label_en": "Fill Cutting Area",
     "menu": ["排版"], "shortcut": "", "action": "fill_area",
     "parameters": [_NUMBER("gap_mm", "副本間距（mm）", 0, 300),
                    _NUMBER("margin_mm", "工作區留邊（mm）", 0, 150),
                    _parameter("max_copies", "integer", "副本數量上限", 1, 1000),
                    _BOOLEAN("allow_rotate", "允許整組旋轉 90 度")],
     "selection": (1, None), "kind": "editable"},
    {"id": "path.optimize", "label_zh": "自動排序切割路徑（內孔先切）", "label_en": "Optimize cut order (inside first)",
     "menu": ["編輯"], "shortcut": "", "action": "path_order",
     "defaults": {"enabled": True}, "parameters": [_BOOLEAN("enabled", "啟用路徑排序")],
     "selection": (0, None), "kind": "any"},
    {"id": "path.smooth", "label_zh": "平滑切割線", "label_en": "Smooth cutlines",
     "menu": ["後編輯"], "shortcut": "", "action": "smooth",
     "parameters": [_NUMBER("tolerance_mm", "最大偏移（mm）", 0, .5)],
     "selection": (1, None), "kind": "smooth"},
    {"id": "shape.add_rectangle", "label_zh": "矩形", "label_en": "Rectangle", "menu": ["插入"],
     "shortcut": "", "action": "add_shape", "fixed": {"shape": "rectangle"},
     "defaults": {"x_mm": 130, "y_mm": 137.5, "width_mm": 40, "height_mm": 25},
     "parameters": [_NUMBER("x_mm", "左上角 X 座標（mm）", -300, 600),
                    _NUMBER("y_mm", "左上角 Y 座標（mm）", -300, 600),
                    _NUMBER("width_mm", "寬度（mm）", .01, 300), _NUMBER("height_mm", "高度（mm）", .01, 300),
                    _STRING("name", "物件名稱")], "selection": (0, None), "kind": "any"},
    {"id": "shape.add_ellipse", "label_zh": "圓形", "label_en": "Ellipse", "menu": ["插入"],
     "shortcut": "", "action": "add_shape", "fixed": {"shape": "ellipse"},
     "defaults": {"x_mm": 135, "y_mm": 135, "width_mm": 30, "height_mm": 30},
     "parameters": [_NUMBER("x_mm", "左上角 X 座標（mm）", -300, 600),
                    _NUMBER("y_mm", "左上角 Y 座標（mm）", -300, 600),
                    _NUMBER("width_mm", "寬度（mm）", .01, 300), _NUMBER("height_mm", "高度（mm）", .01, 300),
                    _STRING("name", "物件名稱")], "selection": (0, None), "kind": "any"},
    {"id": "text.add", "label_zh": "文字輪廓", "label_en": "Text Outline", "menu": ["插入"],
     "shortcut": "", "action": "add_text", "parameters": [_STRING("text", "文字內容，1–1024 字元"),
        _NUMBER("x_mm", "左上角 X 座標（mm）", -300, 600), _NUMBER("y_mm", "左上角 Y 座標（mm）", -300, 600),
        _NUMBER("font_size_mm", "字級（mm）", .1, 100), _STRING("font_family", "字型家族"),
        _STRING("font_style", "字型樣式"), _NUMBER("height_mm", "可見字高（mm）", .1, 100),
        _NUMBER("letter_spacing_mm", "字距（mm）", -5, 50),
        _NUMBER("line_spacing_mm", "行距（基線距離，需提供 height_mm）", 0, 300),
        _parameter("alignment", "string", "多行文字對齊", choices=("left", "center", "right")),
        _STRING("name", "物件名稱")],
     "selection": (0, None), "kind": "any"},
    {"id": "image.retrace", "label_zh": "重新描圖", "label_en": "Retrace Image",
     "menu": ["圖片"], "shortcut": "", "action": "retrace_image",
     "parameters": [_parameter("mask_mode", "string", "遮罩來源", choices=("saved", "alpha", "background", "colors")),
                    _parameter("tolerance", "integer", "色彩容差", 0, 255),
                    _parameter("smoothing", "integer", "平滑程度", 0, 100),
                    _NUMBER("minimum_area_mm2", "最小區域面積（mm²）", 0, 1000),
                    _NUMBER("offset_mm", "輪廓偏移（mm）", -5, 5),
                    _parameter("brush_strokes", "object[]", "包含 mode、width_mm、points_mm 的筆刷描線")],
     "selection": (1, 1), "kind": "image"},
    {"id": "document.configure", "label_zh": "設定紙張與文件…", "label_en": "Configure Paper and Document…",
     "menu": ["文件設定"], "shortcut": "", "action": "configure",
     "parameters": [_parameter("registration", "object", "紙張尺寸、出血框、定位標記及印表機名稱"),
                    _parameter("cut_settings", "object", "自訂刀具、壓力、速度、刀具編號與次數")],
     "selection": (0, None), "kind": "any"},
    {"id": "document.import_artwork", "label_zh": "匯入圖稿", "label_en": "Import Artwork",
     "menu": ["文件"], "shortcut": "", "action": "import_artwork",
     "parameters": [_STRING("path", "SVG、PDF 或支援的點陣圖路徑"),
                    _NUMBER("x_mm", "左上角 X 座標（mm）", -300, 600),
                    _NUMBER("y_mm", "左上角 Y 座標（mm）", -300, 600),
                    _parameter("tracing", "object", "點陣描圖參數"),
                    _parameter("representation", "string", "匯入表示法", choices=("vector", "raster")),
                    _parameter("page", "integer", "PDF 頁碼", 1),
                    _parameter("dpi", "integer", "點陣解析度", 75, 600)],
     "selection": (0, None), "kind": "any"},
)


COMMANDS_BY_ID = {command["id"]: command for command in COMMANDS}


# Each exclusion has a stable ID and a reason. These are UI actions whose
# effect is outside the detached, undoable artwork edit model.
MENU_EXCLUSIONS = {
    "file.new": "Creates or switches a document tab; use Verona document tools.",
    "file.open": "Opens a file dialog; MCP uses verona_editor_open with an explicit path.",
    "file.save": "Opens a file dialog; MCP uses verona_editor_save with an explicit path.",
    "file.save_as": "Opens a file dialog; MCP uses verona_editor_save with an explicit path.",
    "file.import_svg": "Opens an import dialog; MCP uses document.import_artwork with an explicit path.",
    "file.import_image": "Opens an import dialog; MCP uses document.import_artwork with explicit parameters.",
    "file.import_document": "Opens an import dialog; MCP uses document.import_artwork with an explicit path.",
    "file.export_plt": "Writes an external file; use the dedicated export workflow.",
    "file.native_preview": "Shows a source-device preview; it is not an artwork edit.",
    "file.print": "Submits a printer job; device and print actions stay on dedicated tools.",
    "file.export_pdf": "Writes an external file; use verona_editor_export_pdf.",
    "file.close_tab": "Closes a document tab; use dedicated document controls.",
    "file.next_tab": "Changes the active document tab.",
    "file.previous_tab": "Changes the active document tab.",
    "edit.copy": "Uses the GUI clipboard; it does not change the document or create an undo step.",
    "edit.cut": "Uses the GUI clipboard; it is a compound clipboard operation.",
    "edit.paste": "Uses transient GUI clipboard contents; MCP edits require explicit parameters.",
    "edit.paste_in_place": "Uses transient GUI clipboard contents; MCP edits require explicit parameters.",
    "edit.undo": "History is exposed by verona_editor_history.",
    "edit.redo": "History is exposed by verona_editor_history.",
    "edit.deselect": "Changes only the GUI selection; MCP uses verona_editor_select.",
    "edit.edit_mask": "Opens an interactive mask editor; retrace_image accepts explicit parameters.",
    "edit.edit_text": "Opens an interactive text dialog; text.add creates editable text from parameters.",
    "edit.outline_text": "Converts existing text metadata in place; no detached action currently supports it.",
    "edit.select_all": "Changes only the GUI selection; MCP uses verona_editor_select.",
    "view.zoom_in": "Changes only the viewport.",
    "view.zoom_out": "Changes only the viewport.",
    "view.zoom_actual": "Changes only the viewport.",
    "view.fit_height": "Changes only the viewport.",
    "view.guides_visible": "Changes a user interface preference, not the artwork document.",
    "view.guides_locked": "Changes a user interface preference, not the artwork document.",
    "view.smart_guides": "Changes a user interface preference, not the artwork document.",
    "view.guides_clear": "Clears interactive canvas guides; guides are not document artwork objects.",
    "view.theme_light": "Changes only the application appearance.",
    "view.theme_dark": "Changes only the application appearance.",
    "language.english": "Changes only the application language.",
    "language.traditional_chinese": "Changes only the application language.",
    "help.system_info": "Reads device/system information; it is not an edit command.",
    "help.firmware": "Starts a device firmware workflow; hardware actions stay outside the command table.",
}


MENU_ACTION_IDS = {
    "edit.duplicate": "edit.duplicate", "edit.delete": "edit.delete",
    "edit.group": "edit.group", "edit.ungroup": "edit.ungroup",
    "arrange.bring_forward": "arrange.bring_forward",
    "arrange.send_backward": "arrange.send_backward",
    "arrange.bring_to_front": "arrange.bring_to_front",
    "arrange.send_to_back": "arrange.send_to_back",
    "path.weld": "path.weld", "path.subtract": "path.subtract",
    "path.intersect": "path.intersect", "path.exclude": "path.exclude",
    "path.link_cut": "path.link_cut", "path.edit": "path.edit",
    "print.vector_style": "print.vector_style", "print.bleed": "print.bleed",
    "arrange.pack": "arrange.pack", "path.optimize": "path.optimize",
    "shape.add_rectangle": "shape.add_rectangle", "shape.add_ellipse": "shape.add_ellipse",
    "text.add": "text.add",
}


def command_for_step(command_id, parameters):
    command = COMMANDS_BY_ID.get(command_id)
    if command is None:
        raise ValueError(f"Unknown command_id: {command_id}.")
    if not isinstance(parameters, dict):
        raise ValueError("parameters must be an object.")
    allowed = {parameter["name"] for parameter in command["parameters"]}
    if set(parameters) - allowed:
        raise ValueError(f"Unknown parameters for {command_id}.")
    return command["action"], {**command.get("defaults", {}), **command.get("fixed", {}), **parameters}


def command_availability(items, command_id, object_indices):
    command = COMMANDS_BY_ID.get(command_id)
    if command is None:
        return False, "Unknown command."
    if (not isinstance(object_indices, list)
            or any(type(index) is not int or not 0 <= index < len(items) for index in object_indices)):
        return False, "Invalid object indices."
    kind = command["kind"]
    if kind == "any":
        return True, ""
    if kind == "boolean":
        try:
            boolean_indices(items, object_indices)
            return True, ""
        except ValueError as error:
            return False, str(error)
    if kind == "link_cut":
        if len(object_indices) != 2:
            return False, "Choose one image and one separate cutting-path object."
        selected = [items[index] for index in object_indices]
        if sum("image_data" in item for item in selected) != 1:
            return False, "Choose one image and one separate cutting-path object."
        if any(item.get("registration_guide") for item in selected):
            return False, "Registration guides cannot be edited through these tools."
        if any(item.get("locked", False) for item in selected):
            return False, "Unlock the selected layers before editing them."
        return True, ""
    if kind == "image":
        try:
            indices = editable_indices(items, object_indices)
        except ValueError as error:
            return False, str(error)
        minimum, maximum = command["selection"]
        if len(indices) < minimum or maximum is not None and len(indices) > maximum:
            return False, f"Select {minimum if maximum == minimum else f'at least {minimum}'} editable image object(s)."
        if any("image_data" not in items[index] for index in indices):
            return False, "Choose image objects for this command."
        return True, ""
    if kind == "smooth":
        try:
            indices = editable_indices(items, object_indices)
        except ValueError as error:
            return False, str(error)
        minimum, maximum = command["selection"]
        if len(indices) < minimum or maximum is not None and len(indices) > maximum:
            return False, f"Select at least {minimum} editable object(s)."
        if not any(items[index].get("cut", True) and items[index].get("contours") for index in indices):
            return False, "Choose objects with cut contours."
        return True, ""
    if kind == "vector":
        try:
            indices = editable_indices(items, object_indices)
        except ValueError as error:
            return False, str(error)
        minimum, maximum = command["selection"]
        if len(indices) < minimum or maximum is not None and len(indices) > maximum:
            return False, f"Select at least {minimum} editable vector object(s)."
        if any("image_data" in items[index] for index in indices):
            return False, "Choose ordinary vector objects."
        return True, ""
    try:
        indices = editable_indices(items, object_indices) if object_indices else []
    except ValueError as error:
        return False, str(error)
    minimum, maximum = command["selection"]
    units = selection_units(items, indices) if indices else []
    count = len(units) if kind in ("group", "align") else len(indices)
    required = 3 if kind == "align" and command.get("fixed", {}).get("mode", "").startswith("distribute_") else minimum
    if count < required or maximum is not None and count > maximum:
        return False, f"Select at least {required} editable object(s)."
    if kind == "ungroup" and not any(items[index].get("group_path") for index in indices):
        return False, "Choose a grouped object."
    if kind == "vector_one" and (len(indices) != 1 or "image_data" in items[indices[0]]):
        return False, "Choose one vector object."
    return True, ""


def command_list(items, object_indices=None, filter_text=None):
    indices = [] if object_indices is None else object_indices
    normalized_filter = filter_text.casefold() if isinstance(filter_text, str) else None
    values = []
    for command in COMMANDS:
        if normalized_filter and not any(normalized_filter in str(value).casefold()
                for value in (command["id"], command["label_zh"], command["label_en"], *command["menu"])):
            continue
        enabled, reason = command_availability(items, command["id"], indices)
        minimum, maximum = command["selection"]
        values.append({"id": command["id"], "label": {"zh_TW": command["label_zh"], "en": command["label_en"]},
                       "menu_path": command["menu"], "shortcut": command["shortcut"],
                       "parameters": command["parameters"],
                       "selection": {"minimum": minimum, "maximum": maximum,
                                     "object_types": {"editable": "editable artwork objects",
                                                      "boolean": "closed vector paths only",
                                                      "image": "image objects only",
                                                      "link_cut": "one image and one vector path",
                                                      "vector": "ordinary vector objects only",
                                                      "vector_one": "one vector object",
                                                      "group": "two or more independent editable units",
                                                      "ungroup": "a grouped editable object",
                                                      "align": "two objects, or three for distribution",
                                                      "smooth": "editable objects with cut contours",
                                                      "any": "no object selection is required"}[command["kind"]],
                                     "accepts_images": command["kind"] in (
                                         "image", "link_cut", "editable", "smooth", "group", "ungroup", "align"),
                                     "accepts_locked": False},
                       "enabled": enabled, "disabled_reason": reason or None})
    return values
