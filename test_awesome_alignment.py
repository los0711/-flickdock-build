from types import SimpleNamespace
import unittest

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from translation_alignment import SelectionAlignment
from translation_document import TranslationDocument, editor_to_text_position, text_to_editor_position


SOURCE='😀 cafe\u0301 lights.\r\nOff.'
TARGET='😀 请关灯，随后结束。'


def response():
    return dict(protocol_version='1',offset_unit='unicode_codepoint',model_version='fixture',
        alignment_version='1.0.1',
        alignments=[{'source':[2,7],'target':[[2,3],[3,4],[6,8]]},
                    {'source':[8,14],'target':[[4,5]]}],
        reverse_alignments=[{'target':[2,3],'source':[[2,7]]},
                            {'target':[3,4],'source':[[2,7]]},
                            {'target':[4,5],'source':[[8,14]]},
                            {'target':[6,8],'source':[[2,7]]}])


class Api(QObject):
    document_aligned=Signal(int,object,str)
    aligned=Signal(int,object,str)
    def __init__(self):
        super().__init__()
        self.documents=[]
        self.selections=[]
        self.canceled=0
    def align_document(self,*request):
        self.documents.append(request)
    def align(self,*request):
        self.selections.append(request)
    def cancel(self):
        self.canceled+=1


class AwesomeAlignmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def rig(self, fallback=True):
        document=TranslationDocument()
        document.pair(SOURCE,TARGET)
        api=Api()
        legacy=Api()
        alignment=SelectionAlignment(document,legacy,document_api=api,fallback=fallback)
        hits=[]
        alignment.highlight.connect(lambda side,ranges:hits.append((side,ranges)))
        self.addCleanup(alignment.cancel)
        return SimpleNamespace(document=document,api=api,legacy=legacy,alignment=alignment,hits=hits)

    def test_utf16_ranges_and_reverse_lookup_use_one_cached_map(self):
        h=self.rig()
        h.alignment.prepare(SOURCE,TARGET,'en','zh')
        generation,*body=h.api.documents[0]
        self.assertEqual(body,[SOURCE,TARGET,'en','zh'])
        self.assertEqual(editor_to_text_position(SOURCE,3),2)
        self.assertEqual(editor_to_text_position(SOURCE,text_to_editor_position(SOURCE,17)),17)
        h.alignment.select('source',2,7,SOURCE,TARGET)
        self.assertEqual(h.legacy.selections,[])
        h.api.document_aligned.emit(generation,response(),'')
        self.assertEqual(h.hits[-1],('target',[(2,4),(6,8)]))
        h.alignment.select('target',3,5,SOURCE,TARGET)
        self.assertEqual(h.hits[-1],('source',[(2,7),(8,14)]))
        self.assertEqual(len(h.api.documents),1)
        self.assertEqual(h.legacy.selections,[])

    def test_old_reply_cannot_replace_new_pair_after_an_edit(self):
        h=self.rig()
        h.alignment.prepare(SOURCE,TARGET)
        first=h.api.documents[-1][0]
        new_target=TARGET+'确认。'
        h.document.pair(SOURCE,new_target)
        h.alignment.prepare(SOURCE,new_target)
        second=h.api.documents[-1][0]
        self.assertGreater(second,first)
        h.api.document_aligned.emit(first,response(),'')
        self.assertIsNone(h.alignment._word_map)
        h.api.document_aligned.emit(second,response(),'')
        h.alignment.select('target',3,4,SOURCE,new_target)
        self.assertEqual(h.hits[-1],('source',[(2,7)]))

    def test_outage_only_enables_legacy_when_configured(self):
        for fallback in (True,False):
            h=self.rig(fallback)
            h.alignment.prepare(SOURCE,TARGET)
            h.api.document_aligned.emit(h.api.documents[-1][0],None,'Unavailable')
            h.alignment.select('source',2,7,SOURCE,TARGET)
            h.alignment.pointer_released()
            h.alignment._request()
            self.assertEqual(bool(h.legacy.selections),fallback)
