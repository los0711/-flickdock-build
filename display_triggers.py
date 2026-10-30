"""Per-display trigger drafts keyed by stable OS identity, never screen order."""
from __future__ import annotations
import hashlib,re
from PySide6.QtCore import Qt,Signal,QVariantAnimation,QEasingCurve,QTimer
from PySide6.QtGui import QGuiApplication,QColor,QIcon,QPainter,QPixmap,QPen
from PySide6.QtWidgets import QWidget,QFrame,QVBoxLayout,QHBoxLayout,QGridLayout,QLabel,QPushButton,QColorDialog,QAbstractSpinBox

FIELDS=('dock_edge','dock_position','dock_length','dock_thickness','trigger_visible','dock_color','dock_opacity')
PROFILE_FIELDS={'edge':'dock_edge','position':'dock_position','length':'dock_length','thickness':'dock_thickness','color':'dock_color','opacity':'dock_opacity'}
RANGES={'position':(0,100),'length':(32,600),'thickness':(2,40),'opacity':(0,100)}


def display_id(screen):
    values=[str(getattr(screen,name,lambda:'')() or '') for name in ('name','manufacturer','model','serialNumber')]
    identity=values[1:] if values[3] and values[3].strip('0 -') else values
    return hashlib.sha256('\0'.join(identity).encode('utf8')).hexdigest()[:24]


def validate_profiles(value):
    if not isinstance(value,dict) or len(value)>64:raise ValueError('显示器设置无效。')
    result={}
    for key,row in value.items():
        if not isinstance(key,str) or len(key)!=24 or any(c not in '0123456789abcdef' for c in key) or not isinstance(row,dict):raise ValueError('显示器设置无效。')
        if type(row.get('enabled')) is not bool or row.get('edge') not in ('left','right','top','bottom') or type(row.get('position')) is not int or not 0<=row['position']<=100:raise ValueError('显示器位置无效。')
        clean={name:row[name] for name in ('enabled','edge','position')}
        for field,(low,high) in RANGES.items():
            if field in row:
                if type(row[field]) is not int or not low<=row[field]<=high:raise ValueError('显示器触发条参数无效。')
                clean[field]=row[field]
        if 'color' in row:
            if not isinstance(row['color'],str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',row['color']):raise ValueError('显示器标记颜色无效。')
            clean['color']=row['color'].lower()
        result[key]=clean
    return result


def display_values(store,screen,defaults=None):
    values={key:(defaults[key] if defaults and key in defaults else getattr(store,key)) for key in FIELDS}
    profiles=defaults.get('display_triggers',store.display_triggers) if defaults is not None else store.display_triggers
    row=profiles.get(display_id(screen))
    if row:
        values.update({target:row[key] for key,target in PROFILE_FIELDS.items() if key in row})
        values['enabled']=row['enabled']
    else:values['enabled']=display_id(screen)==display_id(QGuiApplication.primaryScreen())
    return values


class _Header(QWidget):
    activated=Signal()
    def mouseReleaseEvent(self,event):
        if event.button()==Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.activated.emit();event.accept();return
        super().mouseReleaseEvent(event)


class DisplayGroup(QFrame):
    changed=Signal()
    geometry_changed=Signal()
    def __init__(self,name,values,expanded,parent):
        from app_settings import SettingsToggleSwitch,SettingsComboBox,SettingsSpinBox,SettingsSlider
        super().__init__(parent);self.setObjectName('settingsInset');self.expanded=expanded;self.controls={};self.sliders={};self.color=values['dock_color']
        box=QVBoxLayout(self);box.setContentsMargins(12,6,12,10);box.setSpacing(8)
        self.header=_Header(self);line=QHBoxLayout(self.header);line.setContentsMargins(0,0,0,0);line.setSpacing(10)
        self.name=QLabel(name,self.header);self.name.setWordWrap(True);self.name.setAttribute(Qt.WA_TransparentForMouseEvents);line.addWidget(self.name,1)
        self.toggle=SettingsToggleSwitch('',self.header);self.toggle.setMinimumSize(44,32);self.toggle.setMaximumSize(44,32);self.toggle.setAccessibleName(name+'启用触发条');line.addWidget(self.toggle)
        self.arrow=QPushButton(self.header);self.arrow.setFixedSize(28,28);self.arrow.setAutoDefault(False);self.arrow.setObjectName('settingsLink');self.arrow.setAccessibleName(name+'展开或收起参数');line.addWidget(self.arrow)
        self.header.activated.connect(lambda:self.set_expanded(not self.expanded));self.arrow.clicked.connect(lambda:self.set_expanded(not self.expanded))
        box.addWidget(self.header)
        self.details=QWidget(self);details=QVBoxLayout(self.details);details.setContentsMargins(0,0,0,0);details.setSpacing(10)
        top=QHBoxLayout();top.setSpacing(12);top.addWidget(QLabel('停靠在',self.details))
        self.edge=SettingsComboBox(self.details)
        for label,key in (('左侧','left'),('右侧','right'),('顶部','top'),('底部','bottom')):self.edge.addItem(label,key)
        self.edge.setMinimumWidth(78);top.addWidget(self.edge);top.addStretch();top.addWidget(QLabel('标记颜色',self.details))
        self.color_button=QPushButton(self.details);self.color_button.setAutoDefault(False);self.color_button.setFixedSize(108,28);self.color_button.clicked.connect(self.pick_color);top.addWidget(self.color_button);details.addLayout(top)
        self.grid=QGridLayout();self.grid.setSpacing(12);self.panels=[]
        for key,label,unit in (('position','边缘位置',' %'),('length','触发条长度',' px'),('thickness','触发条厚度',' px'),('opacity','不透明度',' %')):
            panel=QWidget(self.details);col=QVBoxLayout(panel);col.setContentsMargins(0,0,0,0);col.setSpacing(8);row=QHBoxLayout();row.addWidget(QLabel(label,panel),1)
            spin=SettingsSpinBox(panel);spin.setRange(*RANGES[key]);spin.setSuffix(unit);spin.setButtonSymbols(QAbstractSpinBox.NoButtons);spin.setFixedSize(88,28);spin.lineEdit().setMinimumSize(60,18);spin.setAccessibleName(name+label);row.addWidget(spin);col.addLayout(row)
            slider=SettingsSlider(Qt.Horizontal,panel);slider.setRange(*RANGES[key]);slider.setAccessibleName(name+label);col.addWidget(slider)
            spin.valueChanged.connect(slider.setValue);slider.valueChanged.connect(spin.setValue);spin.valueChanged.connect(self.changed)
            self.controls[key]=spin;self.sliders[key]=slider;self.panels.append(panel)
        details.addLayout(self.grid);box.addWidget(self.details)
        self._height_timer=QTimer(self);self._height_timer.setSingleShot(True)
        self._height_timer.timeout.connect(self._refresh_height)
        self.animation=QVariantAnimation(self);self.animation.setDuration(180);self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.animation.valueChanged.connect(self._expand_frame)
        self.animation.finished.connect(self._expanded_end)
        self.toggle.toggled.connect(self.changed);self.edge.currentIndexChanged.connect(self.changed)
        self.load(values);self.set_expanded(expanded,animate=False);self._columns=None
        self._adapt()

    def load(self,values):
        controls=[self.toggle,self.edge,*self.controls.values()]
        for control in controls:control.blockSignals(True)
        self.toggle.setChecked(values['enabled']);self.edge.setCurrentIndex(self.edge.findData(values['dock_edge']));self.color=values['dock_color'];self._update_color()
        for key,control in self.controls.items():control.setValue(values[PROFILE_FIELDS[key]]);self.sliders[key].setValue(control.value())
        for control in controls:control.blockSignals(False)

    def profile(self):return dict(enabled=self.toggle.isChecked(),edge=self.edge.currentData(),color=self.color,**{key:control.value() for key,control in self.controls.items()})

    def _update_color(self):
        ratio=self.devicePixelRatioF();swatch=QPixmap(round(16*ratio),round(16*ratio));swatch.setDevicePixelRatio(ratio);swatch.fill(Qt.transparent)
        painter=QPainter(swatch);painter.setRenderHint(QPainter.Antialiasing);painter.setPen(QPen(QColor('#8793a3'),.7));painter.setBrush(QColor(self.color));painter.drawEllipse(1,1,14,14);painter.end()
        self.color_button.setText(self.color.upper());self.color_button.setIcon(QIcon(swatch))

    def pick_color(self):
        color=QColorDialog.getColor(QColor(self.color),self,'触发条标记颜色')
        if color.isValid():self.color=color.name();self._update_color();self.changed.emit()

    def _expand_frame(self,value):
        self.details.setMaximumHeight(round(value));self._height_timer.start(0)

    def _expanded_end(self):
        if self.expanded:self.details.setMaximumHeight(16777215)
        else:self.details.hide()
        self._height_timer.start(0)

    def _refresh_height(self):
        # Releasing the animated maxHeight must invalidate every containing
        # layout hint, including the scroll body's vertical constraint.
        # Otherwise rapid reversals can leave it at an intermediate height.
        self.layout().invalidate()
        self.setFixedHeight(self.minimumSizeHint().height())
        self.updateGeometry();self.geometry_changed.emit()

    def _motion_enabled(self):
        from ui_transitions import motion_allowed
        return self.isVisible() and QGuiApplication.platformName() not in ('offscreen','minimal') and motion_allowed()

    def set_expanded(self,value,*,animate=True):
        self.animation.stop();start=self.details.height() if self.details.isVisible() else 0;self.expanded=value
        from native_ui_common import icon
        self.arrow.setIcon(icon('chevron-down' if value else 'chevron-right','#A1A1AA' if getattr(self,'dark',False) else '#68686D'))
        # Both rapid reversal and reduced motion end in the requested state.
        if animate and self._motion_enabled():
            self.details.show();self.details.setMaximumHeight(start)
            self.animation.setStartValue(start);self.animation.setEndValue(self.details.sizeHint().height() if value else 0);self.animation.start()
        else:
            self.details.setMaximumHeight(16777215 if value else 0);self.details.setVisible(value)
        self.arrow.setToolTip('收起参数' if value else '展开参数')
        self._height_timer.start(0)

    def resizeEvent(self,event):super().resizeEvent(event);self._adapt()
    def _adapt(self):
        columns=1 if self.width()<480 else 2
        if getattr(self,'_columns',None)==columns:return
        self._columns=columns
        for i,panel in enumerate(self.panels):self.grid.removeWidget(panel);self.grid.addWidget(panel,i//columns,i%columns)
        if hasattr(self,'_height_timer'):self._height_timer.start(0)

    def apply_theme(self,theme):
        self.dark=theme=='dark'
        for control in (self.toggle,self.edge,*self.controls.values()):control.dark=self.dark;control.update()
        from native_ui_common import icon
        self.arrow.setIcon(icon('chevron-down' if self.expanded else 'chevron-right','#A1A1AA' if self.dark else '#68686D'))


class DisplayTriggerEditor(QWidget):
    profiles_changed=Signal()
    geometry_changed=Signal()
    def __init__(self,store,parent=None):
        super().__init__(parent);self.store=store;self.rows={};self._theme=store.theme;self._draft=store.display_triggers
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(8)
        heading=QLabel('各显示器触发条',self);heading.setObjectName('settingsSection');layout.addWidget(heading)
        self.body=QWidget(self);self.body_layout=QVBoxLayout(self.body);self.body_layout.setContentsMargins(0,0,0,0);self.body_layout.setSpacing(8);layout.addWidget(self.body)
        app=QGuiApplication.instance();app.screenAdded.connect(self.refresh);app.screenRemoved.connect(self.refresh);app.primaryScreenChanged.connect(self.refresh)
        self.refresh()

    def profiles(self):return {key:dict(row) for key,row in self._draft.items()}

    def load_values(self):
        self._draft=self.store.display_triggers;self.refresh()
        for i,row in enumerate(self.rows.values()):row.set_expanded(i==0,animate=False)

    def refresh(self,*_):
        screens=list(QGuiApplication.screens());primary=QGuiApplication.primaryScreen();screens.sort(key=lambda s:s is not primary)
        keys=[display_id(s) for s in screens]
        for key in list(self.rows):
            if key not in keys:
                row=self.rows.pop(key);self.body_layout.removeWidget(row);row.hide();row.deleteLater()
        for i,(screen,key) in enumerate(zip(screens,keys)):
            values=display_values(self.store,screen,dict(display_triggers=self._draft))
            name=(screen.name() or '显示器')+(' · 主屏' if screen is primary else '')
            if key in self.rows:
                row=self.rows[key];row.load(values);row.name.setText(name)
            else:
                row=DisplayGroup(name,values,i==0,self.body);self.rows[key]=row
                row.changed.connect(lambda key=key:self.save(key))
                row.geometry_changed.connect(self._refresh_height)
            self.body_layout.removeWidget(row);self.body_layout.insertWidget(i,row);row.apply_theme(self._theme)
        self.rows={key:self.rows[key] for key in keys}
        self._refresh_height()

    def _refresh_height(self):
        # Freeze only this content group's measured height, never the native
        # window. Nested HFW calculations otherwise compress visible rows.
        self.body_layout.invalidate();self.body_layout.activate()
        self.layout().invalidate();self.setFixedHeight(self.layout().sizeHint().height())
        self.updateGeometry();self.geometry_changed.emit()

    def apply_theme(self,theme):
        self._theme=theme
        for row in self.rows.values():row.apply_theme(theme)

    def save(self,key):
        self._draft[key]=self.rows[key].profile();self.profiles_changed.emit()
