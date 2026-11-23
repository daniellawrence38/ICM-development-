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

MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
EFFORTS = ["low", "medium", "high"]


def _impl():
    """Fresh ``wwnet_arcgis.tools_impl`` for this tool run (drops cached WWNet modules first), so files
    updated by *Update WWNet* take effect without restarting ArcGIS Pro."""
    return _updater.fresh_impl()
MULTILINE = "{E5456E51-0C41-4797-9EE4-5269820C6F0E}"


def _p(name, label, datatype, direction="Input", required=True, multi=False, default=None,
       values=None, filetype=None):
    p = arcpy.Parameter(displayName=label, name=name, datatype=datatype,
                        parameterType="Required" if required else "Optional",
                        direction=direction, multiValue=multi)
    if default is not None:
        p.value = default
    if values:
        p.filter.type = "ValueList"
        p.filter.list = list(values)
    if filetype:
        p.filter.list = list(filetype)
    return p


def _text(p):
    return p.valueAsText if p.value is not None else None


def _layer_obj(param):
    """Resolve a layer parameter to a map Layer (has .name / .dataSource). Accepts a map layer, a
    layer name, or a dataset path (e.g. C:\\m.gpkg\\main.wwn_nodes): a path is matched to the map
    layer showing it, else returned as a lightweight stand-in so tools can still read the table."""
    import types
    from wwnet_arcgis import maplayers as _ml
    v = param.value
    if _ml.layer_source(v):
        return v
    text = param.valueAsText
    layers = list(arcpy.mp.ArcGISProject("CURRENT").activeMap.listLayers())
    for lyr in layers:
        if _ml.layer_name(lyr) == text:
            return lyr
    norm = (text or "").replace("/", "\\").lower()
    for lyr in layers:
        if _ml.layer_source(lyr).replace("/", "\\").lower() == norm:
            return lyr
    if ".gpkg" in norm:
        return types.SimpleNamespace(name=os.path.basename(text), dataSource=text, isFeatureLayer=True,
                                     isGroupLayer=False, isBroken=False, supports=lambda prop: True)
    raise RuntimeError(f"Layer {text!r} not found in the active map.")


def _ml_name(lyr):
    from wwnet_arcgis import maplayers as _ml
    return _ml.layer_name(lyr)


class Toolbox(object):
    def __init__(self):
        self.label = "WWNet"
        self.alias = "wwnet"
        self.tools = [NewModel, ImportSewerData, ImportPressureData, SetPumpStationData, ImportDiurnalProfile, AddModelToMap, CheckModel, RunDesign, RunSimulation, Trace, PipeCapacity, ExportToICM,
                      SetAssistantApiKey, AskWWNetAssistant, VerifySetup, SelfTest, UpdateWWNet]


class NewModel(object):
    def __init__(self):
        self.label = "New Model"
        self.description = "Create an empty WWNet GeoPackage (EPSG:7850) and add it to the map."
        self.category = "Model"

    def getParameterInfo(self):
        out = _p("out_gpkg", "Output GeoPackage", "DEFile", "Output", filetype=["gpkg"])
        return [out, _p("add_to_map", "Add to active map", "GPBoolean", required=False, default=True),
                _p("overwrite", "Overwrite if it exists", "GPBoolean", required=False, default=False)]

    def execute(self, parameters, messages):
        _vendor.activate(_HERE)  # Pro restores sys.path before each tool run
        impl = _impl()
        impl.new_model(arcpy, impl.Messages(arcpy), parameters[0].valueAsText,
                       bool(parameters[1].value), bool(parameters[2].value))


def _source(param):
    """Layer object when the value is a map layer, else its text (path / layer name); None if empty."""
    if param.value is None:
        return None
    return param.value if hasattr(param.value, "dataSource") else param.valueAsText


class ImportSewerData(object):
    # fixed parameter positions: 0 ACs, 1 connection points, 2 sewers, 3 output, 4 snap, 5 grade source,
    # 6 selected only, 7 add to map, 8.. one Field parameter per role in sewer_import.FIELD_ROLES
    _LAYER_PARAM = {"ac": 0, "cp": 1, "sewer": 2}
    _LAYER_NAME = {"ac": "ac_layer", "cp": "cp_layer", "sewer": "sewer_layer"}

    def __init__(self):
        self.label = "Import Sewer Data"
        self.description = ("Convert Water Corporation (WA) gravity sewer GIS layers (access chambers, "
                            "connection points, gravity sewers) into a new WWNet GeoPackage (EPSG:7850), "
                            "validate it and add it to the map. Source field roles are selectable.")
        self.category = "Data"

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
                  _p("snap", "Snap tolerance (m)", "GPDouble", required=False, default=0.5),
                  _p("grade_source", "Grade source", "GPString", required=False, default="levels",
                     values=["levels", "field"]),
                  _p("use_selected", "Use selected features only", "GPBoolean", required=False, default=False),
                  _p("add_to_map", "Add to map", "GPBoolean", required=False, default=True)]
        for role, layer_key, label, required in si.FIELD_ROLES:
            f = _p("f_" + role, label, "Field", required=required)
            f.parameterDependencies = [self._LAYER_NAME[layer_key]]  # Pro requires parameter NAMES
            f.category = "Source fields"
            params.append(f)
        loads = _p("load_cps", "Create loads from connection points", "GPBoolean", required=False, default=True)
        loads.category = "Connection point loads"
        radius = _p("cp_radius", "Connection point search distance (m)", "GPDouble", required=False, default=10.0)
        radius.category = "Connection point loads"
        return params + [loads, radius]

    def updateParameters(self, parameters):
        from wwnet_arcgis import sewer_import as si
        for layer_key, idx in self._LAYER_PARAM.items():
            p = parameters[idx]
            if p.value is None or p.hasBeenValidated:
                continue
            try:
                found = impl.sewer_field_defaults(arcpy, _source(p))
            except Exception:
                continue
            for n, (role, k, _l, _q) in enumerate(si.FIELD_ROLES):
                if k == layer_key:
                    parameters[8 + n].value = found.get(role) or None
        return

    def execute(self, parameters, messages):
        _vendor.activate(_HERE)  # Pro restores sys.path before each tool run
        impl = _impl()
        from wwnet_arcgis import sewer_import as si
        fields = {role: (parameters[8 + n].valueAsText or "")
                  for n, (role, _k, _l, _q) in enumerate(si.FIELD_ROLES)}
        k = 8 + len(si.FIELD_ROLES)       # first connection-point-load parameter
        impl.import_sewer_data(arcpy, impl.Messages(arcpy), _source(parameters[0]), _source(parameters[1]),
                               _source(parameters[2]), parameters[3].valueAsText, fields,
                               parameters[4].value if parameters[4].value is not None else 0.5,
                               parameters[5].valueAsText or "levels", bool(parameters[6].value),
                               bool(parameters[7].value),
                               load_connection_points=bool(parameters[k].value) if parameters[k].value is not None
                               else True,
                               cp_load_search_m=parameters[k + 1].value)


class ImportPressureData(object):
    # fixed positions: 0 model, 1 pump stations, 2 pressure mains, 3 selected only, 4 search distance,
    # 5 snap tolerance, 6 discharge distance, 7 include not in use, 8 add to map, 9.. Field parameters
    _LAYER_PARAM = {"ps": 1, "main": 2}
    _LAYER_NAME = {"ps": "ps_layer", "main": "main_layer"}

    def __init__(self):
        self.label = "Import Pressure Data"
        self.description = ("Add Water Corporation (WA) sewer pump stations and pressure mains (rising mains) to an "
                            "existing WWNet GeoPackage made by Import Sewer Data: chains the main segments, links "
                            "wet wells and discharge nodes. Pump hydraulics are not in the GIS and must be entered.")
        self.category = "Data"

    def getParameterInfo(self):
        from wwnet_arcgis import pressure_import as pi
        ps = _p("ps_layer", "Sewer pump stations", "GPFeatureLayer")
        ps.filter.list = ["Point"]
        mn = _p("main_layer", "Sewer pressure mains", "GPFeatureLayer")
        mn.filter.list = ["Polyline"]
        params = [_p("gpkg", "WWNet GeoPackage (existing model)", "DEFile", filetype=["gpkg"]), ps, mn,
                  _p("use_selected", "Use selected features only", "GPBoolean", required=False, default=True),
                  _p("search", "Search distance pump station to main end / wet well (m)", "GPDouble",
                     required=False, default=30.0),
                  _p("snap", "Snap tolerance between main segments (m)", "GPDouble", required=False, default=1.0),
                  _p("discharge", "Discharge search distance main end to gravity node (m)", "GPDouble",
                     required=False, default=10.0),
                  _p("include_n", "Include not-in-use (N) pump stations and mains", "GPBoolean", required=False,
                     default=False),
                  _p("add_to_map", "Add / refresh layers on map", "GPBoolean", required=False, default=True)]
        for role, layer_key, label, required in pi.FIELD_ROLES:
            f = _p("f_" + role, label, "Field", required=required)
            f.parameterDependencies = [self._LAYER_NAME[layer_key]]  # Pro requires parameter NAMES
            f.category = "Source fields"
            params.append(f)
        return params

    def updateParameters(self, parameters):
        from wwnet_arcgis import pressure_import as pi
        for layer_key, idx in self._LAYER_PARAM.items():
            p = parameters[idx]
            if p.value is None or p.hasBeenValidated:
                continue
            try:
                found = impl.pressure_field_defaults(arcpy, _source(p), layer_key)
            except Exception:
                continue
            for n, (role, k, _l, _q) in enumerate(pi.FIELD_ROLES):
                if k == layer_key:
                    parameters[9 + n].value = found.get(role) or None
        return

    def execute(self, parameters, messages):
        _vendor.activate(_HERE)  # Pro restores sys.path before each tool run
        impl = _impl()
        from wwnet_arcgis import pressure_import as pi
        fields = {role: (parameters[9 + n].valueAsText or "") for n, (role, _k, _l, _q) in enumerate(pi.FIELD_ROLES)}
        impl.import_pressure_data(arcpy, impl.Messages(arcpy), parameters[0].valueAsText, _source(parameters[1]),
                                  _source(parameters[2]), fields, bool(parameters[3].value),
                                  parameters[4].value, parameters[5].value, parameters[6].value,
                                  bool(parameters[7].value), bool(parameters[8].value))


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


class SetPumpStationData(object):
    # fixed positions: 0 model, 1 station, 2 diameter, 3 area, 4 level mode, 5 stop, 6 start, 7 overflow,
    # 8 emergency storage, 9 duty, 10 assist, 11 standby, 12 duty rate, 13 curve, 14 column order,
    # 15 flow units, 16 speed, 17 source note, 18 replace pumps
    def __init__(self):
        self.label = "Set Pump Station Data"
        self.description = ("Enter the hydraulic data of a pump station from its data sheet (wet well size, "
                            "stop / start / overflow levels, pumps and pump curve), which the GIS does not hold. "
                            "Choose a station created by Import Pressure Data; its current values are filled in. "
                            "The pump curve can be pasted from the data sheet, Excel or InfoWorks ICM.")
        self.category = "Data"

    def getParameterInfo(self):
        station = _p("station", "Pump station", "GPString")
        station.filter.type = "ValueList"
        station.filter.list = []
        curve = _p("pump_curve", "Pump curve (paste: flow and head, one point per line)", "GPString", required=False)
        curve.controlCLSID = MULTILINE
        return [_p("gpkg", "WWNet GeoPackage (existing model)", "DEFile", filetype=["gpkg"]),
                station,
                _p("diameter_m", "Wet well diameter (m)", "GPDouble", required=False),
                _p("area_m2", "Wet well area (m2)", "GPDouble", required=False),
                _p("level_mode", "Levels are", "GPString", required=False, default="AHD",
                   values=list(LEVEL_MODES)),
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
                _p("replace_pumps", "Replace the station's existing pumps", "GPBoolean", required=False,
                   default=True)]

    def updateParameters(self, parameters):
        gp, station = parameters[0], parameters[1]
        stations = []
        if gp.value is not None:
            try:
                stations = _pumpdata.list_stations(gp.valueAsText)
            except Exception:
                stations = []
        if gp.value is not None and not gp.hasBeenValidated:
            station.filter.list = [_station_label(s) for s in stations]
            if station.value is not None and _station_id(station.valueAsText) not in [s["ps_id"] for s in stations]:
                station.value = None
        if station.value is None or station.hasBeenValidated:
            return
        sel = next((s for s in stations if s["ps_id"] == _station_id(station.valueAsText)), None)
        if sel is None:
            return
        parameters[2].value = None                      # area is stored; the diameter is derived
        parameters[3].value = sel["wet_well_area_m2"]
        parameters[4].value = "AHD"                      # levels below are always AHD
        parameters[5].value = sel["stop_level"]
        parameters[6].value = sel["start_level"]
        parameters[7].value = sel["overflow_level"]
        parameters[8].value = sel["emergency_storage_m3"]
        counts = sel["pumps"]
        if sum(counts.values()):
            parameters[9].value, parameters[10].value, parameters[11].value = (
                counts.get("duty", 0), counts.get("assist", 0), counts.get("standby", 0))
        else:
            parameters[9].value, parameters[10].value, parameters[11].value = 1, 0, 0
        parameters[12].value = sel["pump_rate_lps"]
        parameters[13].value = _curve_text(sel["curve"])
        parameters[14].value = "flow,head"               # the curve is shown as L/s and m
        parameters[15].value = "L/s"
        return

    def updateMessages(self, parameters):
        curve = parameters[13]
        if curve.value is None or not str(curve.valueAsText).strip():
            return
        try:
            _pts, warns = _pumpdata.parse_curve(
                curve.valueAsText, order=CURVE_ORDERS.get(parameters[14].valueAsText or "flow,head", "flow_head"),
                flow_units=parameters[15].valueAsText or "L/s")
        except ValueError as exc:
            curve.setErrorMessage(str(exc))
            return
        if warns:
            curve.setWarningMessage(" ".join(warns))
        return

    def execute(self, parameters, messages):
        _vendor.activate(_HERE)  # Pro restores sys.path before each tool run
        impl = _impl()
        v = lambda i, d=None: parameters[i].value if parameters[i].value is not None else d  # noqa: E731
        impl.set_pump_station_data(
            arcpy, impl.Messages(arcpy), parameters[0].valueAsText, _station_id(parameters[1].valueAsText),
            wet_well_diameter_m=v(2), wet_well_area_m2=v(3),
            level_mode=LEVEL_MODES.get(parameters[4].valueAsText or "AHD", "AHD"),
            stop=v(5), start=v(6), overflow=v(7), emergency_storage_m3=v(8),
            n_duty=int(v(9, 1)), n_assist=int(v(10, 0)), n_standby=int(v(11, 0)), duty_rate_lps=v(12),
            curve_text=_text(parameters[13]),
            curve_order=CURVE_ORDERS.get(parameters[14].valueAsText or "flow,head", "flow_head"),
            flow_units=parameters[15].valueAsText or "L/s", speed_pct=float(v(16, 100.0)),
            source_note=_text(parameters[17]), replace_pumps=bool(v(18, True)))


class ImportDiurnalProfile(object):
    def __init__(self):
        self.label = "Import Diurnal Profile"
        self.description = ("Import a diurnal flow profile (time as hh:mm or minutes, factor; any interval, "
                            "header optional) from a CSV / TXT file, or from text pasted out of InfoWorks ICM "
                            "(tab-separated Time / Factor), into a WWNet GeoPackage. Upgrades the model to "
                            "schema v2 if needed. The mean factor is checked, not silently changed.")
        self.category = "Data"

    def getParameterInfo(self):
        pasted = _p("pasted", "Or paste profile text (Time<TAB>Factor)", "GPString", required=False)
        pasted.controlCLSID = MULTILINE
        return [_p("gpkg", "WWNet GeoPackage", "DEFile", filetype=["gpkg"]),
                _p("profile_id", "Profile id", "GPString"),
                _p("csv", "Profile CSV / TXT file", "DEFile", required=False, filetype=["csv", "txt"]),
                _p("description", "Description", "GPString", required=False),
                _p("set_default", "Set as the model's default profile", "GPBoolean", required=False, default=True),
                _p("normalise", "Normalise to mean 1", "GPBoolean", required=False, default=False),
                pasted]

    def execute(self, parameters, messages):
        _vendor.activate(_HERE)  # Pro restores sys.path before each tool run
        impl = _impl()
        impl.import_profile(impl.Messages(arcpy), parameters[0].valueAsText, parameters[1].valueAsText,
                            _text(parameters[2]), _text(parameters[3]) or "", bool(parameters[4].value),
                            bool(parameters[5].value), _text(parameters[6]))


class AddModelToMap(object):
    def __init__(self):
        self.label = "Add Model To Map"
        self.description = "Add the wwn_* layers of an existing WWNet GeoPackage to the active map."
        self.category = "Model"

    def getParameterInfo(self):
        return [_p("gpkg", "WWNet GeoPackage", "DEFile", filetype=["gpkg"]),
                _p("symbolize", "Apply symbology", "GPBoolean", required=False, default=True)]

    def execute(self, parameters, messages):
        _vendor.activate(_HERE)  # Pro restores sys.path before each tool run
        impl = _impl()
        impl.add_model_to_map(arcpy, impl.Messages(arcpy), parameters[0].valueAsText, bool(parameters[1].value))


class CheckModel(object):
    def __init__(self):
        self.label = "Check Model"
        self.description = "Run `wwnet check` and report topology / data issues."
        self.category = "Analysis"

    def getParameterInfo(self):
        return [_p("gpkg", "WWNet GeoPackage", "DEFile", filetype=["gpkg"]),
                _p("out_table", "Output issues table (optional)", "DETable", "Output", required=False)]

    def execute(self, parameters, messages):
        _vendor.activate(_HERE)  # Pro restores sys.path before each tool run
        impl = _impl()
        impl.check_model(arcpy, impl.Messages(arcpy), parameters[0].valueAsText, _text(parameters[1]))


class RunDesign(object):
    def __init__(self):
        self.label = "Run Design"
        self.description = "Run `wwnet design` (DS 50 accumulation, hydraulics and rule checks)."
        self.category = "Analysis"

    def getParameterInfo(self):
        return [_p("gpkg", "WWNet GeoPackage", "DEFile", filetype=["gpkg"]),
                _p("show_layer", "Show results layer on map", "GPBoolean", required=False, default=True),
                _p("add_table", "Add results table to map", "GPBoolean", required=False, default=False),
                _p("select_failed", "Select failed conduits", "GPBoolean", required=False, default=True)]

    def execute(self, parameters, messages):
        _vendor.activate(_HERE)  # Pro restores sys.path before each tool run
        impl = _impl()
        impl.run_design(arcpy, impl.Messages(arcpy), parameters[0].valueAsText, bool(parameters[1].value),
                        bool(parameters[2].value), bool(parameters[3].value))


class RunSimulation(object):
    def __init__(self):
        self.label = "Run Simulation"
        self.description = ("Run the EPA SWMM dynamic-wave engine (embedded, no install) on the model: dry weather "
                            "flow from node loads and sub-catchments with their diurnal profiles. Results go to "
                            "the wwn_sim_* tables and a temporary results layer coloured by peak d/D.")
        self.category = "Analysis"

    def getParameterInfo(self):
        return [_p("gpkg", "WWNet GeoPackage", "DEFile", filetype=["gpkg"]),
                _p("duration_h", "Duration (h)", "GPDouble", required=False, default=48.0),
                _p("warmup_h", "Warm-up (h)", "GPDouble", required=False, default=24.0),
                _p("roughness", "Roughness", "GPString", required=False, default="manning",
                   values=["manning", "ks"]),
                _p("manning_n", "Manning n", "GPDouble", required=False, default=0.013),
                _p("flow_scale", "Flow scale", "GPDouble", required=False, default=1.0),
                _p("show_layer", "Show results layer on map", "GPBoolean", required=False, default=True),
                _p("export_inp", "Export SWMM .inp (optional)", "DEFile", "Output", required=False,
                   filetype=["inp"])]

    def execute(self, parameters, messages):
        _vendor.activate(_HERE)  # Pro restores sys.path before each tool run
        impl = _impl()
        v = lambda i, d: parameters[i].value if parameters[i].value is not None else d  # noqa: E731
        impl.run_simulation(arcpy, impl.Messages(arcpy), parameters[0].valueAsText, float(v(1, 48.0)),
                            float(v(2, 24.0)), parameters[3].valueAsText or "manning", float(v(4, 0.013)),
                            float(v(5, 1.0)), bool(v(6, True)), _text(parameters[7]))


class Trace(object):
    def __init__(self):
        self.label = "Trace"
        self.description = "Trace upstream / downstream from a node and select the traced features."
        self.category = "Analysis"

    def getParameterInfo(self):
        nodes = _p("nodes", "WWNet Nodes layer", "GPFeatureLayer")
        node = _p("node_id", "Start node", "GPString")
        node.filter.type = "ValueList"
        node.filter.list = []
        return [nodes, node,
                _p("direction", "Direction", "GPString", default="Upstream",
                   values=["Upstream", "Downstream", "Both"]),
                _p("select", "Select traced features on the map", "GPBoolean", required=False, default=True)]

    def updateParameters(self, parameters):
        if parameters[0].value and not parameters[0].hasBeenValidated:
            try:
                parameters[1].filter.list = impl.node_ids(arcpy, _layer_obj(parameters[0]))
            except Exception:
                parameters[1].filter.list = []
        return

    def execute(self, parameters, messages):
        _vendor.activate(_HERE)  # Pro restores sys.path before each tool run
        impl = _impl()
        impl.trace(arcpy, impl.Messages(arcpy), _layer_obj(parameters[0]), parameters[1].valueAsText,
                   parameters[2].valueAsText, bool(parameters[3].value))


class PipeCapacity(object):
    def __init__(self):
        self.label = "Pipe Capacity"
        self.description = "Colebrook-White capacity of a circular gravity sewer (DS 50 basis)."
        self.category = "Calculators"

    def getParameterInfo(self):
        return [_p("dn", "Diameter DN (mm)", "GPLong", default=225, values=[150, 225, 300, 375, 450, 525, 600]),
                _p("grade", "Grade (1 in x)", "GPDouble", default=300.0),
                _p("ks", "Roughness ks (mm)", "GPDouble", required=False, default=1.0),
                _p("flow", "Design flow (L/s, optional)", "GPDouble", required=False),
                _p("full_flow", "Full-bore capacity (L/s)", "GPDouble", "Output", required=False)]

    def execute(self, parameters, messages):
        _vendor.activate(_HERE)  # Pro restores sys.path before each tool run
        impl = _impl()
        res = impl.pipe_capacity(impl.Messages(arcpy), parameters[0].value, parameters[1].value,
                                 parameters[2].value, parameters[3].value)
        parameters[4].value = res["full_flow_lps"]


class ExportToICM(object):
    def __init__(self):
        self.label = "Export To ICM"
        self.description = ("Write the model's nodes and gravity sewers with InfoWorks ICM field names "
                            "(file geodatabase + CSV + field-mapping notes) for ICM's Open Data Import Centre.")
        self.category = "Data"

    def getParameterInfo(self):
        from wwnet_arcgis import icm_export as ie
        labels = list(ie.OUT_SR)
        return [_p("gpkg", "WWNet GeoPackage", "DEFile", filetype=["gpkg"]),
                _p("out_folder", "Output folder", "DEFolder"),
                _p("out_sr", "Coordinate system (match your ICM network)", "GPString", required=False,
                   default=labels[0], values=labels),
                _p("dem", "DEM to fill missing ground levels (optional)", ["GPRasterLayer", "DERasterDataset"],
                   required=False)]

    def execute(self, parameters, messages):
        _vendor.activate(_HERE)  # Pro restores sys.path before each tool run
        impl = _impl()
        impl.export_icm(arcpy, impl.Messages(arcpy), parameters[0].valueAsText, parameters[1].valueAsText,
                        _text(parameters[2]) or "GDA2020 / MGA zone 50 (EPSG:7850)", dem=_text(parameters[3]))


class SetAssistantApiKey(object):
    def __init__(self):
        self.label = "Set Assistant API Key"
        self.description = ("Store the Anthropic API key (Windows DPAPI, current user) and assistant "
                            "settings. The key is never shown in messages.")
        self.category = "Assistant"

    def getParameterInfo(self):
        cfg = _settings.load()
        return [_p("key", "Anthropic API key", "GPEncryptedString", required=False),
                _p("clear", "Remove stored key", "GPBoolean", required=False, default=False),
                _p("model", "Default model", "GPString", required=False,
                   default=cfg["model"], values=MODELS),
                _p("effort", "Default effort", "GPString", required=False,
                   default=cfg["effort"], values=EFFORTS),
                _p("exe", "WWNet CLI path (optional fallback, only used if wwnet_native is missing)", "DEFile", required=False,
                   default=cfg["wwnet_exe"] or None)]

    def execute(self, parameters, messages):
        _vendor.activate(_HERE)  # Pro restores sys.path before each tool run
        impl = _impl()
        impl.set_settings(impl.Messages(arcpy), _text(parameters[0]), bool(parameters[1].value),
                          _text(parameters[2]), _text(parameters[3]), _text(parameters[4]) or "")


class AskWWNetAssistant(object):
    def __init__(self):
        self.label = "Ask WWNet Assistant"
        self.description = ("Ask the Claude-powered DS 50 design assistant about the model. "
                            "Layer attribute data is sent to Anthropic when the assistant uses its tools.")
        self.category = "Assistant"

    def getParameterInfo(self):
        cfg = _settings.load()
        q = _p("question", "Question", "GPString")
        q.controlCLSID = MULTILINE
        return [q,
                _p("layer", "Context layer (optional)", "GPFeatureLayer", required=False),
                _p("use_selected", "Use selected features", "GPBoolean", required=False, default=False),
                _p("model", "Model", "GPString", default=cfg["model"], values=MODELS),
                _p("effort", "Effort", "GPString", default=cfg["effort"], values=EFFORTS),
                _p("apply", "Apply proposed edits", "GPBoolean", required=False, default=False),
                _p("reset", "Reset conversation", "GPBoolean", required=False, default=False)]

    def execute(self, parameters, messages):
        _vendor.activate(_HERE)  # Pro restores sys.path before each tool run
        impl = _impl()
        lyr = _ml_name(_layer_obj(parameters[1])) if parameters[1].value else None
        impl.ask_assistant(arcpy, impl.Messages(arcpy), parameters[0].valueAsText, lyr,
                           bool(parameters[2].value), parameters[3].valueAsText,
                           parameters[4].valueAsText, bool(parameters[5].value),
                           bool(parameters[6].value))


class VerifySetup(object):
    def __init__(self):
        self.label = "Verify Setup"
        self.description = ("Show which wwnet_native / anthropic / pydantic ... copies are loaded (and from "
                            "where), whether they import, and optionally make one tiny live API call.")
        self.category = "Assistant"

    def getParameterInfo(self):
        return [_p("live", "Live API test (one tiny request with the stored key)", "GPBoolean",
                   required=False, default=False)]

    def execute(self, parameters, messages):
        _vendor.activate(_HERE)  # Pro restores sys.path before each tool run
        impl = _impl()
        impl.verify_setup(impl.Messages(arcpy), bool(parameters[0].value))


class SelfTest(object):
    def __init__(self):
        self.label = "Self Test"
        self.description = ("Run every WWNet tool against the bundled demo model in a scratch folder and write "
                            "one report file to paste back when something does not work.")
        self.category = "Assistant"

    def getParameterInfo(self):
        return [_p("include_map", "Also test map steps (adds and removes layers in the active map)", "GPBoolean",
                   required=False, default=False),
                _p("out_folder", "Output folder (optional; default: a temp folder)", "DEFolder", required=False)]

    def execute(self, parameters, messages):
        _vendor.activate(_HERE)  # Pro restores sys.path before each tool run
        impl = _impl()
        from wwnet_arcgis import selftest  # after _impl(): the module cache was purged, so this is fresh
        selftest.run(arcpy, impl.Messages(arcpy), _HERE, out_dir=_text(parameters[1]),
                     include_map=bool(parameters[0].value))


class UpdateWWNet(object):
    def __init__(self):
        self.label = "Update WWNet"
        self.description = ("Apply a downloaded WWNet update zip (the CI artifact wwnet-arcgis-update) in place: "
                            "no extracting, no Unblock, and no restart of ArcGIS Pro for Python changes.")
        self.category = "Assistant"

    def getParameterInfo(self):
        return [_p("zip_file", "Downloaded update zip", "DEFile", filetype=["zip"])]

    def execute(self, parameters, messages):
        _vendor.activate(_HERE)  # Pro restores sys.path before each tool run
        res = _updater.apply_update(parameters[0].valueAsText, _HERE, log=arcpy.AddMessage)
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
