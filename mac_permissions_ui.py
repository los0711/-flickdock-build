"""One guided entry for actual macOS grants, without broad or automatic permission changes."""
import logging
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import QApplication,QLabel,QPushButton,QHBoxLayout,QVBoxLayout,QFrame,QWidget
from app_settings import ReferenceModalDialog,SettingsCloseButton
from native_ui_common import colors
from mac_permissions import MacPermissionBackend


class MacPermissionsDialog(ReferenceModalDialog):
    ROWS=(('clipboard','剪贴板','记录复制历史、后台同步，以及粘贴历史时恢复原剪贴板。'),
          ('accessibility','辅助功能','把历史内容直接粘贴到其他 App。'),
          ('input','输入监控','可选，用于鼠标指向搁架时捕获粘贴快捷键。'),
          ('network','本地网络','使用局域网互传时需要。'),
          ('files','截图文件夹','截图软件使用自定义保存目录时，可在这里选择。'))
    LABELS={'allowed':'已允许','legacy':'系统无需单独授权','denied':'未允许','ask':'需要设为始终允许',
            'default':'尚未设置','unknown':'需在系统中确认','not_required':'当前监听已可用',
            'observed':'已发现附近设备','on_demand':'按所选文件授权'}
    def __init__(self,manager,parent=None,*,backend=None):
        super().__init__(None,surface_width=580,modal=False)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setFont((parent or manager.shelf).font())
        self._awaiting_system=False
        self._guided_flow=False
        self.manager=manager;self.backend=backend or MacPermissionBackend();self.states={};self._guided=set()
        self.setWindowTitle('FlickDock · Mac 权限设置');self.rows={}
        self.title_bar=QWidget(self.body)
        head=QHBoxLayout(self.title_bar);head.setContentsMargins(0,0,0,0)
        self.title=QLabel('Mac 权限设置',self.title_bar);head.addWidget(self.title,1)
        close=SettingsCloseButton(self.title_bar,glyph_size=14);self.close_button=close;close.setFixedSize(28,28);close.clicked.connect(self.close);head.addWidget(close);self.body_layout.addWidget(self.title_bar)
        from platform_native import app_bundle
        self.bundle=app_bundle()
        self.application_path=QLabel('当前运行的应用\n'+(str(self.bundle) if self.bundle else 'FlickDock 开发环境'),self.body)
        self.application_path.setWordWrap(True);self.application_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        app_row=QHBoxLayout();app_row.addWidget(self.application_path,1)
        if self.bundle is not None:
            reveal=QPushButton('在 Finder 中显示',self.body)
            reveal.setAutoDefault(False);reveal.clicked.connect(self._reveal_current_application)
            app_row.addWidget(reveal)
        self.body_layout.addLayout(app_row)
        intro=QLabel('按需开启对应权限。每次只打开你选的项目，返回后自动重新检测。',self.body);intro.setWordWrap(True);self.body_layout.addWidget(intro)
        for kind,title,detail in self.ROWS:
            frame=QFrame(self.body);frame.setObjectName('permissionRow');row=QHBoxLayout(frame);row.setContentsMargins(12,10,12,10)
            left=QVBoxLayout();left.setSpacing(4);name=QLabel(title,frame);name.setStyleSheet('font-weight:600;');description=QLabel(detail,frame);description.setWordWrap(True);left.addWidget(name);left.addWidget(description);row.addLayout(left,1)
            right=QVBoxLayout();status=QLabel('',frame);status.setAlignment(Qt.AlignRight);right.addWidget(status)
            button=QPushButton('设置',frame);button.setAutoDefault(False);button.clicked.connect(lambda _,kind=kind:self.request(kind));right.addWidget(button,0,Qt.AlignRight);row.addLayout(right)
            self.body_layout.addWidget(frame);self.rows[kind]=(status,button)
        self.hint=QLabel('本地网络没有通用的授权状态查询；未发现设备不等于未授权。更新后若系统要求重新确认，可在这里恢复。',self.body);self.hint.setWordWrap(True);self.body_layout.addWidget(self.hint)
        actions=QHBoxLayout();self.recheck_button=QPushButton('重新检测',self.body);self.recheck_button.setAutoDefault(False);self.recheck_button.clicked.connect(self.refresh);actions.addWidget(self.recheck_button)
        actions.addStretch();self.setup_button=QPushButton('检查并设置',self.body);self.setup_button.setAutoDefault(False);self.setup_button.clicked.connect(self.advance);actions.addWidget(self.setup_button);self.body_layout.addLayout(actions)
        from window_chrome import WindowChrome
        self.chrome=WindowChrome(self,self.title_bar,manager.settings,'mac_permissions',system_resize=False)
        self.chrome.default_size=self.sizeHint()
        self._theme=None;self.apply_theme()
        self._return_timer=QTimer(self);self._return_timer.setSingleShot(True)
        self._return_timer.timeout.connect(self._returned_to_app)
        QApplication.instance().applicationStateChanged.connect(self._application_state)
        self.timer=QTimer(self);self.timer.setInterval(2000);self.timer.timeout.connect(self.refresh);self.refresh()

    def _reveal_current_application(self):
        if self.bundle is None:return
        try:
            self.backend.reveal_application(self.bundle)
        except (ImportError,OSError,RuntimeError):
            logging.getLogger(__name__).exception('Could not reveal the running app in Finder')
            self.hint.setText('请在 Finder 中打开上方显示的应用路径。')

    def _paste_permissions_ready(self):
        return all(self.states[k] in ('allowed','legacy','not_required') for k in ('clipboard','accessibility'))

    def apply_theme(self):
        self._theme=self.manager.settings.theme;c=colors(self._theme);self.set_surface_theme(self._theme);self.close_button.dark=self._theme=='dark'
        self.setStyleSheet(f"QDialog{{background:transparent;}} QLabel{{color:{c['text']};background:transparent;border:0;font-size:12px;}} QFrame#permissionRow{{background:{c['card']};border:0;border-radius:{c['radius_card']}px;}} QPushButton{{color:{c['text']};background:{c['control']};border:1px solid transparent;border-radius:{c['radius_control']}px;padding:6px 12px;}} QPushButton:hover{{background:{c['hover']};}} QPushButton:pressed{{background:{c['selected']};}} QPushButton:disabled{{color:{c['muted']};}} QPushButton[keyboardFocus=\"true\"]{{border:1px solid {c['link']};}}")

    def refresh(self):
        if self._theme!=self.manager.settings.theme:self.apply_theme()
        pointer=getattr(self.manager,'pointer_paste',None)
        if pointer:pointer.retry_native()
        client=getattr(self.manager,'clipboard_sync',None)
        if client:client.recheck_native_access(force=True)
        lan=getattr(self.manager,'lan',None)
        available=bool(pointer and pointer.backend and pointer.backend.available)
        self.states=self.backend.inspect(pointer_ok=available,peers=bool(lan and lan.peers()))
        for kind,(label,button) in self.rows.items():
            state=self.states[kind];label.setText(self.LABELS.get(state,'待检测'))
            if state=='unknown' and kind!='network':label.setText('无法检测，请查看系统设置')
            button.setText('查看设置' if state in ('allowed','legacy','not_required','observed') else '设置')
        pending=(pointer is not None and not available and self.states['accessibility']=='allowed'
                and self.states['input'] in ('allowed','legacy'))
        if getattr(self,'_listener_pending',False) and not pending:
            self.hint.setText('跨应用粘贴监听已恢复。' if available else '权限状态已更新，请检查尚未允许的项目。')
        self._listener_pending=pending
        if (pointer is not None and not available and self.states['accessibility']=='allowed'
                and self.states['input'] in ('allowed','legacy')):
            self.rows['input'][0].setText('权限已允许，监听待恢复')
            self.hint.setText('权限已允许，但跨应用粘贴监听尚未恢复。可重新检测；若系统要求，请关闭并重新打开 FlickDock。')
        if self._paste_permissions_ready():
            self.setup_button.setText('完成')
        else:self.setup_button.setText('继续设置' if self._guided else '检查并设置')

    def _application_state(self,state):
        if self._awaiting_system and state==Qt.ApplicationActive:self._return_timer.start(150)

    def _returned_to_app(self):
        if not self._awaiting_system:return
        self._awaiting_system=False
        self.show()
        self.refresh()
        close_after_setup=self._guided_flow and self._paste_permissions_ready()
        self._guided_flow=False
        if close_after_setup:self.close()

    def request(self,kind):
        if self._awaiting_system:return
        self._return_geometry=self.geometry()
        self._awaiting_system=True
        self.hide()
        shelf=getattr(self.manager,'shelf',None)
        if shelf is not None and hasattr(shelf,'hide_immediately'):shelf.hide_immediately()
        QTimer.singleShot(0,self,lambda:self._request_hidden(kind))

    def _request_hidden(self,kind):
        if not self._awaiting_system:return
        opened=False
        try:
            opened=self.backend.request(kind)
            if not opened:self.hint.setText('未能打开系统设置，请手动前往“系统设置 → 隐私与安全性”。')
            elif kind=='clipboard':self.hint.setText('请在剪贴板/从其他 App 粘贴的设置中，将 FlickDock 设为“始终允许”。只允许一次不能用于持续后台同步。')
            elif kind=='accessibility':self.hint.setText('如果旧条目已开启但仍显示未允许，请点辅助功能里的“＋”，选择上面显示的 FlickDock.app 并开启。')
            elif kind=='files':self.hint.setText('截图文件夹已选择，正常保存的截图会进入本机历史。')
            else:self.hint.setText('请在系统设置中允许 FlickDock，返回后会重新检测；尚未授予的项目会继续标明。')
            self._guided.add(kind)
            if opened and kind=='files':
                history=getattr(self.manager,'clipboard_history',None)
                if history is not None:
                    history.set_screenshot_folders(self.backend.screenshot_folders)
        except (ImportError,OSError,RuntimeError,AttributeError,ValueError,TypeError):
            logging.getLogger(__name__).exception('Mac permission setup action failed')
            self.hint.setText('系统未完成授权请求，请在“系统设置 → 隐私与安全性”中检查 FlickDock。')
        self.refresh()
        if not opened:
            self._awaiting_system=False
            self._guided_flow=False
            self.show()
        elif kind=='files' and QApplication.instance().applicationState()==Qt.ApplicationActive:
            # The native folder chooser has returned synchronously. System
            # Settings can take longer to launch; wait for a real app return.
            self._return_timer.start(150)

    def advance(self):
        self.refresh()
        self._guided_flow=True
        for kind in ('clipboard','accessibility'):
            if self.states[kind] not in ('allowed','legacy','not_required'):
                if not self._awaiting_system:self.request(kind)
                return
        self._guided_flow=False
        self.close()

    def showEvent(self,event):
        self.refresh();self.timer.start();super().showEvent(event)
        if hasattr(self,'_return_geometry'):self.setGeometry(self._return_geometry)

    def closeEvent(self,event):
        self._awaiting_system=False
        self._guided_flow=False
        self._return_timer.stop()
        super().closeEvent(event)

    def hideEvent(self,event):
        self.timer.stop();super().hideEvent(event)
