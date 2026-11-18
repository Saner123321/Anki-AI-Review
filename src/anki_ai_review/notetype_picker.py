from aqt.qt import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel,
                    QScrollArea, QVBoxLayout, QWidget)
from aqt.utils import askUser, showWarning

from .adapter import field_mapping
from .core import DEFAULT_NOTETYPE


class NoteTypePicker(QDialog):
    def __init__(self, parent, note_type, fields, manual, available):
        super().__init__(parent)
        self.setWindowTitle("切换笔记类型")
        self.resize(490, 420)
        self.fields = fields
        self.manual = manual
        self.available = available
        self.mapping = {}
        self.inputs = {}

        layout = QVBoxLayout(self)
        self.type_choice = QComboBox()
        self.type_choice.addItems(sorted(available, key=lambda name: (name != DEFAULT_NOTETYPE, name.casefold())))
        current = self.type_choice.findText(note_type)
        if current >= 0:
            self.type_choice.setCurrentIndex(current)
        layout.addWidget(QLabel("目标笔记类型"))
        layout.addWidget(self.type_choice)
        layout.addWidget(QLabel("为每个目标字段选择要保留的原字段。"))
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        self.form = QFormLayout(content)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("采用此笔记类型")
        buttons.accepted.connect(self.confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.type_choice.currentTextChanged.connect(self.show_fields)
        self.show_fields(self.type_choice.currentText())

    def show_fields(self, note_type):
        while self.form.rowCount():
            self.form.removeRow(0)
        self.inputs.clear()
        target_fields = self.available[note_type]
        suggested = field_mapping(self.fields, target_fields)
        for field in target_fields:
            choice = QComboBox()
            choice.addItem("留空", None)
            for source in self.fields:
                choice.addItem(source, source)
            choice.setCurrentIndex(max(0, choice.findData(suggested.get(field))))
            self.form.addRow(field, choice)
            self.inputs[field] = choice

    def confirm(self):
        mapping = {field: choice.currentData() for field, choice in self.inputs.items()
                   if choice.currentData() is not None}
        mapped = set(mapping.values())
        if len(mapped) != len(mapping):
            showWarning("每个原字段只能对应一个目标字段。", parent=self)
            return
        omitted = [field for field, value in self.fields.items()
                   if field not in mapped and (value or self.manual.get(field))]
        if omitted and not askUser(
            "以下字段有内容，将不随本次切换带入：\n"
            + "、".join(omitted) + "\n\n仍然切换？", parent=self
        ):
            return
        self.mapping = mapping
        self.accept()
