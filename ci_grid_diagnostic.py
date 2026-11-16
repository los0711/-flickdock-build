import json
from unittest.mock import patch
import test_html_native_workspace as workspace

fixture=workspace.WorkspaceTests('test_card_columns_stay_stable_when_preview_reappears')
fixture.setUp()
try:
    popup=fixture.overview();fixture.shelf.manager.motion=True
    with patch('ui_transitions.motion_allowed',return_value=True):
        popup.change_view_mode('icons')
        workspace.until(lambda:popup._build_cursor==len(popup.visible_refs()))
        popup.set_category('uploading');workspace.flush();motion=popup.preview_width_motion
        if motion.active:motion.animation.setCurrentTime(motion.animation.duration());workspace.flush()
        def sample(label):
            layout=popup.body_layout
            print('GRID_PROBE '+json.dumps(dict(label=label,window=popup.width(),body=layout.geometry().width(),
                  sidebar=popup.sidebar.width(),middle=popup.middle.width(),preview=popup.preview.width(),
                  preview_hidden=popup.preview.isHidden(),scroll=popup.scroll.width(),
                  viewport=popup.scroll.viewport().width(),maximum=popup.scroll.maximumViewportSize().width(),
                  contents=popup.scroll.contentsRect().width(),gutter=popup.scroll.verticalScrollBar().sizeHint().width(),
                  computed=popup._grid_column_count(),columns=popup._grid_columns,
                  available=popup._grid_available_width(),active=motion.active,
                  target=motion.target_width,target_visible=motion.target_visible)),flush=True)
        sample('before')
        popup.set_category('all');workspace.flush();sample('start')
        for step in (0,40,80,120,160,200):
            if motion.active:motion.animation.setCurrentTime(step)
            workspace.flush();sample(str(step))
finally:
    fixture.tearDown()
