# -*- coding: utf-8 -*-
"""WWNet ArcGIS Pro Python toolbox (Pro 3.3+). Thin wrappers: logic is in wwnet_arcgis/."""
import os
import sys

import arcpy

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from wwnet_arcgis import vendor as _vendor  # noqa: E402
_vendor.activate(_HERE)  # no-install bundle: <toolbox>/vendor first on sys.path (before wwnet_native / anthropic)

from wwnet_arcgis import updater as _updater  # noqa: E402
from wwnet_arcgis import settings as _settings  # noqa: E402
from wwnet_arcgis import tools_impl as impl  # noqa: E402
from wwnet_arcgis import pumpdata as _pumpdata  # noqa: E402  (stdlib only; used by getParameterInfo / updateParameters)

try:  # one model list for both front ends (the shared assistant client)
    from wwnet_arcgis import shared as _shared  # noqa: E402
    _client = _shared.assistant("client")
    MODELS = [m for m, _ in _client.MODELS]
    EFFORTS = list(_client.EFFORTS)
except Exception:  # noqa: BLE001 - the toolbox must still open
    MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
    EFFORTS = ["low", "medium", "high"]
MULTILINE = "{E5456E51-0C41-4797-9EE4-5269820C6F0E}"


def _impl():
    """Fresh ``wwnet_arcgis.tools_impl`` for this tool run (drops cached WWNet modules first), so files
    updated by *Update WWNet* take effect without restarting ArcGIS Pro."""
    return _updater.fresh_impl()


def _text(p):
    return p.valueAsText if p.value is not None else None


def _p(name, label, datatype, direction="Input", required=True, multi=False, default=None,
       values=None, filetype=None, category=None):
    p = arcpy.Parameter(displayName=label, name=name, datatype=datatype,
                        parameterType="Required" if required else "Optional",
                        direction=direction, multiValue=multi)
    if default is not None:
        p.value = default
    if values is not None:
        p.filter.type = "ValueList"
        p.filter.list = list(values)
    if filetype:
        p.filter.list = list(filetype)
    if category:
        p.category = category
    return p


def _model_param(label="WWNet model (GeoPackage)"):
    """The model: browse to a .gpkg, or leave the model shown in the active map (filled in on open)."""
    return _p("gpkg", label, "DEFile", filetype=["gpkg"])


def _model_out():
    """Derived output = the model, so tools chain in ModelBuilder (Import > Check > Design > Simulate)."""
    return _p("out_gpkg", "Model (output)", "DEFile", "Output", required=False)


class _Params(object):
    """Parameters by name."""

    def __init__(self, parameters):
        self.by = {p.name: p for p in parameters}

    def __getitem__(self, name):
        return self.by[name]

    def __contains__(self, name):
        return name in self.by

    def text(self, name, default=None):
        p = self.by.get(name)
        return p.valueAsText if p is not None and p.value is not None else default

    def val(self, name, default=None):
        p = self.by.get(name)
        return p.value if p is not None and p.value is not None else default

    def flag(self, name, default=False):
        return bool(self.val(name, default))

    def num(self, name, default=None):
        v = self.val(name, default)
        return None if v is None else float(v)

    def fresh(self, name):
        """True when the parameter has a value the user has not validated yet (just changed)."""
        p = self.by.get(name)
        return p is not None and p.value is not None and not p.hasBeenValidated


def _source(param):
    """Layer object when the value is a map layer, else its text (path / layer name); None if empty."""
    if param.value is None:
        return None
    return param.value if hasattr(param.value, "dataSource") else param.valueAsText


class _Tool(object):
    """Base: label / description / category from class attributes; the vendor folder is re-activated
    before every run (Pro restores sys.path); the model parameter defaults to the model in the map."""
    label = ""
    description = ""
    category = ""

    def __init__(self):
        self.label, self.description, self.category = type(self).label, type(self).description, type(self).category
        self.canRunInBackground = False

    def getParameterInfo(self):
        return []

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        p = _Params(parameters)
        if "gpkg" in p and p["gpkg"].direction == "Input" and p["gpkg"].value is None \
                and not getattr(p["gpkg"], "altered", False):
            try:
                from wwnet_arcgis import maplayers
                found = maplayers.default_model(arcpy)
                if found:
                    p["gpkg"].value = found
            except Exception:  # noqa: BLE001 - validation must never fail
                pass
        try:
            self.update(p)
        except Exception:  # noqa: BLE001
            pass

    def update(self, p):
        pass

    def updateMessages(self, parameters):
        return

    def execute(self, parameters, messages):
        _vendor.activate(_HERE)  # Pro restores sys.path before each tool run
        global impl
        impl = _impl()           # fresh modules: files changed by Update WWNet apply without a restart
        p = _Params(parameters)
        out = self.run(p, impl.Messages(arcpy))
        if "out_gpkg" in p and out:
            p["out_gpkg"].value = out

    def run(self, p, msgs):
        raise NotImplementedError


def _runs_list(p, *names):
    """Fill run value lists ('label (run_id)', newest first) when the model changes."""
    from wwnet_arcgis import gpkg
    if not p.val("gpkg") or not (p.fresh("gpkg") or any(not p[n].filter.list for n in names)):
        return []
    titles = gpkg.run_titles(p.text("gpkg"))
    for n in names:
        p[n].filter.list = titles
    return titles


# ---------------------------------------------------------------------- Model

class NewModel(_Tool):
    label = "New Model"
    description = "Create an empty WWNet GeoPackage (EPSG:7850) and add it to the map."
    category = "Model"

    def getParameterInfo(self):
        return [_p("out_gpkg", "Output GeoPackage", "DEFile", "Output", filetype=["gpkg"]),
                _p("add_to_map", "Add to active map", "GPBoolean", required=False, default=True),
                _p("overwrite", "Overwrite if it exists", "GPBoolean", required=False, default=False)]

    def run(self, p, msgs):
        return impl.new_model(arcpy, msgs, p.text("out_gpkg"), p.flag("add_to_map", True), p.flag("overwrite"))


class AddModelToMap(_Tool):
    label = "Add Model To Map"
    description = ("Add the layers of a WWNet GeoPackage to the active map (WWNet group): model layers, and the "
                   "Design / Simulation results and Issues layers once they hold data. Upgrades older models.")
    category = "Model"

    def getParameterInfo(self):
        return [_p("gpkg", "WWNet GeoPackage", "DEFile", filetype=["gpkg"]),
                _p("symbolize", "Apply symbology", "GPBoolean", required=False, default=True)]

    def updateParameters(self, parameters):   # no map default: the point is adding a model that is not there
        return

    def run(self, p, msgs):
        impl.add_model_to_map(arcpy, msgs, p.text("gpkg"), p.flag("symbolize", True))


class ModelSettings(_Tool):
    label = "Model Settings"
    description = ("Show and change the model's loading settings: ADWF per connection (L/day), peaking factor "
                   "(GSDF = ADWF x factor) and the default diurnal profile. Lists the profiles in the model.")
    category = "Model"

    def getParameterInfo(self):
        return [_model_param(),
                _p("adwf_per_connection_lpd", "ADWF per connection (L/day)", "GPDouble", required=False),
                _p("peaking_factor", "Peaking factor (GSDF = ADWF x factor)", "GPDouble", required=False),
                _p("default_profile", "Default diurnal profile", "GPString", required=False,
                   values=[impl.NONE_PROFILE]),
                _model_out()]

    def update(self, p):
        if not p.val("gpkg") or not p.fresh("gpkg"):
            return
        from wwnet_arcgis import gpkg
        path = p.text("gpkg")
        m = gpkg.meta(path)
        p["default_profile"].filter.list = [impl.NONE_PROFILE] + gpkg.profile_ids(path)
        for name, key in (("adwf_per_connection_lpd", "adwf_per_connection_lpd"),
                          ("peaking_factor", "peaking_factor")):
            if not getattr(p[name], "altered", False):
                try:
                    p[name].value = float(m.get(key))
                except (TypeError, ValueError):
                    pass
        if not getattr(p["default_profile"], "altered", False):
            p["default_profile"].value = m.get("default_profile") or impl.NONE_PROFILE

    def run(self, p, msgs):
        path = p.text("gpkg")
        impl.model_settings(msgs, path, p.num("adwf_per_connection_lpd"), p.num("peaking_factor"),
                            p.text("default_profile"))
        return path


# ---------------------------------------------------------------------- Data

class ImportSewerData(_Tool):
    label = "Import Sewer Data"
    description = ("Convert Water Corporation (WA) gravity sewer GIS layers (access chambers, connection points, "
                   "gravity sewers) into a new WWNet GeoPackage (EPSG:7850) with connection point loads, check it "
                   "and add it to the map. Import warnings and check issues go to the model's Issues layer.")
    category = "Data"
    _LAYER_NAME = {"ac": "ac_layer", "cp": "cp_layer", "sewer": "sewer_layer"}

    def getParameterInfo(self):
        from wwnet_arcgis import sewer_import as si
        ac = _p("ac_layer", "Sewer access chambers", "GPFeatureLayer")
        ac.filter.list = ["Point"]
        cp = _p("cp_layer", "Sewer connection points (optional)", "GPFeatureLayer", required=False)
        cp.filter.list = ["Point"]
        sw = _p("sewer_layer", "Gravity sewers", "GPFeatureLayer")
        sw.filter.list = ["Polyline"]
        params = [ac, cp, sw,
                  _p("out_gpkg", "Output GeoPackage", "DEFile", "Output", filetype=["gpkg"]),
                  _p("use_selected", "Use selected features only", "GPBoolean", required=False, default=False),
                  _p("add_to_map", "Add to map", "GPBoolean", required=False, default=True),
                  _p("load_cps", "Create loads from connection points", "GPBoolean", required=False, default=True,
                     category="Connection point loads"),
                  _p("cp_radius", "Connection point search distance (m)", "GPDouble", required=False, default=10.0,
                     category="Connection point loads"),
                  _p("snap", "Snap tolerance (m)", "GPDouble", required=False, default=0.5, category="Options"),
                  _p("grade_source", "Grade source", "GPString", required=False, default="levels",
                     values=["levels", "field"], category="Options"),
                  _p("tee_join", "Tee join distance (m)", "GPDouble", required=False, default=0.5,
                     category="Options")]
        for role, layer_key, label, required in si.FIELD_ROLES:
            f = _p("f_" + role, label, "Field", required=required, category="Source fields")
            f.parameterDependencies = [self._LAYER_NAME[layer_key]]  # Pro requires parameter NAMES
            params.append(f)
        return params

    def update(self, p):
        from wwnet_arcgis import sewer_import as si
        for layer_key, pname in self._LAYER_NAME.items():
            if not p.fresh(pname):
                continue
            try:
                found = impl.sewer_field_defaults(arcpy, _source(p[pname]))
            except Exception:  # noqa: BLE001
                continue
            for role, k, _l, _q in si.FIELD_ROLES:
                if k == layer_key:
                    p["f_" + role].value = found.get(role) or None

    def run(self, p, msgs):
        from wwnet_arcgis import sewer_import as si
        fields = {role: (p.text("f_" + role) or "") for role, _k, _l, _q in si.FIELD_ROLES}
        impl.import_sewer_data(arcpy, msgs, _source(p["ac_layer"]), _source(p["cp_layer"]),
                               _source(p["sewer_layer"]), p.text("out_gpkg"), fields, p.num("snap", 0.5),
                               p.text("grade_source", "levels"), p.flag("use_selected"), p.flag("add_to_map", True),
                               load_connection_points=p.flag("load_cps", True), cp_load_search_m=p.num("cp_radius"),
                               tee_tolerance=p.num("tee_join"))


class ImportDiurnalProfile(_Tool):
    label = "Import Diurnal Profile"
    description = ("Import a diurnal flow profile (time as hh:mm or minutes, factor; any interval, header optional) "
                   "from a CSV / TXT file, or from text pasted out of InfoWorks ICM (tab-separated Time / Factor). "
                   "The mean factor is checked, not silently changed.")
    category = "Data"

    def getParameterInfo(self):
        pasted = _p("pasted", "Or paste profile text (Time<TAB>Factor)", "GPString", required=False)
        pasted.controlCLSID = MULTILINE
        return [_model_param(), _p("profile_id", "Profile id", "GPString"),
                _p("csv", "Profile CSV / TXT file", "DEFile", required=False, filetype=["csv", "txt"]),
                _p("description", "Description", "GPString", required=False),
                _p("set_default", "Set as the model's default profile", "GPBoolean", required=False, default=True),
                _p("normalise", "Normalise to mean 1", "GPBoolean", required=False, default=False),
                pasted, _model_out()]

    def run(self, p, msgs):
        path = p.text("gpkg")
        impl.import_profile(msgs, path, p.text("profile_id"), p.text("csv"), p.text("description", ""),
                            p.flag("set_default", True), p.flag("normalise"), p.text("pasted"))
        return path


class AssignProfile(_Tool):
    label = "Assign Profile"
    description = ("Set the diurnal profile of the node loads of the selected nodes, the selected node loads or "
                   "sub-catchments, or all of them. '(model default)' clears it so the default profile applies.")
    category = "Data"

    def getParameterInfo(self):
        return [_model_param(),
                _p("target", "Assign to", "GPString", default=list(impl.ASSIGN_TARGETS)[0],
                   values=list(impl.ASSIGN_TARGETS)),
                _p("profile", "Profile", "GPString", values=[impl.DEFAULT_PROFILE]), _model_out()]

    def update(self, p):
        if p.val("gpkg") and (p.fresh("gpkg") or len(p["profile"].filter.list) <= 1):
            from wwnet_arcgis import gpkg
            p["profile"].filter.list = [impl.DEFAULT_PROFILE] + gpkg.profile_ids(p.text("gpkg"))

    def run(self, p, msgs):
        path = p.text("gpkg")
        impl.assign_profile(arcpy, msgs, path, p.text("target"), p.text("profile"))
        return path


class ImportPressureData(_Tool):
    label = "Import Pressure Data"
    description = ("Add Water Corporation (WA) sewer pump stations and pressure mains (rising mains) to an existing "
                   "WWNet model made by Import Sewer Data: chains the main segments, links wet wells and discharge "
                   "nodes. Pump hydraulics are not in the GIS: enter them with Set Pump Station Data.")
    category = "Data"
    _LAYER_NAME = {"ps": "ps_layer", "main": "main_layer"}

    def getParameterInfo(self):
        from wwnet_arcgis import pressure_import as pi
        ps = _p("ps_layer", "Sewer pump stations", "GPFeatureLayer")
        ps.filter.list = ["Point"]
        mn = _p("main_layer", "Sewer pressure mains", "GPFeatureLayer")
        mn.filter.list = ["Polyline"]
        params = [_model_param("WWNet model (existing GeoPackage)"), ps, mn,
                  _p("use_selected", "Use selected features only", "GPBoolean", required=False, default=True),
                  _p("add_to_map", "Add / refresh layers on map", "GPBoolean", required=False, default=True),
                  _p("search", "Search distance pump station to main end / wet well (m)", "GPDouble",
                     required=False, default=30.0, category="Options"),
                  _p("snap", "Snap tolerance between main segments (m)", "GPDouble", required=False, default=1.0,
                     category="Options"),
                  _p("discharge", "Discharge search distance main end to gravity node (m)", "GPDouble",
                     required=False, default=10.0, category="Options"),
                  _p("include_n", "Include not-in-use (N) pump stations and mains", "GPBoolean", required=False,
                     default=False, category="Options")]
        for role, layer_key, label, required in pi.FIELD_ROLES:
            f = _p("f_" + role, label, "Field", required=required, category="Source fields")
            f.parameterDependencies = [self._LAYER_NAME[layer_key]]  # Pro requires parameter NAMES
            params.append(f)
        return params + [_model_out()]

    def update(self, p):
        from wwnet_arcgis import pressure_import as pi
        for layer_key, pname in self._LAYER_NAME.items():
            if not p.fresh(pname):
                continue
            try:
                found = impl.pressure_field_defaults(arcpy, _source(p[pname]), layer_key)
            except Exception:  # noqa: BLE001
                continue
            for role, k, _l, _q in pi.FIELD_ROLES:
                if k == layer_key:
                    p["f_" + role].value = found.get(role) or None

    def run(self, p, msgs):
        from wwnet_arcgis import pressure_import as pi
        path = p.text("gpkg")
        fields = {role: (p.text("f_" + role) or "") for role, _k, _l, _q in pi.FIELD_ROLES}
        impl.import_pressure_data(arcpy, msgs, path, _source(p["ps_layer"]), _source(p["main_layer"]), fields,
                                  p.flag("use_selected", True), p.val("search"), p.val("snap"), p.val("discharge"),
                                  p.flag("include_n"), p.flag("add_to_map", True))
        return path


PS_SEP = " \u2014 "
LEVEL_MODES = {"AHD": "AHD", "Depth above wet well invert": "depth"}
CURVE_ORDERS = {"flow,head": "flow_head", "head,flow": "head_flow"}
FLOW_UNITS = ["L/s", "m3/h", "m3/s"]


def _station_label(s):
    return f"{s['ps_id']}{PS_SEP}{s['notes']}" if s.get("notes") else str(s["ps_id"])


def _station_id(text):
    return None if not text else str(text).split(PS_SEP)[0].strip()


def _curve_text(curve):
    return "\n".join(f"{q:g}\t{h:g}" for q, h in curve) if curve else None


class SetPumpStationData(_Tool):
    label = "Set Pump Station Data"
    description = ("Enter the hydraulic data of a pump station from its data sheet (wet well size, stop / start / "
                   "overflow levels, pumps and pump curve), which the GIS does not hold. Choose a station created "
                   "by Import Pressure Data; its current values are filled in. The pump curve can be pasted from "
                   "the data sheet, Excel or InfoWorks ICM.")
    category = "Data"

    def getParameterInfo(self):
        curve = _p("pump_curve", "Pump curve (paste: flow and head, one point per line)", "GPString", required=False)
        curve.controlCLSID = MULTILINE
        return [_model_param("WWNet model (existing GeoPackage)"),
                _p("station", "Pump station", "GPString", values=[]),
                _p("diameter_m", "Wet well diameter (m)", "GPDouble", required=False),
                _p("area_m2", "Wet well area (m2)", "GPDouble", required=False),
                _p("level_mode", "Levels are", "GPString", required=False, default="AHD", values=list(LEVEL_MODES)),
                _p("stop_level", "Stop level", "GPDouble", required=False),
                _p("start_level", "Start level", "GPDouble", required=False),
                _p("overflow_level", "Overflow level", "GPDouble", required=False),
                _p("emergency_m3", "Emergency storage (m3)", "GPDouble", required=False),
                _p("n_duty", "Duty pumps", "GPLong", required=False, default=1),
                _p("n_assist", "Assist pumps", "GPLong", required=False, default=0),
                _p("n_standby", "Standby pumps", "GPLong", required=False, default=0),
                _p("duty_rate", "Duty pump rate (L/s, used when no curve)", "GPDouble", required=False),
                curve,
                _p("curve_order", "Curve column order", "GPString", required=False, default="flow,head",
                   values=list(CURVE_ORDERS)),
                _p("flow_units", "Curve flow units", "GPString", required=False, default="L/s", values=FLOW_UNITS),
                _p("speed_pct", "Pump speed (%)", "GPDouble", required=False, default=100.0),
                _p("source_note", "Source note (e.g. Data sheet PS4 rev C)", "GPString", required=False),
                _p("replace_pumps", "Replace the station's existing pumps", "GPBoolean", required=False, default=True),
                _model_out()]

    def update(self, p):
        stations = []
        if p.val("gpkg") is not None:
            try:
                stations = _pumpdata.list_stations(p.text("gpkg"))
            except Exception:  # noqa: BLE001
                stations = []
        station = p["station"]
        if p.fresh("gpkg") or (p.val("gpkg") and not station.filter.list):
            station.filter.list = [_station_label(s) for s in stations]
            if station.value is not None and _station_id(station.valueAsText) not in [s["ps_id"] for s in stations]:
                station.value = None
        if station.value is None or station.hasBeenValidated:
            return
        sel = next((s for s in stations if s["ps_id"] == _station_id(station.valueAsText)), None)
        if sel is None:
            return
        p["diameter_m"].value = None                       # area is stored; the diameter is derived
        p["area_m2"].value = sel["wet_well_area_m2"]
        p["level_mode"].value = "AHD"                      # levels below are always AHD
        p["stop_level"].value = sel["stop_level"]
        p["start_level"].value = sel["start_level"]
        p["overflow_level"].value = sel["overflow_level"]
        p["emergency_m3"].value = sel["emergency_storage_m3"]
        counts = sel["pumps"]
        if sum(counts.values()):
            p["n_duty"].value, p["n_assist"].value, p["n_standby"].value = (
                counts.get("duty", 0), counts.get("assist", 0), counts.get("standby", 0))
        else:
            p["n_duty"].value, p["n_assist"].value, p["n_standby"].value = 1, 0, 0
        p["duty_rate"].value = sel["pump_rate_lps"]
        p["pump_curve"].value = _curve_text(sel["curve"])
        p["curve_order"].value = "flow,head"               # the curve is shown as L/s and m
        p["flow_units"].value = "L/s"

    def updateMessages(self, parameters):
        p = _Params(parameters)
        curve = p["pump_curve"]
        if curve.value is None or not str(curve.valueAsText).strip():
            return
        try:
            _pts, warns = _pumpdata.parse_curve(
                curve.valueAsText, order=CURVE_ORDERS.get(p.text("curve_order", "flow,head"), "flow_head"),
                flow_units=p.text("flow_units", "L/s"))
        except ValueError as exc:
            curve.setErrorMessage(str(exc))
            return
        if warns:
            curve.setWarningMessage(" ".join(warns))

    def run(self, p, msgs):
        path = p.text("gpkg")
        impl.set_pump_station_data(
            arcpy, msgs, path, _station_id(p.text("station")),
            wet_well_diameter_m=p.val("diameter_m"), wet_well_area_m2=p.val("area_m2"),
            level_mode=LEVEL_MODES.get(p.text("level_mode", "AHD"), "AHD"),
            stop=p.val("stop_level"), start=p.val("start_level"), overflow=p.val("overflow_level"),
            emergency_storage_m3=p.val("emergency_m3"),
            n_duty=int(p.val("n_duty", 1)), n_assist=int(p.val("n_assist", 0)), n_standby=int(p.val("n_standby", 0)),
            duty_rate_lps=p.val("duty_rate"), curve_text=p.text("pump_curve"),
            curve_order=CURVE_ORDERS.get(p.text("curve_order", "flow,head"), "flow_head"),
            flow_units=p.text("flow_units", "L/s"), speed_pct=float(p.val("speed_pct", 100.0)),
            source_note=p.text("source_note"), replace_pumps=p.flag("replace_pumps", True))
        return path


class JoinDanglingEnds(_Tool):
    label = "Join Dangling Sewer Ends"
    description = ("Repair dangling downstream ends (junction / inspection shaft / inspection opening nodes that "
                   "receive a sewer but have no outgoing sewer, which InfoWorks ICM and SWMM treat wrongly): each "
                   "is joined to the nearest sewer within the maximum distance, splitting that sewer at a new tee "
                   "node (inverts by grade, surface level interpolated). Preview first, then untick 'Preview only'. "
                   "Edits the GeoPackage: close attribute tables and remove its layers from the map first.")
    category = "Model"

    def getParameterInfo(self):
        return [_model_param("WWNet model (existing GeoPackage)"),
                _p("max_distance", "Maximum join distance (m)", "GPDouble", required=False, default=5.0),
                _p("preview", "Preview only (do not change the model)", "GPBoolean", required=False, default=True),
                _model_out()]

    def run(self, p, msgs):
        path = p.text("gpkg")
        impl.join_dangling_ends(arcpy, msgs, path, p.num("max_distance", 5.0), p.flag("preview", True))
        return path


# ---------------------------------------------------------------------- Analysis

class CheckModel(_Tool):
    label = "Check Model"
    description = ("Check the model's topology and data (inverts, grades, connectivity, loads). Issues are listed "
                   "and shown in the model's Issues layer (click a point to see it in the attribute table).")
    category = "Analysis"

    def getParameterInfo(self):
        return [_model_param(),
                _p("show_layer", "Show the Issues layer on the map", "GPBoolean", required=False, default=True),
                _model_out()]

    def run(self, p, msgs):
        path = p.text("gpkg")
        impl.check_model(arcpy, msgs, path, p.flag("show_layer", True))
        return path


class RunDesign(_Tool):
    label = "Run Design"
    description = ("Steady DS 50 design: GSDF accumulation, Colebrook-White capacity and the DS 50 checks. "
                   "Results go to the model's Design results layer (kept in the GeoPackage).")
    category = "Analysis"

    def getParameterInfo(self):
        return [_model_param(),
                _p("show_layer", "Show the Design results layer", "GPBoolean", required=False, default=True),
                _p("select_failed", "Select failed conduits", "GPBoolean", required=False, default=True),
                _model_out()]

    def run(self, p, msgs):
        path = p.text("gpkg")
        impl.run_design(arcpy, msgs, path, p.flag("show_layer", True), p.flag("select_failed", True))
        return path


class RunSimulation(_Tool):
    label = "Run Simulation"
    description = ("Run the embedded EPA SWMM dynamic-wave engine: dry weather flow from node loads and "
                   "sub-catchments with their diurnal profiles. The run is stored with its label; the Simulation "
                   "results layers show peak d/D (green < 0.5, yellow < 0.75, orange < 1.0, red surcharged).")
    category = "Analysis"

    def getParameterInfo(self):
        return [_model_param(),
                _p("label", "Run label (e.g. existing x1.0)", "GPString", required=False),
                _p("duration_h", "Duration (h)", "GPDouble", required=False, default=48.0),
                _p("warmup_h", "Warm-up (h)", "GPDouble", required=False, default=24.0),
                _p("flow_scale", "Flow scale", "GPDouble", required=False, default=1.0),
                _p("series_selected", "Store time series for the selected conduits", "GPBoolean", required=False,
                   default=True),
                _p("show_layer", "Show the Simulation results layers", "GPBoolean", required=False, default=True),
                _p("roughness", "Roughness", "GPString", required=False, default="ks", values=["ks", "manning"],
                   category="Options"),
                _p("manning_n", "Manning n (roughness manning)", "GPDouble", required=False, default=0.013,
                   category="Options"),
                _p("export_inp", "Export SWMM .inp (optional)", "DEFile", "Output", required=False,
                   filetype=["inp"], category="Options"),
                _model_out()]

    def update(self, p):
        p["manning_n"].enabled = (p.text("roughness", "ks") == "manning")

    def run(self, p, msgs):
        path = p.text("gpkg")
        impl.run_simulation(arcpy, msgs, path, p.num("duration_h", 48.0), p.num("warmup_h", 24.0),
                            p.text("roughness", "ks"), p.num("manning_n", 0.013), p.num("flow_scale", 1.0),
                            p.flag("show_layer", True), p.text("export_inp"), p.text("label", ""),
                            p.flag("series_selected", True))
        return path


class Trace(_Tool):
    label = "Trace"
    description = ("Trace upstream / downstream from a node or a conduit (leave the start blank to use the one "
                   "selected on the map), select the traced features and report the connections, ADWF and design "
                   "GSDF arriving at the start.")
    category = "Analysis"

    def getParameterInfo(self):
        return [_model_param(),
                _p("start", "Start node or conduit id (blank = selected on the map)", "GPString", required=False),
                _p("direction", "Direction", "GPString", default="Upstream",
                   values=["Upstream", "Downstream", "Both"]),
                _p("select", "Select traced features on the map", "GPBoolean", required=False, default=True)]

    def run(self, p, msgs):
        impl.trace(arcpy, msgs, p.text("gpkg"), p.text("start"), p.text("direction", "Upstream"),
                   p.flag("select", True))


# ---------------------------------------------------------------------- Results

class ShowSimulationRun(_Tool):
    label = "Show Simulation Run"
    description = "Show a stored simulation run (the latest 10 are kept) in the Simulation results layers."
    category = "Results"

    def getParameterInfo(self):
        return [_model_param(), _p("run", "Run", "GPString", values=[]),
                _p("show_layer", "Show the Simulation results layers", "GPBoolean", required=False, default=True)]

    def update(self, p):
        titles = _runs_list(p, "run")
        if titles and p["run"].value is None:
            p["run"].value = titles[0]

    def run(self, p, msgs):
        impl.show_simulation_run(arcpy, msgs, p.text("gpkg"), p.text("run", "latest"), p.flag("show_layer", True))


class CompareSimulationRuns(_Tool):
    label = "Compare Simulation Runs"
    description = ("Compare two stored runs per conduit (B minus A): change in peak flow and peak d/D, newly "
                   "surcharged and relieved conduits, in messages and the Simulation comparison layer.")
    category = "Results"

    def getParameterInfo(self):
        return [_model_param(), _p("run_a", "Run A (baseline)", "GPString", values=[]),
                _p("run_b", "Run B", "GPString", values=[]),
                _p("show_layer", "Show the Simulation comparison layer", "GPBoolean", required=False, default=True)]

    def update(self, p):
        titles = _runs_list(p, "run_a", "run_b")
        if len(titles) >= 2 and p["run_a"].value is None and p["run_b"].value is None:
            p["run_a"].value, p["run_b"].value = titles[1], titles[0]

    def run(self, p, msgs):
        impl.compare_simulation_runs(arcpy, msgs, p.text("gpkg"), p.text("run_a", "previous"),
                                     p.text("run_b", "latest"), p.flag("show_layer", True))


class PlotHydrograph(_Tool):
    label = "Plot Hydrograph"
    description = ("Flow against time for conduits with a stored series in a run (the 5 nearest the outlet, plus "
                   "the conduits selected when the run was made): peaks in the messages, optional CSV, and a "
                   "line chart on the model's time series table.")
    category = "Results"

    def getParameterInfo(self):
        return [_model_param(), _p("run", "Run", "GPString", values=[]),
                _p("ids", "Conduits / nodes", "GPString", multi=True, required=False, values=[]),
                _p("out_csv", "Output CSV (optional)", "DEFile", "Output", required=False, filetype=["csv"]),
                _p("chart", "Create a chart", "GPBoolean", required=False, default=True)]

    def update(self, p):
        from wwnet_arcgis import engine, gpkg
        titles = _runs_list(p, "run")
        if titles and p["run"].value is None:
            p["run"].value = titles[0]
        if p.val("gpkg") and p.val("run") and (p.fresh("run") or p.fresh("gpkg") or not p["ids"].filter.list):
            p["ids"].filter.list = gpkg.series_ids(p.text("gpkg"), engine.run_key(p.text("run")))

    def run(self, p, msgs):
        ids = p.val("ids")
        ids = [str(v) for v in (ids if isinstance(ids, (list, tuple)) else str(ids).split(";"))] if ids else []
        impl.plot_hydrograph(arcpy, msgs, p.text("gpkg"), p.text("run", "latest"), ids, p.text("out_csv"),
                             p.flag("chart", True))


class ExportToICM(_Tool):
    label = "Export To ICM"
    description = ("Write the model's nodes, gravity sewers, rising mains and pumps with InfoWorks ICM field names "
                   "(file geodatabase + CSV + field-mapping notes) for ICM's Open Data Import Centre.")
    category = "Data"

    def getParameterInfo(self):
        from wwnet_arcgis import icm_export as ie
        labels = list(ie.OUT_SR)
        return [_model_param(), _p("out_folder", "Output folder", "DEFolder"),
                _p("out_sr", "Coordinate system (match your ICM network)", "GPString", required=False,
                   default=labels[0], values=labels),
                _p("dem", "DEM to fill missing ground levels (optional)", ["GPRasterLayer", "DERasterDataset"],
                   required=False), _model_out()]

    def run(self, p, msgs):
        path = p.text("gpkg")
        impl.export_icm(arcpy, msgs, path, p.text("out_folder"),
                        p.text("out_sr") or "GDA2020 / MGA zone 50 (EPSG:7850)", dem=p.text("dem"))
        return path


# ---------------------------------------------------------------------- Calculators

class PipeCapacity(_Tool):
    label = "Pipe Capacity"
    description = ("Colebrook-White capacity of a circular gravity sewer of any DN; with a flow, the depth at that "
                   "flow and (DN150 to DN600) the DS 50 checks.")
    category = "Calculators"

    def getParameterInfo(self):
        return [_p("dn", "Diameter DN (mm)", "GPLong", default=225),
                _p("grade", "Grade (1 in x)", "GPDouble", default=300.0),
                _p("ks", "Roughness ks (mm)", "GPDouble", required=False, default=1.0),
                _p("flow", "Design flow (L/s, optional)", "GPDouble", required=False),
                _p("full_flow", "Full-bore capacity (L/s)", "GPDouble", "Output", required=False)]

    def run(self, p, msgs):
        res = impl.pipe_capacity(msgs, p.val("dn"), p.val("grade"), p.val("ks"), p.val("flow"))
        p["full_flow"].value = res["full_flow_lps"]


# ---------------------------------------------------------------------- Assistant / Setup

class AskWWNetAssistant(_Tool):
    label = "Ask WWNet Assistant"
    description = ("Ask the Claude-powered assistant about the model: flows, simulation runs, design checks and "
                   "data. Layer attribute data is sent to Anthropic when the assistant uses its tools.")
    category = "Assistant"

    def getParameterInfo(self):
        cfg = _settings.load()
        q = _p("question", "Question", "GPString", required=False)
        q.controlCLSID = MULTILINE
        return [q,
                _p("layer", "Context layer (optional)", "GPFeatureLayer", required=False),
                _p("use_selected", "Use selected features", "GPBoolean", required=False, default=False),
                _p("apply", "Apply proposed edits during this run", "GPBoolean", required=False, default=False),
                _p("apply_last", "Apply the proposals from the last run (no question is sent)", "GPBoolean",
                   required=False, default=False),
                _p("reset", "Reset conversation", "GPBoolean", required=False, default=False),
                _p("model", "Model", "GPString", default=cfg["model"], values=MODELS, category="Options"),
                _p("effort", "Effort", "GPString", default=cfg["effort"], values=EFFORTS, category="Options")]

    def update(self, p):
        p["question"].enabled = not p.flag("apply_last")

    def run(self, p, msgs):
        from wwnet_arcgis import maplayers
        lyr = None
        if p.val("layer") is not None:
            v = p.val("layer")
            found = v if maplayers.layer_source(v) else maplayers.find_layer(arcpy, p.text("layer"))
            lyr = maplayers.layer_name(found) if found is not None else p.text("layer")
        impl.ask_assistant(arcpy, msgs, p.text("question", ""), lyr, p.flag("use_selected"), p.text("model"),
                           p.text("effort"), p.flag("apply"), p.flag("reset"), apply_last=p.flag("apply_last"))


class SetAssistantApiKey(_Tool):
    label = "Set Assistant API Key"
    description = ("Store the Anthropic API key (Windows DPAPI, current user) and the assistant's default model "
                   "and effort. The key is never shown in messages.")
    category = "Setup"

    def getParameterInfo(self):
        cfg = _settings.load()
        return [_p("key", "Anthropic API key", "GPEncryptedString", required=False),
                _p("clear", "Remove stored key", "GPBoolean", required=False, default=False),
                _p("model", "Default model", "GPString", required=False, default=cfg["model"], values=MODELS),
                _p("effort", "Default effort", "GPString", required=False, default=cfg["effort"], values=EFFORTS)]

    def run(self, p, msgs):
        impl.set_settings(msgs, p.text("key"), p.flag("clear"), p.text("model"), p.text("effort"))


class VerifySetup(_Tool):
    label = "Verify Setup"
    description = ("Show which wwnet_native / anthropic / pydantic ... copies are loaded (and from where), whether "
                   "they import and provide every function the toolbox uses, and optionally make one tiny live "
                   "API call.")
    category = "Setup"

    def getParameterInfo(self):
        return [_p("live", "Live API test (one tiny request with the stored key)", "GPBoolean",
                   required=False, default=False)]

    def run(self, p, msgs):
        impl.verify_setup(msgs, p.flag("live"))


class SelfTest(_Tool):
    label = "Self Test"
    description = ("Run every WWNet tool against the bundled demo model in a scratch folder and write one report "
                   "file to paste back when something does not work.")
    category = "Setup"

    def getParameterInfo(self):
        return [_p("include_map", "Also test map steps (adds and removes layers in the active map)", "GPBoolean",
                   required=False, default=False),
                _p("out_folder", "Output folder (optional; default: a temp folder)", "DEFolder", required=False)]

    def run(self, p, msgs):
        from wwnet_arcgis import selftest  # after _impl(): the module cache was purged, so this is fresh
        selftest.run(arcpy, msgs, _HERE, out_dir=p.text("out_folder"), include_map=p.flag("include_map"))


class UpdateWWNet(_Tool):
    label = "Update WWNet"
    description = ("Apply a downloaded WWNet update zip (the CI artifact wwnet-arcgis-update) in place: no "
                   "extracting, no Unblock, and no restart of ArcGIS Pro for Python changes.")
    category = "Setup"

    def getParameterInfo(self):
        return [_p("zip_file", "Downloaded update zip", "DEFile", filetype=["zip"])]

    def updateParameters(self, parameters):
        return

    def execute(self, parameters, messages):
        """Not the base execute: the update must not import (and so lock) the modules it replaces."""
        _vendor.activate(_HERE)
        p = _Params(parameters)
        res = _updater.apply_update(p.text("zip_file"), _HERE, log=arcpy.AddMessage)
        arcpy.AddMessage(f"Update version: {res['version'] or 'unknown'}")
        upd = res["updated"]
        arcpy.AddMessage(f"{len(upd)} file(s) updated, {res['unchanged']} unchanged.")
        for rel in upd[:30]:
            arcpy.AddMessage(f"  updated: {rel}")
        if len(upd) > 30:
            arcpy.AddMessage(f"  ... and {len(upd) - 30} more")
        for rel in res["pending"]:
            arcpy.AddWarning(f"{rel}: file is locked. Native module staged; it will be swapped in the next "
                             "time ArcGIS Pro starts \u2014 restart Pro once to use it")
        for rel in res["skipped"]:
            arcpy.AddWarning(f"Skipped: {rel}")
        if res["backup"]:
            arcpy.AddMessage(f"Previous versions of changed files saved in: {res['backup']}")
        if "WWNet.pyt" in upd:
            arcpy.AddMessage("WWNet.pyt changed: right-click the WWNet toolbox in the Catalog pane > Refresh "
                             "to see new or changed tool parameters")
        arcpy.AddMessage("Python code changes take effect on the next tool run (no restart needed)")


class Toolbox(object):
    def __init__(self):
        self.label = "WWNet"
        self.alias = "wwnet"
        self.tools = [NewModel, AddModelToMap, ModelSettings, JoinDanglingEnds,
                      ImportSewerData, ImportPressureData, SetPumpStationData, ImportDiurnalProfile, AssignProfile,
                      ExportToICM,
                      CheckModel, RunDesign, RunSimulation, Trace,
                      ShowSimulationRun, CompareSimulationRuns, PlotHydrograph,
                      PipeCapacity,
                      AskWWNetAssistant, SetAssistantApiKey, VerifySetup, SelfTest, UpdateWWNet]
