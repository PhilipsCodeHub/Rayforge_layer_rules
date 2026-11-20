"""Frontend entry point — GTK4 dialog for material parameters."""

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib


class MaterialDialog(Gtk.Dialog):
    """Modal dialog collecting material parameters and behavior toggles.

    preview_provider is an optional callable taking (material_type, is_hard,
    thickness_mm) and returning a list of rows: (label, power_str, speed_str).

    default_material is "balsa" or "ply" — the initial selection of the
    material combo. The caller is responsible for computing this from the
    stock material in the project.
    """

    def __init__(self, parent_window, preview_provider=None,
                 default_material="balsa"):
        super().__init__(
            transient_for=parent_window,
            modal=True,
            title="Laser Parameters",
        )
        self.set_default_size(480, 720)

        self._preview_provider = preview_provider

        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Apply", Gtk.ResponseType.OK)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        self.get_content_area().append(scrolled)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        scrolled.set_child(box)

        # --- Material type ---
        self.material_combo = Gtk.ComboBoxText()
        self.material_combo.append("balsa", "Balsa")
        self.material_combo.append("ply", "Ply")
        if default_material == "ply":
            self.material_combo.set_active_id("ply")
        else:
            self.material_combo.set_active_id("balsa")
        self._row(box, "Material Type", self.material_combo)
        self._caption(
            box,
            "Balsa: 80% power for cuts, 20% for names. "
            "Ply: 100% for cuts, 50% for names.",
        )

        # --- Hardness ---
        self.hard_switch = Gtk.Switch()
        self.hard_switch.set_active(False)
        self._row(box, "Hard Material?", self.hard_switch)
        self._caption(
            box,
            "Enable for hard stock. Cut speed is reduced as if the "
            "material were 1.5\u00d7 thicker, and power is increased by "
            "5 percentage points (capped at 100%).",
        )

        # --- Thickness ---
        self.thickness_spin = Gtk.SpinButton.new_with_range(0.1, 50.0, 0.1)
        self.thickness_spin.set_value(2.0)
        self._row(box, "Thickness (mm)", self.thickness_spin)
        self._caption(box, "Used to compute cut speed via the formula.")

        # --- Compute/set power and speed toggle + live preview table ---
        self.power_speed_switch = self._row_switch(
            box, "Compute/set power and speed"
        )
        self._caption(
            box,
            "Calculates power and cut speed from the material, hardness, "
            "and thickness above.",
        )

        self._preview_power_names = Gtk.Label(xalign=1)
        self._preview_speed_names = Gtk.Label(xalign=1)
        self._preview_power_cut = Gtk.Label(xalign=1)
        self._preview_speed_cut = Gtk.Label(xalign=1)
        self._build_preview_table(box)

        # --- Other toggles ---
        self.cut_side_switch = self._row_switch(
            box, "Set cut side (inside / outside)"
        )
        self._caption(
            box,
            "Applies INSIDE to holes and OUTSIDE to outer contours for "
            "accurate kerf.",
        )

        self.passes_switch = self._row_switch(
            box, "Set multi-pass count (by material)"
        )
        self._caption(box, "2 passes for Balsa, 3 for Ply.")

        self.power_mode_switch = self._row_switch(
            box, "Set laser to constant power mode"
        )
        self._caption(
            box,
            "Forces constant power (M3) on Inside and Outside contours.",
        )

        self.remove_tabs_switch = self._row_switch(
            box, "Remove Tabs layer workflow"
        )
        self._caption(
            box,
            "Deletes the Tabs-layer operation so its geometry is never cut.",
        )

        # --- Wire live-preview updates ---
        self.material_combo.connect("changed", lambda *_: self._refresh_preview())
        self.hard_switch.connect("notify::active", lambda *_: self._refresh_preview())
        self.thickness_spin.connect("value-changed", lambda *_: self._refresh_preview())
        self.power_speed_switch.connect(
            "notify::active", lambda *_: self._refresh_preview()
        )

        self._refresh_preview()

    def _build_preview_table(self, box):
        grid = Gtk.Grid()
        grid.set_column_spacing(20)
        grid.set_row_spacing(2)
        grid.set_margin_start(12)
        grid.set_margin_top(4)
        grid.set_margin_bottom(12)
        grid.add_css_class("dim-label")

        h_power = Gtk.Label(xalign=1)
        h_power.set_markup("<b>power %</b>")
        grid.attach(h_power, 1, 0, 1, 1)

        h_speed = Gtk.Label(xalign=1)
        h_speed.set_markup("<b>speed mm/min</b>")
        grid.attach(h_speed, 2, 0, 1, 1)

        lbl_names = Gtk.Label(label="Engraving / Names", xalign=0)
        grid.attach(lbl_names, 0, 1, 1, 1)
        grid.attach(self._preview_power_names, 1, 1, 1, 1)
        grid.attach(self._preview_speed_names, 2, 1, 1, 1)

        lbl_cut = Gtk.Label(label="Cut (Inside/Outside)", xalign=0)
        grid.attach(lbl_cut, 0, 2, 1, 1)
        grid.attach(self._preview_power_cut, 1, 2, 1, 1)
        grid.attach(self._preview_speed_cut, 2, 2, 1, 1)

        box.append(grid)

    def _refresh_preview(self):
        if self._preview_provider is None:
            return
        try:
            rows = self._preview_provider(
                self.get_material_type(),
                self.get_is_hard(),
                self.get_thickness_mm(),
            )
        except Exception:
            self._preview_power_names.set_text("\u2014")
            self._preview_speed_names.set_text("\u2014")
            self._preview_power_cut.set_text("\u2014")
            self._preview_speed_cut.set_text("\u2014")
            return

        names_row, cut_row = rows[0], rows[1]
        self._preview_power_names.set_text(names_row[1])
        self._preview_speed_names.set_text(names_row[2])
        self._preview_power_cut.set_text(cut_row[1])
        self._preview_speed_cut.set_text(cut_row[2])

    @staticmethod
    def _row(box, label_text, widget):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        label = Gtk.Label(label=label_text, xalign=0)
        label.set_hexpand(True)
        label.set_wrap(True)
        row.append(label)
        widget.set_halign(Gtk.Align.END)
        row.append(widget)
        box.append(row)
        return widget

    @staticmethod
    def _caption(box, text):
        caption = Gtk.Label()
        caption.set_markup(f"<small>{GLib.markup_escape_text(text)}</small>")
        caption.add_css_class("dim-label")
        caption.set_wrap(True)
        caption.set_xalign(0)
        caption.set_margin_start(12)
        caption.set_margin_top(2)
        caption.set_margin_bottom(8)
        box.append(caption)

    @classmethod
    def _row_switch(cls, box, label_text):
        switch = Gtk.Switch()
        switch.set_active(True)
        return cls._row(box, label_text, switch)

    def get_material_type(self):
        return self.material_combo.get_active_id()

    def get_is_hard(self):
        return self.hard_switch.get_active()

    def get_thickness_mm(self):
        return self.thickness_spin.get_value()

    def get_flags(self) -> dict:
        return {
            "set_power_speed": self.power_speed_switch.get_active(),
            "set_cut_side":    self.cut_side_switch.get_active(),
            "set_passes":      self.passes_switch.get_active(),
            "set_power_mode":  self.power_mode_switch.get_active(),
            "remove_tabs":     self.remove_tabs_switch.get_active(),
        }