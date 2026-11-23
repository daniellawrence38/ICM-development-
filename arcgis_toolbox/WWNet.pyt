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

try:  # one model list for both front ends (the shared assistant client)
    from wwnet_arcgis import shared as _shared  # noqa: E402
    _client = _shared.assistant("client")
    MODELS = [m for m, _ in _client.MODELS]
    EFFORTS = list(_client.EFFORTS)
except Exception:  # noqa: BLE001 - the toolbox must still open
    MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
    EFFORTS = ["low", "medium", "high"]
MULTILINE = "{E5456E51-0C41-4797-9EE4-5269820C6F0E}"


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
        p = _Params(parameters)
        out = self.run(p, Messages(arcpy))
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
                     values=["levels", "field"], category="Options")]
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
                               load_connection_points=p.flag("load_cps", True), cp_load_search_m=p.num("cp_radius"))


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


class Toolbox(object):
    def __init__(self):
        self.label = "WWNet"
        self.alias = "wwnet"
        self.tools = [NewModel, AddModelToMap, ModelSettings,
                      ImportSewerData, ImportDiurnalProfile, AssignProfile,
                      CheckModel, RunDesign, RunSimulation, Trace,
                      ShowSimulationRun, CompareSimulationRuns, PlotHydrograph,
                      PipeCapacity,
                      AskWWNetAssistant, SetAssistantApiKey, VerifySetup]
