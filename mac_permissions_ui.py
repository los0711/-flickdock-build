"""One guided entry for actual macOS grants, without broad or automatic permission changes."""
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import QLabel,QPushButton,QHBoxLayout,QVBoxLayout,QFrame
from app_settings import ReferenceModalDialog,SettingsCloseButton
from native_ui_common import colors
from mac_permissions import MacPermissionBackend


class MacPermissionsDialog(ReferenceModalDialog):
    ROWS=(('clipboard','剪贴板','后台同步需要“始终允许”。'),
          ('accessibility','辅助功能','用于鼠标位于搁架时，在其他应用中直接粘贴。'),
          ('input','输入监控','仅在系统仍阻止跨应用按键监听时需要。'),
          ('network','本地网络','用于发现附近设备及局域网互传。'),
          ('files','文件与文件夹','在选择需要读取的文件、接收目录时按需授权。'))
    LABELS={'allowed':'已允许','legacy':'系统无需单独授权','denied':'未允许','ask':'需要设为始终允许',
            'default':'尚未设置','unknown':'需在系统中确认','not_required':'当前监听已可用',
            'observed':'已发现附近设备','on_demand':'按所选文件授权'}
    def __init__(self,manager,parent=None,*,backend=None):
        super().__init__(parent or manager.shelf,surface_width=580,modal=False)
        self.manager=manager;self.backend=backend or MacPermissionBackend();self.states={};self._guided=set()
        self.setWindowTitle('FlickDock · Mac 权限设置');self.rows={}
        head=QHBoxLayout();title=QLabel('Mac 权限设置',self.body);head.addWidget(title,1)
        close=SettingsCloseButton(self.body,glyph_size=14);self.close_button=close;close.setFixedSize(28,28);close.clicked.connect(self.close);head.addWidget(close);self.body_layout.addLayout(head)
        intro=QLabel('在这里集中完成所需授权。系统弹窗和开关需你确认，返回后会自动重新检测。',self.body);intro.setWordWrap(True);self.body_layout.addWidget(intro)
        for kind,title,detail in self.ROWS:
            frame=QFrame(self.body);frame.setObjectName('permissionRow');row=QHBoxLayout(frame);row.setContentsMargins(12,10,12,10)
            left=QVBoxLayout();left.setSpacing(4);name=QLabel(title,frame);name.setStyleSheet('font-weight:600;');description=QLabel(detail,frame);description.setWordWrap(True);left.addWidget(name);left.addWidget(description);row.addLayout(left,1)
            right=QVBoxLayout();status=QLabel('',frame);status.setAlignment(Qt.AlignRight);right.addWidget(status)
            button=QPushButton('设置',frame);button.setAutoDefault(False);button.clicked.connect(lambda _,kind=kind:self.request(kind));right.addWidget(button,0,Qt.AlignRight);row.addLayout(right)
            self.body_layout.addWidget(frame);self.rows[kind]=(status,button)
        self.hint=QLabel('本地网络没有通用的授权状态查询；未发现设备不等于未授权。更新后若系统要求重新确认，可在这里恢复。',self.body);self.hint.setWordWrap(True);self.body_layout.addWidget(self.hint)
        actions=QHBoxLayout();self.recheck_button=QPushButton('重新检测',self.body);self.recheck_button.setAutoDefault(False);self.recheck_button.clicked.connect(self.refresh);actions.addWidget(self.recheck_button)
        actions.addStretch();self.setup_button=QPushButton('检查并设置',self.body);self.setup_button.setAutoDefault(False);self.setup_button.clicked.connect(self.advance);actions.addWidget(self.setup_button);self.body_layout.addLayout(actions)
        self._theme=None;self.apply_theme()
        self.timer=QTimer(self);self.timer.setInterval(2000);self.timer.timeout.connect(self.refresh);self.refresh()

    def apply_theme(self):
        self._theme=self.manager.settings.theme;c=colors(self._theme);self.set_surface_theme(self._theme);self.close_button.dark=self._theme=='dark'
        self.setStyleSheet(f"QDialog{{background:transparent;}} QLabel{{color:{c['text']};background:transparent;border:0;font-size:12px;}} QFrame#permissionRow{{background:{c['card']};border:1px solid {c['border']};border-radius:8px;}} QPushButton{{color:{c['text']};background:{c['control']};border:1px solid {c['border']};border-radius:8px;padding:6px 12px;}} QPushButton:hover{{background:{c['hover']};}}")

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
        if all(self.states[k] in ('allowed','legacy','not_required') for k in ('clipboard','accessibility','input')):
            self.setup_button.setText('检查其他权限' if 'network' not in self._guided else '完成')
        else:self.setup_button.setText('继续设置' if self._guided else '检查并设置')

    def request(self,kind):
        try:
            if not self.backend.request(kind):self.hint.setText('未能打开系统设置，请手动前往“系统设置 → 隐私与安全性”。')
            elif kind=='clipboard':self.hint.setText('请在剪贴板/从其他 App 粘贴的设置中，将 FlickDock 设为“始终允许”。只允许一次不能用于持续后台同步。')
            elif kind=='files':self.hint.setText('文件权限按实际文件和目录管理。使用原来的文件选择器选择内容；接收目录可在传输页面修改。')
            else:self.hint.setText('请在系统设置中允许 FlickDock，返回后会重新检测；尚未授予的项目会继续标明。')
            self._guided.add(kind)
        except (ImportError,OSError,RuntimeError,AttributeError,ValueError,TypeError):
            self.hint.setText('系统未完成授权请求，请在“系统设置 → 隐私与安全性”中检查 FlickDock。')
        self.refresh()

    def advance(self):
        self.refresh()
        for kind in ('clipboard','accessibility','input'):
            if self.states[kind] not in ('allowed','legacy','not_required'):self.request(kind);return
        if 'network' not in self._guided and self.states['network']!='observed':self.request('network');return
        self.close()

    def showEvent(self,event):
        self.refresh();self.timer.start();super().showEvent(event)

    def hideEvent(self,event):
        self.timer.stop();super().hideEvent(event)
