"""
VASP workchain.

---------------
Contains the VaspWorkChain class definition which uses the BaseRestartWorkChain.
"""
import numpy as np

from aiida.engine import while_, if_
from aiida.common.lang import override
#from aiida.engine.job_processes import override
from aiida.common.extendeddicts import AttributeDict
from aiida.common.exceptions import NotExistent, InputValidationError
from aiida.plugins import CalculationFactory
from aiida.orm import Code, KpointsData, Dict

from aiida_vasp.workchains.restart import BaseRestartWorkChain
from aiida_vasp.utils.aiida_utils import get_data_class, get_data_node
from aiida_vasp.utils.workchains import compose_exit_code
from aiida_vasp.utils.workchains import prepare_process_inputs
try:
    from aiida_vasp.assistant.parameters import inherit_and_merge_parameters
except ImportError:
    from aiida_vasp.utils.parameters import inherit_and_merge_parameters

from ..common.inputset.vaspsets import get_ldau_keys


class VaspWorkChain(BaseRestartWorkChain):
    """
    The VASP workchain.

    -------------------
    Error handling enriched wrapper around VaspCalculation.

    Deliberately conserves most of the interface (required inputs) of the VaspCalculation class, but
    makes it possible for a user to interact with a workchain and not a calculation.

    This is intended to be used instead of directly submitting a VaspCalculation,
    so that future features like
    automatic restarting, error checking etc. can be propagated to higher level workchains
    automatically by implementing them here.

    Usage::

        from aiida.common.extendeddicts import AttributeDict
        from aiida.work import submit
        basevasp = WorkflowFactory('vasp.vasp')
        inputs = basevasp.get_builder()
        inputs = AttributeDict()
        ## ... set inputs
        submit(basevasp, **inputs)

    To see a working example, including generation of input nodes from scratch, please
    refer to ``examples/run_vasp_lean.py``.


    Additional functionalities:

    - Automatic setting LDA+U key using the ``ldau_mapping`` input port.

    - Set kpoints using spacing in A^-1 * 2pi with the ``kpoints_spacing`` input port.

    - Perform dryrun and set parameters such as KPAR and NCORE automatically if ``auto_parallel`` input port exists.
      this will give rise to an additional output node ``parallel_settings`` containing the strategy obtained.

    """
    _verbose = False
    _calculation = CalculationFactory('vasp.vasp')

    @classmethod
    def define(cls, spec):
        super(VaspWorkChain, cls).define(spec)
        spec.input('code', valid_type=Code)
        spec.input('structure', valid_type=(get_data_class('structure'), get_data_class('cif')), required=True)
        spec.input('kpoints', valid_type=get_data_class('array.kpoints'), required=False)
        spec.input('potential_family', valid_type=get_data_class('str'), required=True)
        spec.input('potential_mapping', valid_type=get_data_class('dict'), required=True)
        spec.input('parameters', valid_type=get_data_class('dict'), required=True)
        spec.input('options', valid_type=get_data_class('dict'), required=True)
        spec.input('settings', valid_type=get_data_class('dict'), required=False)
        spec.input('wavecar', valid_type=get_data_class('vasp.wavefun'), required=False)
        spec.input('chgcar', valid_type=get_data_class('vasp.chargedensity'), required=False)
        spec.input('restart_folder',
                   valid_type=get_data_class('remote'),
                   required=False,
                   help="""
            The restart folder from a previous workchain run that is going to be used.
            """)
        spec.input('max_iterations',
                   valid_type=get_data_class('int'),
                   required=False,
                   default=lambda: get_data_node('int', 5),
                   help="""
            The maximum number of iterations to perform.
            """)
        spec.input('clean_workdir',
                   valid_type=get_data_class('bool'),
                   required=False,
                   default=lambda: get_data_node('bool', True),
                   help="""
            If True, clean the work dir upon the completion of a successfull calculation.
            """)
        spec.input('verbose',
                   valid_type=get_data_class('bool'),
                   required=False,
                   default=lambda: get_data_node('bool', False),
                   help="""
            If True, enable more detailed output during workchain execution.
            """)
        spec.input('ldau_mapping',
                   valid_type=get_data_class('dict'),
                   required=False,
                   help="Mappings, see the doc string of 'get_ldau_keys'")
        spec.input('kpoints_spacing',
                   valid_type=get_data_class('float'),
                   required=False,
                   help='Spacing for the kpoints in units A^-1 * 2pi')
        spec.input('auto_parallel',
                   valid_type=get_data_class('dict'),
                   required=False,
                   help='Automatic parallelisation settings, keywords passed to `get_jobscheme` function.')
        spec.outline(
            cls.init_context,
            cls.init_inputs,
            if_(cls.run_auto_parallel)(
                cls.init_calculation,
                cls.perform_autoparallel
            ),
            while_(cls.run_calculations)(
                cls.init_calculation,
                cls.run_calculation,
                cls.verify_calculation
            ),
            cls.results,
            cls.finalize
        )  # yapf: disable

        spec.output('misc', valid_type=get_data_class('dict'))
        spec.output('remote_folder', valid_type=get_data_class('remote'))
        spec.output('retrieved', valid_type=get_data_class('folder'))
        spec.output('structure', valid_type=get_data_class('structure'), required=False)
        spec.output('kpoints', valid_type=get_data_class('array.kpoints'), required=False)
        spec.output('trajectory', valid_type=get_data_class('array.trajectory'), required=False)
        spec.output('chgcar', valid_type=get_data_class('vasp.chargedensity'), required=False)
        spec.output('wavecar', valid_type=get_data_class('vasp.wavefun'), required=False)
        spec.output('bands', valid_type=get_data_class('array.bands'), required=False)
        spec.output('forces', valid_type=get_data_class('array'), required=False)
        spec.output('stress', valid_type=get_data_class('array'), required=False)
        spec.output('dos', valid_type=get_data_class('array'), required=False)
        spec.output('occupancies', valid_type=get_data_class('array'), required=False)
        spec.output('energies', valid_type=get_data_class('array'), required=False)
        spec.output('projectors', valid_type=get_data_class('array'), required=False)
        spec.output('dielectrics', valid_type=get_data_class('array'), required=False)
        spec.output('born_charges', valid_type=get_data_class('array'), required=False)
        spec.output('hessian', valid_type=get_data_class('array'), required=False)
        spec.output('dynmat', valid_type=get_data_class('array'), required=False)
        spec.output('parallel_settings', valid_type=get_data_class('dict'), required=False)
        spec.exit_code(0, 'NO_ERROR', message='the sun is shining')
        spec.exit_code(700, 'ERROR_NO_POTENTIAL_FAMILY_NAME', message='the user did not supply a potential family name')
        spec.exit_code(701, 'ERROR_POTENTIAL_VALUE_ERROR', message='ValueError was returned from get_potcars_from_structure')
        spec.exit_code(702, 'ERROR_POTENTIAL_DO_NOT_EXIST', message='the potential does not exist')
        spec.exit_code(703,
                       'ERROR_INVALID_PARAMETER_DETECTED',
                       message='the parameter massager found invalid tags in the input parameters.')
        spec.exit_code(704,
                       'ERROR_MISSING_PARAMETER_DETECTED',
                       message='the parameter massager did not find expected tags in the input parameters.')

    def init_calculation(self):
        """Set the restart folder and set parameters tags for a restart."""
        # Check first if the calling workchain wants a restart in the same folder
        if 'restart_folder' in self.inputs:
            self.ctx.inputs.restart_folder = self.inputs.restart_folder

        # Then check if we the restart workchain wants a restart
        if isinstance(self.ctx.restart_calc, self._calculation):
            self.ctx.inputs.restart_folder = self.ctx.restart_calc.outputs.remote_folder
            old_parameters = AttributeDict(self.ctx.inputs.parameters.get_dict())
            parameters = old_parameters.copy()
            if 'istart' in parameters:
                parameters.istart = 1
            if 'icharg' in parameters:
                parameters.icharg = 1
            if parameters != old_parameters:
                self.ctx.inputs.parameters = get_data_node('dict', dict=parameters)

    def init_inputs(self):
        """Make sure all the required inputs are there and valid, create input dictionary for calculation."""
        self.ctx.inputs = AttributeDict()

        # Set the code
        self.ctx.inputs.code = self.inputs.code

        # Set the structure (poscar)
        self.ctx.inputs.structure = self.inputs.structure

        # Set the kpoints (kpoints)
        if 'kpoints' in self.inputs:
            self.ctx.inputs.kpoints = self.inputs.kpoints
        elif 'kpoints_spacing' in self.inputs:
            kpoints = KpointsData()
            kpoints.set_cell_from_structure(self.ctx.inputs.structure)
            kpoints.set_kpoints_mesh_from_density(self.inputs.kpoints_spacing.value * np.pi * 2)
            self.ctx.inputs.kpoints = kpoints
        else:
            raise InputValidationError("Must supply either 'kpoints' or 'kpoints_spacing'")

        # Perform inputs massage to accommodate generalization in higher lying workchains
        # and set parameters
        parameters_massager = ParametersMassage(self, self.inputs.parameters)
        # Check exit codes from the parameter massager and set it if it exists
        if parameters_massager.exit_code is not None:
            return parameters_massager.exit_code
        self.ctx.inputs.parameters = parameters_massager.parameters

        # Setup LDAU keys
        if 'ldau_mapping' in self.inputs:
            ldau_settings = self.inputs.ldau_mapping.get_dict()
            ldau_keys = get_ldau_keys(self.ctx.inputs.structure, **ldau_settings)
            # Directly update the raw inputs passed to VaspCalculation
            self.ctx.inputs.parameters.update(ldau_keys)

        # Set settings
        if 'settings' in self.inputs:
            self.ctx.inputs.settings = self.inputs.settings

        # Set options
        # Options is very special, not storable and should be
        # wrapped in the metadata dictionary, which is also not storable
        # and should contain an entry for options
        if 'options' in self.inputs:
            options = {}
            options.update(self.inputs.options)
            self.ctx.inputs.metadata = {}
            self.ctx.inputs.metadata['options'] = options
            # Override the parser name if it is supplied by the user.
            parser_name = self.ctx.inputs.metadata['options'].get('parser_name')
            if parser_name:
                self.ctx.inputs.metadata['options']['parser_name'] = parser_name
            # Also make sure we specify the entry point for the
            # Set MPI to True, unless the user specifies otherwise
            withmpi = self.ctx.inputs.metadata['options'].get('withmpi', True)
            self.ctx.inputs.metadata['options']['withmpi'] = withmpi
        # Utilise default input/output selections
        self.ctx.inputs.metadata['options']['input_filename'] = 'INCAR'
        self.ctx.inputs.metadata['options']['output_filename'] = 'OUTCAR'

        # Set the CalcJobNode to have the same label as the WorkChain
        self.ctx.inputs.metadata['label'] = self.inputs.metadata.get('label', '')

        # Verify and set potentials (potcar)
        if not self.inputs.potential_family.value:
            self.report(  # pylint: disable=not-callable
                'An empty string for the potential family name was detected.')
            return self.exit_codes.ERROR_NO_POTENTIAL_FAMILY_NAME  # pylint: disable=no-member
        try:
            self.ctx.inputs.potential = get_data_class('vasp.potcar').get_potcars_from_structure(
                structure=self.inputs.structure,
                family_name=self.inputs.potential_family.value,
                mapping=self.inputs.potential_mapping.get_dict())
        except ValueError as err:
            return compose_exit_code(self.exit_codes.ERROR_POTENTIAL_VALUE_ERROR.status, str(err))  # pylint: disable=no-member
        except NotExistent as err:
            return compose_exit_code(self.exit_codes.ERROR_POTENTIAL_DO_NOT_EXIST.status, str(err))  # pylint: disable=no-member

        try:
            self._verbose = self.inputs.verbose.value
        except AttributeError:
            pass
        # Set the charge density (chgcar)
        if 'chgcar' in self.inputs:
            self.ctx.inputs.charge_density = self.inputs.chgcar

        # Set the wave functions (wavecar)
        if 'wavecar' in self.inputs:
            self.ctx.inputs.wavefunctions = self.inputs.wavecar

        return self.exit_codes.NO_ERROR  # pylint: disable=no-member

    def run_auto_parallel(self):
        """Wether we should run auto-parallelisation test"""
        return 'auto_parallel' in self.inputs

    def perform_autoparallel(self):
        """Dry run and obtain the best parallelisation settings"""
        from aiida_user_addons.tools.dryrun import get_jobscheme
        self.report(f'Performing local dryrun for auto-parallelisation')  # pylint: disable=not-callable

        ind = prepare_process_inputs(self.ctx.inputs)

        nprocs = self.ctx.inputs.metadata['options']['resources']['tot_num_mpiprocs']

        # Take the settings pass it to the function
        kwargs = self.inputs.auto_parallel.get_dict()
        if 'cpus_per_node' not in kwargs:
            kwargs['cpus_per_node'] = self.inputs.code.computer.get_default_mpiprocs_per_machine()

        # If the dryrun errored, proceed the workchain
        try:
            scheme = get_jobscheme(ind, nprocs, **kwargs)
        except Exception as error:
            self.report(f'Dry-run errorred, process with cautions, message: {error.args}')  # pylint: disable=not-callable
            return

        if (scheme.ncore is None) or (scheme.kpar is None):
            self.report(f'Error NCORE: {scheme.ncore}, KPAR: {scheme.kpar}')  # pylint: disable=not-callable
            return

        parallel_opts = {'ncore': scheme.ncore, 'kpar': scheme.kpar}
        self.report(f'Found optimum KPAR={scheme.kpar}, NCORE={scheme.ncore}')  # pylint: disable=not-callable
        self.ctx.inputs.parameters.update(parallel_opts)
        self.out('parallel_settings', Dict(dict={'ncore': scheme.ncore, 'kpar': scheme.kpar}).store())

    @override
    def on_except(self, exc_info):
        """Handle excepted state."""
        try:
            last_calc = self.ctx.calculations[-1] if self.ctx.calculations else None
            if last_calc is not None:
                self.report(  # pylint: disable=not-callable
                    'Last calculation: {calc}'.format(calc=repr(last_calc)))
                sched_err = last_calc.outputs.retrieved.get_file_content('_scheduler-stderr.txt')
                sched_out = last_calc.outputs.retrieved.get_file_content('_scheduler-stdout.txt')
                self.report('Scheduler output:\n{}'.format(sched_out or ''))  # pylint: disable=not-callable
                self.report('Scheduler stderr:\n{}'.format(sched_err or ''))  # pylint: disable=not-callable
        except AttributeError:
            self.report('No calculation was found in the context. '  # pylint: disable=not-callable
                        'Something really awefull happened. '
                        'Please inspect messages and act.')

        return super(VaspWorkChain, self).on_except(exc_info)
