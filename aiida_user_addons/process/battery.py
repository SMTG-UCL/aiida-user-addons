"""
Module with battery related processes
"""
from aiida.orm import Float
from aiida.engine import calcfunction
from pymatgen.analysis.reaction_calculator import Reaction

__version__ = '0.0.1'


@calcfunction
def compute_li_voltage(lithiated_structure, lithiated_res, delithiated_structure, delithiated_res, li_ref_structure, li_ref_res):
    """
    Compute Li voltage using and energies and their corresponding structures

    Structures are only used for extracting the composition.
    """

    lith_comp = lithiated_structure.get_pymatgen().composition
    lith_eng = lithiated_res['total_energies']['energy_no_entropy']
    deli_comp = delithiated_structure.get_pymatgen().composition
    deli_eng = delithiated_res['total_energies']['energy_no_entropy']
    li_comp = li_ref_structure.get_pymatgen().composition
    li_eng = li_ref_res['total_energies']['energy_no_entropy']

    reaction = Reaction([lith_comp], [li_comp, deli_comp])
    # How many atoms does the Li reference have?
    nli = li_comp.num_atoms
    # Normalise to one Li in the product, hence the reaction energy is the voltage
    reaction.normalize_to(li_comp, factor=1 / nli)
    eng = reaction.calculate_energy({lith_comp: lith_eng, deli_comp: deli_eng, li_comp: li_eng})
    return Float(eng)


def compute_li_voltage_shortcut(lithiated, delithiated, li_ref=None, store_provenance=True):
    """
    Compute voltage from three calculations
    """
    if li_ref is None:
        indict = lithiated.inputs.parameters.get_dict()
        encut = _get_incar_tag('encut', indict)
        gga = _get_incar_tag('gga', indict)
        li_ref = _obtain_li_ref_calc(encut, gga)

    # Check if the calculations are comparable
    if not _is_comparable(lithiated, delithiated):
        raise RuntimeError('Cannot compare two calculations - parameters mismatch')
    elif not _is_comparable(delithiated, li_ref):
        raise RuntimeError('Cannot compare with the reference - mismatching parameters')

    lith_struct = lithiated.inputs.structure
    deli_struct = delithiated.inputs.structure
    li_ref_struct = li_ref.inputs.structure

    lith_res = lithiated.outputs.misc
    deli_res = delithiated.outputs.misc
    li_ref_res = li_ref.outputs.misc
    metadata = {}
    if not store_provenance:
        metadata['store_provenance'] = False

    return compute_li_voltage(lith_struct, lith_res, deli_struct, deli_res, li_ref_struct, li_ref_res, metadata=metadata)


def _get_incar_tag(tag, input_dict):
    """Obtain incar tag from dict"""
    if 'vasp' in input_dict:
        input_dict = input_dict['vasp']
    value = input_dict.get(tag)
    # Special case the GGA tag - None is pe
    if (tag == 'gga') and (value is None):
        return 'pe'
    return value


def _obtain_li_ref_calc(encut, gga, group_name='li-metal-refs'):
    """Return the reference calculation for Li metal"""
    from aiida.orm import QueryBuilder, Group, WorkChainNode, Dict
    if gga is None:
        gga = 'pe'
    q = QueryBuilder()
    q.append(Group, filters={'label': group_name})
    q.append(WorkChainNode, with_group=Group, filters={'attributes.exit_status': 0}, project=['*'])
    q.append(Dict,
             with_outgoing=WorkChainNode,
             filters={
                 'attributes.vasp.encut': encut,
                 'attributes.vasp.gga': gga
             },
             edge_filters={'label': 'parameters'})

    matches = q.all()
    if len(matches) > 1:
        print(f'WARNING: more than one matches found for gga:{gga} encut:{encut}')
    if len(matches) == 0:
        raise RuntimeError(f'ERROR: No matche found for gga:{gga} encut:{encut}')
    return matches[0][0]


def list_li_ref_calcs(group_name='li-metal-refs'):
    """Return the reference calculation for Li metal"""
    from aiida.orm import QueryBuilder, Group, WorkChainNode, Dict
    q = QueryBuilder()
    q.append(Group, filters={'label': group_name})
    q.append(WorkChainNode, with_group=Group, project=['*'])
    q.append(Dict, with_outgoing=WorkChainNode, project=['attributes.vasp'], edge_filters={'label': 'parameters'})

    matches = q.all()
    return matches


def _is_comparable(calc1, calc2):
    """Check wether two calculations can be compared"""
    critical_keys = ['encut', 'lreal', 'prec', 'gga']
    warn_keys = ['ismear', 'sigma']
    indict1 = calc1.inputs.parameters.get_dict()
    indict2 = calc2.inputs.parameters.get_dict()
    for key in critical_keys:
        v1 = _get_incar_tag(key, indict1)
        v2 = _get_incar_tag(key, indict2)
        if v1 != v2:
            print(f'Critical key mismatch {key} - {v1} vs {v2}')
            return False
    for key in warn_keys:
        if _get_incar_tag(key, indict1) != _get_incar_tag(key, indict2):
            print(f'WARNING: mismatch in key {key} - two calculation may not be comparable')
    return True