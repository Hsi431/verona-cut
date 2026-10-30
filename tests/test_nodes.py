"""Node/cubic geometry, saved control consistency and direct Qt gestures."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QTransform
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from verona.commands import encode_document
from verona.editor_actions import build_edit
from verona.editor_document import build_document_edit
from verona.i18n import get_language, set_language
from verona.node_editor import NodeEditorDialog
from verona.nodes import (editable_path, edit_path, flatten_path, insert_node,
                                move_node, validate_saved_paths)
from verona.ui import VeronaWindow, _transform_item
from tests.test_editor_actions import image_item, rectangle


def curved():
    return {"closed": False, "nodes": [
        {"point": (20, 20), "in": None, "out": (20, 40)},
        {"point": (40, 20), "in": (40, 40), "out": None}]}


class NodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.folder.name)
        cls.app = QApplication.instance() or QApplication([])

    def test_node_drag_reuses_untouched_contours_and_their_display_paths(self):
        from verona import artwork
        item = image_item(bleed={"width_mm": 1, "color": "#ffffff"})
        item["contours"] = [[(x + 2*k, 20 + k) for x in range(20, 120)] for k in range(40)]
        original = copy.deepcopy(item)
        path = editable_path(item, 3)
        move_node(path, 5, (30, 30))
        changed = edit_path(item, 3, path)
        self.assertEqual(item, original)
        self.assertTrue(all(changed["contours"][k] is item["contours"][k] for k in range(40) if k != 3))
        self.assertIsNot(changed["bleed"], item["bleed"])
        self.assertEqual(changed["contours"][3][5], (30, 30))
        dialog = NodeEditorDialog(item)
        try:
            dialog.canvas.resize(600, 400)
            dialog.canvas.grab()
            with patch("verona.artwork.contour_path", wraps=artwork.contour_path) as build:
                move_node(dialog.path, 2, (25, 30))
                dialog.changed()
                dialog.canvas.grab()
            # Only the edited contour is rebuilt for display (a first-show fit
            # may still measure the whole item once).
            rebuilt = [call for call in build.call_args_list if len(call.args[0]) == 1]
            self.assertEqual(len(rebuilt), 1)
        finally:
            dialog.deleteLater()

    def test_node_gesture_help_is_available_on_the_canvas(self):
        self.addCleanup(set_language, get_language())
        for language, arrows, zoom in (("en", "Arrow keys", "scroll to zoom"),
                                       ("zh_TW", "方向鍵", "滾輪縮放")):
            with self.subTest(language=language):
                set_language(language)
                dialog = NodeEditorDialog(rectangle())
                self.assertIn(arrows, dialog.canvas.toolTip())
                self.assertIn(zoom, dialog.canvas.toolTip())
                dialog.close()

    def test_cubic_flatten_split_and_move_keep_shape_and_handles(self):
        path = curved()
        before = flatten_path(path)
        self.assertEqual(before[0], (20, 20))
        self.assertEqual(before[-1], (40, 20))
        self.assertEqual(max(y for x, y in before), 35)
        insert_node(path, 0)
        self.assertEqual(path["nodes"][1]["point"], (30, 35))
        self.assertEqual(flatten_path(path), before)
        old = copy.deepcopy(path["nodes"][1])
        move_node(path, 1, (31, 37))
        self.assertEqual(path["nodes"][1]["in"], (old["in"][0]+1, old["in"][1]+2))
        self.assertEqual(path["nodes"][1]["out"], (old["out"][0]+1, old["out"][1]+2))
        # Dense analytical cubic samples are within the promised chord bound.
        for step in range(1001):
            t=step/1000; u=1-t
            point=(20*u**3+60*u*u*t+120*u*t*t+40*t**3, 20*u**3+120*u*u*t+120*u*t*t+20*t**3)
            distances=[]
            for a,b in zip(before,before[1:]):
                dx,dy=b[0]-a[0],b[1]-a[1]
                v=max(0,min(1,((point[0]-a[0])*dx+(point[1]-a[1])*dy)/(dx*dx+dy*dy)))
                distances.append(((point[0]-a[0]-v*dx)**2+(point[1]-a[1]-v*dy)**2)**.5)
            self.assertLessEqual(min(distances), .025)

    def test_node_edit_preserves_image_mask_and_other_contours_and_noop_is_exact(self):
        item = image_item()
        item["contours"].append(rectangle(x=50,y=50)["contours"][0])
        original = copy.deepcopy(item)
        self.assertEqual(edit_path(item, 0, editable_path(item, 0)), original)
        result = edit_path(item, 0, curved())
        self.assertNotEqual(result["contours"][0], original["contours"][0])
        self.assertEqual(result["contours"][1], original["contours"][1])
        for key in ("image_data", "image_clip", "image_transform", "bleed", "cut"):
            self.assertEqual(result.get(key), original.get(key))
        self.assertEqual(item, original)
        result["node_paths"] = validate_saved_paths(result, result["node_paths"])

    def test_transform_save_reopen_and_encoder_share_generated_geometry(self):
        item = edit_path(rectangle(), 0, curved())
        transform = QTransform(-1.2,.2,.3,1.5,100,10)
        _transform_item(item, transform)
        self.assertEqual(flatten_path(item["node_paths"]["0"]), item["contours"][0])
        self.assertEqual(item["node_paths"]["0"]["nodes"][0]["point"], (82.,44.))
        document = {"format":"verona","version":1,"units":"mm","items":[item]}
        items,settings = VeronaWindow._validate_document(json.loads(json.dumps(document)))
        self.assertEqual(items,[item])
        self.assertEqual(encode_document(items,{},settings),encode_document([item],{},settings))
        bad = copy.deepcopy(document)
        bad["items"][0]["contours"][0][0]=(0,0)
        with self.assertRaisesRegex(ValueError,"disagree"):
            VeronaWindow._validate_document(bad)
        for path in ({"closed":1,"nodes":[]},{"closed":False,"nodes":[{"point":[True,0]}]*2},
                {"closed":False,"nodes":[{"point":[float('nan'),0]}]*2},
                {"closed":True,"nodes":[{"point":[0,0]}]*2}):
            with self.assertRaises(ValueError):
                edit_path(item,0,path)

    def test_link_transfers_and_reindexes_saved_controls(self):
        source = edit_path(rectangle(),0,curved())
        source["contours"].append(rectangle(x=60,y=60)["contours"][0])
        source = edit_path(source,1,{"closed":False,"nodes":[{"point":(60,60)},{"point":(70,70),"in":(60,70)}]})
        result,_,_=build_edit([image_item(),source],{},[0,1],"link_cut",{"contour_indices":[1]})
        self.assertEqual(result[0]["node_paths"]["0"],source["node_paths"]["1"])
        self.assertEqual(result[1]["node_paths"]["0"],source["node_paths"]["0"])
        for item in result:
            validate_saved_paths(item,item["node_paths"])

    def test_detached_edit_and_gui_drag_numeric_fields_insert_delete_undo(self):
        window=VeronaWindow()
        window.items=[rectangle()]
        window.refresh_all();window.select(0)
        original=window._document_state()
        dialog=NodeEditorDialog(window.items[0],window)
        try:
            dialog.show();self.app.processEvents();dialog.canvas.fit()
            dialog.handles["out"].setChecked(True)
            dialog.fields["out",0].setValue(25)
            dialog.fields["point",0].setValue(22)
            self.assertEqual(dialog.path["nodes"][0]["out"][0],27)
            start=dialog.canvas.screen(dialog.path["nodes"][0]["point"]).toPoint()
            finish=start+type(start)(10,5)
            QTest.mousePress(dialog.canvas,Qt.MouseButton.LeftButton,pos=start)
            QTest.mouseMove(dialog.canvas,finish)
            QTest.mouseRelease(dialog.canvas,Qt.MouseButton.LeftButton,pos=finish)
            self.assertNotEqual(dialog.path["nodes"][0]["point"],(22.,20.))
            count=len(dialog.path["nodes"])
            dialog.insert();self.assertEqual(len(dialog.path["nodes"]),count+1)
            dialog.remove();self.assertEqual(len(dialog.path["nodes"]),count)
            dialog.hide()
            with patch.object(NodeEditorDialog,"exec",lambda instance: (setattr(instance,"item",dialog.item) or QDialog.DialogCode.Accepted)):
                window.edit_nodes()
            self.assertEqual(len(window.undo_stack),1)
            window.undo();self.assertEqual(window._document_state(),original)
            result=build_document_edit(window.items,{},window._cut_settings(),[0],"edit_path",
                {"contour_index":0,"path":curved()})
            self.assertEqual(result[0][0]["node_paths"]["0"],curved())
            self.assertEqual(window._document_state(),original)
        finally:
            dialog.close();dialog.deleteLater()
            with patch.object(window,"_confirm_unsaved_changes",return_value="discard"):
                window.close()
