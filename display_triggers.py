"""Per-display trigger preferences, keyed by OS display identity, never order."""
from __future__ import annotations
import hashlib
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLabel


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
        result[key]={name:row[name] for name in ('enabled','edge','position')}
    return result


def display_values(store,screen,defaults=None):
    values=dict(defaults or {key:getattr(store,key) for key in ('dock_edge','dock_position','dock_length','dock_thickness','trigger_visible','dock_color','dock_opacity')})
    row=getattr(store,'display_triggers',{}).get(display_id(screen))
    if row:
        values.update(dock_edge=row['edge'],dock_position=row['position'])
        values['enabled']=row['enabled']
    else:values['enabled']=display_id(screen)==display_id(QGuiApplication.primaryScreen())
    return values


class DisplayTriggerEditor(QWidget):
    def __init__(self,store,parent=None):
        super().__init__(parent);self.store=store;self.rows={};self._screens=[];self._theme=store.theme
        layout=QVBoxLayout(self);layout.setContentsMargins(0,6,0,0);layout.setSpacing(8)
        heading=QLabel('各显示器触发条',self);heading.setObjectName('settingsSection');layout.addWidget(heading)
        self.body=QWidget(self);self.body_layout=QVBoxLayout(self.body);self.body_layout.setContentsMargins(0,0,0,0);self.body_layout.setSpacing(8);layout.addWidget(self.body)
        self.error=QLabel(self);self.error.setWordWrap(True);self.error.hide();layout.addWidget(self.error)
        app=QGuiApplication.instance();app.screenAdded.connect(self.refresh);app.screenRemoved.connect(self.refresh);app.primaryScreenChanged.connect(self.refresh)
        self.refresh()

    def refresh(self,*_):
        from app_settings import SettingsToggleSwitch,SettingsComboBox,SettingsSpinBox
        screens=QGuiApplication.screens();keys=[display_id(s) for s in screens]
        for key in list(self.rows):
            if key not in keys:
                self.rows.pop(key)[0].deleteLater()
        for screen,key in zip(screens,keys):
            if key in self.rows:
                _,toggle,edge,position=self.rows[key]
                values=display_values(self.store,screen)
                name=(screen.name() or '显示器')+(' · 主屏' if screen is QGuiApplication.primaryScreen() else '')
                for control in (toggle,edge,position):control.blockSignals(True)
                toggle.setText(name);toggle.setChecked(values['enabled'])
                edge.setCurrentIndex(max(0,edge.findData(values['dock_edge'])));position.setValue(values['dock_position'])
                for control in (toggle,edge,position):control.blockSignals(False)
                continue
            row=QWidget(self.body);layout=QHBoxLayout(row);layout.setContentsMargins(0,0,0,0);layout.setSpacing(8)
            values=display_values(self.store,screen)
            name=(screen.name() or '显示器')+(' · 主屏' if screen is QGuiApplication.primaryScreen() else '')
            toggle=SettingsToggleSwitch(name,row);toggle.setChecked(values['enabled']);layout.addWidget(toggle,1)
            toggle.dark=self._theme=='dark'
            edge=SettingsComboBox(row)
            for text,value in (('左侧','left'),('右侧','right'),('顶部','top'),('底部','bottom')):edge.addItem(text,value)
            edge.setCurrentIndex(max(0,edge.findData(values['dock_edge'])));edge.setMinimumWidth(76);layout.addWidget(edge)
            edge.dark=self._theme=='dark'
            position=SettingsSpinBox(row);position.setRange(0,100);position.setSuffix('%');position.setValue(values['dock_position']);position.setMinimumWidth(78)
            position.setAccessibleName(name+'沿边缘的位置');edge.setAccessibleName(name+'触发条边缘');layout.addWidget(position)
            position.dark=self._theme=='dark'
            self.rows[key]=(row,toggle,edge,position);self.body_layout.addWidget(row)
            for signal in (toggle.toggled,edge.currentIndexChanged,position.valueChanged):signal.connect(lambda *_,key=key:self.save(key))

    def apply_theme(self,theme):
        self._theme=theme
        for _,toggle,edge,position in self.rows.values():
            for control in (toggle,edge,position):control.dark=theme=='dark';control.update()

    def save(self,key):
        _,toggle,edge,position=self.rows[key]
        profiles=self.store.display_triggers
        profiles[key]=dict(enabled=toggle.isChecked(),edge=edge.currentData(),position=position.value())
        try:self.store.update(display_triggers=profiles);self.error.clear();self.error.hide()
        except (OSError,ValueError) as error:self.error.setText(str(error));self.error.show()
