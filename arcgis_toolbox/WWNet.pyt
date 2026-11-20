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

from wwnet_arcgis import settings as _settings  # noqa: E402
from wwnet_arcgis import tools_impl as impl  # noqa: E402
from wwnet_arcgis.messages import Messages  # noqa: E402

MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
EFFORTS = ["low", "medium", "high"]
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
    """Resolve a layer parameter to the map Layer (has .name / .dataSource)."""
    v = param.value
    if hasattr(v, "dataSource"):
        return v
    name = param.valueAsText
    for lyr in arcpy.mp.ArcGISProject("CURRENT").activeMap.listLayers():
        if lyr.name == name:
            return lyr
    raise RuntimeError(f"Layer {name!r} not found in the active map.")


class Toolbox(object):
    def __init__(self):
        self.label = "WWNet"
        self.alias = "wwnet"
        self.tools = [NewModel, ImportSewerData, ImportDiurnalProfile, AddModelToMap, CheckModel, RunDesign, Trace, PipeCapacity,
                      SetAssistantApiKey, AskWWNetAssistant, VerifySetup]


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
        impl.new_model(arcpy, Messages(arcpy), parameters[0].valueAsText,
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
        from wwnet_arcgis import sewer_import as si
        fields = {role: (parameters[8 + n].valueAsText or "")
                  for n, (role, _k, _l, _q) in enumerate(si.FIELD_ROLES)}
        k = 8 + len(si.FIELD_ROLES)       # first connection-point-load parameter
        impl.import_sewer_data(arcpy, Messages(arcpy), _source(parameters[0]), _source(parameters[1]),
                               _source(parameters[2]), parameters[3].valueAsText, fields,
                               parameters[4].value if parameters[4].value is not None else 0.5,
                               parameters[5].valueAsText or "levels", bool(parameters[6].value),
                               bool(parameters[7].value),
                               load_connection_points=bool(parameters[k].value) if parameters[k].value is not None
                               else True,
                               cp_load_search_m=parameters[k + 1].value)


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
        impl.import_profile(Messages(arcpy), parameters[0].valueAsText, parameters[1].valueAsText,
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
        impl.add_model_to_map(arcpy, Messages(arcpy), parameters[0].valueAsText, bool(parameters[1].value))


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
        impl.check_model(arcpy, Messages(arcpy), parameters[0].valueAsText, _text(parameters[1]))


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
        impl.run_design(arcpy, Messages(arcpy), parameters[0].valueAsText, bool(parameters[1].value),
                        bool(parameters[2].value), bool(parameters[3].value))


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
        impl.trace(arcpy, Messages(arcpy), _layer_obj(parameters[0]), parameters[1].valueAsText,
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
        res = impl.pipe_capacity(Messages(arcpy), parameters[0].value, parameters[1].value,
                                 parameters[2].value, parameters[3].value)
        parameters[4].value = res["full_flow_lps"]


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
        impl.set_settings(Messages(arcpy), _text(parameters[0]), bool(parameters[1].value),
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
        lyr = _layer_obj(parameters[1]).name if parameters[1].value else None
        impl.ask_assistant(arcpy, Messages(arcpy), parameters[0].valueAsText, lyr,
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
        impl.verify_setup(Messages(arcpy), bool(parameters[0].value))
