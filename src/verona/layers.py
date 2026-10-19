"""Shared layer state for editing, preview and physical output."""


def printable(item):
    return (item.get("visible", True) and item.get("print", True)
            and ("image_data" in item or bool(item.get("print_style"))))


def cuttable(item):
    return item.get("visible", True) and item.get("cut", True) and not item.get("registration_guide", False)


def editable(item):
    return not item.get("locked", False)
