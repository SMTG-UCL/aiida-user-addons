"""
Module with battery related processes
"""
from typing import Dict, List, Tuple

from aiida.engine import calcfunction
from aiida.orm import Float, StructureData
from ase.build import sort
from bsym.interface.pymatgen import unique_structure_substitutions
from pymatgen.analysis.ewald import EwaldSummation
from pymatgen.analysis.phase_diagram import CompoundPhaseDiagram
from pymatgen.analysis.reaction_calculator import Reaction
from pymatgen.core import Composition, Element, Structure
from pymatgen.entries import Entry

from aiida_user_addons.common.misc import get_energy_from_misc

__version__ = "0.0.1"


@calcfunction
def compute_li_voltage(
    lithiated_structure,
    lithiated_res,
    delithiated_structure,
    delithiated_res,
    li_ref_structure,
    li_ref_res,
):
    """
    Compute Li voltage using and energies and their corresponding structures

    Structures are only used for extracting the composition.
    """

    lith_comp = lithiated_structure.get_pymatgen().composition
    lith_eng = get_energy_from_misc(lithiated_res)
    deli_comp = delithiated_structure.get_pymatgen().composition
    deli_eng = get_energy_from_misc(delithiated_res)
    li_comp = li_ref_structure.get_pymatgen().composition
    li_eng = get_energy_from_misc(li_ref_res)

    reaction = Reaction([lith_comp], [li_comp, deli_comp])
    # How many atoms does the Li reference have?
    nli = li_comp.num_atoms
    # Normalise to one Li in the product, hence the reaction energy is the voltage
    reaction.normalize_to(li_comp, factor=1 / nli)
    eng = reaction.calculate_energy(
        {lith_comp: lith_eng, deli_comp: deli_eng, li_comp: li_eng}
    )
    return Float(eng)


def compute_li_voltage_shortcut(
    lithiated,
    delithiated,
    li_ref=None,
    li_ref_group_name="li-metal-refs",
    store_provenance=True,
):
    """
    Compute voltage from three calculations.

    """
    if li_ref is None:
        indict = get_input_parameters_dict(lithiated.outputs.misc)
        encut = _get_incar_tag("encut", indict)
        gga = _get_incar_tag("gga", indict)
        li_ref = _obtain_li_ref_calc(encut, gga, group_name=li_ref_group_name)

    # Check if the calculations are comparable
    if not _is_comparable(lithiated, delithiated):
        raise RuntimeError("Cannot compare two calculations - parameters mismatch")
    elif not _is_comparable(delithiated, li_ref):
        raise RuntimeError("Cannot compare with the reference - mismatching parameters")

    lith_struct = lithiated.inputs.structure
    deli_struct = delithiated.inputs.structure
    li_ref_struct = li_ref.inputs.structure

    lith_res = lithiated.outputs.misc
    deli_res = delithiated.outputs.misc
    li_ref_res = li_ref.outputs.misc
    metadata = {}
    if not store_provenance:
        metadata["store_provenance"] = False

    return compute_li_voltage(  # pylint: disable=unexpected-keyword-arg
        lith_struct,
        lith_res,
        deli_struct,
        deli_res,
        li_ref_struct,
        li_ref_res,
        metadata=metadata,
    )


def _get_incar_tag(tag, input_dict):
    """
    Obtain incar tag from dict. Handle special cases. Return value in lowercase.
    """
    if "vasp" in input_dict:
        input_dict = input_dict["vasp"]
    elif "incar" in input_dict:
        input_dict = input_dict["incar"]

    value = input_dict.get(tag)
    # Special case the GGA tag - None is pe
    if (tag == "gga") and (value is None):
        return "pe"
    if isinstance(value, str):
        return value.lower()
    return value


def _obtain_li_ref_calc(encut, gga, group_name="li-metal-refs"):
    """
    Return the reference calculation for Li metal

    WARNING: This works for only calculation performed using PBE pseudopotentials
    """
    from aiida.orm import Dict, Group, QueryBuilder, WorkChainNode

    if gga is None:
        gga = "pe"
    qdb = QueryBuilder()
    qdb.append(Group, filters={"label": group_name})
    qdb.append(
        WorkChainNode,
        with_group=Group,
        filters={"attributes.exit_status": 0},
        project=["*"],
    )
    qdb.append(
        Dict,
        with_outgoing=WorkChainNode,
        filters={
            "or": [
                {"attributes.vasp.encut": encut, "attributes.vasp.gga": gga},
                {"attributes.incar.encut": encut, "attributes.incar.gga": gga},
            ]
        },
        edge_filters={"label": "parameters"},
    )

    matches = qdb.all()
    if len(matches) > 1:
        print(f"WARNING: more than one matches found for gga:{gga} encut:{encut}")
    if len(matches) == 0:
        raise RuntimeError(f"ERROR: No matche found for gga:{gga} encut:{encut}")
    return matches[0][0]


def check_li_ref_calc(encut, gga, group_name="li-metal-refs"):
    from aiida.orm import Dict, Group, QueryBuilder, WorkChainNode

    if gga is None:
        gga = "pe"
    q = QueryBuilder()
    q.append(Group, filters={"label": group_name})
    q.append(
        WorkChainNode,
        with_group=Group,
        filters={"attributes.exit_status": 0},
        project=["*"],
    )
    q.append(
        Dict,
        with_outgoing=WorkChainNode,
        filters={
            "or": [
                {"attributes.vasp.encut": encut, "attributes.vasp.gga": gga},
                {"attributes.incar.encut": encut, "attributes.incar.gga": gga},
            ]
        },
        edge_filters={"label": "parameters"},
    )

    nmatch = q.count()
    if nmatch > 1:
        print(f"WARNING: more than one matches found for gga:{gga} encut:{encut}")
        return True
    if nmatch == 1:
        return True
    if nmatch == 0:
        return False


def list_li_ref_calcs(group_name="li-metal-refs"):
    """Return the reference calculation for Li metal"""
    from aiida.orm import Dict, Group, QueryBuilder, WorkChainNode

    qdb = QueryBuilder()
    qdb.append(Group, filters={"label": group_name})
    qdb.append(WorkChainNode, with_group=Group, project=["*"])
    qdb.append(
        Dict,
        with_outgoing=WorkChainNode,
        project=["attributes.vasp"],
        edge_filters={"label": "parameters"},
    )

    matches = qdb.all()
    return matches


def _is_comparable(calc1, calc2):
    """Check wether two calculations can be compared"""
    critical_keys = ["encut", "lreal", "prec", "gga"]
    warn_keys = ["ismear", "sigma"]
    indict1 = get_input_parameters_dict(calc1.outputs.misc)
    indict2 = get_input_parameters_dict(calc2.outputs.misc)
    for key in critical_keys:
        v1 = _get_incar_tag(key, indict1)
        v2 = _get_incar_tag(key, indict2)
        if v1 != v2:
            print(f"Critical key mismatch {key} - {v1} vs {v2}")
            return False
    for key in warn_keys:
        if _get_incar_tag(key, indict1) != _get_incar_tag(key, indict2):
            print(
                f"WARNING: mismatch in key {key} - two calculations may not be comparable"
            )
    return True


def get_input_parameters_dict(out_node):
    """
    Get the input parameters for the output.
    This can be used to trace the exact inputs (not those for the workchain)
    that used to obtain the misc.
    """
    from aiida.orm import CalcJobNode, Dict, Node, QueryBuilder

    qdb = QueryBuilder()
    qdb.append(Node, filters={"id": out_node.pk}, tag="out")
    qdb.append(CalcJobNode, with_outgoing="out")
    qdb.append(Dict, with_outgoing=CalcJobNode, edge_filters={"label": "parameters"})
    return qdb.one()[0].get_dict()


class DelithiationManager:
    """Utility tool for managing delithiation process"""

    def __init__(
        self, structure: Structure, working_ion="Li", tm_ions=("Fe", "Mn", "Co", "Ni")
    ):
        """Instantiate a `DelithiationManager` object by giving a structure"""
        self.structure = structure
        comp = structure.composition
        self.composition = comp
        self.working_ion = working_ion
        self.tm_ions = tm_ions

        # Analyse the content
        self.nli = int(self.composition[working_ion])
        self.composition_without_li = Composition(
            {key: comp[key] for key in comp if key.symbol != working_ion}
        )
        self.nother = self.composition_without_li.num_atoms

    @property
    def reduced_non_working_composition(self):
        return self.composition_without_li.reduced_composition

    @property
    def lithiation_level(self):
        """Stable lithiation level normalised per reduced non-working formula"""
        _, factor = self.composition_without_li.get_reduced_composition_and_factor()
        return self.nli / factor

    def get_conventional_li_level_representation(self):
        """
        Return the number of working ions per reduced formula for the non-working part
        For example: LiCoO2 -> (1, CoO2), Li3Co4O8 -> (3/4, CoO2).
        This is useful when plotting the level of delithiations.
        """
        (
            reduced,
            factor,
        ) = self.composition_without_li.get_reduced_composition_and_factor()
        return self.nli / factor, reduced

    def create_delithaited_structures(
        self, num_remove, atol=1e-5, dummy="He"
    ) -> List[Structure]:
        """
        Generated delithiated structures
        """

        nli = self.nli
        subs = unique_structure_substitutions(
            self.structure, "Li", {"Li": nli - num_remove, dummy: num_remove}, atol=atol
        )
        for structure in subs:
            structure.remove_species([dummy])
        return subs

    def create_delithiated_structures_multiple_levels(
        self,
        final_li_level: float = 0.0,
        atol=1e-5,
        dummy="He",
        oxidation_state_mapping: Dict[str, float] = None,
        pick_ewald_n_lowest: int = None,
    ) -> Dict[int, List[Structure]]:
        """
        Create delithiated structures at multiple levels

        This method is useful for generating structures to be relaxed for voltage curve extraction.

        Args:
            final_li_level (float): The final level of lithiation
            oxidation_state_mapping (dict): Mapping of the oxidation states, used for filtering using Ewald summation.
            pick_ewald_n_lowest (Int): Pick only the N lowest structures for each level.
            dummy (str): Symbol of the dummy specie to be used.

        Returns:
            A dictionary with keys being the number of Li removed from the structure and values being the unique
                structures at such delithiation level.
        """

        nli = self.nli
        frac_remove = 1 - (final_li_level / self.lithiation_level)
        max_remove = nli * frac_remove
        if abs(round(max_remove) - max_remove) > 1e-5:
            raise RuntimeError(
                f"The final lithiation level ({final_li_level}) does not represent an integer number ({self.lithiation_level})" 
                f"of {self.working_ion} atoms in the unit cell (requested to remove {max_remove} atoms)."
            )
        max_remove = int(round(max_remove))
        records = {}
        for num_remove in range(1, max_remove + 1):
            frames = self.create_delithaited_structures(
                num_remove, dummy=dummy, atol=atol
            )
            # If taking only N lowest energy states....
            if pick_ewald_n_lowest is not None:
                if oxidation_state_mapping is None:
                    raise ValueError(
                        "Keyword argument 'oxidation_state_mapping' must be passed for ranking with electrostatic energy."
                    )
                for frame in frames:
                    frame.add_oxidation_state_by_element(oxidation_state_mapping)
                frames = sorted_by_ewald(frames)
                # Remove the oxidation states
                for frame in frames:
                    frame.remove_oxidation_states()
                # Pick only the top structures
                records[num_remove] = frames[:pick_ewald_n_lowest]
            else:
                records[num_remove] = frames
        return records


def remove_composition(entries: List[Entry], comp: str) -> List[Entry]:
    """Remove a specific composition from a list of entries"""
    return [
        entry
        for entry in entries
        if entry.composition.reduced_formula != comp.reduced_formula
    ]


class VoltageCurve:
    """
    Class for analysing and computing the voltage

    Attributes:
        entries: list of entries from which the voltage curves to be computed.
        ref_entry: The reference entry for the working ion.
        working_ion: name of the working ion.
    """

    def __init__(self, entries: List[Entry], ref_entry: Entry, working_ion="Li"):
        """Instantiate a VoltageCurve object"""
        self.entries = list(entries)
        self.ref_entry = ref_entry
        self.working_ion = working_ion

        # Sort the entries with decreasing Li content
        self.entries.sort(
            key=lambda x: x.composition[working_ion] / x.composition.num_atoms,
            reverse=True,
        )

        # Find the terminal compositions
        lithiated = self.entries[0].composition  # One with the maximum lithation level
        non_working_lithiated = Composition(
            {key: lithiated[key] for key in lithiated if key.symbol != working_ion}
        )
        delithiated = self.entries[-1].composition
        non_working_delithiated = Composition(
            {key: delithiated[key] for key in delithiated if key.symbol != working_ion}
        )

        # Sanity check
        assert (
            non_working_lithiated.reduced_composition
            == non_working_delithiated.reduced_composition
        )

        # Normalise terminal composition to the delithiated composition
        factor = non_working_lithiated.num_atoms / non_working_delithiated.num_atoms

        self.phase_diagram = CompoundPhaseDiagram(
            self.entries,
            terminal_compositions=[
                self.entries[0].composition,
                self.entries[-1].composition * factor,
            ],
            normalize_terminal_compositions=False,
        )
        self.stable_entries = [
            entry.original_entry for entry in self.phase_diagram.stable_entries
        ]
        self.stable_entries.sort(
            key=lambda x: x.composition[working_ion] / x.composition.num_atoms,
            reverse=True,
        )

    @property
    def included_compositions(self):
        all_comps = list(
            {entry.composition.reduced_composition for entry in self.entries}
        )
        all_comps.sort(key=lambda x: x[self.working_ion] / x.num_atoms, reverse=True)
        return all_comps

    @property
    def stable_compositions(self):
        all_comps = list(
            {entry.composition.reduced_composition for entry in self.stable_entries}
        )
        all_comps.sort(key=lambda x: x[self.working_ion] / x.num_atoms, reverse=True)
        return all_comps

    @property
    def average_voltage(self):
        """Return the average voltage between most lithiated and delithiated phases."""
        return voltage_between_pair(
            self.stable_entries[0],
            self.stable_entries[-1],
            self.ref_entry,
            self.working_ion,
        )

    def __repr__(self):

        formula = self.entries[0].composition.reduced_formula
        nentry = len(self.entries)
        output = f"VoltageCurve for {formula} with {nentry} entries"
        output += (
            f"\nAverage voltage: {self.average_voltage:.3f}\nCompositions: (* stable)"
        )
        # Find the compositions
        all_comps = self.included_compositions
        stable_comps = self.stable_compositions
        for comp in all_comps:
            output += f"\n{comp.reduced_formula}"
            if comp in stable_comps:
                output += " (*)"
        return output

    def compute_voltages(self) -> List[Tuple[List[Composition], float]]:
        """
        Compute the voltages
        Returns:
            a list of composition pairs and voltages.
        """
        nstable = len(self.stable_entries)
        conc_pair_and_voltage = []
        for i in range(nstable - 1):
            lith = self.stable_entries[i]
            deli = self.stable_entries[i + 1]
            voltage = voltage_between_pair(lith, deli, self.ref_entry, self.working_ion)
            conc_pair_and_voltage.append(
                [(lith.composition, deli.composition), voltage]
            )
        return conc_pair_and_voltage

    def get_plot_data(
        self, norm_formula=None, x_axis_deli=False
    ) -> Tuple[List[float], List[float]]:
        """
        Return the data used for ploting.

        Args:
            norm_formula(str): Fully lithiated formula to be used for normalisation
            x_axis_deli(bool): If set to True, return the level of delithiation for the x axis.

        Returns:
            a tuple of x and y values to be used for plotting.
        """
        x_comp = []
        y_volt = []
        for i, (comps, vol) in enumerate(self.compute_voltages()):
            x_comp.extend(comps)
            y_volt.extend([vol, vol])

        # Now adapat the x axis
        if norm_formula is None:
            norm_formula = self.stable_entries[0].composition.reduced_formula

        norm = Composition(norm_formula)
        li_norm_conc = ion_conc(norm, self.working_ion)
        nli_norm = norm[self.working_ion]
        x_val = []
        for comp in x_comp:
            conc = ion_conc(comp, self.working_ion)
            eff_nli = (
                conc / li_norm_conc * nli_norm
            )  # Effective Li number refected to the normalisation formula

            if x_axis_deli:
                x_val.append(nli_norm - eff_nli)
            else:
                x_val.append(eff_nli)

        return x_val, y_volt

    def plot_voltages(self, norm_formula=None, x_axis_deli=True, ax=None):
        """Plot the voltages"""
        import matplotlib.pyplot as plt

        if ax is None:
            _, ax = plt.subplots(1, 1)

        if norm_formula is None:
            norm_formula = self.stable_entries[0].composition.reduced_formula
        x, y = self.get_plot_data(norm_formula, x_axis_deli)
        ax.plot(x, y)
        ax.set_label("Voltage (V)")
        comp = dict(Composition(norm_formula))
        string = ""
        nli = comp[Element(self.working_ion)]
        for key, value in comp.items():
            if str(key) == self.working_ion:
                if x_axis_deli:
                    string += f"{str(key)}_{{{nli:.0f}-x}}"
                else:
                    string += f"{str(key)}_{{x}}"
            elif value == 1.0:
                string += f"{str(key)}"
            else:
                string += f"{str(key)}_{{{value:.0f}}}"

        if x_axis_deli:
            ax.set_ylabel("Voltage (V)")
            ax.set_xlabel(r"$\mathrm{" + string + "}$")

        return ax


def voltage_between_pair(lith, deli, ref_entry, working_ion="Li") -> float:
    """
    Compute the voltages between two pair of entries

    Args:
        lith: Entry for the lithiate phase
        deli: Entry for the delithiated phase
        ref_entry: Entry for the working ion
        working_ion: Name of teh workion ion. Defaults to Li

    Returns:
        The voltage
    """
    # Get the compensation factor
    nform1 = lith.composition.num_atoms - lith.composition[working_ion]
    nform2 = deli.composition.num_atoms - deli.composition[working_ion]
    nli1 = lith.composition[working_ion]
    nli2 = deli.composition[working_ion]
    effective_change = (
        nli1 - nli2 / nform2 * nform1
    )  # Normalised to the delithiated phase
    e1 = lith.energy
    e2 = deli.energy
    de = e2 / nform2 * nform1 - e1
    # print(de, effective_change, nform1, nform2)
    # Voltage is the change of free energy (energy) per Li
    voltage = (
        de + effective_change * ref_entry.energy / ref_entry.composition.num_atoms
    ) / effective_change
    return voltage


def ion_conc(comp: Composition, ion: str) -> float:
    """Return the Li concentration, normalised to the non-Li part"""
    nli = comp[ion]
    nother = sum(comp[key] for key in comp if key.symbol != ion)
    li_conc = nli / nother  # Li concentration in the reference
    return li_conc


def count_delithiated_multiple_level(
    structure: Structure, final_li_level: float, atol=1e-5
):
    """Return the total number of structures to be generated"""
    manager = DelithiationManager(structure)
    frame_dict = manager.create_delithiated_structures_multiple_levels(
        float(final_li_level), atol=atol
    )
    return sum(len(sublist) for sublist in frame_dict.values())


@calcfunction
def create_delithiated_multiple_level(
    structure, final_li_level, rattle, **params
) -> Dict[str, StructureData]:
    """
    Create a series of delithiated frames with different lithiation levels

    This function is essentially an wrapper for the DelithiationManager methods

    The outputs are placed in a flat dictionary with the keying being '<nremoved>_<idex>'.
    """

    if "atol" in params:
        atol = params["atol"].value
    else:
        atol = 1e-5
    ewald_filter_settings = params.get("ewald_filter_settings", {})
    if ewald_filter_settings:
        ewald_filter_settings = ewald_filter_settings.get_dict()

    struct = structure.get_pymatgen()
    manager = DelithiationManager(struct)
    ok = False
    while not ok:
        try:
            frame_dict = manager.create_delithiated_structures_multiple_levels(
                float(final_li_level), atol=atol, **ewald_filter_settings
            )
        except ValueError:
            atol *= 10
            print(f"Increased symmetry tolerance to: {atol} and retry")
        else:
            ok = True
        if atol > 0.01:
            raise ValueError("Persisting symmetry error - aborting the process")
    # Process the frame_dict output and convert each frame to orm.StructureData
    output = {}
    for nremoved, value in frame_dict.items():
        for idx, frame in enumerate(value):
            # Rattle the atoms and perform sorting by species
            new_structure = StructureData(pymatgen=frame)
            atoms = new_structure.get_ase()
            if rattle.value > 0:
                atoms.rattle(rattle.value)
            new_structure = StructureData(ase=sort(atoms))
            new_structure.label = structure.label + f" DELI {nremoved} {idx}"
            new_structure.description = f"Delithiated structure with {nremoved} removed Li atoms and index {nremoved} of the returned unique structures."
            output["structure_" + str(nremoved) + f"_{idx}"] = new_structure
    return output


def sorted_by_ewald(structures: List[Structure]) -> List[Structure]:
    """
    Sort the structures by Ewald energy
    """
    return sorted(structures, key=lambda x: EwaldSummation(x).total_energy)
