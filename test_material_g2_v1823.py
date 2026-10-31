"""Native boundary receives true cubic masks, including overlapping regions."""
import unittest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtWidgets import QWidget
from continuous_corners import continuous_rect_path
from window_material import MaterialRegion, install_material
from test_window_material_v1823 import APP, cocoa_fixture


class MaterialContinuousMasks(unittest.TestCase):
    def setUp(self):
        self.owner=QWidget();self.owner.resize(500,300)
        self.owner.setAttribute(Qt.WA_TranslucentBackground)
        self.native=cocoa_fixture();self.handle=install_material(self.owner,backend=self.native.backend)
        self.owner.show();APP.processEvents()

    def tearDown(self):
        self.handle.close();self.owner.hide();self.owner.deleteLater();APP.processEvents()

    def test_public_region_factory_reaches_appkit_as_eight_g2_cubics(self):
        region=MaterialRegion.rounded('actual-card',QRectF(90,70,300,200),24)
        self.assertEqual(sum(region.path.elementAt(i).isCurveTo() for i in range(region.path.elementCount())),8)
        self.handle.set_regions([region])
        self.assertEqual(sum(op[0]=='curve' for op in self.native.paths[-1].ops),8)

    def test_overlapping_masks_preserve_all_sixteen_cubics_without_boolean_flattening(self):
        first=MaterialRegion('first',continuous_rect_path(QRectF(40,35,220,140),24),.6)
        second=MaterialRegion('second',continuous_rect_path(QRectF(85,55,220,140),24),.6)
        self.handle.set_regions([first,second]);mask=self.native.backend.view.mask
        paths=self.native.paths[-2:]
        self.assertEqual([sum(op[0]=='curve' for op in path.ops) for path in paths],[8,8])
        self.assertEqual(mask.size,(265,160));self.assertTrue(mask.draw(None))
        before=len(self.native.images)
        self.handle.set_regions([first,second])
        self.assertEqual(len(self.native.images),before)
        self.assertEqual(self.native.parent.children,[self.native.backend.view,self.native.native])

    def test_partial_mask_crops_only_image_and_owner_resize_does_not_reuse_old_bounds(self):
        region=MaterialRegion('partial',continuous_rect_path(QRectF(-30,-30,100,100),20))
        self.handle.set_regions([region])
        self.assertEqual(self.native.backend.view.frame,((0.,0.),(70.,70.)))
        self.assertEqual(sum(op[0]=='curve' for op in self.native.paths[-1].ops),8)
        self.assertTrue(any(any(value<0 for value in op[1]) for op in self.native.paths[-1].ops))
        self.owner.resize(50,50);APP.processEvents()
        self.assertEqual(self.native.backend.view.frame,((0.,0.),(50.,50.)))
        self.assertEqual(sum(op[0]=='curve' for op in self.native.paths[-1].ops),8)
        self.handle.set_regions([])
        self.assertTrue(self.native.backend.view.hidden);self.assertIsNone(self.native.backend.view.mask)


if __name__=='__main__':unittest.main()
