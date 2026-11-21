"""Virtual native history list and an interruptible child-only drawer."""
from datetime import datetime

from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLineEdit, QListWidget,
    QListWidgetItem, QStyledItemDelegate, QStyle, QToolButton, QVBoxLayout, QWidget)

from app_settings import SettingsComboBox
from display_frames import DisplayAnimation
from native_ui_common import colors, icon, paint_rounded_surface, qss_color, reduced_motion, reference_font, set_ui_weight, RoundedScrollBar, RADIUS_PANEL_PX, RADIUS_SUBCONTROL_PX
from reference_widgets import button, label
from translation_languages import TARGETS
from translator_widgets import TranslatorSurface
import ui_theme


def history_actions(rect):
    edge=rect.right()
    return {'favorite':QRectF(edge-60,rect.top()+6,26,26),'delete':QRectF(edge-31,rect.top()+6,26,26)}


class HistoryDelegate(QStyledItemDelegate):
    def __init__(self,parent):
        super().__init__(parent)
        self.fonts=[]
        for size,weight in ((12,500),(11,400),(11,400)):
            font=reference_font();font.setPixelSize(size);set_ui_weight(font,weight);self.fonts.append((font,QFontMetricsF(font)))
    def sizeHint(self,option,index):return QSize(300,108)
    def paint(self,painter,option,index):
        row=index.data(Qt.UserRole);c=colors(ui_theme.current_theme());rect=QRectF(option.rect).adjusted(1,2,-1,-6)
        painter.save();paint_rounded_surface(painter,rect,10,qss_color(c['hover'] if option.state&QStyle.State_MouseOver else c['card']),qss_color(c['border']))
        bounds=rect.adjusted(12,9,-12,-8);font,metrics=self.fonts[0];painter.setFont(font);painter.setPen(qss_color(c['text']))
        painter.drawText(QRectF(bounds.x(),bounds.y(),bounds.width()-60,22),Qt.AlignVCenter,metrics.elidedText(row['source_text'][:140].replace('\n',' '),Qt.ElideRight,bounds.width()-60))
        font,metrics=self.fonts[1];painter.setFont(font);painter.setPen(qss_color(c['file_description']))
        painter.drawText(QRectF(bounds.x(),bounds.y()+27,bounds.width(),22),Qt.AlignVCenter,metrics.elidedText(row['text'][:160].replace('\n',' '),Qt.ElideRight,bounds.width()))
        font,metrics=self.fonts[2];painter.setFont(font);painter.setPen(qss_color(c['secondary']))
        source=TARGETS.get(row['source'],('自动检测',''))[0];target=TARGETS[row['target']][0]
        try:stamp=datetime.fromisoformat(row['created_at']).astimezone().strftime('%m-%d %H:%M')
        except ValueError:stamp=''
        details=metrics.elidedText(source+' → '+target+' · '+stamp,Qt.ElideRight,bounds.width())
        painter.drawText(QRectF(bounds.x(),bounds.y()+61,bounds.width(),17),Qt.AlignVCenter,details)
        owner=self.parent()
        for key,area in history_actions(rect).items():
            entry=(row['id'],key)
            hover=owner.hover_action==entry;pressed=owner.pressed_action==entry and hover
            if hover:
                fill=c['control_pressed' if pressed else 'control_hover'] if key=='favorite' else ('rgba(255,69,58,62)' if pressed else 'rgba(255,69,58,34)')
                paint_rounded_surface(painter,area,RADIUS_SUBCONTROL_PX,qss_color(fill))
            glyph='trash' if key=='delete' else 'star-filled' if row['favorite'] else 'star'
            tint=c['red'] if key=='delete' and hover else '#E5AA17' if key=='favorite' and row['favorite'] else c['text'] if hover else c['secondary']
            icon(glyph,tint).paint(painter,area.adjusted(5,5,-5,-5).toAlignedRect())
        painter.restore()


class HistoryList(QListWidget):
    restore_requested=Signal(str)
    favorite_requested=Signal(str)
    delete_requested=Signal(str)
    def __init__(self,parent):
        super().__init__(parent);self.setFrameShape(QFrame.NoFrame);self.setMouseTracking(True)
        self.setItemDelegate(HistoryDelegate(self));self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);self.origin=None
        self.hover_action=None;self.pressed_action=None
        self.setProperty('scrollBackdropRole','popup_surface')
        self.setVerticalScrollBar(RoundedScrollBar(Qt.Vertical,self,
            thumb_color=lambda:colors(ui_theme.current_theme())['input_border']))
        self.setStyleSheet("""QListWidget {background:transparent;}
            QScrollBar:vertical {background:transparent;border:0;width:8px;}
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0;}""")
    def _action_at(self,point):
        item=self.itemAt(point)
        if item is None:return None
        rect=QRectF(self.visualItemRect(item)).adjusted(1,2,-1,-6)
        for key,area in history_actions(rect).items():
            if area.contains(point):return item.data(Qt.UserRole)['id'],key
        return None
    def mouseMoveEvent(self,event):
        action=self._action_at(event.position().toPoint())
        if action!=self.hover_action:
            self.hover_action=action;self.viewport().update()
        super().mouseMoveEvent(event)
    def viewportEvent(self,event):
        if event.type()==QEvent.Leave and self.hover_action is not None:
            self.hover_action=None;self.viewport().update()
        return super().viewportEvent(event)
    def mousePressEvent(self,event):
        self.origin=event.position().toPoint()
        if event.button()==Qt.LeftButton:
            self.pressed_action=self.hover_action=self._action_at(self.origin);self.viewport().update()
        super().mousePressEvent(event)
    def mouseReleaseEvent(self,event):
        super().mouseReleaseEvent(event)
        pressed,self.pressed_action=self.pressed_action,None
        if pressed:self.viewport().update()
        point=event.position().toPoint();item=self.itemAt(point)
        if event.button()!=Qt.LeftButton or item is None or self.origin is None:return
        if (point-self.origin).manhattanLength()>QApplication.startDragDistance():return
        action=self._action_at(point);identifier=item.data(Qt.UserRole)['id']
        if action and action==pressed:
            (self.delete_requested if action[1]=='delete' else self.favorite_requested).emit(identifier)
        elif action is None and pressed is None:self.restore_requested.emit(identifier)


class _Backdrop(QWidget):
    clicked=Signal()
    def paintEvent(self,event):
        from surface_morph import surface_clip
        painter=QPainter(self);painter.setRenderHint(QPainter.Antialiasing)
        shade=QColor(0,0,0,95) if ui_theme.current_theme()=='dark' else QColor(187,190,198,145)
        painter.fillPath(surface_clip(QRectF(self.parentWidget().rect()).translated(-self.x(),-self.y())),shade)
        painter.end()
    def mousePressEvent(self,event):self.clicked.emit();event.accept()


class TranslatorHistory(TranslatorSurface):
    def __init__(self,owner,parent):
        super().__init__(parent,role='popup_surface',radius=RADIUS_PANEL_PX)
        self.owner=owner;self.store=owner.store;self.opened=False;self.progress=0.;self._key=None;self._confirm_clear=False
        self.backdrop=_Backdrop(parent);self.backdrop.clicked.connect(lambda:self.set_open(False));self.backdrop.hide()
        self.animation=DisplayAnimation(self);self.animation.setDuration(230);self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.animation.valueChanged.connect(self._step);self.animation.finished.connect(self._finish)
        root=QVBoxLayout(self);root.setContentsMargins(14,12,14,12);root.setSpacing(9)
        title=QHBoxLayout();title.addWidget(label('翻译历史与收藏',12,600,parent=self),1)
        self.clear_button=button('清空历史',parent=self);self.clear_button.clicked.connect(self._clear);title.addWidget(self.clear_button)
        from translator_widgets import TranslatorToolButton
        close=TranslatorToolButton('close',self);close.setToolTip('关闭历史');close.setAccessibleName('关闭历史');close.clicked.connect(lambda:self.set_open(False));title.addWidget(close);root.addLayout(title)
        self.search=QLineEdit(self);self.search.setPlaceholderText('搜索原文或译文');self.search.setClearButtonEnabled(True);root.addWidget(self.search)
        filters=QHBoxLayout();filters.setSpacing(6)
        self.favorite_filter=SettingsComboBox(self);self.favorite_filter.addItem('全部记录',False);self.favorite_filter.addItem('仅收藏',True);filters.addWidget(self.favorite_filter)
        self.language_filter=SettingsComboBox(self);self.language_filter.addItem('全部语言','all')
        for code,(name,_) in TARGETS.items():self.language_filter.addItem(name,code)
        filters.addWidget(self.language_filter)
        self.sort=SettingsComboBox(self);self.sort.addItem('最新优先',True);self.sort.addItem('最早优先',False);filters.addWidget(self.sort);root.addLayout(filters)
        for control in (self.favorite_filter,self.language_filter,self.sort):control.setMinimumWidth(0);control.currentIndexChanged.connect(self.refresh)
        clear=self.search.findChild(QToolButton)
        if clear is not None:clear.setToolTip('清除搜索')
        self.search_timer=QTimer(self);self.search_timer.setSingleShot(True);self.search_timer.setInterval(120);self.search_timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda:self.search_timer.start())
        self.clear_timer=QTimer(self);self.clear_timer.setSingleShot(True);self.clear_timer.setInterval(4000);self.clear_timer.timeout.connect(self._reset_clear)
        self.list=HistoryList(self);root.addWidget(self.list,1)
        self.empty=label('暂无翻译记录',11,400,'secondary',self);self.empty.setAlignment(Qt.AlignCenter);root.addWidget(self.empty,1)
        self.list.restore_requested.connect(self._restore);self.list.favorite_requested.connect(self._favorite);self.list.delete_requested.connect(self._delete)
        self.owner.history_button.setCheckable(True);self.hide()
    def reposition(self):
        parent=self.parentWidget();width=min(430,max(330,parent.width()-70));height=max(100,parent.height()-76)
        self.resize(width,height);self.move(round(parent.width()-8-self.progress*(width+8)),58)
        title_bottom=self.owner.title_bar.geometry().bottom()+1
        self.backdrop.setGeometry(QRect(0,title_bottom,parent.width(),max(0,parent.height()-title_bottom)))
    def _step(self,value):self.progress=float(value);self.reposition()
    def set_open(self,opened,*,animate=True):
        self.opened=bool(opened);self.owner.history_button.setChecked(self.opened);self.animation.stop()
        if self.opened:self.refresh();self.backdrop.show();self.backdrop.raise_();self.show();self.raise_()
        if not animate or not self.owner.isVisible() or reduced_motion():self._step(1. if self.opened else 0.);self._finish();return
        self.animation.setStartValue(self.progress);self.animation.setEndValue(1. if self.opened else 0.);self.animation.start()
    def _finish(self):
        if not self.opened:self.hide();self.backdrop.hide();self.search_timer.stop();self.clear_timer.stop();self._reset_clear()
    def refresh(self,*args):
        if not self.opened:return
        query=self.search.text().strip().casefold();fav=self.favorite_filter.currentData();language=self.language_filter.currentData()
        rows=[row for row in self.store.data['history'] if (not fav or row['favorite']) and (language=='all' or row['target']==language or row['source']==language)
              and (not query or query in row['source_text'].casefold() or query in row['text'].casefold())]
        rows.sort(key=lambda row:row['created_at'],reverse=self.sort.currentData() is not False)
        key=tuple((row['id'],row['created_at'],row['text'],row['favorite']) for row in rows)
        if key!=self._key:
            self._key=key;self.list.clear()
            for row in rows:
                item=QListWidgetItem(self.list);item.setData(Qt.UserRole,row);item.setSizeHint(QSize(300,108))
        self.empty.setVisible(not rows);self.list.setVisible(bool(rows));self.clear_button.setEnabled(bool(self.store.data['history']))
        for control in (self.favorite_filter,self.language_filter,self.sort):control.dark=self.owner.manager.settings.theme=='dark';control.update()
    def _restore(self,identifier):
        row=next((row for row in self.store.data['history'] if row['id']==identifier),None)
        if row:self.owner.session.restore_record(row);self.set_open(False)
    def _favorite(self,identifier):
        row=next((row for row in self.store.data['history'] if row['id']==identifier),None)
        if row:
            row['favorite']=not row['favorite']
            if self.owner.session.history_id==identifier:self.owner.session.favorite=row['favorite']
            self.store.save()
    def _delete(self,identifier):
        self.store.data['history'][:]=[row for row in self.store.data['history'] if row['id']!=identifier]
        self.owner.session.forget_record(identifier)
        self.store.save()
    def _reset_clear(self):self._confirm_clear=False;self.clear_button.setText('清空历史')
    def _clear(self):
        if not self._confirm_clear:self._confirm_clear=True;self.clear_button.setText('确认清空');self.clear_timer.start();return
        self.store.data['history'].clear();self.owner.session.forget_record()
        self.store.save();self._reset_clear()
