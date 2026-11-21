"""Live monitor diagram and independent edge-region controls."""
from copy import deepcopy
from PySide6.QtCore import QEvent,QRectF,QTimer,Qt,Signal
from PySide6.QtGui import QColor,QGuiApplication,QPainter,QPen
from PySide6.QtWidgets import QWidget,QLabel,QPushButton,QVBoxLayout,QHBoxLayout,QGridLayout
from magnetic_settings import EDGES,LABELS,default_edges,region_rect,validate
from display_triggers import display_id


from monitor_diagram import MonitorDiagram

class MagneticEditor(QWidget):
    changed=Signal()
    def __init__(self,store,parent=None):
        super().__init__(parent);self.store=store;self._draft=store.magnetic_edges;self._loading=False;self.selected='left'
        from app_settings import SettingsToggleSwitch,SettingsComboBox,SettingsSlider,SettingsSpinBox,_SettingsDivider,_SettingsParamCell
        box=QVBoxLayout(self);box.setContentsMargins(14,12,14,14);box.setSpacing(12)
        header=QHBoxLayout();title=QLabel('文件边缘磁吸',self);title.setObjectName('settingsSection');header.addWidget(title,1)
        self.toggle=SettingsToggleSwitch('',self);self.toggle.setMinimumSize(44,32);self.toggle.setMaximumSize(44,32);self.toggle.setAccessibleName('开启文件边缘磁吸');header.addWidget(self.toggle);box.addLayout(header)
        box.addWidget(_SettingsDivider(self))
        screen_cell=_SettingsParamCell(self,role='card',radius=14);line=QHBoxLayout(screen_cell);line.setContentsMargins(12,8,12,8);line.setSpacing(10)
        screen_lbl=QLabel('显示器',screen_cell);screen_lbl.setObjectName('settingsDetail');line.addWidget(screen_lbl,1)
        self.screen_combo=SettingsComboBox(screen_cell);self.screen_combo.setMinimumWidth(190);line.addWidget(self.screen_combo);box.addWidget(screen_cell)
        self.diagram=MonitorDiagram(self);box.addWidget(self.diagram)
        box.addWidget(_SettingsDivider(self))
        ctrl_row=QHBoxLayout();ctrl_row.setSpacing(10)
        directions=QHBoxLayout();directions.setSpacing(4);self.edge_buttons={}
        for edge in EDGES:
            button=QPushButton(LABELS[edge],self);button.setCheckable(True);button.setAutoExclusive(True)
            button.setObjectName('settingsLink');button.clicked.connect(lambda _,edge=edge:self.select(edge));directions.addWidget(button)
            self.edge_buttons[edge]=button
        ctrl_row.addLayout(directions);ctrl_row.addStretch(1)
        self.edge_label=QLabel(self);ctrl_row.addWidget(self.edge_label)
        self.edge_toggle=SettingsToggleSwitch('',self);self.edge_toggle.setMinimumSize(44,32);self.edge_toggle.setMaximumSize(44,32);self.edge_toggle.setAccessibleName('启用当前磁吸边缘');ctrl_row.addWidget(self.edge_toggle)
        box.addLayout(ctrl_row)
        self.controls={};self.sliders={};self.labels={}
        grid=QGridLayout();grid.setHorizontalSpacing(10);grid.setVerticalSpacing(10)
        for index,(key,label,low,high,suffix) in enumerate((('width','触发宽度',8,240,' px'),('length','区域长度',5,100,' %'),('position','上下位置',0,100,' %'))):
            panel=_SettingsParamCell(self,role='card',radius=14);layout=QVBoxLayout(panel);layout.setContentsMargins(12,10,12,10);layout.setSpacing(6)
            row=QHBoxLayout();text=QLabel(label,panel);row.addWidget(text,1);self.labels[key]=text
            spin=SettingsSpinBox(panel);spin.setRange(low,high);spin.setSuffix(suffix);spin.setFixedSize(84,28);spin.setAccessibleName('磁吸'+label);row.addWidget(spin);layout.addLayout(row)
            slider=SettingsSlider(Qt.Horizontal,panel);slider.setRange(low,high);slider.setFixedHeight(22);slider.setAccessibleName('磁吸'+label);layout.addWidget(slider)
            slider.valueCaption=lambda value,unit=suffix:f'{value}{unit}'
            spin.valueChanged.connect(slider.setValue);slider.valueChanged.connect(spin.setValue)
            spin.valueChanged.connect(self.edit_current);self.controls[key]=spin;self.sliders[key]=slider
            grid.addWidget(panel,index//2,0 if index==2 else index%2,1,2 if index==2 else 1)
        box.addLayout(grid)
        hint=QLabel('点击显示器边缘开关磁吸；下方可分别调整每条边。',self);hint.setObjectName('settingsDetail');hint.setWordWrap(True);box.addWidget(hint)
        self.toggle.toggled.connect(self.master_changed);self.edge_toggle.toggled.connect(self.edit_current)
        self.diagram.edge_clicked.connect(self.toggle_edge);self.screen_combo.currentIndexChanged.connect(self.load_current)
        app=QGuiApplication.instance();app.screenAdded.connect(self.refresh_screens);app.screenRemoved.connect(self.refresh_screens)
        self._fit_timer=QTimer(self);self._fit_timer.setSingleShot(True);self._fit_timer.timeout.connect(self._fit_height)
        self.refresh_screens();self.load_values();self.apply_theme(store.theme);self._fit_height()

    def event(self,event):
        if event.type()==QEvent.LayoutRequest and hasattr(self,'_fit_timer'):self._fit_timer.start(0)
        return super().event(event)

    def _fit_height(self):
        self.layout().invalidate();self.layout().activate();height=self.layout().sizeHint().height()
        if self.minimumHeight()!=height or self.maximumHeight()!=height:
            self.setFixedHeight(height);self.updateGeometry()

    def hideEvent(self,event):
        self._fit_timer.stop();super().hideEvent(event)

    def showEvent(self,event):
        super().showEvent(event);self._fit_timer.start(0)

    def values(self):return validate(self._draft)
    def key(self):return self.screen_combo.currentData()
    def rows(self):return self._draft['displays'].get(self.key(),default_edges())

    def refresh_screens(self,*_):
        previous=self.key();self.screen_combo.blockSignals(True);self.screen_combo.clear()
        primary=QGuiApplication.primaryScreen()
        for screen in QGuiApplication.screens():
            self.screen_combo.addItem((screen.name() or '显示器')+(' · 主屏' if screen is primary else ''),display_id(screen))
        index=self.screen_combo.findData(previous);self.screen_combo.setCurrentIndex(max(0,index));self.screen_combo.blockSignals(False);self.load_current()

    def load_values(self):
        self._draft=self.store.magnetic_edges;self._loading=True;self.toggle.setChecked(self._draft['enabled']);self._loading=False;self.load_current()

    def select(self,edge):self.selected=edge;self.load_current()

    def load_current(self,*_):
        self._loading=True;rows=self.rows();row=rows[self.selected]
        self.edge_buttons[self.selected].setChecked(True);self.edge_label.setText(LABELS[self.selected]+'磁吸')
        self.edge_toggle.setChecked(row['enabled']);self.labels['position'].setText('上下位置' if self.selected in ('left','right') else '左右位置')
        for key,control in self.controls.items():control.setValue(row[key])
        self.diagram.set_values(rows,self._draft['enabled'],self.selected);self._loading=False

    def edit_current(self,*_):
        if self._loading or not self.key():return
        rows=deepcopy(self.rows());rows[self.selected]=dict(enabled=self.edge_toggle.isChecked(),**{key:control.value() for key,control in self.controls.items()})
        self._draft['displays'][self.key()]=rows;self.diagram.set_values(rows,self._draft['enabled'],self.selected);self.changed.emit()

    def toggle_edge(self,edge):
        if not self.key():return
        rows=deepcopy(self.rows());rows[edge]['enabled']=not rows[edge]['enabled'];self._draft['displays'][self.key()]=rows
        self.selected=edge;self.load_current();self.changed.emit()

    def master_changed(self,value):
        if self._loading:return
        self._draft['enabled']=bool(value);self.diagram.set_values(self.rows(),value,self.selected);self.changed.emit()

    def paintEvent(self,event):
        from native_ui_common import colors as ui_colors,paint_rounded_surface,qss_color
        theme=getattr(self.window(),'_applied_settings_theme','light')
        c=ui_colors(theme);p=QPainter(self)
        paint_rounded_surface(p,self.rect(),18,qss_color(c['subcard']),qss_color(c['border']));p.end()

    def apply_theme(self,theme):
        from native_ui_common import colors as ui_colors
        c=ui_colors(theme)
        for button in self.edge_buttons.values():
            button.setFixedHeight(28)
            button.setStyleSheet(f'QPushButton{{background:{c["card"]};color:{c["secondary"]};border:1px solid {c["border"]};border-radius:14px;padding:3px 12px;font-size:12px;}}'
                f'QPushButton:checked{{background:{c["segment_selected"]};color:{c["segment_selected_ink"]};border-color:{c["segment_selected"]};font-weight:600;}}'
                f'QPushButton:hover:!checked{{background:{c["control_hover"]};color:{c["text"]};}}')
        self.diagram.dark=theme=='dark';self.diagram.update()
        for control in (self.toggle,self.edge_toggle,self.screen_combo,*self.controls.values(),*self.sliders.values()):
            control.dark=theme=='dark';control.update()
        self._fit_timer.start(0)
