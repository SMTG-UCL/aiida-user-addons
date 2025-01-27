"""
Workchain implementation for performing phonon calculation using `aiida-phonopy`

Difference to the workflow in `aiida-phonopy`: here we do not use and *immigrant* method
for supporting imported calculations. Also, add the relaxation step to fully converge the
structure first - potentially allowing a direct structure -> phonon property workflow.

The a few VASP specific points has been marked. In theory, the work chain can be adapted to
support any code that does force and energy output.
"""
import aiida.orm as orm
from aiida.common.exceptions import InputValidationError
from aiida.common.extendeddicts import AttributeDict
from aiida.engine import ToContext, WorkChain, if_
from aiida.orm.nodes.data.base import to_aiida_type
from aiida.plugins import WorkflowFactory
from aiida_phonopy.common.utils import (
    generate_phonopy_cells,
    get_force_constants,
    get_nac_params,
    get_phonon,
    get_vasp_force_sets_dict,
)

from aiida_user_addons.common.opthold import (
    FloatOption,
    IntOption,
    ListOption,
    ListOrStringOption,
    OptionContainer,
)
from aiida_user_addons.vworkflows.common import (
    OVERRIDE_NAMESPACE,
    nested_update,
    nested_update_dict_node,
)

__version__ = "0.1.0"


class VaspAutoPhononWorkChain(WorkChain):
    """
    VaspAutoPhononWorkChain

    A workchain to perform automated relaxation followed by finite displacement
    single point calculations and finally use phonopy to obtain the band structure
    and density of states.

    """

    _relax_entrypoint = "vaspu.relax"
    _relax_chain = WorkflowFactory(_relax_entrypoint)
    _singlepoint_entrypoint = "vaspu.vasp"
    _singlepoint_chain = WorkflowFactory(_singlepoint_entrypoint)

    @classmethod
    def define(cls, spec):

        super().define(spec)
        spec.outline(
            cls.setup,
            if_(cls.should_run_relax)(cls.run_relaxation, cls.inspect_relaxation),
            cls.create_displacements,
            if_(cls.should_run_supercell_chgcar)(
                cls.run_supercell_chgcar,
                cls.inspect_supercell_calc,
            ),
            cls.run_force_and_nac_calcs,
            cls.create_force_set_and_constants,
            if_(cls.remote_phonopy)(cls.run_phonopy_remote, cls.collect_remote_run_data).else_(
                cls.create_force_constants,
                cls.run_phonopy_local,
            ),
        )

        # Standard calculation inputs
        spec.expose_inputs(
            cls._relax_chain,
            namespace="relax",
            exclude=("structure",),
            namespace_options={
                "required": False,
                "help": "Inputs for the relaxation to be performed.",
                "populate_defaults": False,
            },
        )
        spec.expose_inputs(
            cls._singlepoint_chain,
            namespace="singlepoint",
            exclude=("structure",),
            namespace_options={
                "required": True,
                "help": "Additional inputs for the singlepoint calculations.",
                "populate_defaults": True,
            },
        )
        spec.expose_inputs(
            cls._singlepoint_chain,
            namespace="nac",
            exclude=("structure",),
            namespace_options={
                "required": False,
                "populate_defaults": False,
                "help": "Inputs for the DFPT NAC calculation.",
            },
        )
        # Phonon specific inputs
        spec.input(
            "remote_phonopy",
            serializer=to_aiida_type,
            default=lambda: orm.Bool(False),
            help="Run phonopy as a remote code.",
        )
        spec.input(
            "symmetry_tolerance",
            serializer=to_aiida_type,
            valid_type=orm.Float,
            default=lambda: orm.Float(1e-5),
        )
        spec.input(
            "subtract_residual_forces",
            serializer=to_aiida_type,
            valid_type=orm.Bool,
            default=lambda: orm.Bool(False),
        )
        spec.input(
            "structure",
            valid_type=orm.StructureData,
            help="Structure of which the phonons should calculated",
        )
        spec.input(
            "phonon_settings",
            serializer=to_aiida_type,
            valid_type=orm.Dict,
            validator=PhononSettings.validate_dict,
            help="Settings for the underlying phonopy calculations",
        )
        spec.input(
            "phonon_code",
            valid_type=orm.Code,
            help="Code for the phonopy for remote calculations",
            required=False,
        )
        spec.input(
            "options",
            serializer=to_aiida_type,
            valid_type=orm.Dict,
            help="Options for the remote phonopy calculation",
            required=False,
        )
        spec.input(
            "reuse_supercell_calc",
            valid_type=orm.Str,
            serializer=to_aiida_type,
            validator=validate_reuse_supercell_calc,
            required=False,
            help=(
                "Perform calculation for the perfect supercell and use its CHGCAR to bootstrap displacement calculations."
                "Choose from: <retrieve> or <restart>"
            ),
        )

        # Phonon specific outputs
        spec.output("force_constants", valid_type=orm.ArrayData, required=False)
        spec.output("primitive", valid_type=orm.StructureData, required=False)
        spec.output("supercell", valid_type=orm.StructureData, required=False)
        spec.output("force_sets", valid_type=orm.ArrayData, required=False)
        spec.output("nac_params", valid_type=orm.ArrayData, required=False)
        spec.output("thermal_properties", valid_type=orm.XyData, required=False)
        spec.output("band_structure", valid_type=orm.BandsData, required=False)
        spec.output("dos", valid_type=orm.XyData, required=False)
        spec.output("pdos", valid_type=orm.XyData, required=False)
        spec.output("phonon_setting_info", valid_type=orm.Dict, required=True)
        spec.output(
            "relaxed_structure",
            valid_type=orm.StructureData,
            required=False,
            help="The output structure of the high precision relaxation, used for phonon calculations.",
        )

        spec.exit_code(501, "ERROR_RELAX_FAILURE", message="Initial relaxation has failed!")

    def setup(self):
        """Setup the workspace"""
        # Current structure for calculations
        self.ctx.current_structure = self.inputs.structure
        self.ctx.label = self.inputs.metadata.get("label", "")

    def should_run_relax(self):
        if "relax" in self.inputs:
            return True
        self.report("Not performing relaxation - assuming the input structure is fully relaxed.")
        return False

    def should_run_supercell_chgcar(self):
        """Wether to run additional supercell calculation to get CHGCAR"""
        value = self.inputs.get("reuse_supercell_calc", None)
        return bool(value)

    def run_relaxation(self):
        """Perform high-precision relaxation of the initial structure"""

        inputs = self.exposed_inputs(self._relax_chain, "relax")
        inputs.structure = self.inputs.structure
        inputs.metadata.call_link_label = "high_prec_relax"
        if "label" not in inputs.metadata or (not inputs.metadata.label):
            inputs.metadata.label = self.ctx.label + " HIGH-PREC RELAX"

        running = self.submit(self._relax_chain, **inputs)

        self.report(f"Submitted high-precision relaxation {running}")
        return ToContext(relax_calc=running)

    def inspect_relaxation(self):
        """Check if the relaxation finished OK"""
        if "relax_calc" not in self.ctx:
            raise RuntimeError("Relaxation workchain not found in the context")

        workchain = self.ctx.relax_calc
        if not workchain.is_finished_ok:
            self.report("Relaxation finished with error, abort further actions")
            return self.exit_codes.ERROR_RELAX_FAILURE  # pylint: disable=no-member

        # All OK
        self.ctx.current_structure = workchain.outputs.relax__structure  # NOTE: this is workchain specific
        self.report("Relaxation finished OK, recorded the relaxed structure")
        self.out("relaxed_structure", self.ctx.current_structure)

    def create_displacements(self):
        """Create displacements using phonopy"""

        self.report("Creating displacements")
        phonon_settings = self.inputs.phonon_settings.get_dict()

        # Check if we are doing magnetic calculations
        force_calc_inputs = self.exposed_inputs(self._singlepoint_chain, "singlepoint")

        if self.should_run_relax():
            relax_calc_inputs = self.exposed_inputs(self._relax_chain, "relax")
            # Fetch the magmom from the relaxation calculation (eg. for the starting structure)
            try:
                magmom = relax_calc_inputs.vasp.parameters[OVERRIDE_NAMESPACE].get("magmom")
            except AttributeError:
                magmom = None
        else:
            magmom = None

        # MAGMOM tag in the phonon_settings input port take the precedence
        if magmom and ("magmom" not in phonon_settings):
            self.report("Using MAGMOM from the inputs for the relaxation calculations")
            phonon_settings["magmom"] = magmom
            phonon_settings_dict = orm.Dict(dict=phonon_settings)
        else:
            phonon_settings_dict = self.inputs.phonon_settings

        if "supercell_matrix" not in phonon_settings:
            raise RuntimeError("Must supply 'supercell_matrix' in the phonon_settings input.")

        kwargs = {}
        return_vals = generate_phonopy_cells(
            phonon_settings_dict,
            self.ctx.current_structure,
            self.inputs.symmetry_tolerance,
            **kwargs,
        )

        # Store these in the context and set the output
        for key in ("phonon_setting_info", "primitive", "supercell"):
            self.ctx[key] = return_vals[key]
            self.out(key, self.ctx[key])

        self.ctx.supercell_structures = {}

        for key in return_vals:
            if "supercell_" in key:
                self.ctx.supercell_structures[key] = return_vals[key]

        if self.inputs.subtract_residual_forces:
            # The 000 structure is the original supercell
            digits = len(str(len(self.ctx.supercell_structures)))
            label = "supercell_{}".format("0".zfill(digits))
            self.ctx.supercell_structures[label] = return_vals["supercell"]

        self.report("Supercells for phonon calculations created")

    def run_supercell_chgcar(self):
        """Run a supercell calculatio to boot strap the calculations"""

        calc_inputs = self.exposed_inputs(self._singlepoint_chain, "singlepoint")
        calc_inputs.structure = self.ctx.supercell

        # For magnetisation
        magmom = self.ctx.phonon_setting_info.get_dict().get("_supercell_magmom")
        if magmom:
            self.report("Using MAGMOM from the phonopy output")
            param = calc_inputs.parameters.get_dict()
            param[OVERRIDE_NAMESPACE]["magmom"] = magmom
            calc_inputs.parameters = orm.Dict(dict=param)

        # Ensure either the chgcar is retrieved or the remote workdir is not cleaned
        if self.inputs.reuse_supercell_calc.value == "retrieve":
            ensure_parse_objs(calc_inputs, ["chgcar"])
            ensure_retrieve_objs(calc_inputs, ["CHGCAR"], temp=False)
        else:
            # Ensure that the remote folder is not cleaned
            calc_inputs.clean_workdir = orm.Bool(False)

        # Make sure the calculation writes CHGCAR and WAVECAR
        # Turn off WAVECAR - otherwise it may take too much disk space
        calc_inputs.parameters = nested_update_dict_node(
            calc_inputs.parameters,
            {OVERRIDE_NAMESPACE: {"lcharg": True, "lwave": False}},
        )

        calc_inputs.metadata.label = self.ctx.label + " SUPERCELL"
        calc_inputs.metadata.call_link_label = "supercell_calc"
        running = self.submit(self._singlepoint_chain, **calc_inputs)
        self.report("Submitted {} for {}".format(running, "Supercell calculation"))
        self.to_context(supercell_calc=running)

    def inspect_supercell_calc(self):
        """Check if the supercell calculation went OK"""
        if "supercell_calc" not in self.ctx:
            raise RuntimeError("Supercell workchain not found in the context")

        workchain = self.ctx.supercell_calc
        if not workchain.is_finished_ok:
            self.report("Supercell calculation finished with error, abort further actions")
            return self.exit_codes.ERROR_RELAX_FAILURE  # pylint: disable=no-member

        if "chgcar" in workchain.outputs:
            self.ctx.supercell_chgcar = workchain.outputs.chgcar
            self.ctx.supercell_remote_folder = None
            self.report("Supercell calculation finished OK, recorded the CHGCAR")
        else:
            self.ctx.supercell_chgcar = None
            self.ctx.supercell_remote_folder = workchain.outputs.remote_folder
            self.report("Supercell calculation finished OK, will reuse the restart folder")
        return None

    def run_force_and_nac_calcs(self):
        """Submit the force and non-analytical correction calculations"""
        # Forces
        force_calc_inputs = self.exposed_inputs(self._singlepoint_chain, "singlepoint")

        # Set the CHGCAR or restart folder
        if self.should_run_supercell_chgcar():
            # Set start from constant charge
            force_calc_inputs.parameters = nested_update_dict_node(force_calc_inputs.parameters, {"charge": {"from_charge": True}})
            # Supply the inputs if needed
            if self.ctx.supercell_chgcar:
                force_calc_inputs.chgcar = self.ctx.chgcar
            else:
                force_calc_inputs.restart_folder = self.ctx.supercell_remote_folder

        magmom = self.ctx.phonon_setting_info.get_dict().get("_supercell_magmom")
        if magmom:
            self.report("Using MAGMOM from the phonopy output")
            param = force_calc_inputs.parameters.get_dict()
            param[OVERRIDE_NAMESPACE]["magmom"] = magmom
            force_calc_inputs.parameters = orm.Dict(dict=param)

        # Ensure we parser the forces
        ensure_parse_objs(force_calc_inputs, ["forces"])

        for key, node in self.ctx.supercell_structures.items():
            label = "force_calc_" + key.split("_")[-1]
            force_calc_inputs.structure = node
            force_calc_inputs.metadata.call_link_label = label
            # Set the label of the force calculation
            force_calc_inputs.metadata.label = self.ctx.label + " FC_" + key.split("_")[-1]

            running = self.submit(self._singlepoint_chain, **force_calc_inputs)

            self.report(f"Submitted {running} for {label}")
            self.to_context(**{label: running})

        if self.is_nac():
            self.report("calculate born charges and dielectric constant")
            nac_inputs = self.exposed_inputs(self._singlepoint_chain, "nac")
            # NAC needs to use the primitive structure!
            nac_inputs.structure = self.ctx.primitive
            nac_inputs.metadata.call_link_label = "nac_calc"
            if "label" not in nac_inputs.metadata or (not nac_inputs.metadata.label):
                nac_inputs.metadata.label = self.ctx.label + " NAC"
            ensure_parse_objs(nac_inputs, ["dielectrics", "born_charges"])

            running = self.submit(self._singlepoint_chain, **nac_inputs)
            self.report(f"Submitted calculation for nac: {running}")
            self.to_context(**{"born_and_epsilon_calc": running})

    def check_wavecar_chgcar(self):
        """Check if WAVECAR and CHGCAR exits and valid in the remote folder"""
        remote = self.ctx.supercell_remote_folder
        if not remote:
            return False, False
        content = remote.listdir_withattributes()
        has_wavecar = False
        has_chgcar = False
        for entry in content:
            if entry["name"] == "WAVECAR" and entry["attributes"].st_size > 10:
                has_wavecar = True
            if entry["name"] == "CHGCAR" and entry["attributes"].st_size > 10:
                has_wavecar = True
        return has_wavecar, has_chgcar

    def create_force_set_and_constants(self):
        """Create the force set and constants from the finished calculations"""

        self.report("Creating force set and nac (if applicable)")
        forces_dict = collect_vasp_forces_and_energies(self.ctx, self.ctx.supercell_structures, "force_calc")

        # Will set force_sets, supercell_forces, supercell_energy - the latter two are optional
        for key, value in get_vasp_force_sets_dict(**forces_dict).items():
            self.ctx[key] = value
            self.out(key, self.ctx[key])

        if self.is_nac():

            self.report("Create nac data")
            calc = self.ctx.born_and_epsilon_calc
            # NOTE: this is VASP specific outputs -- but I can implement the same for CASTEP plugin
            if isinstance(calc, dict):  # For imported calculations - not used here
                calc_dict = calc
                structure = calc["structure"]
            else:
                calc_dict = calc.outputs
                structure = calc.inputs.structure

            if "born_charges" not in calc_dict:
                raise RuntimeError("Born effective charges could not be found " "in the calculation. Please check the calculation setting.")
            if "dielectrics" not in calc_dict:
                raise RuntimeError("Dielectric constant could not be found " "in the calculation. Please check the calculation setting.")

            self.ctx.nac_params = get_nac_params(
                calc_dict["born_charges"],
                calc_dict["dielectrics"],
                structure,
                self.inputs.symmetry_tolerance,
            )
            self.out("nac_params", self.ctx.nac_params)

    def run_phonopy_remote(self):
        """Run phonopy as remote code"""
        self.report("run remote phonopy calculation")

        code_string = self.inputs.code_string.value
        builder = orm.load_code(code_string).get_builder()
        builder.structure = self.ctx.current_structure
        builder.settings = self.ctx.phonon_setting_info  # This was generated by the earlier call
        builder.metadata.options.update(self.inputs.options)
        builder.metadata.label = self.ctx.label
        builder.force_sets = self.ctx.force_sets  # Generated earlier
        if "nac_params" in self.ctx:
            builder.nac_params = self.ctx.nac_params
            builder.primitive = self.ctx.primitive
        future = self.submit(builder)

        self.report(f"Submitted phonopy calculation: {future.pk}")
        self.to_context(**{"phonon_properties": future})

    def create_force_constants(self):
        self.report("Creating force constants")

        self.ctx.force_constants = get_force_constants(
            self.ctx.current_structure,
            self.ctx.phonon_setting_info,
            self.ctx.force_sets,
        )
        self.out("force_constants", self.ctx.force_constants)

    def run_phonopy_local(self):
        """
        Run phonopy in the local interpreter.
        WARRNING! This could put heavy strain on the local python process and
        potentially affect the daemon worker executions. Long running time
        can make the work lose contact with the daemon and give rise to double
        execution problems. USE WITH CAUTION
        """
        self.report("Perform phonopy calculation in workchain")

        params = {}
        if "nac_params" in self.ctx:
            params["nac_params"] = self.ctx.nac_params
        result = get_phonon(
            self.ctx.current_structure,
            self.ctx.phonon_setting_info,
            self.ctx.force_constants,
            **params,
        )
        self.out("thermal_properties", result["thermal_properties"])
        self.out("dos", result["dos"])
        self.out("band_structure", result["band_structure"])

        self.report("Completed local phonopy calculation, workchain finished.")

    def collect_remote_run_data(self):
        """Collect the data from a remote phonopy run"""
        self.report("Collecting  data from a remote phonopy run")
        ph_props = (
            "thermal_properties",
            "dos",
            "pdos",
            "band_structure",
            "force_constants",
        )

        for prop in ph_props:
            if prop in self.ctx.phonon_properties.outputs:
                self.out(prop, self.ctx.phonon_properties.outputs[prop])

        self.report("Completed collecting remote phonopy data, workchain finished.")

    def is_nac(self):
        """
        Check if nac calculations should be performed.
        Returns trun if the 'nac' input namespace exists.
        """
        return bool(self.inputs.get("nac"))

    def remote_phonopy(self):
        """Weither to run phonopy as a remote code"""
        node = self.inputs.remote_phonopy
        return bool(node)


def collect_vasp_forces_and_energies(ctx, ctx_supercells, prefix="force_calc", obj=None):
    """
    Collect forces and energies from VASP calculations.
    This is essentially for pre-process before dispatching to the calcfunction for creating
    the force_set

    Returns:
        A dictionary with keys like "forces_<num>" and "misc_<num>", mapping to aiida nodes
    """
    forces_dict = {}
    for key in ctx_supercells:
        # key: e.g. "supercell_001", "phonon_supercell_001"
        num = key.split("_")[-1]  # e.g. "001"
        calc = ctx[f"{prefix}_{num}"]

        # Also works for imported calculations
        if type(calc) is dict:
            calc_dict = calc
        else:
            calc_dict = calc.outputs
        if "forces" not in calc_dict:
            msg = "Force not found in the VaspWorkChain - trying to recover using the last called VaspCalculation - procedd with caution."
            if obj:
                obj.report(msg)
            else:
                print(msg)
            calc_node = calc.called[0]
            if "forces" in calc_node.outputs:
                calc_dict = calc_node.outputs

        if "forces" in calc_dict and "final" in calc_dict["forces"].get_arraynames():
            forces_dict[f"forces_{num}"] = calc_dict["forces"]
        else:
            raise RuntimeError(f"Forces could not be found in calculation {num}.")

        if "misc" in calc_dict and "total_energies" in calc_dict["misc"].keys():  # needs .keys() - calc_dict can be a dict or a LinkManager
            forces_dict[f"misc_{num}"] = calc_dict["misc"]

    return forces_dict


class PhononSettings(OptionContainer):
    """Options for phonon_settings input"""

    supercell_matrix = ListOption("Supercell matrix for phonons", required=True)
    primitive_matrix = ListOrStringOption(
        'Primitive matrix for phonons, can be set to "auto"',
        required=True,
    )
    mesh = IntOption("Mesh for phonon calculation", required=True)
    magmom = ListOption("Starting magnetic moments for phonopy", required=False)
    distance = FloatOption("Distance for band structure", required=False)


def ensure_parse_objs(input_port, objs):
    """
    Ensure parser will parse certain objects

    Arguments:
        input_port: input port to be update, assume the existence of `settings`
        objs: a list of objects to include, for example ['structure', 'forces']

    Returns:
        process_port: the port with the new settings
    """
    update = {"parser_settings": {f"add_{obj}": True for obj in objs}}
    if "settings" not in input_port:
        input_port.settings = orm.Dict(dict=update)
    else:
        settings = input_port.settings
        settings = nested_update_dict_node(settings, update)
        input_port.settings = settings
    return input_port


def ensure_retrieve_objs(input_port, fnames, temp=False):
    """
    Ensure files to be retrieved

    Arguments:
        input_port: input port to be update, assume the existence of `settings`
        fnames: a list of file names to include, for example ['CHGCAR', 'WAVECAR']

    Returns:
        process_port: the port with the new settings
    """
    if temp:
        update = {"ADDITIONAL_RETRIEVE_TEMPORARY_LIST": fnames}
    else:
        update = {"ADDITIONAL_RETRIEVE_LIST": fnames}
    if "settings" not in input_port:
        input_port.settings = orm.Dict(dict=update)
    else:
        settings = input_port.settings
        nested_update_dict_node(settings, update)
        input_port.settings = settings
    return input_port


def validate_reuse_supercell_calc(node, port=None):
    """Validate the reuse_supercell_calc port"""
    if not node:
        return
    if not node.value in ["restart", "retrieve"]:
        raise InputValidationError("Valid options for <reuse_supercell_calc> are: 'retrieve' and 'restart'")
