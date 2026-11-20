"""layer_rules addon backend for Rayforge.

Provides the "Process Layers" action: configurable per-layer application
of power / cut_speed / cut_side / passes / power_mode, optional removal of
Tabs-layer workflow steps, and unconditional layer ordering
(Names -> Inside -> Outside).

Stock-material detection and Balsa material creation are retained because
Process Layers uses them to preselect the material and ensure a suitable
stock material exists.

Cut speed formula:
  Valid up to an effective material thickness of 5.6 mm (hardness factor
  included). Above that, the speed is held at the 5.6 mm value and a warning
  is printed to the console.

Behavior toggles (all default ON) in the material dialog:
  - set_power_speed   -> compute and apply power + cut_speed
  - set_cut_side      -> apply cut_side (INSIDE / OUTSIDE)
  - set_passes        -> apply multi-pass count via MultiPassTransformer
  - set_power_mode    -> force CONSTANT power mode (M3)
  - remove_tabs       -> remove the Tabs layer's workflow steps

Layer reorder is unconditional (Names -> Inside -> Outside).

Multi-pass:
  Stored in each step's per_step_transformers_dicts list, in the
  'MultiPassTransformer' entry:
    - Names:          1 pass
    - Inside/Outside: 2 passes (Balsa) or 3 passes (Ply)
  z_step_down is forced to 0.0 so the laser stays focused between passes.

Live preview:
  The material dialog receives a preview_provider callback. It calls
  _compute_preview(material, is_hard, thickness) on every input change and
  displays a small table with rows for Engraving / Names and Cut / Inside.
"""

import shutil
import traceback
from pathlib import Path

import pluggy
import yaml
from gi.repository import Gio, Gtk
from rayforge.context import get_context
from rayforge.ui_gtk.action_registry import MenuPlacement

from .frontend import MaterialDialog

hookimpl = pluggy.HookimplMarker("rayforge")

_main_window = None

MAX_CUT_SPEED = 1400
MAX_SUPPORTED_THICKNESS_MM = 5.6  # speed formula is only valid up to here
LAYER_ORDER = ["Names", "Inside", "Outside"]



@hookimpl
def main_window_ready(main_window):
    global _main_window
    _main_window = main_window
    print(f"[layer_rules] Main window captured: {type(main_window).__name__}")


# ===========================================================================
# Stock material detection (returns structured info)
# ===========================================================================
def _detect_stock_info() -> dict:
    """Return a dict describing the document's stock.

    Keys:
      material  : "balsa" | "ply" | "other"  (normalised for dialogs)
      name      : the raw material name, if resolvable
      has_stock : True if any stock exists in the document
    """
    default = {
        "material": "balsa",
        "name": None,
        "has_stock": False,
    }

    if _main_window is None:
        return default

    try:
        de = _main_window.doc_editor
        doc = de.doc
    except Exception:
        return default

    material_mgr = None
    try:
        material_mgr = getattr(get_context(), "material_mgr", None)
    except Exception:
        pass

    # --- Collect stock candidates ---
    candidates = []
    try:
        stock_items = list(getattr(doc, "stock_items", []) or [])
        for item in stock_items:
            asset_uid = getattr(item, "stock_asset_uid", None)
            visible = getattr(item, "visible", True)
            asset = None
            try:
                asset = getattr(item, "stock_asset", None)
            except Exception:
                pass
            if asset is None and asset_uid:
                getter = getattr(doc, "get_asset_by_uid", None)
                if callable(getter):
                    try:
                        asset = getter(asset_uid)
                    except Exception:
                        pass
            material_uid = getattr(asset, "material_uid", None) if asset else None
            if material_uid:
                candidates.append((bool(visible), "stock item", item, asset, material_uid))
    except Exception:
        pass

    selected = next((c for c in candidates if c[0]), None)
    if selected is None and candidates:
        selected = candidates[0]

    if selected is None:
        # Rotary stock: layer.stock_material_uid
        try:
            for layer in doc.layers:
                uid = getattr(layer, "stock_material_uid", None)
                if uid:
                    selected = (True, "layer", layer, None, uid)
                    break
        except Exception:
            pass

    if selected is None:
        return default

    has_stock = True
    _visible, _source, owner, asset, material_uid = selected

    # --- Resolve the material object ---
    material = None
    if material_mgr is not None:
        lookup = getattr(material_mgr, "get_material_or_none", None)
        if callable(lookup):
            try:
                material = lookup(material_uid)
            except Exception:
                pass
    if material is None:
        for obj in (asset, owner):
            try:
                c = getattr(obj, "material", None) if obj is not None else None
                if c is not None:
                    material = c
                    break
            except Exception:
                pass

    name = getattr(material, "name", None) if material is not None else None
    normalized = str(name).casefold() if name else ""

    if "ply" in normalized or "sperrholz" in normalized:
        material_key = "ply"
    elif "balsa" in normalized:
        material_key = "balsa"
    else:
        material_key = "other"

    return {
        "material": material_key,
        "name": name,
        "has_stock": has_stock,
    }


# ===========================================================================
# Material existence and creation
# ===========================================================================
def _material_exists(name_substring: str) -> bool:
    """True if a loaded material's name contains the substring (casefolded)."""
    try:
        mgr = get_context().material_mgr
        needle = name_substring.casefold()
        for m in mgr.get_all_materials():
            name = str(getattr(m, "name", "") or "").casefold()
            if needle in name:
                return True
    except Exception as e:
        print(f"[layer_rules] Could not check for {name_substring!r}: {e}")
    return False


_BIRCH_HINTS = ("birch", "birke", "bouleau", "abedul", "betula")
_WOOD_HINTS = ("wood", "holz", "bois", "madera")

BALSA_UID = "balsa"
BALSA_NAME = "Balsa"
BALSA_DESCRIPTION = "Lightweight balsa wood."
USER_LIBRARY_NAME = "Layer Rules"
USER_LIBRARY_DIRNAME = "layer_rules"  # only used by the manual fallback


def _text_of(value) -> str:
    """Casefolded text of a str / LocalizedField (substring checks only)."""
    return str(value or "").casefold()


def _is_wood_material(material) -> bool:
    category = _text_of(getattr(material, "category", ""))
    return any(hint in category for hint in _WOOD_HINTS)


def _pick_template_material():
    """Return an existing material whose YAML will be cloned for Balsa.

    Only materials with an existing YAML file on disk qualify. Preference:
      1. wood + birch-ish name + texture file
      2. wood + texture file
      3. any wood material
      4. any material
    """
    try:
        materials = list(get_context().material_mgr.get_all_materials())
    except Exception as e:
        print(f"[layer_rules] Could not enumerate materials: {e}")
        return None

    def _has_file(m):
        fp = getattr(m, "file_path", None)
        return fp is not None and Path(fp).is_file()

    def _has_texture(m):
        try:
            tp = m.get_texture_path()
        except Exception:
            return False
        return tp is not None and Path(tp).is_file()

    def _is_birch(m):
        name = _text_of(getattr(m, "name", ""))
        return any(hint in name for hint in _BIRCH_HINTS)

    usable = [m for m in materials if _has_file(m)]
    tiers = (
        lambda m: _is_wood_material(m) and _has_texture(m) and _is_birch(m),
        lambda m: _is_wood_material(m) and _has_texture(m),
        _is_wood_material,
        lambda m: True,
    )
    for predicate in tiers:
        for m in usable:
            if predicate(m):
                return m
    return None


def _set_text_fields(node, values: dict):
    """Replace 'name' / 'description' values anywhere in a YAML tree.

    Handles plain strings as well as per-language dicts (every language
    receives the same text). The 'appearance' subtree is left alone.
    """
    if isinstance(node, dict):
        for key in list(node.keys()):
            if key == "appearance":
                continue
            value = node[key]
            if key in values:
                if isinstance(value, dict):
                    node[key] = {lang: values[key] for lang in value}
                else:
                    node[key] = values[key]
            else:
                _set_text_fields(value, values)
    elif isinstance(node, list):
        for item in node:
            _set_text_fields(item, values)


def _library_dir(lib):
    directory = getattr(lib, "_directory", None)
    return Path(directory) if directory is not None else None


def _find_target_library(mgr):
    """Return the library that should hold the Balsa material, or None.

    Preference: any writable library; otherwise the library this addon
    registered manually (see _ensure_user_library, fallback 2).
    """
    libs = list(mgr._libraries.values())
    for lib in libs:
        if not lib.read_only and _library_dir(lib) is not None:
            return lib
    try:
        manual_dir = (Path(mgr.user_dir) / USER_LIBRARY_DIRNAME).resolve()
    except Exception:
        return None
    for lib in libs:
        d = _library_dir(lib)
        if d is not None and d.resolve() == manual_dir:
            return lib
    return None


def _ensure_user_library(mgr):
    """Return a library that can hold the Balsa material (creating one)."""
    lib = _find_target_library(mgr)
    if lib is not None:
        return lib

    # 1) The manager's own factory.
    try:
        mgr.create_user_library(USER_LIBRARY_NAME)
    except Exception:
        print("[layer_rules] create_user_library() failed:\n"
              + traceback.format_exc())
    lib = _find_target_library(mgr)
    if lib is not None:
        return lib

    # 2) Manual: create the library folder and register it.
    try:
        from rayforge.core.material_library import MaterialLibrary
        target = Path(mgr.user_dir) / USER_LIBRARY_DIRNAME
        if not target.exists():
            if MaterialLibrary.create(target, USER_LIBRARY_NAME) is None:
                raise RuntimeError("MaterialLibrary.create() returned None")
        mgr.add_library_from_path(target)
    except Exception:
        print("[layer_rules] Manual library creation failed:\n"
              + traceback.format_exc())
        return None

    lib = _find_target_library(mgr)
    if lib is None:
        print("[layer_rules] Library was created but is not registered.")
    return lib


def _write_balsa_files(template, target_dir: Path) -> Path:
    """Write balsa.yaml (cloned from template) + texture into target_dir.

    Returns the path of the written YAML file.
    """
    src_yaml = Path(template.file_path)
    with open(src_yaml, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Template YAML {src_yaml} is not a mapping")

    for key in ("uid", "id"):
        if key in data:
            data[key] = BALSA_UID
    _set_text_fields(data, {"name": BALSA_NAME,
                            "description": BALSA_DESCRIPTION})
    if not _is_wood_material(template):
        data["category"] = "Wood"

    target_dir.mkdir(parents=True, exist_ok=True)

    # Copy the texture file, keeping its position relative to the YAML.
    try:
        tex = template.get_texture_path()
    except Exception:
        tex = None
    if tex is not None and Path(tex).is_file():
        tex = Path(tex)
        try:
            rel = tex.relative_to(src_yaml.parent)
        except ValueError:
            rel = Path(tex.name)
        dest = target_dir / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tex, dest)

    out_path = target_dir / f"{BALSA_UID}.yaml"
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return out_path


def _create_balsa_material() -> bool:
    """Create a 'Balsa' material in a writable library.

    Returns True on success. Safe to call repeatedly: returns True
    immediately if a material containing 'balsa' already exists.
    """
    if _material_exists("balsa"):
        return True

    try:
        mgr = get_context().material_mgr
    except Exception as e:
        print(f"[layer_rules] material_mgr unavailable: {e}")
        return False

    template = _pick_template_material()
    if template is None:
        print("[layer_rules] No template material available for Balsa.")
        return False

    lib = _ensure_user_library(mgr)
    if lib is None:
        return False
    print(f"[layer_rules] Creating 'Balsa' (template: "
          f"{str(getattr(template, 'name', ''))!r}, "
          f"library: {lib.display_name!r}).")

    yaml_path = None
    try:
        yaml_path = _write_balsa_files(template, _library_dir(lib))
        lib.reload()
        if not _material_exists("balsa"):
            # The manager may cache; ask it to re-read once.
            mgr.reload_libraries()
    except Exception:
        print("[layer_rules] Creating Balsa failed:\n"
              + traceback.format_exc())

    if _material_exists("balsa"):
        try:
            mgr.libraries_changed.send(mgr)  # refresh the Materials UI
        except Exception as e:
            print(f"[layer_rules] libraries_changed signal failed: {e}")
        print("[layer_rules] 'Balsa' material created.")
        return True

    # Roll back so a file Rayforge cannot load never stays in the library.
    if yaml_path is not None:
        try:
            yaml_path.unlink()
            lib.reload()
            print(f"[layer_rules] Removed {yaml_path} again.")
        except Exception as e:
            print(f"[layer_rules] Could not remove {yaml_path}: {e}")
    print("[layer_rules] Balsa material could not be created.")
    return False


# ===========================================================================
# Ensure default stock
# ===========================================================================
def _ensure_default_stock(material_type: str,
                          thickness_mm: float = None) -> bool:
    """Create the add-on's default sheet stock if appropriate.

    Rules:
      - If the document already has Balsa or Ply stock, leave it alone.
      - If the document has any other stock, a new stock is created
        alongside it.
      - If the document has no stock, a new stock is created.

    Material lookup prefers an exact 'Balsa' (creating it if needed) or
    'Sperrholz'; existing stock is never modified.

    thickness_mm: thickness of the new stock. If None (or <= 0), Rayforge's
    default_stock_thickness_mm from the config is used.
    """
    if _main_window is None:
        return False

    try:
        de = _main_window.doc_editor
        doc = de.doc
    except Exception as exc:
        print(f"[layer_rules] Could not access document for stock creation: "
              f"{exc}")
        return False

    # Inspect existing stock and decide whether to skip creation.
    try:
        existing_stock_items = list(getattr(doc, "stock_items", []) or [])
    except Exception as exc:
        print(f"[layer_rules] Could not inspect document stock items: {exc}")
        return False

    # If a stock is already Balsa or Ply, don't duplicate.
    if existing_stock_items:
        info = _detect_stock_info()
        if info["material"] in ("balsa", "ply"):
            print(f"[layer_rules] Document already has {info['material']} "
                  "stock; leaving it unchanged.")
            return False
        print("[layer_rules] Existing stock is neither Balsa nor Ply; "
              "creating a new stock alongside it.")

    # ----- Material resolution -----
    if material_type == "ply":
        material_names = ["Sperrholz", "Ply"]
        width_mm, height_mm = 400.0, 600.0
        # Balsa material creation is not needed for Ply.
    else:
        # Ensure a Balsa material exists before trying to resolve it.
        _create_balsa_material()
        material_names = ["Balsa"]
        width_mm, height_mm = 100.0, 1000.0

    material = None
    try:
        ctx = get_context()
        material_mgr = ctx.material_mgr
        materials = material_mgr.get_all_materials()
    except Exception as exc:
        print(f"[layer_rules] Could not look up materials: {exc}")
        return False

    for needle in material_names:
        # Exact match first, then case-insensitive substring.
        material = next(
            (m for m in materials
             if str(getattr(m, "name", "") or "") == needle),
            None,
        )
        if material is None:
            folded = needle.casefold()
            material = next(
                (m for m in materials
                 if folded in str(getattr(m, "name", "") or "").casefold()),
                None,
            )
        if material is not None:
            break

    if material is None:
        if material_type == "balsa":
            print("[layer_rules] Stock not created: no Balsa material could "
                  "be found or created.")
        else:
            print("[layer_rules] Stock not created: no Ply material "
                  f"(tried {material_names}) was found in the libraries.")
        return False

    # ----- Stock creation -----
    try:
        from raygeo.geo import Geometry, Matrix
        from rayforge.core.stock import StockItem
        from rayforge.core.stock_asset import StockAsset
        from rayforge.core.undo import Command

        geometry = Geometry()
        geometry.move_to(0, 0)
        geometry.line_to(width_mm, 0)
        geometry.line_to(width_mm, height_mm)
        geometry.line_to(0, height_mm)
        geometry.close_path()

        stock_name = "Stock 1"
        stock_asset = StockAsset(name=stock_name, geometry=geometry)
        stock_asset.material_uid = material.uid
        if thickness_mm is not None and thickness_mm > 0:
            stock_asset.thickness = float(thickness_mm)
        else:
            stock_asset.thickness = float(
                getattr(ctx.config, "default_stock_thickness_mm", 2.0) or 2.0
            )

        stock_item = StockItem(
            stock_asset_uid=stock_asset.uid,
            name=stock_name,
        )
        natural_width, natural_height = stock_asset.get_natural_size()
        stock_item.matrix = Matrix.scale(natural_width, natural_height)

        position = (0.0, 0.0)
        machine = getattr(ctx.config, "machine", None)
        panel = getattr(machine, "panel", None) if machine is not None else None
        reference = getattr(panel, "reference_position_world", None)
        position_from_origin = getattr(panel, "world_position_from_origin", None)
        if reference is not None and callable(position_from_origin):
            position = position_from_origin(
                reference[0], reference[1], (width_mm, height_mm)
            )
        stock_item.pos = position

        class _CreateDefaultStockCommand(Command):
            def __init__(self):
                super().__init__(name="Create default stock")

            def execute(self):
                self.do()

            def do(self):
                doc.add_asset(stock_asset, silent=True)
                doc.add_child(stock_item)

            def undo(self):
                doc.remove_child(stock_item)
                doc.remove_asset_by_uid(stock_asset.uid)

        doc.history_manager.execute(_CreateDefaultStockCommand())

        if not any(getattr(item, "uid", None) == stock_item.uid
                   for item in doc.stock_items):
            raise RuntimeError("Rayforge did not register the new StockItem")

        print(
            f"[layer_rules] Created {material.name} stock "
            f"({width_mm:g} x {height_mm:g} mm)."
        )
        return True

    except Exception as exc:
        print(
            f"[layer_rules] Failed to create {material.name} stock: "
            f"{type(exc).__name__}: {exc}"
        )
        return False


# ===========================================================================
# Rule calculation
# ===========================================================================
def _calculate_rules(material_type: str, is_hard: bool, thickness_mm: float,
                     warn: bool = False) -> dict:
    """Compute power / speed / passes per layer.

    warn: print a console warning if the thickness exceeds the supported
    range. The live preview calls this on every input change and passes
    warn=False to avoid flooding the console.
    """
    height_tenths = thickness_mm * 10.0
    hardness_factor = 1.5 if is_hard else 1.0
    h_eff = height_tenths * hardness_factor

    # The quadratic formula is only valid up to 5.6 mm (effective); beyond
    # that the speed is held constant at the 5.6 mm value.
    h_limit = MAX_SUPPORTED_THICKNESS_MM * 10.0
    if h_eff > h_limit + 1e-6:
        if warn:
            if thickness_mm > MAX_SUPPORTED_THICKNESS_MM + 1e-6:
                print(f"[layer_rules] WARNING: Material thicknesses above "
                      f"{MAX_SUPPORTED_THICKNESS_MM:g} mm are not supported "
                      f"by this addon (selected: {thickness_mm:g} mm). "
                      f"Cut speed is held at the "
                      f"{MAX_SUPPORTED_THICKNESS_MM:g} mm value.")
            else:
                print(f"[layer_rules] WARNING: Hard material: effective "
                      f"thickness {h_eff / 10.0:g} mm ({thickness_mm:g} mm "
                      f"x {hardness_factor:g}) exceeds "
                      f"{MAX_SUPPORTED_THICKNESS_MM:g} mm, which is not "
                      f"supported by this addon. Cut speed is held at the "
                      f"{MAX_SUPPORTED_THICKNESS_MM:g} mm value.")
        h_eff = h_limit

    cut_speed = int(0.3726 * h_eff ** 2 - 42 * h_eff + 1465)
    cut_speed = max(100, min(MAX_CUT_SPEED, cut_speed))

    if material_type == "ply":
        names_power = 0.50
        cut_power = 1.00
    else:
        names_power = 0.20
        cut_power = 0.80

    if is_hard:
        names_power = min(1.0, names_power + 0.05)
        cut_power = min(1.0, cut_power + 0.05)

    cut_passes = 2 if material_type == "balsa" else 3

    return {
        "Names":   {"power": names_power, "cut_speed": 1000, "passes": 1},
        "Inside":  {"power": cut_power, "cut_speed": cut_speed,
                    "cut_side": "INSIDE", "passes": cut_passes,
                    "power_mode": "CONSTANT"},
        "Outside": {"power": cut_power, "cut_speed": cut_speed,
                    "cut_side": "OUTSIDE", "passes": cut_passes,
                    "power_mode": "CONSTANT"},
    }


def _compute_preview(material_type: str, is_hard: bool,
                     thickness_mm: float) -> list:
    rules = _calculate_rules(material_type, is_hard, thickness_mm)
    names = rules["Names"]
    cut = rules["Inside"]
    return [
        ("Engraving / Names",
         str(int(round(names["power"] * 100))),
         str(names["cut_speed"])),
        ("Cut (Inside/Outside)",
         str(int(round(cut["power"] * 100))),
         str(cut["cut_speed"])),
    ]


def _filter_rules(rules: dict, flags: dict) -> dict:
    out = {}
    for layer_name, rule in rules.items():
        r = {}
        if flags.get("set_power_speed", True):
            r["power"] = rule["power"]
            r["cut_speed"] = rule["cut_speed"]
        if flags.get("set_cut_side", True) and "cut_side" in rule:
            r["cut_side"] = rule["cut_side"]
        if flags.get("set_passes", True):
            r["passes"] = rule["passes"]
        if flags.get("set_power_mode", True) and "power_mode" in rule:
            r["power_mode"] = rule["power_mode"]
        out[layer_name] = r
    return out


# ===========================================================================
# Power mode resolution
# ===========================================================================
def _resolve_power_mode(step, mode_name: str):
    current = getattr(step, "power_mode", None)
    if current is None:
        return None
    enum_cls = type(current)
    for candidate in (mode_name, mode_name.upper(), mode_name.lower(),
                      "CONSTANT", "CW", "FIXED", "STATIC"):
        if hasattr(enum_cls, candidate):
            return getattr(enum_cls, candidate)
    print(f"[layer_rules]   WARNING: could not resolve PowerMode.{mode_name}. "
          f"Available: {[n for n in dir(enum_cls) if not n.startswith('_')]}")
    return None


# ===========================================================================
# Multi-pass via MultiPassTransformer
# ===========================================================================
def _set_passes(step, de, passes_value: int) -> bool:
    transformers = getattr(step, "per_step_transformers_dicts", None)
    if not isinstance(transformers, list):
        return False

    changed = False
    found = False
    for t in transformers:
        if not isinstance(t, dict):
            continue
        if t.get("name") != "MultiPassTransformer":
            continue
        found = True
        if t.get("passes") != passes_value:
            t["passes"] = passes_value
            changed = True
        if not t.get("enabled", True):
            t["enabled"] = True
            changed = True
        if t.get("z_step_down", 0.0) != 0.0:
            t["z_step_down"] = 0.0
            changed = True

    if not found or not changed:
        return False

    target_dict = getattr(step, "settings", None) or step.__dict__
    try:
        de.step.set_step_param(
            target_dict, "per_step_transformers_dicts", transformers,
            f"Set passes on {step.name}",
        )
        return True
    except Exception:
        pass
    try:
        step.per_step_transformers_dicts = list(transformers)
        return True
    except Exception:
        return False


# ===========================================================================
# Tabs layer step removal
# ===========================================================================
def _notify_signals(layer, doc):
    try:
        layer.updated.send(layer)
    except Exception:
        pass
    try:
        doc.descendant_updated.send(doc, origin=layer, parent_of_origin=doc)
    except Exception:
        pass


def _remove_tabs_layer_steps(de, doc):
    tabs_layer = None
    for layer in doc.layers:
        if layer.name == "Tabs":
            tabs_layer = layer
            break

    if tabs_layer is None:
        return

    wf = tabs_layer.workflow

    def _count():
        try:
            return len(list(wf))
        except Exception:
            return -1

    before = _count()
    if before <= 0:
        return

    print(f"[layer_rules] Tabs layer has {before} step(s) to remove.")
    steps = list(wf)

    def _try(label, fn):
        try:
            fn()
        except Exception as e:
            print(f"[layer_rules] {label} raised: {e}")
            return False
        after = _count()
        if after == 0:
            print(f"[layer_rules] Removed via {label} ({before} -> 0).")
            _notify_signals(tabs_layer, doc)
            return True
        return False

    if hasattr(wf, "remove_child"):
        if _try("workflow.remove_child()",
                lambda: [wf.remove_child(s) for s in steps]):
            return

    if hasattr(wf, "remove"):
        if _try("workflow.remove()",
                lambda: [wf.remove(s) for s in list(wf)]):
            return

    if hasattr(wf, "clear"):
        if _try("workflow.clear()", wf.clear):
            return

    print("[layer_rules] Neutralizing Tabs step(s) with power=0.")
    for s in steps:
        try:
            s.power = 0.0
        except Exception:
            pass
    _notify_signals(tabs_layer, doc)


# ===========================================================================
# Layer reorder
# ===========================================================================
def _reorder_layers(de, doc):
    try:
        current = list(doc.children)
    except Exception as e:
        print(f"[layer_rules] Could not read doc.children: {e}")
        return

    by_name = {}
    for layer in current:
        name = getattr(layer, "name", None)
        if name is not None:
            by_name[name] = layer

    desired = [by_name[n] for n in LAYER_ORDER if n in by_name]
    desired += [l for l in current if l not in desired]

    if all(desired[i] is current[i] for i in range(len(current))):
        print("[layer_rules] Layer order already correct.")
        return

    print(f"[layer_rules] Reordering layers: "
          f"{[getattr(l, 'name', '?') for l in current]} -> "
          f"{[getattr(l, 'name', '?') for l in desired]}")

    active_layer = None
    try:
        idx = getattr(doc, "_active_layer_index", None)
        if idx is not None and 0 <= idx < len(current):
            active_layer = current[idx]
    except Exception:
        pass

    try:
        doc.children[:] = desired
    except Exception as e:
        print(f"[layer_rules] doc.children[:] = failed: {e}")
        return

    if active_layer is not None:
        new_idx = next(
            (i for i, l in enumerate(desired) if l is active_layer), None
        )
        if new_idx is not None:
            try:
                doc._active_layer_index = new_idx
            except Exception:
                pass

    try:
        doc.descendant_updated.send(
            doc, origin=desired[0], parent_of_origin=doc,
        )
    except Exception:
        pass

    try:
        moved_inside = by_name.get("Inside")
        if moved_inside is not None:
            doc.descendant_removed.send(
                doc, origin=moved_inside, parent_of_origin=doc,
            )
            doc.descendant_added.send(
                doc, origin=moved_inside, parent_of_origin=doc,
            )
    except Exception:
        pass

    try:
        sig = getattr(doc, "job_assembly_invalidated", None)
        if sig is not None:
            sig.send(doc)
    except Exception:
        pass

    print(f"[layer_rules] Final layer order: "
          f"{[getattr(l, 'name', '?') for l in doc.children]}")


# ===========================================================================
# Per-step rule application
# ===========================================================================
def _apply_rule_to_step(de, step, rule, layer_wp_uids):
    changed = False
    changes = []

    if not step.workpiece_uids and layer_wp_uids:
        step.workpiece_uids = layer_wp_uids
        changes.append("workpiece_uids set")
        changed = True

    target_dict = getattr(step, "settings", None) or step.__dict__

    if "power_mode" in rule:
        desired_mode = _resolve_power_mode(step, rule["power_mode"])
        if desired_mode is not None and getattr(step, "power_mode", None) != desired_mode:
            try:
                de.step.set_step_param(
                    target_dict, "power_mode", desired_mode,
                    f"Set power_mode on {step.name}",
                )
                changes.append(f"power_mode -> {rule['power_mode']}")
                changed = True
            except Exception:
                try:
                    step.power_mode = desired_mode
                    changes.append(f"power_mode -> {rule['power_mode']} (direct)")
                    changed = True
                except Exception:
                    pass

    params = {}
    for key in ("power", "cut_speed", "cut_side"):
        if key in rule:
            params[key] = rule[key]

    for key, new_val in params.items():
        current = getattr(step, key, None)
        if isinstance(new_val, float) and isinstance(current, (int, float)):
            if abs(float(current) - new_val) < 1e-9:
                continue
        elif current == new_val:
            continue

        try:
            de.step.set_step_param(
                target_dict, key, new_val,
                f"Set {key} on {step.name}",
            )
            changes.append(f"{key} -> {new_val}")
        except Exception:
            try:
                setattr(step, key, new_val)
                changes.append(f"{key} -> {new_val} (direct)")
            except Exception:
                continue
        changed = True

    if "passes" in rule:
        if _set_passes(step, de, rule["passes"]):
            changes.append(f"passes -> {rule['passes']}")
            changed = True

    if changed:
        print(f"[layer_rules]   {step.name}: {', '.join(changes)}")
        try:
            step.updated.send(step)
        except Exception:
            pass
    else:
        print(f"[layer_rules]   {step.name}: unchanged")

    return changed


def _apply_rules(rules: dict, flags: dict):
    if _main_window is None:
        return

    de = _main_window.doc_editor
    doc = de.doc

    print(f"[layer_rules] Flags: {flags}")

    if flags.get("remove_tabs", True):
        _remove_tabs_layer_steps(de, doc)
    else:
        print("[layer_rules] Tabs layer removal disabled.")

    filtered = _filter_rules(rules, flags)

    total = 0
    for layer in doc.layers:
        rule = filtered.get(layer.name)
        if rule is None:
            continue
        layer_wp_uids = [wp.uid for wp in layer.workpieces]
        for step in layer.workflow:
            if _apply_rule_to_step(de, step, rule, layer_wp_uids):
                total += 1

    _reorder_layers(de, doc)

    print(f"[layer_rules] Applied rules; {total} step(s) modified.")


# ===========================================================================
# Action callbacks — Process Layers
# ===========================================================================
def on_process_layers_activated(action, parameter):
    if _main_window is None:
        return

    info = _detect_stock_info()
    print(f"[layer_rules] Stock info: {info}")

    default_material = info["material"] if info["material"] in ("balsa", "ply") else "balsa"
    print(f"[layer_rules] Default material for dialog: {default_material}")

    dialog = MaterialDialog(
        _main_window,
        preview_provider=_compute_preview,
        default_material=default_material,
    )

    def _on_resp(dlg, rid):
        if rid in (Gtk.ResponseType.OK, Gtk.ResponseType.APPLY):
            rules = _calculate_rules(
                dlg.get_material_type(),
                dlg.get_is_hard(),
                dlg.get_thickness_mm(),
                warn=True,
            )
            flags = dlg.get_flags()
            _ensure_default_stock(dlg.get_material_type(),
                                  thickness_mm=dlg.get_thickness_mm())
            print(f"[layer_rules] Dialog input → rules: {rules}")
            _apply_rules(rules, flags)
        dlg.destroy()

    dialog.connect("response", _on_resp)
    dialog.present()


# ===========================================================================
# Register actions
# ===========================================================================
@hookimpl
def register_actions(action_registry):
    rules_action = Gio.SimpleAction.new("process-layers", None)
    rules_action.connect("activate", on_process_layers_activated)
    action_registry.register(
        action_name="process-layers",
        action=rules_action,
        addon_name="layer_rules",
        label="Process Layers (layer_rules)",
        icon_name="document-send-symbolic",
        shortcut="<Ctrl><Alt>p",
        menu=MenuPlacement(menu_id="tools", priority=50),
    )

