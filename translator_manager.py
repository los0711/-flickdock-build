"""Small native editors for protection terms and rewrite presets, no settings page."""
from uuid import uuid4
from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QListWidgetItem, QPlainTextEdit

from app_settings import ReferenceModalDialog
from native_ui_common import colors, font_css_stack
from reference_widgets import button, css, label, refresh_tree
from translator_widgets import LanguageButton, LanguagePopup, TranslatorToolButton, picker_list
from translation_languages import TARGETS


class TranslatorEditorDialog(ReferenceModalDialog):
    def __init__(self,owner):
        super().__init__(owner,surface_width=540,modal=False)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def sizeHint(self):
        # Match the two 22 px shadow gutters to avoid a four-pixel scroll range.
        hint=super().sizeHint();hint.setHeight(hint.height()+4)
        return hint


class TranslatorManager(TranslatorEditorDialog):
    def __init__(self,owner,kind):
        super().__init__(owner)
        self.owner=owner;self.store=owner.store;self.kind=kind;self.selected_id=None
        self.setFont(owner.font());self.setWindowTitle('术语保护' if kind=='glossary' else '润色预设')
        heading=QHBoxLayout();heading.addWidget(label(self.windowTitle(),14,600,parent=self.body),1)
        close=TranslatorToolButton('close',self.body);close.setAccessibleName('关闭'+self.windowTitle())
        close.clicked.connect(self.close);heading.addWidget(close);self.body_layout.addLayout(heading)
        self.list=picker_list(self.body);self.list.setFixedHeight(180)
        self._list_style=self.list._reference_style
        self.list.currentItemChanged.connect(self._selected);self.body_layout.addWidget(self.list)
        self.edit_heading=label('新术语' if kind=='glossary' else '新预设',12,500,parent=self.body)
        self.body_layout.addWidget(self.edit_heading)
        self.name=QLineEdit(self.body);self.name.setMaxLength(80 if kind=='glossary' else 24)
        self.name.setPlaceholderText('输入需要保持原文的品牌、型号或名称' if kind=='glossary' else '预设名称');self.body_layout.addWidget(self.name)
        self.instruction=QPlainTextEdit(self.body);self.instruction.setPlaceholderText('输入这项预设使用的润色指令…');self.instruction.setFixedHeight(86)
        self.instruction.setVisible(kind=='presets');self.body_layout.addWidget(self.instruction)
        self.message=label('',11,400,'red',self.body);self.message.setWordWrap(True);self.body_layout.addWidget(self.message)
        operations=QHBoxLayout();operations.setSpacing(6)
        self.new_button=button('新建','plus',variant='secondary',parent=self.body)
        self.new_button.clicked.connect(self.new_entry);operations.addWidget(self.new_button)
        self.move_buttons=[]
        for title,callback in (('上移',lambda:self._move(-1)),('下移',lambda:self._move(1))):
            control=button(title,variant='secondary',parent=self.body);control.clicked.connect(callback)
            control.setVisible(kind=='presets');operations.addWidget(control);self.move_buttons.append(control)
        operations.addStretch()
        self.remove=button('删除所选','trash',variant='danger',parent=self.body)
        self.remove.clicked.connect(self._delete);operations.addWidget(self.remove)
        self.add=button('添加术语' if kind=='glossary' else '添加预设',variant='primary',parent=self.body)
        self.add.clicked.connect(self._add);operations.addWidget(self.add)
        self.update_button=button('保存修改','check',variant='primary',parent=self.body)
        self.update_button.clicked.connect(self._update);operations.addWidget(self.update_button)
        self.body_layout.addLayout(operations)
        self.name.returnPressed.connect(self._save_selection)
        for control in self.body.findChildren(type(self.add)):
            control.setAutoDefault(False)
        self.refresh()
        self._selected(None,None)
    def _values(self):return self.store.data[self.kind]
    def refresh(self):
        theme=self.owner.manager.settings.theme;c=colors(theme);self.set_surface_theme(theme)
        self.setStyleSheet(f'QLineEdit,QPlainTextEdit{{background:{c["input"]};color:{c["text"]};border:1px solid {c["input_border"]};border-radius:8px;padding:8px;font:12px {font_css_stack()};}} QLineEdit:focus,QPlainTextEdit:focus{{border-color:{c["ring"]};}}')
        css(self.list,self._list_style[theme=='light']+f'QListWidget{{color:{c["text"]};background:{c["card"]};border:1px solid {c["border"]};border-radius:8px;padding:4px;}} QListWidget::item{{padding:7px 10px;border-radius:6px;}} QListWidget::item:hover{{background:{c["control_hover"]};}} QListWidget::item:selected{{background:{c["action"]};color:{c["on_action"]};}}')
        refresh_tree(self,theme)
        selection=self.selected_id
        blocker=QSignalBlocker(self.list);self.list.clear()
        selected=None
        for value in self._values():
            item=QListWidgetItem(value if self.kind=='glossary' else value['label'],self.list)
            item.setData(Qt.UserRole,value if self.kind=='glossary' else value['id'])
            if item.data(Qt.UserRole)==selection:selected=item
        if selected is not None:self.list.setCurrentItem(selected)
        del blocker
        if selected is None and selection is not None:self._selected(None,None)
    def _selected(self,item,previous):
        self.selected_id=item.data(Qt.UserRole) if item else None
        editing=item is not None
        noun='术语' if self.kind=='glossary' else '预设'
        self.edit_heading.setText(('编辑' if editing else '新')+noun)
        self.add.setVisible(not editing);self.update_button.setVisible(editing)
        self.remove.setEnabled(editing)
        index=self.list.currentRow()
        for direction,control in zip((-1,1),self.move_buttons):
            control.setEnabled(editing and 0<=index+direction<len(self._values()))
        self.message.clear()
        if self.kind=='glossary' and item:
            self.name.setText(self.selected_id)
        if self.kind=='presets' and item:
            row=next((row for row in self._values() if row['id']==self.selected_id),None)
            if row:self.name.setText(row['label']);self.instruction.setPlainText(row['instruction'])
    def new_entry(self):
        self.list.setCurrentItem(None);self._selected(None,None)
        self.name.clear();self.instruction.clear();self.name.setFocus()
    def _save_selection(self):
        self._update() if self.selected_id is not None else self._add()
    def _add(self):
        name=self.name.text().strip();values=self._values();limit=100 if self.kind=='glossary' else 40
        if not name or len(values)>=limit:self.message.setText('请输入名称；最多可保存 '+str(limit)+' 项。');return
        if self.kind=='glossary':
            if name in values:self.message.setText('这条术语已存在。');return
            values.append(name)
        else:
            prompt=self.instruction.toPlainText().strip()
            if not prompt or len(prompt)>1000:self.message.setText('请输入 1–1000 个字符的指令。');return
            values.append(dict(id=str(uuid4()),label=name,instruction=prompt))
        self.store.save();self.name.clear();self.instruction.clear();self.message.clear();self.refresh()
    def _update(self):
        if not self.selected_id:return
        name=self.name.text().strip()
        if self.kind=='glossary':
            values=self._values()
            if not name:self.message.setText('请输入术语。');return
            if name in values and name!=self.selected_id:self.message.setText('这条术语已存在。');return
            if self.selected_id not in values:return
            values[values.index(self.selected_id)]=name
            self.selected_id=name
            self.store.save();self.refresh();self.message.clear();return
        row=next((row for row in self._values() if row['id']==self.selected_id),None)
        prompt=self.instruction.toPlainText().strip()
        if row and name and prompt and len(prompt)<=1000:row.update(label=name,instruction=prompt);self.store.save();self.refresh()
        else:self.message.setText('请输入名称和 1–1000 个字符的指令。')
    def _move(self,direction):
        if self.kind!='presets' or not self.selected_id:return
        values=self._values();index=next((index for index,row in enumerate(values) if row['id']==self.selected_id),-1);target=index+direction
        if index<0 or not 0<=target<len(values):return
        values[index],values[target]=values[target],values[index];self.store.save();self.refresh();self.list.setCurrentRow(target)
    def _delete(self):
        if self.selected_id is None:return
        values=self._values()
        values[:]=[value for value in values if (value if self.kind=='glossary' else value['id'])!=self.selected_id]
        self.store.save();self.refresh();self.new_entry()

class TranslationRuleManager(TranslatorEditorDialog):
    def __init__(self,owner):
        super().__init__(owner)
        self.owner=owner
        self.settings=owner.manager.settings
        self.selected_source=None
        self._pending_source=None
        self._saving=False
        self._writer=self.settings.preference_writer()
        self._writer.completed.connect(self._saved,Qt.QueuedConnection)
        self.codes={'source':'en','target':'zh'}
        self.setFont(owner.font());self.setWindowTitle('翻译规则')
        heading=QHBoxLayout();heading.addWidget(label('翻译规则',14,600,parent=self.body),1)
        close=TranslatorToolButton('close',self.body);close.setAccessibleName('关闭翻译规则')
        close.clicked.connect(self.close);heading.addWidget(close);self.body_layout.addLayout(heading)
        self.list=picker_list(self.body);self.list.setFixedHeight(200)
        self.list.setAccessibleName('已保存的翻译规则')
        self.list.currentItemChanged.connect(self._select_rule)
        self.body_layout.addWidget(self.list)
        self.empty=label('还没有翻译规则',12,400,'secondary',self.body)
        self.body_layout.addWidget(self.empty)
        self.controls={}
        row=QHBoxLayout()
        for side,title in (('source','原文语言'),('target','目标语言')):
            row.addWidget(label(title,12,400,parent=self.body))
            control=LanguageButton(self.body);control.setAccessibleName(title);row.addWidget(control,1)
            popup=LanguagePopup(owner.store,self,side=side)
            control.chevron.attach(popup)
            popup.selected.connect(lambda code,side=side:self._set_code(side,code))
            control.clicked.connect(lambda checked=False,side=side:self._show_languages(side))
            self.controls[side]=(control,popup)
        self.body_layout.addLayout(row)
        self.message=label('',11,400,'red',self.body);self.message.setWordWrap(True)
        self.body_layout.addWidget(self.message)
        actions=QHBoxLayout()
        new=button('新建','plus',variant='secondary',parent=self.body);new.clicked.connect(self._new)
        actions.addWidget(new);actions.addStretch(1)
        self.remove=button('删除所选','trash',variant='danger',parent=self.body)
        self.remove.clicked.connect(self._delete);actions.addWidget(self.remove)
        self.save=button('添加规则',variant='primary',parent=self.body)
        self.save.clicked.connect(self._save);actions.addWidget(self.save)
        self.body_layout.addLayout(actions)
        for control in (new,self.remove,self.save):control.setAutoDefault(False)
        self.refresh()

    def refresh(self):
        theme=self.settings.theme;c=colors(theme)
        self.set_surface_theme(theme);refresh_tree(self,theme)
        self.list.setStyleSheet(f'QListWidget{{background:{c["card"]};color:{c["text"]};border:1px solid {c["border"]};border-radius:12px;padding:4px;}} QListWidget::item{{padding:7px 10px;border-radius:8px;}} QListWidget::item:hover{{background:{c["control_hover"]};}} QListWidget::item:selected{{background:{c["action"]};color:{c["on_action"]};}}')
        rules=self.settings.translation_rules
        with QSignalBlocker(self.list):
            self.list.clear()
            for code,(name,_) in TARGETS.items():
                if code not in rules:continue
                item=QListWidgetItem(name+'  →  '+TARGETS[rules[code]][0],self.list)
                item.setData(Qt.UserRole,code)
                if code==self.selected_source:self.list.setCurrentItem(item)
        self.empty.setVisible(not rules)
        self.remove.setEnabled(self.selected_source in rules and not self._saving)
        self.save.setEnabled(not self._saving)
        self.save.setText('保存修改' if self.selected_source in rules else '添加规则')
        for side,(control,_) in self.controls.items():control.set_language(self.codes[side])

    def _select_rule(self,item,_previous):
        if item is None or self._saving:return
        self.selected_source=item.data(Qt.UserRole)
        self.codes.update(source=self.selected_source,target=self.settings.translation_rules[self.selected_source])
        self.message.clear()
        for side,(control,_) in self.controls.items():control.set_language(self.codes[side])
        self.remove.setEnabled(True);self.save.setText('保存修改')

    def _set_code(self,side,code):
        self.codes[side]=code
        self.controls[side][0].set_language(code)
        self.message.clear()

    def _show_languages(self,side):
        control,popup=self.controls[side]
        if popup.isVisible():popup.hide();return
        for _,other in self.controls.values():other.hide()
        popup.configure(self.codes[side],allow_auto=False)
        popup.popup_below(control);popup.search.setFocus()

    def _new(self):
        self.selected_source=None
        self.message.clear()
        self.refresh()

    def _save(self):
        source,target=self.codes['source'],self.codes['target']
        rules=self.settings.translation_rules
        if source==target:
            self.message.setText('原文语言和目标语言必须不同。');return
        if source in rules and source!=self.selected_source:
            self.message.setText('这门原文语言已有规则，请选中后修改。');return
        if self.selected_source is not None:rules.pop(self.selected_source,None)
        rules[source]=target
        self._write(rules,source)

    def _delete(self):
        rules=self.settings.translation_rules
        if self.selected_source not in rules:return
        del rules[self.selected_source]
        self._write(rules,None)

    def _write(self,rules,selection):
        if self._saving:return
        self._pending_source=selection
        self._saving=True;self.message.clear();self.refresh()
        self._writer.request(translation_rules=rules)

    def _saved(self,fields,error):
        if not self._saving or 'translation_rules' not in fields:return
        self._saving=False
        if error:self.message.setText('未能保存规则，请重试。')
        else:self.selected_source=self._pending_source
        self.refresh()

    def hideEvent(self,event):
        for _,popup in getattr(self,'controls',{}).values():popup.hide()
        super().hideEvent(event)
