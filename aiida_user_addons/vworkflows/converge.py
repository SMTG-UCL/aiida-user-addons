"""
Convergence workchain.

----------------------
Intended to be used to control convergence checks for plane-wave calculations.


#TODO
* Change convergence criteria to be per-atom
* Test displacement convergence
* More verbose
"""
import copy

# pylint: disable=too-many-lines, too-many-locals, too-many-statements, too-many-public-methods, too-many-branches, attribute-defined-outside-init
from tempfile import mkdtemp

import numpy as np
from aiida.common.extendeddicts import AttributeDict
from aiida.engine import WorkChain, append_, calcfunction, if_
from aiida.orm.nodes.data.array.bands import find_bandgap
from aiida.plugins import DataFactory, WorkflowFactory
from aiida_vasp.utils.aiida_utils import (
    compressed_structure,
    displaced_structure,
    get_data_class,
    get_data_node,
)
from aiida_vasp.utils.extended_dicts import update_nested_dict
from aiida_vasp.utils.workchains import (
    compose_exit_code,
    fetch_k_grid,
    prepare_process_inputs,
)


class RapidConvergeWorkChain(WorkChain):
    """
    A workchain to perform convergence tests.
    Modified from the original VaspConverge WorkChain
    """

    _verbose = False
    _next_workchain_string = "vasp.relax"
    _next_workchain = WorkflowFactory(_next_workchain_string)

    _ALLOWED_CUTOFF_TYPES = {"energy": 0, "forces": 1, "vbm": 2, "gap": 3}

    @classmethod
    def define(cls, spec):
        super().define(spec)
        spec.expose_inputs(
            cls._next_workchain,
            exclude=("kpoints", "parameters", "structure", "settings", "relax"),
        )
        spec.input("parameters", valid_type=get_data_class("dict"))
        spec.input(
            "structure", valid_type=(get_data_class("structure"), get_data_class("cif"))
        )
        spec.input(
            "kpoints", valid_type=get_data_class("array.kpoints"), required=False
        )
        spec.input("settings", valid_type=get_data_class("dict"), required=False)
        spec.input_namespace("relax", required=False, dynamic=True)
        spec.input(
            "converge.pwcutoff",
            valid_type=get_data_class("float"),
            required=False,
            help="""
                   The plane-wave cutoff to be used during convergence tests in electron volts.
                   """,
        )
        spec.input(
            "converge.kgrid",
            valid_type=get_data_class("list"),
            required=False,
            help="""
                   The k-point grid to be used during convergence tests.
                   """,
        )
        spec.input(
            "converge.pwcutoff_start",
            valid_type=get_data_class("float"),
            required=False,
            default=lambda: get_data_node("float", 200.0),
            help="""
                   The plane-wave cutoff in electron volts.
                   """,
        )
        spec.input(
            "converge.pwcutoff_step",
            valid_type=get_data_class("float"),
            required=False,
            default=lambda: get_data_node("float", 50.0),
            help="""
                   The plane-wave cutoff step (increment) in electron volts.
                   """,
        )
        spec.input(
            "converge.pwcutoff_samples",
            valid_type=get_data_class("int"),
            required=False,
            default=lambda: get_data_node("int", 10),
            help="""
                   The number of plane-wave cutoff samples.
                   """,
        )
        spec.input(
            "converge.k_dense",
            valid_type=get_data_class("float"),
            required=False,
            default=lambda: get_data_node("float", 0.20),
            help="""
                   The target k-point stepping at the densest grid in inverse AA.
                   """,
        )
        spec.input(
            "converge.k_coarse",
            valid_type=get_data_class("float"),
            required=False,
            default=lambda: get_data_node("float", 0.50),
            help="""
                   The target k-point stepping at the coursest grid in inverse AA.
                   """,
        )
        spec.input(
            "converge.k_spacing",
            valid_type=get_data_class("float"),
            required=False,
            default=lambda: get_data_node("float", 0.30),
            help="""
                   The default k-point spacing in inverse AA.
                   """,
        )
        spec.input(
            "converge.k_samples",
            valid_type=get_data_class("int"),
            required=False,
            default=lambda: get_data_node("int", 10),
            help="""
                   The number of k-point samples.
                   """,
        )
        spec.input(
            "converge.cutoff_type",
            valid_type=get_data_class("str"),
            required=False,
            default=lambda: get_data_node("str", "energy"),
            help="""
                   The cutoff_type to check convergence against. Currently the following
                   options are accepted:
                   * energy
                   * gap
                   * vbm (not yet currently supported)
                   * forces
                   """,
        )
        spec.input(
            "converge.cutoff_value",
            valid_type=get_data_class("float"),
            required=False,
            default=lambda: get_data_node("float", 0.01),
            help="""
                   The cutoff value to be used. When the difference between two convergence
                   calculations are within this value for ``cutoff_type``, then it is
                   considered converged.
                   """,
        )
        spec.input(
            "converge.cutoff_value_r",
            valid_type=get_data_class("float"),
            required=False,
            default=lambda: get_data_node("float", 0.01),
            help="""
                   The relative cutoff value to be used. When the difference between two convergence
                   calculations are within this value for ``cutoff_type``, then it is
                   considered converged. However, in this case the cutoff value is the difference
                   between `cutoff_type` for the input structure and an atomic displacement or a
                   compression of the unitcell.
                   """,
        )
        spec.input(
            "converge.compress",
            valid_type=get_data_class("bool"),
            required=False,
            default=lambda: get_data_node("bool", False),
            help="""
                   If True, a convergence test of the compressed structure is also
                   performed. The difference of the ``cutoff_type`` values for each
                   calculations are evaluated and when the difference between these are
                   less than ``cutoff_value_r``, the calculation is considered converged.
                   The largest planw-wave cutoff and densest k-point grid are used.
                   """,
        )
        spec.input(
            "converge.displace",
            valid_type=get_data_class("bool"),
            required=False,
            default=lambda: get_data_node("bool", False),
            help="""
                   If True, a convergence test of the displaced structure is also
                   performed. The difference of the ``cutoff_type`` values for each
                   calculations are evaluated and when the difference between these are
                   less than ``cutoff_value_r``, the calculation is considered converged.
                   The largest planw-wave cutoff and densest k-point grid are used.
                   """,
        )
        spec.input(
            "converge.displacement_vector",
            valid_type=get_data_class("list"),
            required=False,
            default=lambda: get_data_node("list", list=[1.0, 1.0, 1.0]),
            help="""The displacement unit vector for the displacement test. Sets the direction
            of displacement.""",
        )
        spec.input(
            "converge.displacement_distance",
            valid_type=get_data_class("float"),
            required=False,
            default=lambda: get_data_node("float", 0.2),
            help="""
                   The displacement distance (L2 norm) for the displacement test in AA. Follows
                   the direction of ``displacement_vector``.
                   """,
        )
        spec.input(
            "converge.displacement_atom",
            valid_type=get_data_class("int"),
            required=False,
            default=lambda: get_data_node("int", 1),
            help="""
                   Which atom to displace? Index starts from 1 and follows the sequence for the
                   sites in the Aiida ``structure`` object.
                   """,
        )
        spec.input(
            "converge.volume_change",
            valid_type=get_data_class("list"),
            required=False,
            default=lambda: get_data_node("list", list=[1.05, 1.05, 1.05]),
            help="""
                   The volume change in direct coordinates for each lattice vector.
                   """,
        )
        spec.input(
            "converge.relax",
            valid_type=get_data_class("bool"),
            required=False,
            default=lambda: get_data_node("bool", False),
            help="""
                   If True, we relax for each convergence test.
                   """,
        )
        spec.input(
            "converge.total_energy_type",
            valid_type=get_data_class("str"),
            required=False,
            default=lambda: get_data_node("str", "energy_no_entropy"),
            help="""
                   The energy type that is used when ``cutoff_type`` is set to `energy`.
                   Consult the options available in the parser for the current version.
                   """,
        )
        spec.input(
            "converge.testing",
            valid_type=get_data_class("bool"),
            required=False,
            default=lambda: get_data_node("bool", False),
            help="""
                   If True, we assume testing to be performed (e.g. dummy calculations).
                   """,
        )

        spec.outline(
            cls.initialize,
            if_(cls.run_conv_calcs)(
                if_(cls.run_pw_conv_calcs)(
                    cls.run_pw_conv_many,
                    cls.save_pw_conv_many,
                ),
                cls.analyze_pw_conv_many,
                if_(cls.run_kpoints_conv_calcs)(
                    cls.run_kpoints_conv_many,
                    cls.save_kpoints_conv_many,
                ),
                cls.init_disp_conv,
                if_(cls.run_pw_conv_disp_calcs)(
                    cls.run_pw_conv_many,
                    cls.save_pw_conv_many,
                ),
                if_(cls.analyze_pw_after_disp)(
                    cls.analyze_pw_conv_many,
                ),
                if_(cls.run_kpoints_conv_disp_calcs)(
                    cls.run_kpoints_conv_many,
                    cls.save_kpoints_conv_many,
                ),
                cls.init_comp_conv,
                if_(cls.run_pw_conv_comp_calcs)(
                    cls.run_pw_conv_many,
                    cls.save_pw_conv_many,
                ),
                if_(cls.analyze_pw_after_comp)(
                    cls.analyze_pw_conv_many,
                ),
                if_(cls.run_kpoints_conv_comp_calcs)(
                    cls.run_kpoints_conv_many,
                    cls.save_kpoints_conv_many,
                ),
                cls.analyze_conv,
                cls.store_conv,
            ),
            cls.init_converged,
            cls.init_next_workchain,
            cls.run_next_workchain,
            cls.verify_next_workchain,
            cls.results,
            cls.finalize,
        )  # yapf: disable

        spec.expose_outputs(cls._next_workchain)
        spec.output("converge.data", valid_type=get_data_class("array"), required=False)
        spec.exit_code(0, "NO_ERROR", message="the sun is shining")
        spec.exit_code(
            500,
            "ERROR_UNKNOWN",
            message="unknown error detected in the converge workchain",
        )
        spec.exit_code(
            420, "ERROR_NO_CALLED_WORKCHAIN", message="no called workchain detected"
        )

    def initialize(self):
        """Initialize."""

        # Initialise the context - where the internal parameters are save and
        # checkpointed between each methods defined in the outline being called
        self._init_context()

        # Initialise the inputs
        self.ctx.inputs.parameters = self._init_parameters()
        try:
            self._verbose = self.inputs.verbose.value
            self.ctx.inputs.verbose = self.inputs.verbose
        except AttributeError:
            pass

        # Initialise convergence related tasks
        # Fetch a temporary StructureData and Dict that we will use throughout,
        # overwrite previous inputs (they are still stored in self.inputs for later ref).
        # Since we cannot execute a calc (that seals the node on completion) we store
        # these in converge instead of input and copy them over when needed.
        if (
            self.ctx.inputs.parameters.converge.compress
            or self.ctx.inputs.parameters.converge.displace
        ):
            # Only copy if we are going to change the structure
            self.ctx.converge.structure = self.inputs.structure.clone()
        else:
            self.ctx.converge.structure = self.inputs.structure
        # Also create a dummy KpointsData in order to calculate the reciprocal
        # unit cell
        kpoints = get_data_class("array.kpoints")()
        kpoints.set_kpoints_mesh([1, 1, 1])
        kpoints.set_cell_from_structure(self.ctx.converge.structure)
        self.ctx.converge.kpoints = kpoints

        # Initialise speific pw/kpoints tasks
        self._init_pw_conv()
        self._init_kpoints_conv()

        # Make sure the parser settings at least contains 'add_bands' and the correct
        # output_params settings.
        if self.run_conv_calcs():
            # This case we are running convergence calculations
            dict_entry = {
                "add_bands": True,
                "output_params": ["total_energies", "maximum_force"],
            }
            compress = False
            displace = False
            try:
                compress = self.inputs.converge.compress.value
            except AttributeError:
                pass
            try:
                displace = self.inputs.converge.displace.value
            except AttributeError:
                pass
            if compress or displace:
                dict_entry.update({"add_structure": True})
            if "settings" in self.inputs:
                settings = AttributeDict(self.inputs.settings.get_dict())
                try:
                    settings.parser_settings.update(dict_entry)
                except AttributeError:
                    settings.parser_settings = dict_entry
            else:
                settings = AttributeDict({"parser_settings": dict_entry})
            self.ctx.inputs.settings = settings
        else:
            # This case we are not running convergence calculations
            # Pass it through
            if "settings" in self.inputs:
                self.ctx.inputs.settings = AttributeDict(
                    self.inputs.settings.get_dict()
                )

    def _init_parameters(self):
        """
        Collect input to the workchain in the converge namespace and put that into the parameters.
        The sets of parameters initialised may be attached to ctx.inputs and ctx.converge.paraemters.
        The latter is a staging area for convergence calcualtions, while the former is used by the
        immediate sub-workchin
        """

        # At some point we will replace this with possibly input checking using the PortNamespace on
        # a dict parameter type. As such we remove the workchain input parameters as node entities. Much of
        # the following is just a workaround until that is in place in AiiDA core.

        # First collect input that is under the converge namespace defined on the workchain itself and
        # put that into parameters.
        parameters = AttributeDict()
        parameters.converge = AttributeDict()
        for key, item in self.inputs.converge.items():
            # Make sure we unwrap the AiiDA data nodes
            if isinstance(item, DataFactory("array")):
                self.report(f"Key <{key}> is a ArrayData which cannot be unwrapped")
                raise RuntimeError
            elif isinstance(item, DataFactory("list")):
                parameters.converge[key] = item.get_list()
            else:
                # Assume only Str, Int, Float except ArrayData
                parameters.converge[key] = item.value
        # Second collect input that is under the relax namespace defined on the workchain itself and
        # put that into parameters.
        parameters.relax = AttributeDict()
        for key, item in self.inputs.relax.items():
            # Make sure we unwrap the AiiDA data nodes
            if isinstance(item, DataFactory("array")):
                self.report(f"Key <{key}> is a ArrayData which cannot be unwrapped")
                parameters.relax[key] = item.get_array("array")
                raise RuntimeError
            elif isinstance(item, DataFactory("list")):
                parameters.converge[key] = item.get_list()
            else:
                # Assume only Str, Int, Float except ArrayData
                parameters.relax[key] = item.value

        # Now get the input parameters and update the dictionary. This means,
        # any supplied parameters in the converge namespace will override what is supplied to the workchain
        # in the converge and relax namespace.
        try:
            input_parameters = AttributeDict(self.inputs.parameters.get_dict())
            # We cannot use update here, as we only want to replace each key if it exists, if a key
            # contains a new dict we need to traverse that, hence we have a function to perform this update
            update_nested_dict(parameters, input_parameters)
        except AttributeError:
            pass

        return parameters

    def _init_context(self):
        """Initialize context variables that are used during the logical flow of the BaseRestartWorkChain."""

        # Stanard context
        self.ctx.exit_code = self.exit_codes.ERROR_UNKNOWN  # pylint: disable=no-member
        self.ctx.workchains = []
        self.ctx.inputs = AttributeDict()
        self.ctx.set_input_nodes = True

        # Converge related context
        self.ctx.inputs_next = (
            AttributeDict()
        )  # This is the namespace that gets submitted
        self.ctx.converge = AttributeDict()  # Store converge related data
        # Stores settings of the converge operations
        # This settings is not the setting passed to the calculation
        self.ctx.converge.settings = AttributeDict()

        # PW convergence context
        settings = self.ctx.converge.settings
        self.ctx.running_pw = False
        self.ctx.pw_workchains = []
        self.ctx.converge.pw_data = None
        self.ctx.converge.run_pw_conv_calcs = False
        self.ctx.converge.run_pw_conv_calcs_org = False
        self.ctx.converge.pwcutoff_sampling = []
        self.ctx.converge.pw_iteration = 0
        self.ctx.pw_workchains_uuids = []
        self.ctx.pw_workchains = []
        settings.pwcutoff = None
        settings.pwcutoff_list = []
        # Set supplied pwcutoff
        try:
            settings.pwcutoff = copy.deepcopy(self.inputs.converge.pwcutoff.value)
        except AttributeError:
            pass
        # Check if pwcutoff is supplied in the parameters input, this takes presence over
        # the pwcutoff supplied in the inputs
        parameters_dict = self.inputs.parameters.get_dict()
        ppwcutoff = parameters_dict.get("encut", None)
        if not ppwcutoff:
            ppwcutoff = parameters_dict.get("ENCUT", None)
        if ppwcutoff:
            settings.pwcutoff = ppwcutoff
        # We need a copy of the original pwcutoff as we will modify it
        self.ctx.converge.settings.pwcutoff_org = copy.deepcopy(settings.pwcutoff)

        # Initialize kpoints related context
        settings = self.ctx.converge.settings
        self.ctx.running_kpoints = False
        self.ctx.kpoints_workchains = []
        self.ctx.converge.k_data = None
        self.ctx.converge.run_kpoints_conv_calcs = False
        self.ctx.converge.run_kpoints_conv_calcs_org = False
        self.ctx.converge.kpoints_iteration = 0
        self.ctx.converge.k_sampling = []
        self.ctx.kpoints_workchains_uuids = []
        settings.kgrid = None
        settings.kgrid_list = []
        # We need a special flag that lets us know that we have supplied
        # a k-point grid (e.g. then we do not have access to the grid sampling
        # etc. during user information etc.). Also, the user might want to run
        # plane wave cutoff tests with custom k-point grids. This takes
        # presence over a supplied `kgrid` setting.
        settings.supplied_kmesh = True
        try:
            self.inputs.kpoints
        except AttributeError:
            # No kpoints supplied, is there kgrid passed as array at converge.kgrid
            settings.supplied_kmesh = False
            try:
                settings.kgrid = self.inputs.converge.kgrid.get_list()
            except AttributeError:
                # Here settings.kgrid is None
                pass
        # We need a copy of the original kgrid as we will modify it
        if settings.kgrid is not None:
            self.ctx.converge.settings.kgrid_org = settings.kgrid
        else:
            self.ctx.converge.settings.kgrid_org = None

        # Debug
        self.ctx.debug = AttributeDict(
            dictionary=dict(wkdir=mkdtemp(), label_counts={})
        )
        self.report(f"Debug checkpoint dir: {self.ctx.debug.wkdir}")

    def _init_pw_conv(self):
        """Initialize the plane wave convergence tests."""

        converge = self.ctx.converge
        settings = converge.settings
        supplied_kmesh = settings.supplied_kmesh
        pwcutoff_org = settings.pwcutoff_org
        kgrid_org = settings.kgrid_org
        pwcutoff_start = self.ctx.inputs.parameters.converge.pwcutoff_start
        pwcutoff_step = self.ctx.inputs.parameters.converge.pwcutoff_step
        pwcutoff_samples = self.ctx.inputs.parameters.converge.pwcutoff_samples
        # Detect what kind of convergence tests that needs to be run.
        if pwcutoff_org is None:
            # No pwcutoff supplied, run plane wave convergence tests.
            converge.pw_data = []
            # Clone the input parameters if we have no pwcutoff,
            # we will inject this into the parameters as we go
            converge.parameters = self._init_parameters()
            if not supplied_kmesh and kgrid_org is None:
                self._set_default_kgrid()
            # Turn on plane wave convergene tests.
            converge.run_pw_conv_calcs = True
            converge.run_pw_conv_calcs_org = True
            # make pwcutoff test vector
            converge.pwcutoff_sampling = [
                pwcutoff_start + x * pwcutoff_step for x in range(pwcutoff_samples)
            ]

    def _init_kpoints_conv(self):
        """
        Initialize the kpoints convergence tests.
        Logic:
          If there is as supplied k-mesh, then we do not run kpoints convergence
          tests - (In the future this may be overriden...)
          Otherwise, generated k_spacing list and kgrids list and store them in the
          context.
        """

        converge = self.ctx.converge
        settings = converge.settings
        kgrid_org = settings.kgrid_org
        supplied_kmesh = settings.supplied_kmesh
        # If not kpoints supplied at all - run the conergence test
        if not supplied_kmesh and kgrid_org is None:
            converge.k_data = []

            # No kpoint grid supplied, run kpoints convergence tests.
            converge.run_kpoints_conv_calcs = True
            converge.run_kpoints_conv_calcs_org = True

            # Make kpoint test vectors.
            # Usually one expect acceptable convergence with a
            # step size of 0.1/AA, typically:
            # 8 AA lattice vector needs roughly 8 kpoints.
            # 4 AA lattice vector needs roughly 16 kpoints etc.
            # Start convergence test with a step size of 0.5/AA,
            # round values up.
            stepping = (
                self.ctx.inputs.parameters.converge.k_coarse
                - self.ctx.inputs.parameters.converge.k_dense
            ) / self.ctx.inputs.parameters.converge.k_samples
            converge.k_sampling = [
                self.ctx.inputs.parameters.converge.k_coarse - x * stepping
                for x in range(self.ctx.inputs.parameters.converge.k_samples + 1)
            ]
            # Generate the kgrids, and make sure they are not
            # duplicated
            converge.kgrids = []
            ktmp = get_data_class("array.kpoints")()
            ktmp.set_cell_from_structure(self.inputs.structure)
            rec_cell = ktmp.reciprocal_cell
            last_kgrid = None
            for spacing in converge.k_sampling:
                kgrid = fetch_k_grid(rec_cell, spacing)
                if kgrid == last_kgrid:
                    continue
                converge.kgrids.append(kgrid)
                last_kgrid = kgrid

    def init_rel_conv(self):
        """Initialize the relative convergence tests."""

        # Most of the needed parameters are already set initially by `init_conv`. Here,
        # we only reset counters and clear workchain arrays to prepare for a new batch
        # of convergence tests.
        self.ctx.converge.pw_iteration = 0
        self.ctx.converge.kpoints_iteration = 0

    def init_disp_conv(self):
        """Initialize the displacement convergence tests."""

        converge = self.ctx.converge
        settings = converge.settings
        if self.ctx.inputs.parameters.converge.displace:
            # Make sure we reset the plane wave and k-point tests
            if converge.run_pw_conv_calcs_org:
                converge.run_pw_conv_calcs = True
            if converge.run_kpoints_conv_calcs_org:
                converge.run_kpoints_conv_calcs = True
            self.init_rel_conv()
            # Set the new displaced structure
            converge.structure = self._displace_structure()
            # Set extra information on verbose info
            converge.settings.inform_details = ", using a displaced structure"
        # Also, make sure the data arrays from previous convergence tests are saved
        # in order to be able to calculate the relative convergence
        # criterias later.
        converge.pw_data_org = copy.deepcopy(converge.pw_data)
        converge.k_data_org = copy.deepcopy(converge.k_data)
        # Emtpy arrays
        converge.pw_data = []
        converge.k_data = []
        # Finally, reset k-point grid if plane wave cutoff is not supplied
        if settings.pwcutoff_org is None:
            if not settings.supplied_kmesh and settings.kgrid_org is None:
                self._set_default_kgrid()

        self.reset_context_pw_kgrid_lists()

    def reset_context_pw_kgrid_lists(self):
        converge = self.ctx.converge
        converge.settings.pwcutoff_list = []
        converge.settings.kgrid_list = []

    def init_comp_conv(self):
        """Initialize the compression convergence tests."""

        converge = self.ctx.converge
        settings = converge.settings
        if self.ctx.inputs.parameters.converge.compress:
            # Make sure we reset the plane wave and k-point tests
            if converge.run_pw_conv_calcs_org:
                converge.run_pw_conv_calcs = True
            if converge.run_kpoints_conv_calcs_org:
                converge.run_kpoints_conv_calcs = True
            self.init_rel_conv()
            # Set the new compressed structure
            converge.structure = self._compress_structure()
            # Set extra information on verbose info
            converge.settings.inform_details = ", using a compressed structure"
        # Also, make sure the data arrays from previous convergence tests are saved
        # in order to be able to calculate the relative convergence criterias later.
        # If we jumped the displacement tests, we have already saved the original data.
        if self.ctx.inputs.parameters.converge.displace:
            converge.pw_data_displacement = copy.deepcopy(converge.pw_data)
            converge.k_data_displacement = copy.deepcopy(converge.k_data)
        # Empty arrays
        converge.pw_data = []
        converge.k_data = []
        self.reset_context_pw_kgrid_lists()
        # Finally, reset k-point grid if plane wave cutoff is not supplied
        if settings.pwcutoff_org is None:
            if not settings.supplied_kmesh and settings.kgrid_org is None:
                self._set_default_kgrid()

    def _set_default_kgrid(self):
        """Sets the default k-point grid for plane wave convergence tests."""
        converge = self.ctx.converge
        rec_cell = converge.kpoints.reciprocal_cell
        k_spacing = self.ctx.inputs.parameters.converge.k_spacing
        kgrid = fetch_k_grid(rec_cell, k_spacing)
        converge.settings.kgrid = kgrid
        # Update grid.
        kpoints = get_data_class("array.kpoints")()
        kpoints.set_kpoints_mesh(kgrid)
        kpoints.set_cell_from_structure(converge.structure)
        converge.kpoints = kpoints

    def init_converged(self):
        """Prepare to run the final calculation."""
        # Structure should be the same as the initial.
        self.ctx.inputs.structure = self.inputs.structure
        # Same with settings (now we do not do convergence, so any updates
        # from these routines to settings can be skipped)
        try:
            self.ctx.inputs.settings = self.inputs.settings
        except AttributeError:
            pass
        # We also pass along relaxation parameters
        try:
            self.ctx.inputs.relax = self.inputs.relax
        except AttributeError:
            pass
        # The plane wave cutoff needs to be updated in the parameters to the set
        # value.
        self.ctx.inputs.parameters.update(
            {"encut": self.ctx.converge.settings.pwcutoff}
        )
        # And finally, the k-point grid needs to be updated to the set value, but
        # only if a kpoint mesh was not supplied
        if not self.ctx.converge.settings.supplied_kmesh:
            kpoints = get_data_class("array.kpoints")()
            kpoints.set_kpoints_mesh(self.ctx.converge.settings.kgrid)
            kpoints.set_cell_from_structure(self.ctx.inputs.structure)
            self.ctx.inputs.kpoints = kpoints
        else:
            self.ctx.inputs.kpoints = self.inputs.kpoints

        self.ctx.running_kpoints = False
        self.ctx.running_pw = False
        if not self.ctx.inputs.parameters.converge.testing:
            self.ctx.set_input_nodes = False
        # inform user
        if self._verbose:
            if not self.ctx.converge.settings.supplied_kmesh:
                self.report(
                    "executing a calculation with an assumed converged "
                    "plane wave cutoff of {pwcutoff} eV and a {kgrid0}x{kgrid1}x{kgrid2} "
                    "k-point grid".format(
                        pwcutoff=self.ctx.converge.settings.pwcutoff,
                        kgrid0=self.ctx.converge.settings.kgrid[0],
                        kgrid1=self.ctx.converge.settings.kgrid[1],
                        kgrid2=self.ctx.converge.settings.kgrid[2],
                    )
                )
            else:
                self.report(
                    "executing a calculation with an assumed converged "
                    "plane wave cutoff of {pwcutoff} eV and a supplied k-point grid".format(
                        pwcutoff=self.ctx.converge.settings.pwcutoff
                    )
                )

    def _set_input_nodes(self):
        """Sets the ctx.input nodes from the previous calculations."""
        # We need to check if relaxation is turned on, disable it during
        # the convergence tests (unless converge.relax is set to True)
        # It is reenabled when we initialize the final calculation
        if (
            self.ctx.inputs.parameters.relax.perform
            and not self.ctx.inputs.parameters.converge.relax
        ):
            self.ctx.inputs.parameters.relax.perform = False

        # If we want relaxation during convergence tests, it overrides
        if self.ctx.inputs.parameters.converge.relax:
            self.ctx.inputs.parameters.relax.perform = True

        # Then the structure
        self.ctx.inputs.structure = self.ctx.converge.structure.clone()

        # Make sure updated plane wave cutoff is set
        if (
            self.ctx.converge.settings.pwcutoff_org is None
            or self.ctx.inputs.parameters.converge.testing
        ):
            self.ctx.inputs.parameters = self.ctx.converge.parameters

        # And then the k-points if no mesh was supplied
        if not self.ctx.converge.settings.supplied_kmesh:
            self.ctx.inputs.kpoints = self.ctx.converge.kpoints.clone()
        else:
            self.ctx.inputs.kpoints = self.inputs.kpoints

    def init_next_workchain(self):
        """Initialize the next workchain calculation."""

        try:
            self.ctx.inputs
        except AttributeError:
            raise ValueError("no input dictionary was defined in self.ctx.inputs")

        # Add exposed inputs
        self.ctx.inputs.update(self.exposed_inputs(self._next_workchain))

        # Set input nodes
        if self.ctx.set_input_nodes:
            self._set_input_nodes()

        # Make sure we do not have any floating dict (convert to Dict) in the input
        # Also, make sure we do not pass the converge parameter namespace as there are no relevant
        # code specific parameters there
        self.ctx.inputs_next = prepare_process_inputs(
            self.ctx.inputs, namespaces=["verify"], exclude_parameters=["converge"]
        )

    def run_next_workchain(self):
        """Run next workchain."""
        inputs = self.ctx.inputs_next
        running = self.submit(self._next_workchain, **inputs)
        self.report(f"launching {self._next_workchain.__name__}<{running.pk}> ")

        if self.ctx.running_pw:
            return self.to_context(pw_workchains=append_(running))
        if self.ctx.running_kpoints:
            return self.to_context(kpoints_workchains=append_(running))

        return self.to_context(workchains=append_(running))

    def submit_next_workchain(self):
        """Run next workchain, but do not block the WorkChain"""
        inputs = self.ctx.inputs_next
        running = self.submit(self._next_workchain, **inputs)
        self.report(f"launching {self._next_workchain.__name__}<{running.pk}> ")
        return running

    def run_pw_conv_calcs(self):
        """Should a new plane wave cutoff convergence calculation run?"""
        return self.ctx.converge.run_pw_conv_calcs

    def run_pw_conv_disp_calcs(self):
        """Should a new plane wave cutoff displacement convergence calculation run?"""

        return (
            self.ctx.converge.run_pw_conv_calcs
            and self.ctx.inputs.parameters.converge.displace
        )

    def run_pw_conv_comp_calcs(self):
        """Should a new plane wave cutoff compression convergence calculation run?"""

        return (
            self.ctx.converge.run_pw_conv_calcs
            and self.ctx.inputs.parameters.converge.compress
        )

    def run_kpoints_conv_calcs(self):
        """Should a new kpoints convergence calculation run?"""
        return self.ctx.converge.run_kpoints_conv_calcs

    def run_kpoints_conv_disp_calcs(self):
        """Should a new kpoints displacement convergence calculation run?"""

        return (
            self.ctx.converge.run_kpoints_conv_calcs
            and self.ctx.inputs.parameters.converge.displace
        )

    def run_kpoints_conv_comp_calcs(self):
        """Should a new kpoints compression convergence calculation run?"""

        return (
            self.ctx.converge.run_kpoints_conv_calcs
            and self.ctx.inputs.parameters.converge.compress
        )

    def init_pw_conv_calc(self):
        """Initialize a single plane wave convergence calculation."""
        # Update the plane wave cutoff
        pwcutoff = self.ctx.converge.pwcutoff_sampling[self.ctx.converge.pw_iteration]
        self.ctx.converge.settings.pwcutoff = pwcutoff
        parameters_dict = self.ctx.converge.parameters
        # Push the cut-off to the actual parameters
        parameters_dict.update({"encut": self.ctx.converge.settings.pwcutoff})
        self.ctx.running_pw = True
        self.ctx.running_kpoints = False
        inform_details = self.ctx.converge.settings.get("inform_details")
        if inform_details is None:
            inform_details = ""

        # inform user
        if self._verbose:
            if self.ctx.converge.settings.supplied_kmesh:
                self.report(
                    "running plane wave convergence test on the supplied k-point "
                    "mesh for a plane wave cutoff of {pwcutoff} eV".format(
                        pwcutoff=pwcutoff
                    )
                    + inform_details
                )
            else:
                self.report(
                    "running plane wave convergence test for k-point sampling "
                    "of {kgrid0}x{kgrid1}x{kgrid2} for a plane wave cutoff of {pwcutoff} eV".format(
                        kgrid0=self.ctx.converge.settings.kgrid[0],
                        kgrid1=self.ctx.converge.settings.kgrid[1],
                        kgrid2=self.ctx.converge.settings.kgrid[2],
                        pwcutoff=pwcutoff,
                    )
                    + inform_details
                )

    def init_pw_conv_calc_no_block(self, pw_iteration):
        """Initialize a single plane wave convergence calculation."""
        # Update the plane wave cutoff
        pwcutoff = self.ctx.converge.pwcutoff_sampling[pw_iteration]
        self.ctx.converge.settings.pwcutoff = pwcutoff
        self.ctx.converge.settings.pwcutoff_list.append(pwcutoff)
        parameters_dict = self.ctx.converge.parameters

        # Push the cut-off to the actual parameters
        parameters_dict.update({"encut": self.ctx.converge.settings.pwcutoff})
        self.ctx.running_pw = True
        self.ctx.running_kpoints = False
        inform_details = self.ctx.converge.settings.get("inform_details")
        if inform_details is None:
            inform_details = ""

        # inform user
        if self._verbose:
            if self.ctx.converge.settings.supplied_kmesh:
                self.report(
                    "running plane wave convergence test on the supplied k-point "
                    "mesh for a plane wave cutoff of {pwcutoff} eV".format(
                        pwcutoff=pwcutoff
                    )
                    + inform_details
                )
            else:
                self.report(
                    "running plane wave convergence test for k-point sampling "
                    "of {kgrid0}x{kgrid1}x{kgrid2} for a plane wave cutoff of {pwcutoff} eV".format(
                        kgrid0=self.ctx.converge.settings.kgrid[0],
                        kgrid1=self.ctx.converge.settings.kgrid[1],
                        kgrid2=self.ctx.converge.settings.kgrid[2],
                        pwcutoff=pwcutoff,
                    )
                    + inform_details
                )

    def results_pw_conv_calc(self):
        """Fetch and store the relevant convergence parameters for each plane wave calculation."""

        # Check if there is in fact a workchain present
        try:
            workchain = self.ctx.pw_workchains[-1]
        except IndexError:
            self.report(
                f"There is no {self._next_workchain.__name__} in the called workchain list."
            )
            return (
                self.exit_codes.ERROR_NO_CALLED_WORKCHAIN
            )  # pylint: disable=no-member
        # Check if called workchain was successfull
        next_workchain_exit_status = self.ctx.pw_workchains[-1].exit_status
        next_workchain_exit_message = self.ctx.pw_workchains[-1].exit_message
        if next_workchain_exit_status:
            exit_code = compose_exit_code(
                next_workchain_exit_status, next_workchain_exit_message
            )
            self.report(
                "The called {}<{}> returned a non-zero exit status. "
                "The exit status {} is inherited and this single plane-wave "
                "convergence calculation has to be considered failed. Continuing "
                "the convergence tests.".format(
                    workchain.__class__.__name__, workchain.pk, exit_code
                )
            )

        # Update plane wave iteration index.
        self.ctx.converge.pw_iteration += 1
        # Check if the index has an entry, if not, do not perform further
        # calculations.
        try:
            self.ctx.converge.pwcutoff_sampling[self.ctx.converge.pw_iteration]
        except IndexError:
            self.ctx.converge.run_pw_conv_calcs = False

        pwcutoff = self.ctx.converge.settings.pwcutoff
        if not next_workchain_exit_status:
            misc = workchain.outputs.misc.get_dict()
            # fetch total energy
            total_energy = misc["total_energies"][
                self.ctx.inputs.parameters.converge.total_energy_type
            ]

            # fetch max force
            max_force = misc["maximum_force"]

            # fetch bands and occupations
            bands = workchain.outputs.bands

            # fetch band
            _, gap = find_bandgap(bands)
            if gap is None:
                gap = 0.0
            gap = float(gap)  # Ensure GAP is a float
            # Aiida cannot do VBM, yet, so set to zero for now
            max_valence_band = 0.0

            # add stuff to the converge context
            self.ctx.converge.pw_data.append(
                [pwcutoff, total_energy, max_force, max_valence_band, gap]
            )
        else:
            # add None entries for the failed test
            self.ctx.converge.pw_data.append([pwcutoff, None, None, None, None])

        return self.exit_codes.NO_ERROR  # pylint: disable=no-member

    def results_pw_conv_calc_no_block(self, pw_iteration):
        """Fetch and store the relevant convergence parameters for each plane wave calculation."""
        # Check if there is in fact a workchain present
        try:
            workchain = self.ctx.pw_workchains[pw_iteration]
        except IndexError:
            self.report(
                f"There is no {self._next_workchain.__name__} in the called workchain list."
            )
            return (
                self.exit_codes.ERROR_NO_CALLED_WORKCHAIN
            )  # pylint: disable=no-member
        # Check if called workchain was successfull
        next_workchain_exit_status = self.ctx.pw_workchains[pw_iteration].exit_status
        next_workchain_exit_message = self.ctx.pw_workchains[pw_iteration].exit_message
        if next_workchain_exit_status:
            exit_code = compose_exit_code(
                next_workchain_exit_status, next_workchain_exit_message
            )
            self.report(
                "The called {}<{}> returned a non-zero exit status. "
                "The exit status {} is inherited and this single plane-wave "
                "convergence calculation has to be considered failed. Continuing "
                "the convergence tests.".format(
                    workchain.__class__.__name__, workchain.pk, exit_code
                )
            )

        pwcutoff = self.ctx.converge.settings.pwcutoff_list[pw_iteration]
        if not next_workchain_exit_status:
            misc = workchain.outputs.misc.get_dict()
            # fetch total energy
            total_energy = misc["total_energies"][
                self.ctx.inputs.parameters.converge.total_energy_type
            ]

            # fetch max force
            max_force = misc["maximum_force"]

            # fetch bands and occupations
            bands = workchain.outputs.bands

            # fetch band
            _, gap = find_bandgap(bands)
            if gap is None:
                gap = 0.0
            # Aiida cannot do VBM, yet, so set to zero for now
            max_valence_band = 0.0

            # add stuff to the converge context
            self.ctx.converge.pw_data.append(
                [pwcutoff, total_energy, max_force, max_valence_band, gap]
            )
        else:
            # add None entries for the failed test
            self.ctx.converge.pw_data.append([pwcutoff, None, None, None, None])

        return self.exit_codes.NO_ERROR  # pylint: disable=no-member

    def run_pw_conv_many(self):
        """
        Launch PW convergence calculations in parallel rather than serial.
        """
        # DEBUG - save context
        self._debug_save_ctx("run_pw_conv_many")

        nsamples = len(self.ctx.converge.pwcutoff_sampling)
        pw_work_uuids = []
        for pw_id in range(nsamples):
            # This set the next pw converge calculation
            self.init_pw_conv_calc_no_block(pw_id)
            self.init_next_workchain()
            running = self.submit_next_workchain()
            pw_work_uuids.append(running.uuid)
            self.to_context(pw_workchains=append_(running))
        self.ctx.pw_workchains_uuids = pw_work_uuids

    def run_kpoints_conv_many(self):
        """
        Launch kpoints convergence calculations in parallel rather than serial.
        """
        # DEBUG - save context
        self._debug_save_ctx("run_pw_conv_many")

        nsamples = len(self.ctx.converge.kgrids)
        k_work_uuids = []
        for pw_id in range(nsamples):
            # This set the next pw converge calculation
            self.init_kpoints_conv_calc_no_block(pw_id)
            self.init_next_workchain()
            running = self.submit_next_workchain()
            k_work_uuids.append(running.uuid)
            self.to_context(kpoints_workchains=append_(running))
        self.ctx.kpoints_workchains_uuids = k_work_uuids

    def sort_pw_workchains(self):
        """
        The pw_workchains in the context may not in the order that
        has been submitted. This method sorts the list according to the pw_workchains_uuids
        save in the context
        """
        if not self.ctx.pw_workchains:
            return
        sorted_pw = sort_with_uuids(
            self.ctx.pw_workchains, self.ctx.pw_workchains_uuids
        )
        self.ctx.pw_workchains = sorted_pw

    def sort_kpoints_workchains(self):
        """
        The kpoints_workchains in the context may not in the order that
        has been submitted. This method sorts the list according to the pw_workchains_uuids
        save in the context
        """
        if not self.ctx.kpoints_workchains:
            return
        sorted_kpoints = sort_with_uuids(
            self.ctx.kpoints_workchains, self.ctx.kpoints_workchains_uuids
        )
        self.ctx.kpoints_workchains = sorted_kpoints
        return sorted_kpoints

    def init_kpoints_conv_calc(self):
        """Initialize a single k-point grid convergence calculation."""

        # Fetch k-point grid by using the distance between each point
        kstep = self.ctx.converge.k_sampling[self.ctx.converge.kpoints_iteration]
        rec_cell = self.ctx.converge.kpoints.reciprocal_cell
        kgrid = fetch_k_grid(rec_cell, kstep)
        # Check if the existing entry already exists from the previous run (can
        # happen for low grid densities due to roundoff)
        if kgrid == self.ctx.converge.settings.kgrid:
            # Increment all entries by one
            kgrid = [element + 1 for element in kgrid]
        self.ctx.converge.settings.kgrid = kgrid
        # Update grid.
        kpoints = get_data_class("array.kpoints")()
        kpoints.set_kpoints_mesh(kgrid)
        kpoints.set_cell_from_structure(self.ctx.converge.structure)
        self.ctx.converge.kpoints = kpoints
        self.ctx.running_kpoints = True
        self.ctx.running_pw = False
        inform_details = self.ctx.converge.settings.get("inform_details")
        if inform_details is None:
            inform_details = ""

        # Update pwcutoff
        self.ctx.inputs.parameters.update(
            {"encut": self.ctx.converge.settings.pwcutoff}
        )

        # inform user
        if self._verbose:
            self.report(
                "running k-point convergence test for k-point sampling "
                "of {}x{}x{} for a plane wave cutoff of {pwcutoff} eV".format(
                    kgrid[0],
                    kgrid[1],
                    kgrid[2],
                    pwcutoff=self.ctx.converge.settings.pwcutoff,
                )
                + inform_details
            )

    def init_kpoints_conv_calc_no_block(self, kpt_id):
        """Initialize a single k-point grid convergence calculation."""

        # Extract kgrid from generate dlist of kgrids
        kgrid = self.ctx.converge.kgrids[kpt_id]
        self.ctx.converge.settings.kgrid = kgrid
        # settings.kgrid_list may be redundant, keep if for now
        # for consistency
        self.ctx.converge.settings.kgrid_list.append(kgrid)
        # Update grid.
        kpoints = get_data_class("array.kpoints")()
        kpoints.set_kpoints_mesh(kgrid)
        kpoints.set_cell_from_structure(self.ctx.converge.structure)
        self.ctx.converge.kpoints = kpoints
        self.ctx.running_kpoints = True
        self.ctx.running_pw = False
        inform_details = self.ctx.converge.settings.get("inform_details")
        if inform_details is None:
            inform_details = ""

        # Make sure the cutoff is also up-to-date
        self.ctx.inputs.parameters.update(
            {"encut": self.ctx.converge.settings.pwcutoff}
        )

        # inform user
        if self._verbose:
            self.report(
                "running k-point convergence test for k-point sampling "
                "of {}x{}x{} for a plane wave cutoff of {pwcutoff} eV".format(
                    kgrid[0],
                    kgrid[1],
                    kgrid[2],
                    pwcutoff=self.ctx.converge.settings.pwcutoff,
                )
                + inform_details
            )

    def results_kpoints_conv_calc(self):
        """Fetch and store the relevant convergence parameters for each k-point grid calculation."""

        try:
            workchain = self.ctx.kpoints_workchains[-1]
        except IndexError:
            self.report(
                f"There is no {self._next_workchain.__name__} in the called workchain list."
            )
            return (
                self.exit_codes.ERROR_NO_CALLED_WORKCHAIN
            )  # pylint: disable=no-member

        # Check if child workchain was successfull
        next_workchain_exit_status = self.ctx.kpoints_workchains[-1].exit_status
        next_workchain_exit_message = self.ctx.kpoints_workchains[-1].exit_message
        if next_workchain_exit_status:
            exit_code = compose_exit_code(
                next_workchain_exit_status, next_workchain_exit_message
            )
            self.report(
                "The called {}<{}> returned a non-zero exit status. "
                "The exit status {} is inherited and this single plane-wave "
                "convergence calculation has to be considered failed. Continuing "
                "the convergence tests.".format(
                    workchain.__class__.__name__, workchain.pk, exit_code
                )
            )

        # Update kpoints iteration index
        self.ctx.converge.kpoints_iteration += 1
        # Check if the index has an entry, if not, do not perform further
        # calculations
        try:
            self.ctx.converge.k_sampling[self.ctx.converge.kpoints_iteration]
        except IndexError:
            self.ctx.converge.run_kpoints_conv_calcs = False

        kgrid = self.ctx.converge.settings.kgrid
        pwcutoff = self.ctx.converge.settings.pwcutoff
        if not next_workchain_exit_status:
            misc = workchain.outputs.misc.get_dict()
            # fetch total energy
            total_energy = misc["total_energies"][
                self.ctx.inputs.parameters.converge.total_energy_type
            ]

            # fetch max force
            max_force = misc["maximum_force"]

            # fetch bands and occupations
            bands = workchain.outputs.bands
            # fetch band
            _, gap = find_bandgap(bands)
            if gap is None:
                gap = 0.0
            # Aiida cannot do VBM, yet, so set to zero for now
            max_valence_band = 0.0

            # add stuff to the converge context
            self.ctx.converge.k_data.append(
                [
                    kgrid[0],
                    kgrid[1],
                    kgrid[2],
                    pwcutoff,
                    total_energy,
                    max_force,
                    max_valence_band,
                    gap,
                ]
            )
        else:
            # add None entries for the failed test
            self.ctx.converge.k_data.append(
                [kgrid[0], kgrid[1], kgrid[2], pwcutoff, None, None, None, None]
            )

        return self.exit_codes.NO_ERROR  # pylint: disable=no-member

    def results_kpoints_conv_calc_no_block(self, kpt_id):
        """Fetch and store the relevant convergence parameters for each k-point grid calculation."""

        try:
            workchain = self.ctx.kpoints_workchains[kpt_id]
        except IndexError:
            self.report(
                f"There is no {self._next_workchain.__name__} in the called workchain list."
            )
            return (
                self.exit_codes.ERROR_NO_CALLED_WORKCHAIN
            )  # pylint: disable=no-member

        # Check if child workchain was successfull
        next_workchain_exit_status = self.ctx.kpoints_workchains[kpt_id].exit_status
        next_workchain_exit_message = self.ctx.kpoints_workchains[kpt_id].exit_message
        if next_workchain_exit_status:
            exit_code = compose_exit_code(
                next_workchain_exit_status, next_workchain_exit_message
            )
            self.report(
                "The called {}<{}> returned a non-zero exit status. "
                "The exit status {} is inherited and this single plane-wave "
                "convergence calculation has to be considered failed. Continuing "
                "the convergence tests.".format(
                    workchain.__class__.__name__, workchain.pk, exit_code
                )
            )

        kgrid = self.ctx.converge.settings.kgrid_list[kpt_id]
        pwcutoff = self.ctx.converge.settings.pwcutoff
        if not next_workchain_exit_status:
            misc = workchain.outputs.misc.get_dict()
            # fetch total energy
            total_energy = misc["total_energies"][
                self.ctx.inputs.parameters.converge.total_energy_type
            ]

            # fetch max force
            max_force = misc["maximum_force"]

            # fetch bands and occupations
            bands = workchain.outputs.bands
            # fetch band
            _, gap = find_bandgap(bands)
            if gap is None:
                gap = 0.0
            # Aiida cannot do VBM, yet, so set to zero for now
            max_valence_band = 0.0

            # add stuff to the converge context
            self.ctx.converge.k_data.append(
                [
                    kgrid[0],
                    kgrid[1],
                    kgrid[2],
                    pwcutoff,
                    total_energy,
                    max_force,
                    max_valence_band,
                    gap,
                ]
            )
        else:
            # add None entries for the failed test
            self.ctx.converge.k_data.append(
                [kgrid[0], kgrid[1], kgrid[2], pwcutoff, None, None, None, None]
            )

        return self.exit_codes.NO_ERROR  # pylint: disable=no-member

    def analyze_pw_after_comp(self):
        """Return True if we are running compressed convergence tests."""
        return self.ctx.inputs.parameters.converge.compress

    def analyze_pw_after_disp(self):
        """Return True if we are running displaced convergence tests."""
        return self.ctx.inputs.parameters.converge.displace

    def analyze_pw_conv(self):
        """Analyze the plane wave convergence and store it if need be."""

        # Only analyze plane wave cutoff if the pwcutoff is not supplied
        if self.ctx.converge.settings.pwcutoff_org is None:
            pwcutoff = self._check_pw_converged()
            # Check if something went wrong
            if pwcutoff is None:
                self.report(
                    "We were not able to obtain a convergence of the plane wave cutoff "
                    "to the specified cutoff. This could also be caused by failures of "
                    "the calculations producing results for the convergence tests. Setting "
                    "the plane wave cutoff to the highest specified value: {pwcutoff} eV".format(
                        pwcutoff=self.ctx.converge.pw_data[-1][0]
                    )
                )
                self.ctx.converge.settings.pwcutoff = self.ctx.converge.pw_data[-1][0]
            else:
                self.ctx.converge.settings.pwcutoff = pwcutoff

    def save_pw_conv_many(self):
        """Save the PW workchains peroformed in parallel"""
        # Sort ctx.pw_workchains
        self.sort_pw_workchains()
        nsamples = len(self.ctx.converge.pwcutoff_sampling)
        for pw_id in range(nsamples):
            self.results_pw_conv_calc_no_block(pw_id)

    def analyze_pw_conv_many(self):
        """Analyze the plane wave convergence and store it if need be."""

        # Only analyze plane wave cutoff if the pwcutoff is not supplied
        if self.ctx.converge.settings.pwcutoff_org is None:
            pwcutoff = self._check_pw_converged()
            # Check if something went wrong
            if pwcutoff is None:
                # The last pw_convgence
                self.report(
                    "We were not able to obtain a convergence of the plane wave cutoff "
                    "to the specified cutoff. This could also be caused by failures of "
                    "the calculations producing results for the convergence tests. Setting "
                    "the plane wave cutoff to the highest specified value: {pwcutoff} eV".format(
                        pwcutoff=self.ctx.converge.pw_data[-1][0]
                    )
                )
                self.ctx.converge.settings.pwcutoff = self.ctx.converge.pw_data[-1][0]
            else:
                self.ctx.converge.settings.pwcutoff = pwcutoff
        # Reset lists
        self.ctx.converge.settings.pwcutoff_list = []
        self.ctx.converge.settings.kgrid_list = []

    def save_kpoints_conv_many(self):
        """Analyze the plane wave convergence and store it if need be."""

        # First we need to collect the results
        self.sort_kpoints_workchains()
        nsamples = len(self.ctx.converge.kgrids)
        for kpt_id in range(nsamples):
            self.results_kpoints_conv_calc_no_block(kpt_id)

    def _set_pwcutoff_and_kgrid(self, pwcutoff, kgrid):
        """Sets the pwcutoff and kgrid (if mesh was not supplied)."""
        settings = self.ctx.converge.settings
        settings.pwcutoff = pwcutoff
        if not settings.supplied_kmesh:
            settings.kgrid = kgrid

    def analyze_conv(self):
        """Analyze convergence and store its parameters."""

        settings = self.ctx.converge.settings
        displace = self.ctx.inputs.parameters.converge.displace
        compress = self.ctx.inputs.parameters.converge.compress

        # Notify the user
        if self._verbose:
            self.report("All convergence tests are done.")

        if displace:
            (
                pwcutoff_diff_displacement,
                kgrid_diff_displacement,
            ) = self._analyze_conv_disp()
            self._set_pwcutoff_and_kgrid(
                pwcutoff_diff_displacement, kgrid_diff_displacement
            )

        if compress:
            # We have data sitting from the compression tests
            self.ctx.converge.pw_data_comp = self.ctx.converge.pw_data
            self.ctx.converge.k_data_comp = self.ctx.converge.k_data
            pwcutoff_diff_comp, kgrid_diff_comp = self._analyze_conv_comp()
            self._set_pwcutoff_and_kgrid(pwcutoff_diff_comp, kgrid_diff_comp)

        if displace and compress:
            pwcutoff_disp_comp, kgrid_disp_comp = self._analyze_conv_disp_comp(
                pwcutoff_diff_displacement,
                pwcutoff_diff_comp,
                kgrid_diff_displacement,
                kgrid_diff_comp,
            )
            self._set_pwcutoff_and_kgrid(pwcutoff_disp_comp, kgrid_disp_comp)

        if not (displace or compress):
            pwcutoff, kgrid = self._analyze_conv()
            self._set_pwcutoff_and_kgrid(pwcutoff, kgrid)

        # Check if any we have None entries for pwcutoff or kgrid, which means something failed,
        # or that we where not able to reach the requested converge.
        if settings.pwcutoff is None:
            self.report(
                "We were not able to obtain a convergence of the plane wave cutoff "
                "to the specified cutoff. This could also be caused by failures of "
                "the calculations producing results for the convergence tests. Setting "
                "the plane wave cutoff to the highest specified value: {pwcutoff} eV".format(
                    pwcutoff=self.ctx.converge.pw_data_org[-1][0]
                )
            )
            settings.pwcutoff = self.ctx.converge.pw_data_org[-1][0]
        if not settings.supplied_kmesh and self.ctx.converge.settings.kgrid is None:
            self.report(
                "We were not able to obtain a convergence of the k-point grid "
                "to the specified cutoff. This could also be caused by failures of "
                "the calculations producing results for the convergence tests. Setting "
                "the k-point grid sampling to the highest specified value: {kgrid}".format(
                    kgrid=self.ctx.converge.k_data_org[-1][0:3]
                )
            )
            settings.kgrid = self.ctx.converge.k_data_org[-1][0:3]

    def _analyze_conv(self):
        """
        Analyze convergence using no displacements or compression.

        Note that, in the case of no displacements or compressions, the
        converged plane wave cutoff is already stored.
        """

        settings = self.ctx.converge.settings
        cutoff_type = self.ctx.inputs.parameters.converge.cutoff_type
        cutoff_value = self.ctx.inputs.parameters.converge.cutoff_value

        # Already stored
        pwcutoff = settings.pwcutoff
        # Also notice that the data resides in k_data_org in order to open for
        # relative comparisons in a flexible manner
        k_data = self.ctx.converge.k_data_org
        if self._verbose:
            self.report(
                "No atomic displacements or compressions were performed. The convergence test suggests:"
            )
        if settings.pwcutoff_org is None:
            if self._verbose:
                self.report(f"plane wave cutoff: {pwcutoff} eV.")
        else:
            if self._verbose:
                self.report("plane wave cutoff: User supplied.")

        if not settings.supplied_kmesh:
            kgrid = self._check_kpoints_converged(k_data, cutoff_type, cutoff_value)
            if self._verbose:
                if kgrid is not None:
                    self.report(f"k-point grid: {kgrid[0]}x{kgrid[1]}x{kgrid[2]}")
                else:
                    self.report("k-point grid: Failed")
        else:
            kgrid = None
            if self._verbose:
                self.report("k-point grid: User supplied")

        if self._verbose:
            self.report(
                "for the convergence criteria {cutoff_type} and a cutoff of {cutoff_value}".format(
                    cutoff_type=cutoff_type, cutoff_value=cutoff_value
                )
            )

        return pwcutoff, kgrid

    def _analyze_conv_disp_comp(
        self, pwcutoff_displacement, pwcutoff_comp, kgrid_displacement, kgrid_comp
    ):  # noqa: MC0001
        """
        Analyze the convergence when both displacements and compression is performed.

        We take the maximum of the plane wave cutoff and the densest k-point grid as
        the recommended values.

        """

        cutoff_type = self.ctx.inputs.parameters.converge.cutoff_type
        cutoff_value = self.ctx.inputs.parameters.converge.cutoff_value_r
        # return the highest plane wave cutoff and densest grid (L2 norm)
        # of the two
        pwcutoff = max(pwcutoff_displacement, pwcutoff_comp)
        if self._verbose:
            self.report(
                "The convergence tests, taking the highest required plane-wave and "
                "k-point values for both the atomic displacement and compression "
                "tests suggests:"
            )

        if not self.ctx.converge.settings.supplied_kmesh:
            if np.sqrt(sum(x**2 for x in kgrid_displacement)) > np.sqrt(
                sum(x**2 for x in kgrid_comp)
            ):
                kgrid = kgrid_displacement
            else:
                kgrid = kgrid_comp

        if (
            self.ctx.converge.settings.pwcutoff_org is None
            and pwcutoff_displacement is not None
            and pwcutoff_comp is not None
        ):
            if self._verbose:
                self.report(f"plane wave cutoff: {pwcutoff} eV")
        elif self.ctx.converge.settings.pwcutoff_org is not None:
            if self._verbose:
                self.report("plane wave cutoff: User supplied")
        else:
            if self._verbose:
                self.report("plane wave cutoff: Failed")

        if (
            not self.ctx.converge.settings.supplied_kmesh
            and kgrid_displacement is not None
            and kgrid_comp is not None
        ):
            if self._verbose:
                self.report(f"k-point grid: {kgrid[0]}x{kgrid[1]}x{kgrid[2]}")
        elif self.ctx.converge.settings.supplied_kmesh:
            if self._verbose:
                self.report("k-point grid: User supplied.")
        else:
            if self._verbose:
                self.report("k-point grid: Failed.")

        if self._verbose:
            self.report(
                "for the convergence criteria "
                "{cutoff_type} and a cutoff of {cutoff_value}.".format(
                    cutoff_type=cutoff_type, cutoff_value=cutoff_value
                )
            )

        return pwcutoff, kgrid

    def _analyze_conv_disp(self):  # noqa: MC000
        """Analyze the convergence when atomic displacements are performed."""
        settings = self.ctx.converge.settings
        pwcutoff_org = settings.pwcutoff_org
        kgrid_org = settings.kgrid_org
        cutoff_type = self.ctx.inputs.parameters.converge.cutoff_type
        cutoff_value = self.ctx.inputs.parameters.converge.cutoff_value
        cutoff_value_r = self.ctx.inputs.parameters.converge.cutoff_value_r
        pw_data_org = self.ctx.converge.pw_data_org
        k_data_org = self.ctx.converge.k_data_org
        pw_data_displacement = self.ctx.converge.pw_data_displacement
        pwcutoff_displacement = self._check_pw_converged(
            pw_data_displacement, cutoff_type, cutoff_value
        )
        if not settings.supplied_kmesh:
            k_data_displacement = self.ctx.converge.k_data_displacement
            kgrid_displacement = self._check_kpoints_converged(
                k_data_displacement, cutoff_type, cutoff_value
            )
        else:
            kgrid_diff_displacement = None
        # Calculate diffs for the plane wave cutoff
        if pwcutoff_org is None:
            pw_data = pw_data_displacement
            for index, _ in enumerate(pw_data):
                pw_data[index][1:] = [
                    pw_data_displacement[index][j + 1] - pw_data_org[index][j + 1]
                    for j in range(len(pw_data_displacement[0]) - 1)
                ]
            pwcutoff_diff_displacement = self._check_pw_converged(
                pw_data, cutoff_type, cutoff_value_r
            )
        else:
            pwcutoff_diff_displacement = pwcutoff_org

        # Then for the k points
        if kgrid_org is None and not settings.supplied_kmesh:
            k_data = k_data_displacement
            for index, _ in enumerate(k_data_displacement):
                k_data[index][4:] = [
                    k_data_displacement[index][j + 4] - k_data_org[index][j + 4]
                    for j in range(len(k_data_displacement[0]) - 4)
                ]
            kgrid_diff_displacement = self._check_kpoints_converged(
                k_data, cutoff_type, cutoff_value_r
            )
        if self._verbose:
            self.report("Performed atomic displacements.")
            self.report(
                "The convergence test using the difference between "
                "the original and displaced dataset suggests:"
            )
        if (
            pwcutoff_org is None
            and pwcutoff_diff_displacement is not None
            and pwcutoff_displacement is not None
        ):
            if self._verbose:
                self.report(
                    "plane wave cutoff: {pwcutoff_diff_displacement} "
                    "({pwcutoff_displacement} for the isolated displacement tests) eV".format(
                        pwcutoff_diff_displacement=pwcutoff_diff_displacement,
                        pwcutoff_displacement=pwcutoff_displacement,
                    )
                )
        elif pwcutoff_org:
            if self._verbose:
                self.report("plane wave cutoff: User supplied")
        else:
            if self._verbose:
                self.report("plane wave cutoff: Failed")

        if (
            not settings.supplied_kmesh
            and kgrid_diff_displacement is not None
            and kgrid_displacement is not None
        ):
            if self._verbose:
                self.report(
                    "a k-point grid of {kgrid_diff_displacement0}x{kgrid_diff_displacement1}"
                    "x{kgrid_diff_displacement2} ({kgrids0}x{kgrids1}x{kgrids2} for the "
                    "isolated displacement tests)".format(
                        kgrid_diff_displacement0=kgrid_diff_displacement[0],
                        kgrid_diff_displacement1=kgrid_diff_displacement[1],
                        kgrid_diff_displacement2=kgrid_diff_displacement[2],
                        kgrids0=kgrid_displacement[0],
                        kgrids1=kgrid_displacement[1],
                        kgrids2=kgrid_displacement[2],
                    )
                )
        elif settings.supplied_kmesh:
            if self._verbose:
                self.report("k-point grid: User supplied")
        else:
            if self._verbose:
                self.report("k-point grid: Failed")

        if self._verbose:
            self.report(
                "for the convergence criteria {cutoff_type} and a cutoff "
                "of {cutoff_value_r} ({cutoff_value} for the isolated displacement tests).".format(
                    cutoff_type=cutoff_type,
                    cutoff_value_r=cutoff_value_r,
                    cutoff_value=cutoff_value,
                )
            )

        return pwcutoff_diff_displacement, kgrid_diff_displacement

    def _analyze_conv_comp(self):  # noqa: MC0001
        """Analize the relative convergence due to unit cell compression."""

        settings = self.ctx.converge.settings
        pwcutoff_org = settings.pwcutoff_org
        kgrid_org = settings.kgrid_org
        cutoff_type = self.ctx.inputs.parameters.converge.cutoff_type
        cutoff_value = self.ctx.inputs.parameters.converge.cutoff_value
        cutoff_value_r = self.ctx.inputs.parameters.converge.cutoff_value_r
        pw_data_org = self.ctx.converge.pw_data_org
        k_data_org = self.ctx.converge.k_data_org
        pw_data_comp = self.ctx.converge.pw_data_comp
        pwcutoff_comp = self._check_pw_converged(
            pw_data_comp, cutoff_type, cutoff_value
        )
        if not settings.supplied_kmesh:
            k_data_comp = self.ctx.converge.k_data_comp
            kgrid_comp = self._check_kpoints_converged(
                k_data_comp, cutoff_type, cutoff_value
            )
        else:
            kgrid_diff_comp = None
        # Calculate diffs for pwcutoff
        if pwcutoff_org is None:
            pw_data = pw_data_comp
            for index, _ in enumerate(pw_data):
                pw_data[index][1:] = [
                    pw_data_comp[index][j + 1] - pw_data_org[index][j + 1]
                    for j in range(len(pw_data_comp[0]) - 1)
                ]
            pwcutoff_diff_comp = self._check_pw_converged(
                pw_data, cutoff_type, cutoff_value_r
            )
        else:
            pwcutoff_diff_comp = pwcutoff_org
        # Then for the k points
        if kgrid_org is None and not settings.supplied_kmesh:
            k_data = k_data_comp
            for index, _ in enumerate(k_data_comp):
                k_data[index][4:] = [
                    k_data_comp[index][j + 4] - k_data_org[index][j + 4]
                    for j in range(len(k_data_comp[0]) - 4)
                ]
            kgrid_diff_comp = self._check_kpoints_converged(
                k_data, cutoff_type, cutoff_value_r
            )
        if self._verbose:
            self.report("Performed compression.")
            self.report(
                "The convergence test using the difference between the "
                "original and dataset with a volume change suggests:"
            )
        if (
            pwcutoff_org is None
            and pwcutoff_diff_comp is not None
            and pwcutoff_comp is not None
        ):
            if self._verbose:
                self.report(
                    "plane wave cutoff: {pwcutoff_diff_comp} "
                    "({pwcutoff_comp} for the isolated compression tests) eV".format(
                        pwcutoff_diff_comp=pwcutoff_diff_comp,
                        pwcutoff_comp=pwcutoff_comp,
                    )
                )
        elif pwcutoff_org:
            if self._verbose:
                self.report("plane wave cutoff: User supplied")
        else:
            if self._verbose:
                self.report("plane wave cutoff: Failed")
        if (
            not settings.supplied_kmesh
            and kgrid_diff_comp is not None
            and kgrid_comp is not None
        ):
            if self._verbose:
                self.report(
                    "k-point grid: {kgrid_diff_comp0}x{kgrid_diff_comp1}x{kgrid_diff_comp2} "
                    "({kgrid_comp0}x{kgrid_comp1}x{kgrid_comp2} for the isolated "
                    "compression tests)".format(
                        kgrid_diff_comp0=kgrid_diff_comp[0],
                        kgrid_diff_comp1=kgrid_diff_comp[1],
                        kgrid_diff_comp2=kgrid_diff_comp[2],
                        kgrid_comp0=kgrid_comp[0],
                        kgrid_comp1=kgrid_comp[1],
                        kgrid_comp2=kgrid_comp[2],
                    )
                )
        elif settings.supplied_kmesh:
            if self._verbose:
                self.report("k-point grid: User supplied")
        else:
            if self._verbose:
                self.report("k-point grid: Failed")

        if self._verbose:
            self.report(
                "for the convergence criteria {cutoff_type} and a cutoff "
                "of {cutoff_value_r} ({cutoff_value} for the isolated compression tests).".format(
                    cutoff_type=cutoff_type,
                    cutoff_value_r=cutoff_value_r,
                    cutoff_value=cutoff_value,
                )
            )

        return pwcutoff_diff_comp, kgrid_diff_comp

    def store_conv(self):
        """Set up the convergence data and put it in a data node."""
        keys = [
            "pw_data_org",
            "pw_data",
            "k_data_org",
            "k_data",
            "pw_data_displacement",
            "k_data_displacement",
            "pw_data_comp",
            "k_data_comp",
        ]
        convergence_dict = {}
        for key, value in self.ctx.converge.items():
            if key in keys:
                convergence_dict[key] = value
        convergence_context = get_data_node("dict", dict=convergence_dict)
        convergence = store_conv_data(convergence_context)
        if self._verbose:
            self.report(
                "attaching the node {}<{}> as '{}'".format(
                    convergence.__class__.__name__, convergence.pk, "converge.data"
                )
            )
        self.out("converge.data", convergence)

    def _check_pw_converged(self, pw_data=None, cutoff_type=None, cutoff_value=None):
        """
        Check if plane wave cutoffs are converged to the specified value.

        :return pwcutoff: The converged plane wave cutoff in eV

        """

        if pw_data is None:
            pw_data = self.ctx.converge.pw_data
        if cutoff_type is None:
            cutoff_type = self.ctx.inputs.parameters.converge.cutoff_type
        if cutoff_value is None:
            cutoff_value = self.ctx.inputs.parameters.converge.cutoff_value

        # Make sure we do not analyze entries that have a None entry
        pw_data = [elements for elements in pw_data if None not in elements]
        # Since we are taking deltas, make sure we have at least two entries,
        # otherwise return None
        if len(pw_data) < 2:
            return None
        # Analyze which pwcutoff to use further (cutoff_type sets which parameter)
        pwcutoff_okey = False
        index = 0
        criteria = self._ALLOWED_CUTOFF_TYPES[cutoff_type]
        # Here we only check two consecutive steps, consider to at least check three,
        # and pick the first if both steps are within the criteria
        for pwcutoff in range(1, len(pw_data)):
            delta = abs(
                pw_data[pwcutoff][criteria + 1] - pw_data[pwcutoff - 1][criteria + 1]
            )
            if delta < cutoff_value:
                pwcutoff_okey = True
                index = pwcutoff
                break
        if not pwcutoff_okey:
            # if self._verbose:
            #     self.report('Could not obtain convergence for {cutoff_type} with a cutoff '
            #                 'parameter of {cutoff_value}'.format(cutoff_type=cutoff_type, cutoff_value=cutoff_value))
            return None

        return pw_data[index][0]

    def _check_kpoints_converged(
        self, k_data=None, cutoff_type=None, cutoff_value=None
    ):
        """
        Check if the k-point grid are converged to the specified value.

        :return kgrid: The converged k-point grid sampling in each direction.

        """

        if k_data is None:
            k_data = self.ctx.converge.k_data
        if cutoff_type is None:
            cutoff_type = self.ctx.inputs.parameters.converge.cutoff_type
        if cutoff_value is None:
            cutoff_value = self.ctx.inputs.parameters.converge.cutoff_value

        # Make sure we do not analyze entries that have a None entry
        k_data = [elements for elements in k_data if None not in elements]
        # Since we are taking deltas, make sure we have at least two entries,
        # otherwise return None
        if len(k_data) < 2:
            return None
        # now analyze which k-point grid to use
        k_cut_okey = False
        index = 0
        criteria = self._ALLOWED_CUTOFF_TYPES[cutoff_type]
        # Here we only check two consecutive steps, consider to at least check three,
        # and pick the first if both steps are within the criteria
        for k in range(1, len(k_data)):
            delta = abs(k_data[k][criteria + 4] - k_data[k - 1][criteria + 4])
            if delta < cutoff_value:
                k_cut_okey = True
                index = k
                break
        if not k_cut_okey:
            # self.report('Could not find a dense enough grid to obtain a {cutoff_type} '
            #             'cutoff of {cutoff_value})'.format(cutoff_type=cutoff_type, cutoff_value=cutoff_value))
            return None

        return k_data[index][0:3]

    def verify_next_workchain(self):
        """Verify and inherit exit status from child workchains."""

        try:
            workchain = self.ctx.workchains[-1]
        except IndexError:
            self.report(
                f"There is no {self._next_workchain.__name__} in the called workchain list."
            )
            return (
                self.exit_codes.ERROR_NO_CALLED_WORKCHAIN
            )  # pylint: disable=no-member

        workchain = self.ctx.workchains[-1]
        # Inherit exit status from last workchain (supposed to be
        # successfull)
        next_workchain_exit_status = workchain.exit_status
        next_workchain_exit_message = workchain.exit_message
        if not next_workchain_exit_status:
            self.ctx.exit_code = self.exit_codes.NO_ERROR  # pylint: disable=no-member
        else:
            self.ctx.exit_code = compose_exit_code(
                next_workchain_exit_status, next_workchain_exit_message
            )
            self.report(
                "The called {}<{}> returned a non-zero exit status. "
                "The exit status {} is inherited".format(
                    workchain.__class__.__name__, workchain.pk, self.ctx.exit_code
                )
            )

        return self.ctx.exit_code

    def results(self):
        """Attach the remaining output results."""

        workchain = self.ctx.workchains[-1]
        self.out_many(self.exposed_outputs(workchain, self._next_workchain))

    def finalize(self):
        """Finalize the workchain."""
        return self.ctx.exit_code

    def run_conv_calcs(self):
        """Determines if convergence calcs are to be run at all."""
        return self.run_kpoints_conv_calcs() or self.run_pw_conv_calcs()

    def _displace_structure(self):
        """Displace the input structure according to the supplied settings."""

        displacement_vector = self.ctx.inputs.parameters.converge.displacement_vector
        displacement_distance = (
            self.ctx.inputs.parameters.converge.displacement_distance
        )
        displacement_atom = self.ctx.inputs.parameters.converge.displacement_atom
        # Set displacement
        displacement_vector = np.array(displacement_vector)
        displacement = displacement_distance * displacement_vector

        # Displace and return new structure
        return displaced_structure(
            self.ctx.converge.structure, displacement, displacement_atom
        )

    def _compress_structure(self):
        """Compress the input structure according to the supplied settings."""

        volume_change = self.ctx.inputs.parameters.converge.volume_change
        volume_change = np.array(volume_change)
        # Apply compression and tension
        comp_structure = compressed_structure(
            self.ctx.converge.structure, volume_change
        )
        # Make sure we also reset the reciprocal cell
        kpoints = get_data_class("array.kpoints")()
        kpoints.set_kpoints_mesh([1, 1, 1])
        kpoints.set_cell_from_structure(comp_structure)
        self.ctx.converge.kpoints = kpoints

        return comp_structure

    def _debug_save_ctx(self, label):
        """
        Serlize context for debugging
        """
        from pathlib import Path

        from aiida.orm.utils.serialize import serialize

        wkdir = Path(self.ctx.debug.wkdir)
        label_counts = self.ctx.debug.label_counts
        if label in label_counts:
            label_counts[label] += 1
        else:
            label_counts[label] = 0
        ibundle = label_counts[label]
        fname = f"{label}-{ibundle}"
        with open(wkdir / fname, "w") as fh:
            fh.write(serialize(self.ctx))


def default_array(name, array):
    """Used to set ArrayData for spec.input."""
    array_cls = get_data_node("array")
    array_cls.set_array(name, array)

    return array_cls


@calcfunction
def store_conv_data(convergence_context):
    """Store convergence data in the array."""
    convergence = get_data_class("array")()
    converge = convergence_context.get_dict()
    # Store regular conversion data
    try:
        store_conv_data_single(convergence, "pw_regular", converge["pw_data_org"])
    except KeyError:
        store_conv_data_single(convergence, "pw_regular", converge["pw_data"])

    try:
        store_conv_data_single(convergence, "kpoints_regular", converge["k_data_org"])
    except KeyError:
        store_conv_data_single(convergence, "kpoints_regular", converge["k_data"])

    # Then possibly displacement
    try:
        store_conv_data_single(
            convergence, "pw_displacement", converge["pw_data_displacement"]
        )
        store_conv_data_single(
            convergence, "kpoints_displacement", converge["k_data_displacement"]
        )
    except KeyError:
        pass

    # And finally for compression
    try:
        store_conv_data_single(convergence, "pw_compression", converge["pw_data_comp"])
        store_conv_data_single(
            convergence, "kpoints_compression", converge["k_data_comp"]
        )
    except KeyError:
        pass

    return convergence


def store_conv_data_single(array, key, data):
    """Store a single convergence data entry in the array."""
    if data:
        array.set_array(key, np.array(data))


def sort_with_uuids(nodes, uuids):
    """
    Sort a list of nodes using a list of uuids
    """
    sorted_list = []
    look_up = {node.uuid: node for node in nodes}
    for uuid in uuids:
        sorted_list.append(look_up[uuid])
    return sorted_list
