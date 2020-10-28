"""
Collection of process functions for AiiDA, used for structure transformation
"""
import re
import numpy as np
from ase.build import sort
from aiida.orm import StructureData, List, ArrayData, Node, QueryBuilder, CalcFunctionNode
from aiida.engine import calcfunction

from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
from pymatgen import Structure


@calcfunction
def make_vac(cell, indices, supercell):
    """Make a defect containing cell"""
    atoms = cell.get_ase()
    supercell = atoms.repeat(supercell.get_list())
    mask = np.in1d(np.arange(len(supercell)), indices.get_list())
    supercell = supercell[~mask]  ## Remove any atoms in the original indices
    supercell.set_tags(None)
    supercell.set_masses(None)
    # Now I sort the supercell in the order of chemcial symbols
    supercell = sort(supercell)
    output = StructureData(ase=supercell)
    return output


@calcfunction
def make_vac_at_o(cell, excluded_sites, nsub, supercell):
    """
    Make lots of vacancy containing cells usnig BSYM

    Use BSYM to do the job, vacancies are subsituted with P and
    later removed. Excluded sites are subsituted with S and later
    converted back to O.
    """
    from pymatgen import Composition
    from bsym.interface.pymatgen import unique_structure_substitutions
    nsub = nsub.value
    struc = cell.get_pymatgen()
    excluded = excluded_sites.get_list()

    for n, site in enumerate(struc.sites):
        if n in excluded:
            site.species = Composition('S')

    # Expand the supercell with S subsituted strucutre
    struc = struc * supercell.get_list()
    noxygen = int(struc.composition['O'])
    unique_structure = unique_structure_substitutions(struc, 'O', {'P': nsub, 'O': noxygen - nsub})
    # Convert back to normal structure
    # Remove P as they are vacancies, Convert S back to O
    for ustruc in unique_structure:
        p_indices = [n for n, site in enumerate(ustruc.sites) if site.species == Composition('P')]
        ustruc.remove_sites(p_indices)
        # Convert S sites back to O
        ustruc['S'] = 'O'
    output_structs = {}
    for n, s in enumerate(unique_structure):
        stmp = StructureData(pymatgen=s)
        stmp.set_attribute('vac_id', n)
        stmp.set_attribute('supercell', ' '.join(map(str, supercell.get_list())))
        stmp.label = cell.label + f' VAC {n}'
        output_structs['structure_{:04d}'.format(n)] = stmp

    return output_structs


@calcfunction
def make_vac_at_o_and_shake(cell, excluded_sites, nsub, supercell, shake_amp):
    """
    Make lots of vacancy containing cells usnig BSYM

    Use BSYM to do the job, vacancies are subsituted with P and
    later removed. Excluded sites are subsituted with S and later
    converted back to O.

    In addition, we shake the nearest neighbours with that given by shake_amp.
    """
    from pymatgen import Composition
    from pymatgen.transformations.standard_transformations import PerturbStructureTransformation
    from bsym.interface.pymatgen import unique_structure_substitutions
    nsub = nsub.value
    struc = cell.get_pymatgen()
    excluded = excluded_sites.get_list()

    for n, site in enumerate(struc.sites):
        if n in excluded:
            site.species = Composition('S')

    # Expand the supercell with S subsituted strucutre
    struc = struc * supercell.get_list()
    noxygen = int(struc.composition['O'])
    unique_structure = unique_structure_substitutions(struc, 'O', {'P': nsub, 'O': noxygen - nsub})
    # Convert back to normal structure
    # Remove P as they are vacancies, Convert S back to O
    for ustruc in unique_structure:
        p_indices = [n for n, site in enumerate(ustruc.sites) if site.species == Composition('P')]

        ustruc.remove_sites(p_indices)
        # Convert S sites back to O
        ustruc['S'] = 'O'

    # Perturb structures
    trans = PerturbStructureTransformation(distance=float(shake_amp))
    unique_structure = [trans.apply_transformation(ustruc) for ustruc in unique_structure]

    output_structs = {}
    for n, s in enumerate(unique_structure):
        stmp = StructureData(pymatgen=s)
        stmp.set_attribute('vac_id', n)
        stmp.set_attribute('supercell', ' '.join(map(str, supercell.get_list())))
        stmp.label = cell.label + f' VAC {n}'
        output_structs['structure_{:04d}'.format(n)] = stmp

    return output_structs


@calcfunction
def rattle(structure, amp):
    """
    Rattle the structure by a certain amplitude
    """
    native_keys = ['cell', 'pbc1', 'pbc2', 'pbc3', 'kinds', 'sites', 'mp_id']
    # Keep the foreign keys as it is
    foreign_attrs = {key: value for key, value in structure.attributes.items() if key not in native_keys}
    atoms = structure.get_ase()
    atoms.rattle(amp.value)
    # Clean any tags etc
    atoms.set_tags(None)
    atoms.set_masses(None)
    # Convert it back
    out = StructureData(ase=atoms)
    out.set_attribute_many(foreign_attrs)
    out.label = structure.label + ' RATTLED'
    return out


def res2structure_smart(file):
    """Create StructureData from SingleFileData, return existing node if there is any"""
    q = QueryBuilder()
    q.append(Node, filters={'id': file.pk})
    q.append(CalcFunctionNode, filters={'attributes.function_name': 'res2structure'})
    q.append(StructureData)
    if q.count() > 0:
        print('Existing StructureData found')
        return q.first()
    else:
        return res2structure(file)


@calcfunction
def res2structure(file):
    """Create StructureData from SingleFile data"""
    from toolchest.resutils import read_res
    from aiida.orm import StructureData
    with file.open(file.filename) as fhandle:
        titls, atoms = read_res(fhandle.readlines())
    atoms.set_tags(None)
    atoms.set_masses(None)
    atoms.set_calculator(None)
    atoms.wrap()
    struct = StructureData(ase=atoms)
    struct.set_attribute('H', titls.enthalpy)
    struct.set_attribute('search_label', titls.label)
    struct.label = file.filename
    return struct


@calcfunction
def get_primitive(structure):
    """Create primitive structure use pymatgen interface"""
    from aiida.orm import StructureData
    pstruct = structure.get_pymatgen()
    ps = pstruct.get_primitive_structure()
    out = StructureData(pymatgen=ps)
    out.label = structure.label + ' PRIMITIVE'
    return out


@calcfunction
def get_refined_structure(structure, symprec, angle_tolerance):
    """Create refined structure use pymatgen's interface"""
    from aiida.orm import StructureData
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
    pstruct = structure.get_pymatgen()
    ana = SpacegroupAnalyzer(pstruct, symprec=symprec.value, angle_tolerance=angle_tolerance.value)
    ps = ana.get_refined_structure()
    out = StructureData(pymatgen=ps)
    out.label = structure.label + ' REFINED'
    return out


@calcfunction
def get_conventional_standard_structure(structure, symprec, angle_tolerance):
    """Create conventional standard structure use pymatgen's interface"""
    from aiida.orm import StructureData
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
    pstruct = structure.get_pymatgen()
    ana = SpacegroupAnalyzer(pstruct, symprec=symprec.value, angle_tolerance=angle_tolerance.value)
    ps = ana.get_conventional_standard_structure()
    out = StructureData(pymatgen=ps)
    out.label = structure.label + ' CONVENTIONAL STANDARD'
    return out


@calcfunction
def make_supercell(structure, supercell, tags):
    """Make supercell structure, keep the tags in order"""
    atoms = structure.get_ase()
    atoms.set_tags(tags)

    satoms = atoms.repeat(supercell.get_list())
    satoms = sort(satoms)
    stags = satoms.get_tags().tolist()
    satoms.set_tags(None)

    out = StructureData(ase=satoms)
    out.label = structure.label + ' SUPER {} {} {}'.format(*supercell.get_list())

    return {'structure': out, 'tags': List(list=stags)}


@calcfunction
def delithiate_by_wyckoff(structure, wyckoff):
    """Remove ALL lithium in a certain wyckoff sites for a given structure"""
    remove_symbol = 'Li'
    remove_wyckoff = wyckoff.value

    ana = SpacegroupAnalyzer(structure.get_pymatgen())
    psymm = ana.get_symmetrized_structure()
    natoms = len(psymm.sites)

    rm_indices = []
    for lsite, lidx, symbol in zip(psymm.equivalent_sites, psymm.equivalent_indices, psymm.wyckoff_symbols):
        site = lsite[0]
        if site.species_string != remove_symbol:
            continue
        if symbol != remove_wyckoff:
            continue
        rm_indices.extend(lidx)
    assert rm_indices, f'Nothing to remove for wyckoff {remove_wyckoff}'
    psymm.remove_sites(rm_indices)
    out = StructureData(pymatgen=Structure.from_sites(psymm.sites))
    for kind in out.kinds:
        assert not re.search(r'\d', kind.name), f'Kind name: {kind.name} contains indices'

    # Set some special attribute
    out.set_attribute('removed_specie', remove_symbol)
    out.set_attribute('removed_wyckoff', remove_wyckoff)
    out.label += f' delithiated {remove_wyckoff}'

    # Prepare a mask for the removed structures
    mask = []
    for i in range(natoms):
        if i in rm_indices:
            mask.append(False)
        else:
            mask.append(True)
    outdict = {'structure': out, 'mask': List(list=mask)}
    return outdict


@calcfunction
def delithiate_full(structure):
    """
    Perform full delithation via removing all Li ions

    Returns:
        A dictionary of the StructureData without Li under the key 'structure'.
        The mask of the sites that are kept during the process is given under the 'mask' key.
        It can be useful for transforming other properties such as MAGMOM and tags.
    """
    remove_symbol = 'Li'
    pstruct = structure.get_pymatgen()
    to_remove = [idx for idx, site in enumerate(pstruct.sites) if site.species_string == remove_symbol]
    pstruct.remove_sites(to_remove)

    out = StructureData(pymatgen=pstruct)
    out.set_attribute('removed_specie', remove_symbol)
    out.label = structure.label + f' fully delithiated'
    out.description = f'A fully delithiated structure, crated from {structure.uuid}'

    # Create the mask
    mask = []
    natoms = len(structure.sites)
    for i in range(natoms):
        if i in to_remove:
            mask.append(False)
        else:
            mask.append(True)
    outdict = {'structure': out, 'mask': List(list=mask)}
    return outdict


@calcfunction
def delithiate_one(structure):
    """
    Remove one lithium atom, enumerate the possible structures

    Symmetry is not taken into account in this function

    Returns:
        A dictionary of the StructureData without 1 Li under the key 'structure_<id>'.
        The mask of the sites that are kept during the process is given under the 'mask_<id>' key.
        It can be useful for transforming other properties such as MAGMOM and tags.
    """
    remove_symbol = 'Li'
    pstruct = structure.get_pymatgen()
    to_remove = [idx for idx, site in enumerate(pstruct.sites) if site.species_string == remove_symbol]
    outdict = {}
    for idx, site in enumerate(to_remove):
        tmp_struct = structure.get_pymatgen()
        tmp_struct.remove_sites([site])

        out = StructureData(pymatgen=tmp_struct)
        out.set_attribute('removed_specie', remove_symbol)
        out.set_attribute('removed_site', site)
        out.label = structure.label + f' delithiated 1 - {idx}'
        out.description = f'A structure with one Li removed, crated from {structure.uuid}'

        # Create the mask
        mask = []
        natoms = len(structure.sites)
        for i in range(natoms):
            if i == site:
                mask.append(False)
            else:
                mask.append(True)
        outdict.update({f'structure_{idx}': out, f'mask_{idx}': List(list=mask)})
    return outdict


@calcfunction
def delithiate_unique_sites(cell, excluded_sites, nsub, atol):
    """
    Make lots of delithiated non-equivalent cells using BSYM

    Use BSYM to do the job, vacancies are subsituted with P and
    later removed. Excluded sites are subsituted with S and later
    converted back to Li.
    """
    from pymatgen import Composition
    from bsym.interface.pymatgen import unique_structure_substitutions
    nsub = nsub.value
    struc = cell.get_pymatgen()
    excluded = excluded_sites.get_list()

    for n, site in enumerate(struc.sites):
        if n in excluded:
            site.species = Composition('Ar')

    # Expand the supercell with S subsituted strucutre
    noli = int(struc.composition['Li'])
    unique_structure = unique_structure_substitutions(struc, 'Li', {'He': nsub, 'Li': noli - nsub}, verbose=True, atol=float(atol))
    # Convert back to normal structure
    # Remove He as they are vacancies, Convert Ar back to Li
    for ustruc in unique_structure:
        p_indices = [n for n, site in enumerate(ustruc.sites) if site.species == Composition('He')]
        ustruc.remove_sites(p_indices)
        # Convert S sites back to O
        ustruc['Ar'] = 'Li'
    output_dict = {}
    for n, s in enumerate(unique_structure):
        stmp = StructureData(pymatgen=s)
        stmp.set_attribute('delithiate_id', n)
        stmp.label = cell.label + f' delithiate {n}'
        output_dict['structure_{:04d}'.format(n)] = stmp

        # Create the mask to map old site to the new sites
        # can be used to redfine per-site properties such as the mangetic moments
        mapping = []
        for i_new, new_site in enumerate(s.sites):
            found = False
            for i_old, old_site in enumerate(struc.sites):
                dist = new_site.distance(old_site)
                if dist < 0.1:
                    mapping.append(i_old)
                    found = True
                    break
            if not found:
                raise RuntimeError(f'Cannot found original site for {new_site}')
        map_array = ArrayData()
        map_array.set_array('site_mapping', np.array(mapping))
        output_dict[f'mapping_{n:04d}'] = map_array

    return output_dict