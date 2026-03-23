"""Keyboard / mouse shortcut management mixin."""

import json

from PyQt6.QtCore import (
    Qt,
)
from PyQt6.QtGui import (
    QAction,
    QKeySequence,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGraphicsView,
    QGroupBox,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QStyleOptionGroupBox,
    QTextEdit,
    QVBoxLayout,
)

from open_road_editor.constants import *  # noqa: F401,F403


class _ShortcutsMixin:
    """Mixin — see viewer/main.py for the assembled class."""

    @staticmethod
    def _mode_toggle_tooltips(readonly_tooltip: str) -> tuple[str, str]:
        ro_tip = str(readonly_tooltip or 'Read-only mode').strip() or 'Read-only mode'
        if 'read-only mode' in ro_tip.lower():
            edit_tip = ro_tip.replace('read-only mode', 'edit mode enabled')
            edit_tip = edit_tip.replace('Read-only mode', 'Edit mode enabled')
        else:
            prefix = ro_tip.split(':', 1)[0] if ':' in ro_tip else ro_tip
            edit_tip = f'{prefix} edit mode enabled'
        return edit_tip, ro_tip

    @staticmethod
    def _set_mode_toggle_visual(button: QPushButton, checked: bool) -> None:
        # Use explicit, high-contrast states: pencil for edit, lock for read-only.
        button.setText('✎' if checked else '🔒')
        button.setStyleSheet(
            'QPushButton { padding: 0; border: 1px solid palette(mid); border-radius: 4px; }'
            'QPushButton:checked { background: #227a4c; color: white; border-color: #1b5e3a; }'
            'QPushButton:!checked { background: #5b636a; color: white; border-color: #4a5157; }'
        )

    def _load_keyboard_shortcuts(self) -> dict:
        raw = self.settings.value('keyboard_shortcuts')
        loaded = None
        if isinstance(raw, dict):
            loaded = raw
        elif isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    loaded = parsed
            except Exception:
                loaded = None
        merged = dict(KEYBOARD_SHORTCUT_DEFAULTS)
        if isinstance(loaded, dict):
            for key in KEYBOARD_SHORTCUT_DEFAULTS:
                if key in loaded:
                    merged[key] = str(loaded[key] or '').strip()
        return merged

    def _save_keyboard_shortcuts(self):
        self.settings.setValue('keyboard_shortcuts', json.dumps(self._keyboard_shortcuts))

    @staticmethod
    def _modifier_text_to_flags(text: str):
        value = str(text or 'None').strip()
        if value == 'Ctrl':
            return Qt.KeyboardModifier.ControlModifier
        if value == 'Shift':
            return Qt.KeyboardModifier.ShiftModifier
        if value == 'Alt':
            return Qt.KeyboardModifier.AltModifier
        if value == 'Ctrl+Shift':
            return Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        if value == 'Ctrl+Alt':
            return Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
        if value == 'Shift+Alt':
            return Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier
        if value == 'Ctrl+Shift+Alt':
            return (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.ShiftModifier
                | Qt.KeyboardModifier.AltModifier
            )
        return Qt.KeyboardModifier.NoModifier

    @staticmethod
    def _normalize_modifier_text(text: str) -> str:
        value = str(text or 'None').strip()
        return value if value in MOUSE_MODIFIER_OPTIONS else 'None'

    def _load_mouse_bindings(self) -> dict:
        raw = self.settings.value('mouse_bindings')
        loaded = None
        if isinstance(raw, dict):
            loaded = raw
        elif isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    loaded = parsed
            except Exception:
                loaded = None
        merged = dict(MOUSE_BINDING_DEFAULTS)
        if isinstance(loaded, dict):
            for key in MOUSE_BINDING_DEFAULTS:
                if key in loaded:
                    merged[key] = self._normalize_modifier_text(loaded[key])
        return merged

    def _save_mouse_bindings(self):
        self.settings.setValue('mouse_bindings', json.dumps(self._mouse_bindings))

    def _setup_keyboard_shortcuts(self):
        self._keyboard_shortcuts = self._load_keyboard_shortcuts()
        self._mouse_bindings = self._load_mouse_bindings()
        self.sc_undo = QShortcut(self)
        self.sc_undo.activated.connect(self._undo_active_edit)
        self.sc_redo1 = QShortcut(self)
        self.sc_redo1.activated.connect(self._redo_active_edit)
        self.sc_redo2 = QShortcut(self)
        self.sc_redo2.activated.connect(self._redo_active_edit)
        self.sc_delete_segment = QShortcut(self)
        self.sc_delete_segment.activated.connect(self._delete_active_segment)
        self.sc_refresh_all_layers = QShortcut(self)
        self.sc_refresh_all_layers.activated.connect(self.refresh_all_layers)
        self._apply_keyboard_shortcuts()

    def _set_shortcut_for_action(self, action: QAction, seq_text: str):
        action.setShortcut(QKeySequence(seq_text) if seq_text else QKeySequence())

    def _set_shortcut_for_shortcut(self, shortcut, seq_text: str):
        shortcut.setKey(QKeySequence(seq_text) if seq_text else QKeySequence())

    def _apply_keyboard_shortcuts(self):
        if not hasattr(self, '_keyboard_shortcuts'):
            self._keyboard_shortcuts = dict(KEYBOARD_SHORTCUT_DEFAULTS)
        m = self._keyboard_shortcuts
        self._set_shortcut_for_action(self.action_new, m.get('file_new', ''))
        self._set_shortcut_for_action(self.action_open, m.get('file_open', ''))
        self._set_shortcut_for_action(self.action_save, m.get('file_save', ''))
        self._set_shortcut_for_action(self.action_save_as, m.get('file_save_as', ''))
        self._set_shortcut_for_shortcut(self.sc_undo, m.get('osm_undo', ''))
        self._set_shortcut_for_shortcut(self.sc_redo1, m.get('osm_redo_primary', ''))
        self._set_shortcut_for_shortcut(self.sc_redo2, m.get('osm_redo_secondary', ''))
        self._set_shortcut_for_shortcut(self.sc_delete_segment, m.get('osm_delete_segment', ''))
        self._set_shortcut_for_shortcut(
            self.sc_refresh_all_layers, m.get('refresh_all_layers', '')
        )

    def _shortcut_export_xodr(self) -> None:
        if (
            hasattr(self, 'action_export_opendrive')
            and self.action_export_opendrive.isVisible()
            and self.action_export_opendrive.isEnabled()
        ):
            self._export_xodr_file()

    def _update_file_menu_actions_visibility(
        self, has_xodr: bool | None = None, has_osm: bool | None = None
    ) -> None:
        has_xodr = bool(self.xodr_path is not None) if has_xodr is None else bool(has_xodr)
        has_osm = bool(self.osm_path is not None) if has_osm is None else bool(has_osm)

        if hasattr(self, 'action_import_osm'):
            self.action_import_osm.setVisible(True)

        if hasattr(self, 'action_export_osm'):
            self.action_export_osm.setVisible(has_osm)

        if hasattr(self, 'action_export_opendrive'):
            self.action_export_opendrive.setVisible(has_osm and has_xodr)

    def _shortcut_update_xodr_width(self) -> None:
        return

    def _undo_active_edit(self) -> None:
        self._osm_undo_move()

    def _redo_active_edit(self) -> None:
        self._osm_redo_move()

    def _delete_active_segment(self) -> None:
        # Do not hijack Del while typing in an editor widget.
        focused = QApplication.focusWidget()
        if isinstance(
            focused,
            (
                QLineEdit,
                QKeySequenceEdit,
                QAbstractSpinBox,
                QComboBox,
                QTextEdit,
                QPlainTextEdit,
            ),
        ):
            return
        if not self._delete_selected_osm_node() and not self._delete_selected_osm_sign_node():
            self._delete_selected_osm_segment()

    def _open_keyboard_shortcuts_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('Keyboard Shortcuts')
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('Configure keyboard shortcuts for common operations.'))
        form = QFormLayout()
        editors = {}
        for key, label in KEYBOARD_SHORTCUT_LABELS:
            current = self._keyboard_shortcuts.get(key, KEYBOARD_SHORTCUT_DEFAULTS.get(key, ''))
            editor = QKeySequenceEdit(QKeySequence(current))
            editors[key] = editor
            form.addRow(f'{label}:', editor)
        layout.addLayout(form)

        roundabout_info = QLabel(
            'Roundabout move: select a roundabout in OSM edit mode, then use '
            'Ctrl+Arrow keys to move it. Hold Shift for a larger step.'
        )
        roundabout_info.setWordWrap(True)
        layout.addWidget(roundabout_info)

        node_move_info = QLabel(
            'Control nodes: in OSM edit mode, select a segment and Ctrl+drag a control node '
            'circle to move it.'
        )
        node_move_info.setWordWrap(True)
        layout.addWidget(node_move_info)

        mouse_group = QGroupBox('Mouse + Keyboard Bindings (Right-click)')
        mouse_layout = QFormLayout(mouse_group)
        mouse_editors = {}
        for key, label in MOUSE_BINDING_LABELS:
            combo = QComboBox()
            combo.addItems(MOUSE_MODIFIER_OPTIONS)
            current = self._mouse_bindings.get(key, MOUSE_BINDING_DEFAULTS.get(key, 'None'))
            combo.setCurrentText(self._normalize_modifier_text(current))
            mouse_editors[key] = combo
            mouse_layout.addRow(f'{label}:', combo)
        layout.addWidget(mouse_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_reset = buttons.addButton('Restore Defaults', QDialogButtonBox.ButtonRole.ResetRole)

        def _restore_defaults():
            for key, editor in editors.items():
                editor.setKeySequence(QKeySequence(KEYBOARD_SHORTCUT_DEFAULTS.get(key, '')))
            for key, combo in mouse_editors.items():
                combo.setCurrentText(MOUSE_BINDING_DEFAULTS.get(key, 'None'))

        btn_reset.clicked.connect(_restore_defaults)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        updated = {}
        for key, _label in KEYBOARD_SHORTCUT_LABELS:
            seq = (
                editors[key]
                .keySequence()
                .toString(QKeySequence.SequenceFormat.PortableText)
                .strip()
            )
            updated[key] = seq
        self._keyboard_shortcuts = updated
        updated_mouse = {}
        for key, _label in MOUSE_BINDING_LABELS:
            updated_mouse[key] = self._normalize_modifier_text(mouse_editors[key].currentText())
        self._mouse_bindings = updated_mouse
        self._save_keyboard_shortcuts()
        self._save_mouse_bindings()
        self._apply_keyboard_shortcuts()
        self._show_project_status('Updated input bindings')

    def _import_file(self, file_type: str):
        """Import an OSM file via the File menu."""
        if file_type == 'osm':
            path, _ = QFileDialog.getOpenFileName(
                self,
                'Import OSM',
                '',
                'OpenStreetMap Files (*.osm *.xml);;All Files (*)',
                options=QFileDialog.Option.DontUseNativeDialog,
            )
            if path:
                has_osm = bool(
                    self.osm_path or self._compose_current_osm_content() or self._osm_content
                )
                has_xodr = bool(self.xodr_path)

                if has_osm:
                    prompt = 'An OSM file is already loaded. Importing another OSM will clear it. Continue?'
                    reply = QMessageBox.question(
                        self,
                        'Confirm Import',
                        prompt,
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if reply == QMessageBox.StandardButton.No:
                        return
                    if has_xodr:
                        self.edit_xodr.setText('')
                elif has_xodr:
                    reply = QMessageBox.question(
                        self,
                        'Confirm Import',
                        'Importing OSM will replace the current map content. Continue?',
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if reply == QMessageBox.StandardButton.No:
                        return
                    self.edit_xodr.setText('')

                self.edit_osm.setText(path)  # triggers on_osm_path_changed
                self.check_osm.setChecked(True)
                # OSM import: show both layers, OSM first
                self._arrange_import_layers(
                    show_xodr=True, show_osm=True, osm_first=True, reset_objects=True
                )

    def _arrange_import_layers(
        self, show_xodr: bool, show_osm: bool, osm_first: bool = False, reset_objects: bool = False
    ):
        """Show/hide and reorder the OpenDRIVE and OSM layer rows."""
        # Remove both from the layout (doesn't destroy them)
        self._layers_layout.removeWidget(self._opendrive_layer_widget)
        self._layers_layout.removeWidget(self._osm_layer_widget)
        # Re-insert at the top in the desired order
        if osm_first:
            self._layers_layout.insertWidget(0, self._osm_layer_widget)
            self._layers_layout.insertWidget(1, self._opendrive_layer_widget)
        else:
            self._layers_layout.insertWidget(0, self._opendrive_layer_widget)
            self._layers_layout.insertWidget(1, self._osm_layer_widget)
        self._opendrive_layer_widget.setVisible(show_xodr)
        self._osm_layer_widget.setVisible(show_osm)
        if reset_objects:
            if hasattr(self, 'check_opendrive_objects'):
                self.check_opendrive_objects.setChecked(True)
            if hasattr(self, 'check_osm_objects'):
                self.check_osm_objects.setChecked(True)

    def _start_project_status_fade(self):
        self._project_status_fade_anim.stop()
        self._project_status_fade_anim.setStartValue(self._project_status_effect.opacity())
        self._project_status_fade_anim.setEndValue(0.0)
        self._project_status_fade_anim.start()

    def _on_project_status_fade_finished(self):
        if self._project_status_effect.opacity() <= 0.01:
            self.lbl_project_status.setVisible(False)

    def _show_project_status(self, message: str):
        self._project_status_hide_timer.stop()
        self._project_status_fade_anim.stop()
        self.lbl_project_status.setText(message)
        self.lbl_project_status.setVisible(True)
        self._project_status_effect.setOpacity(1.0)
        self._project_status_hide_timer.start(TOAST_NOTIFICATION_DURATION_MS)

    def _osm_edit_enabled(self) -> bool:
        return bool(
            self.check_osm.isChecked()
            and getattr(self, 'btn_osm_edit_mode', None)
            and self.btn_osm_edit_mode.isChecked()
        )

    def _on_osm_edit_mode_toggled(self, checked: bool) -> None:
        if hasattr(self, 'btn_osm_edit_mode'):
            self._set_mode_toggle_visual(self.btn_osm_edit_mode, checked)
            self.btn_osm_edit_mode.setToolTip(
                'OSM edit mode enabled' if checked else 'OSM read-only mode'
            )
            self._position_osm_edit_mode_button()
        if not checked:
            self._osm_relation_pick_mode = None
            self._osm_tags_edit_mode = False
            self._osm_node_tags_edit_mode = False
            self._osm_relation_edit_mode['preceding'] = False
            self._osm_relation_edit_mode['succeeding'] = False
            self._osm_relation_draft['preceding'] = None
            self._osm_relation_draft['succeeding'] = None
        sel = self._osm_selected_item
        if sel is not None:
            self._osm_show_props(sel)
            self._show_osm_node_dots(sel)
            if not checked:
                self._show_selected_osm_node_props()
        self._show_project_status('OSM edit mode enabled' if checked else 'OSM edit mode disabled')

    def _on_osm_props_edit_mode_toggled(self, checked: bool) -> None:
        if not hasattr(self, 'btn_osm_props_edit_mode'):
            return
        self._set_mode_toggle_visual(self.btn_osm_props_edit_mode, checked)
        self.btn_osm_props_edit_mode.setToolTip(
            'Segment properties edit mode enabled' if checked else 'Segment properties read-only'
        )
        self._position_osm_props_edit_mode_button()
        if not checked:
            self._osm_tags_edit_mode = False
            sel = self._osm_selected_item
            if sel is not None:
                try:
                    # Persist any pending tag edits before re-populating the UI
                    if hasattr(self, '_on_osm_tag_edited'):
                        self._on_osm_tag_edited(sel)
                except Exception:
                    pass
                self._osm_show_props(sel)
            return
        if not self._osm_edit_enabled():
            self._show_project_status('Enable OSM Edit mode first')
            self.btn_osm_props_edit_mode.setChecked(False)
            return
        sel = self._osm_selected_item
        if sel is None:
            self._show_project_status('Select an OSM segment first')
            self.btn_osm_props_edit_mode.setChecked(False)
            return
        self._osm_tags_edit_mode = True
        self._osm_show_props(sel)

    def _on_osm_node_props_edit_mode_toggled(self, checked: bool) -> None:
        if not hasattr(self, 'btn_osm_node_props_edit_mode'):
            return
        self._set_mode_toggle_visual(self.btn_osm_node_props_edit_mode, checked)
        self.btn_osm_node_props_edit_mode.setToolTip(
            'Node properties edit mode enabled' if checked else 'Node properties read-only'
        )
        self._position_osm_node_props_edit_mode_button()
        if not checked:
            self._osm_node_tags_edit_mode = False
            try:
                if hasattr(self, '_commit_selected_osm_node_tag_edits'):
                    self._commit_selected_osm_node_tag_edits()
            except Exception:
                pass
            self._show_selected_osm_node_props()
            return
        if not self._osm_edit_enabled():
            self._show_project_status('Enable OSM Edit mode first')
            self.btn_osm_node_props_edit_mode.setChecked(False)
            return
        if not self._osm_selected_node_id():
            self._show_project_status('Select an OSM node first')
            self.btn_osm_node_props_edit_mode.setChecked(False)
            return
        self._osm_node_tags_edit_mode = True
        self._show_selected_osm_node_props()

    def _position_osm_edit_mode_button(self) -> None:
        if not hasattr(self, 'grp_osm_opts') or not hasattr(self, 'btn_osm_edit_mode'):
            return
        option = QStyleOptionGroupBox()
        self.grp_osm_opts.initStyleOption(option)
        title_rect = self.grp_osm_opts.style().subControlRect(
            QStyle.ComplexControl.CC_GroupBox,
            option,
            QStyle.SubControl.SC_GroupBoxLabel,
            self.grp_osm_opts,
        )
        btn = self.btn_osm_edit_mode
        x = title_rect.right() + 6
        y = title_rect.center().y() - (btn.height() // 2)
        btn.move(x, y)
        btn.raise_()

    def _position_osm_props_edit_mode_button(self) -> None:
        if not hasattr(self, '_osm_props_group') or not hasattr(self, 'btn_osm_props_edit_mode'):
            return
        option = QStyleOptionGroupBox()
        self._osm_props_group.initStyleOption(option)
        title_rect = self._osm_props_group.style().subControlRect(
            QStyle.ComplexControl.CC_GroupBox,
            option,
            QStyle.SubControl.SC_GroupBoxLabel,
            self._osm_props_group,
        )
        btn = self.btn_osm_props_edit_mode
        x = title_rect.right() + 6
        y = title_rect.center().y() - (btn.height() // 2)
        btn.move(x, y)
        btn.raise_()

    def _position_osm_node_props_edit_mode_button(self) -> None:
        if not hasattr(self, '_osm_node_props_group') or not hasattr(
            self, 'btn_osm_node_props_edit_mode'
        ):
            return
        option = QStyleOptionGroupBox()
        self._osm_node_props_group.initStyleOption(option)
        title_rect = self._osm_node_props_group.style().subControlRect(
            QStyle.ComplexControl.CC_GroupBox,
            option,
            QStyle.SubControl.SC_GroupBoxLabel,
            self._osm_node_props_group,
        )
        btn = self.btn_osm_node_props_edit_mode
        x = title_rect.right() + 6
        y = title_rect.center().y() - (btn.height() // 2)
        btn.move(x, y)
        btn.raise_()

    def _position_world_edit_mode_button(self) -> None:
        if not hasattr(self, 'grp_world_info') or not hasattr(self, 'btn_world_edit_mode'):
            return
        option = QStyleOptionGroupBox()
        self.grp_world_info.initStyleOption(option)
        title_rect = self.grp_world_info.style().subControlRect(
            QStyle.ComplexControl.CC_GroupBox,
            option,
            QStyle.SubControl.SC_GroupBoxLabel,
            self.grp_world_info,
        )
        btn = self.btn_world_edit_mode
        x = title_rect.right() + 6
        y = title_rect.center().y() - (btn.height() // 2)
        btn.move(x, y)
        btn.raise_()

    def _position_esri_edit_mode_button(self) -> None:
        if not hasattr(self, 'grp_esri') or not hasattr(self, 'btn_esri_edit_mode'):
            return
        option = QStyleOptionGroupBox()
        self.grp_esri.initStyleOption(option)
        title_rect = self.grp_esri.style().subControlRect(
            QStyle.ComplexControl.CC_GroupBox,
            option,
            QStyle.SubControl.SC_GroupBoxLabel,
            self.grp_esri,
        )
        btn = self.btn_esri_edit_mode
        x = title_rect.right() + 6
        y = title_rect.center().y() - (btn.height() // 2)
        btn.move(x, y)
        btn.raise_()

    def _position_carla_edit_mode_button(self) -> None:
        if not hasattr(self, 'grp_carla_bev') or not hasattr(self, 'btn_carla_edit_mode'):
            return
        option = QStyleOptionGroupBox()
        self.grp_carla_bev.initStyleOption(option)
        title_rect = self.grp_carla_bev.style().subControlRect(
            QStyle.ComplexControl.CC_GroupBox,
            option,
            QStyle.SubControl.SC_GroupBoxLabel,
            self.grp_carla_bev,
        )
        btn = self.btn_carla_edit_mode
        x = title_rect.right() + 6
        y = title_rect.center().y() - (btn.height() // 2)
        btn.move(x, y)
        btn.raise_()

    def _style_osm_lock_button(self, button: QPushButton, checked: bool, tooltip: str) -> None:
        button.setCheckable(True)
        button.setChecked(checked)
        button.setFixedSize(26, 20)
        edit_tip, readonly_tip = self._mode_toggle_tooltips(tooltip)
        button.setToolTip(edit_tip if checked else readonly_tip)
        self._set_mode_toggle_visual(button, checked)

    def _on_world_edit_mode_toggled(self, checked: bool) -> None:
        if hasattr(self, 'btn_world_edit_mode'):
            self._set_mode_toggle_visual(self.btn_world_edit_mode, checked)
            self.btn_world_edit_mode.setToolTip(
                'World edit mode enabled' if checked else 'World read-only mode'
            )
            self._position_world_edit_mode_button()
        if not checked:
            self._extent_drag_edge = None
            self._extent_drag_start_vp = None
            self._clear_extent_hover()
            if getattr(self, 'btn_select_extent', None) is not None:
                self.btn_select_extent.setChecked(False)
            if getattr(self, 'view', None) is not None:
                self.view.viewport().unsetCursor()
        world_spinboxes = [
            getattr(self, 'spin_origin_lat', None),
            getattr(self, 'spin_origin_lon', None),
            getattr(self, 'spin_bound_north', None),
            getattr(self, 'spin_bound_south', None),
            getattr(self, 'spin_bound_east', None),
            getattr(self, 'spin_bound_west', None),
        ]
        for spinbox in world_spinboxes:
            if spinbox is not None:
                spinbox.setReadOnly(not checked)
                spinbox.setButtonSymbols(
                    QAbstractSpinBox.ButtonSymbols.UpDownArrows
                    if checked
                    else QAbstractSpinBox.ButtonSymbols.NoButtons
                )
        world_edit_widgets = [
            getattr(self, 'btn_select_extent', None),
        ]
        for widget in world_edit_widgets:
            if widget is not None:
                widget.setEnabled(bool(checked))
        self._update_world_bounds_action_buttons()

    def _update_world_bounds_action_buttons(self) -> None:
        world_edit_enabled = bool(
            hasattr(self, 'btn_world_edit_mode') and self.btn_world_edit_mode.isChecked()
        )

        if hasattr(self, 'btn_world_update_xodr'):
            self.btn_world_update_xodr.setEnabled(
                world_edit_enabled
                and hasattr(self, 'check_opendrive')
                and self.check_opendrive.isChecked()
            )
        if hasattr(self, 'btn_world_update_carla'):
            self.btn_world_update_carla.setEnabled(
                world_edit_enabled
                and hasattr(self, 'check_carla_bev')
                and self.check_carla_bev.isChecked()
            )

    def _on_esri_edit_mode_toggled(self, checked: bool) -> None:
        if hasattr(self, 'btn_esri_edit_mode'):
            self._set_mode_toggle_visual(self.btn_esri_edit_mode, checked)
            self.btn_esri_edit_mode.setToolTip(
                'ESRI edit mode enabled' if checked else 'ESRI read-only mode'
            )
            self._position_esri_edit_mode_button()
        if checked:
            self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.view.viewport().setCursor(Qt.CursorShape.SizeAllCursor)
            self.view.installEventFilter(self)
            self.view.setFocus()
        else:
            self.view.removeEventFilter(self)
            self.view.viewport().unsetCursor()
            self._esri_drag_last = None
        esri_spinboxes = [
            getattr(self, 'spin_esri_x', None),
            getattr(self, 'spin_esri_y', None),
            getattr(self, 'spin_esri_nudge_step', None),
            getattr(self, 'spin_esri_shift_nudge_step', None),
        ]
        for spinbox in esri_spinboxes:
            if spinbox is not None:
                spinbox.setReadOnly(not checked)
                spinbox.setButtonSymbols(
                    QAbstractSpinBox.ButtonSymbols.UpDownArrows
                    if checked
                    else QAbstractSpinBox.ButtonSymbols.NoButtons
                )
        for widget_name in ('btn_esri_offset_reset',):
            widget = getattr(self, widget_name, None)
            if widget is not None:
                widget.setEnabled(bool(checked))

    def _on_carla_edit_mode_toggled(self, checked: bool) -> None:
        if hasattr(self, 'btn_carla_edit_mode'):
            self._set_mode_toggle_visual(self.btn_carla_edit_mode, checked)
            self.btn_carla_edit_mode.setToolTip(
                'CARLA edit mode enabled' if checked else 'CARLA read-only mode'
            )
            self._position_carla_edit_mode_button()
        for widget_name in ('edit_server_ip', 'edit_server_port'):
            widget = getattr(self, widget_name, None)
            if widget is not None:
                widget.setReadOnly(not checked)

    def _set_osm_props_height(self, height: int) -> None:
        h = int(height)
        h = max(self._osm_props_min_h, min(self._osm_props_max_h, h))
        self._osm_props_scroll.setMinimumHeight(h)
        self._osm_props_scroll.setMaximumHeight(h)

    def _set_osm_node_props_height(self, height: int) -> None:
        h = int(height)
        h = max(self._osm_props_min_h, min(self._osm_props_max_h, h))
        self._osm_node_props_scroll.setMinimumHeight(h)
        self._osm_node_props_scroll.setMaximumHeight(h)
