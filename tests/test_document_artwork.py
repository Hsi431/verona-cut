"""Physical page imports, explicit vector ink and consistent rendered output."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QMarginsF, QPointF, QSize, QSizeF, Qt
from PySide6.QtGui import QColor, QImage, QPageSize, QPainter, QPdfWriter
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

from verona.artwork import item_bounds
from verona.commands import encode_document
from verona.document_import import pdf_page_count, render_artwork
from verona.editor_actions import build_edit, render_preview
from verona.editor_document import build_document_edit
from verona.packing import _footprint
from verona.printing import write_print_pdf
from verona.raster_import import ImportImageDialog, _png_base64
from verona.ui import VeronaWindow
from verona.vector_print import VectorPrintDialog
from tests.test_editor_actions import image_item, rectangle

SETTINGS={'custom':False,'force':10,'speed':5,'tool':0}
META={'print_cut':{'enabled':True,'paper_width':100,'paper_height':100,'bounds':[10,10,90,90]}}


def pdf(path):
    writer=QPdfWriter(str(path));writer.setResolution(72)
    writer.setPageSize(QPageSize(QSizeF(50,30),QPageSize.Unit.Millimeter));writer.setPageMargins(QMarginsF(0,0,0,0))
    painter=QPainter(writer)
    painter.fillRect(0,0,1000,1000,QColor('#ff0000'))
    writer.newPage();painter.fillRect(0,0,1000,1000,QColor('#0000ff'));painter.end()


class DocumentArtworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def test_pdf_page_size_selection_source_unchanged_and_explicit_resize(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'source.pdf';pdf(path);original=path.read_bytes()
            self.assertEqual(pdf_page_count(path),2)
            image,size,info=render_artwork(path,2,150)
            self.assertEqual(info['pages'],2);self.assertEqual(image.pixelColor(50,50).name(),'#0000ff')
            doc=QPdfDocument();doc.load(str(path));physical=doc.pagePointSize(1);doc.close()
            self.assertAlmostEqual(size['width_mm'],physical.width()*25.4/72)
            for changes,ratio in (({},1),({'width_mm':25},None)):
                items,meta,_,_,details=build_document_edit([rectangle()],copy.deepcopy(META),SETTINGS,[],
                    'import_artwork',{'path':str(path),'page':2,'dpi':150,'tracing':{'mode':'background',**changes}})
                imported=items[-1];transform=imported['image_transform']
                expected=size['width_mm'] if ratio else 25
                self.assertAlmostEqual(transform[2]*image.width(),expected)
                if ratio:self.assertAlmostEqual(transform[5]*image.height(),size['height_mm'])
                self.assertTrue(details['rasterized']);self.assertEqual(meta,META)
            self.assertEqual(path.read_bytes(),original)
            for page in (0,3,True):
                with self.assertRaises(ValueError):render_artwork(path,page)
            with self.assertRaises(ValueError):render_artwork(path,1,6000)

    def test_svg_text_css_refs_gradients_embedded_image_and_mask_render_at_mm_size(self):
        blue=QImage(2,2,QImage.Format.Format_RGBA8888);blue.fill(QColor('#0000ff'))
        image_data=_png_base64(blue)
        source=f'''<svg xmlns="http://www.w3.org/2000/svg" width="50mm" height="30mm" viewBox="0 0 50 30">
        <style>.red{{fill:#ff0000}} .green{{fill:#00ff00}}</style><defs>
        <rect id="r" width="10" height="10"/><linearGradient id="g"><stop offset="0" stop-color="#ff0000"/><stop offset="1" stop-color="#0000ff"/></linearGradient>
        <mask id="m"><rect width="50" height="30" fill="white"/><rect x="42" y="7" width="3" height="3" fill="black"/></mask></defs>
        <use href="#r" x="5" y="5" class="red"/><rect x="20" y="5" width="10" height="10" fill="url(#g)"/>
        <image href="data:image/png;base64,{image_data}" x="32" y="5" width="5" height="5"/>
        <rect x="40" y="5" width="8" height="10" fill="#ff0000" mask="url(#m)"/>
        <text x="5" y="25" font-size="8" class="green">A</text></svg>'''
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'art.svg';path.write_text(source)
            image,size,info=render_artwork(path,1,254)
            self.assertEqual(size,{'width_mm':50.,'height_mm':30.});self.assertEqual(info['pixels'],[500,300])
            self.assertEqual(image.pixelColor(70,70).name(),'#ff0000')
            self.assertEqual(image.pixelColor(340,70).name(),'#0000ff')
            self.assertLess(image.pixelColor(220,80).blue(),image.pixelColor(280,80).blue())
            self.assertEqual(image.pixelColor(435,85).alpha(),0)
            self.assertTrue(any(image.pixelColor(x,y).green()>150 and image.pixelColor(x,y).red()<80
                                for x in range(50,110) for y in range(170,250)))
            with self.assertRaises(ValueError):
                build_document_edit([],{},SETTINGS,[],'import_artwork',{'path':str(path)})

    def test_svg_unsupported_external_cyclic_and_oversize_inputs_reject(self):
        bodies=['<clipPath id="c"><rect width="1" height="1"/></clipPath>', '<filter id="f"/>',
                '<script/>','<image href="file:///tmp/a.png"/>','<style>.r{fill:url(https://example.com/x)}</style>',
                '<style>@import url(a);</style>','<rect style="clip-path:url(#x)"/>',
                '<g id="g"><use href="#g"/></g>','<animate attributeName="x"/>']
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'art.svg'
            for body in bodies:
                path.write_text(f'<svg width="20mm" height="10mm">{body}</svg>')
                with self.subTest(body=body),self.assertRaises(ValueError):render_artwork(path)
            path.write_text('<svg width="400mm" height="10mm"><rect width="1" height="1"/></svg>')
            with self.assertRaises(ValueError):render_artwork(path)
            path.write_text('<svg width="300mm" height="300mm"><rect width="1" height="1"/></svg>')
            with self.assertRaises(ValueError):render_artwork(path,dpi=600)

    def test_vector_print_is_independent_of_cut_and_consistent_in_pdf_preview_footprints(self):
        item=rectangle(x=25,y=25,width=20,height=20,cut=False)
        result,_,_,_,details=build_document_edit([item],META,SETTINGS,[0],'vector_print',
            {'fill':'#ff0000','stroke':'#0000ff','width_mm':2})
        self.assertEqual(result[0]['contours'],item['contours']);self.assertFalse(result[0]['cut'])
        self.assertTrue(details['requires_reprint']);self.assertEqual(item_bounds(result[0],printed=True).left(),24)
        footprint,printed=_footprint(result,True)
        self.assertTrue(printed.contains(QPointF(25,35)))
        self.assertTrue(footprint.contains(QPointF(25,35)))
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'print.pdf';preview=Path(folder)/'preview.png'
            write_print_pdf(output,result,META);render_preview(result,META,preview)
            self.assertEqual(QImage(str(preview)).pixelColor(105,105).name(),'#ff0000')
            doc=QPdfDocument();doc.load(str(output));image=doc.render(0, QSize(1000,1000));doc.close()
            self.assertEqual(image.pixelColor(350,350).name(),'#ff0000')
            self.assertEqual(image.pixelColor(250,350).name(),'#0000ff')
        document={'format':'verona','version':1,'units':'mm','items':result}
        self.assertEqual(VeronaWindow._validate_document(json.loads(json.dumps(document)))[0],result)
        removed=build_document_edit(result,META,SETTINGS,[0],'vector_print',{'fill':None,'stroke':None,'width_mm':.2})[0]
        self.assertEqual(removed,[item])
        cut=copy.deepcopy(result);cut[0]['cut']=True
        self.assertEqual(encode_document(cut,{},SETTINGS),encode_document([{**item,'cut':True}],{},SETTINGS))
        with self.assertRaises(ValueError):build_edit([image_item(),cut[0]],{},[0,1],'link_cut',{})

    def test_gui_pdf_page_import_and_vector_print_are_one_undo(self):
        window=VeronaWindow();window.items=[rectangle()];window.metadata=copy.deepcopy(META);window.refresh_all();window.select(0)
        original=window._document_state()
        try:
            with patch.object(VectorPrintDialog,'exec',lambda instance:QDialog.DialogCode.Accepted):window.edit_vector_print()
            self.assertIn('print_style',window.items[0]);window.undo();self.assertEqual(window._document_state(),original)
            with tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'art.pdf';pdf(path)
                def accept(instance):
                    instance.mode_combo.setCurrentIndex(instance.mode_combo.findData('background'))
                    path.unlink()  # Applying must reuse the already previewed page image.
                    return QDialog.DialogCode.Accepted
                with patch('verona.ui.QFileDialog.getOpenFileName',return_value=(str(path),'')),patch('verona.ui.QInputDialog.getInt',return_value=(2,True)),patch.object(ImportImageDialog,'exec',accept):
                    window.import_document_artwork()
                self.assertEqual(len(window.items),2);self.assertEqual(window.metadata,META)
                self.assertEqual(len(window.undo_stack),1)
                window.undo();self.assertEqual(window._document_state(),original)
        finally:
            with patch.object(window,'_confirm_unsaved_changes',return_value='discard'):window.close()

    def test_malformed_svg_is_a_value_error_in_render_and_gui_import(self):
        window=VeronaWindow()
        try:
            with tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'broken.svg'
                path.write_text('<svg width="20mm" height="10mm"><rect')
                with self.assertRaisesRegex(ValueError,'SVG'):render_artwork(path)
                with patch.object(QFileDialog,'getOpenFileName',return_value=(str(path),'SVG')), \
                        patch.object(QMessageBox,'critical') as critical:
                    window.import_document_artwork()
                critical.assert_called_once()
                self.assertEqual(window.items,[])
        finally:
            with patch.object(window,'_confirm_unsaved_changes',return_value='discard'):window.close()
