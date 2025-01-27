"""
Bridging between CLEASE database and AiiDA
"""

from pathlib import Path
from typing import Callable, Tuple, Union

import aiida.orm as orm
from aiida.common.exceptions import NotExistent
from ase.build import sort
from ase.calculators.singlepoint import SinglePointCalculator
from ase.db import connect
from clease.tools import update_db

from aiida_user_addons.process.transform import niggli_reduce
from aiida_user_addons.tools.relax_analyser import RelaxationAnalyser


def dbtoaiida(
    db_path: Union[str, Path],
    group_structure: orm.Group,
    group_reduced_structure: orm.Group = None,
    gen: int = None,
    reduction: Callable = niggli_reduce,
    dryrun: bool = False,
    limit_to_group: bool = False,
):
    """
    Standard method for depositing structure from ase.db generated by CLEASE into AiiDA

    Args:

        db_path (str, Path): Path to the database file
        group_structure: Group used to store the imported structures.
        group_reduced_structure: Group used to store the reduced structures.
        gen (int): Only select this generation for deposition.
        reduction (callable): A callable for performing reduction of the input structure, typically the niggli reduction.
            It should be a @calcfunction wrapped python function.
        dryrun (bool): Wether to perform a dryrun.
        limit_to_group (bool): Limit to the Group when testing if the structure have already been stored.
            Usually it should be False, so the entire database is searched.
    """
    db_path = str(db_path)

    row_filters = {}
    if gen:
        row_filters["gen"] = gen

    if group_reduced_structure is None:
        group_reduced_structure = orm.Group.objects.get_or_create(label=group_structure.label + "-reduced")[0]
    with connect(db_path) as conn:
        for i, row in enumerate(conn.select(converged=False, struct_type="initial", **row_filters)):
            q = orm.QueryBuilder()
            q.append(
                orm.StructureData,
                filters={
                    "attributes.ce_uuid": row.unique_id,
                    "attributes.ce_uid": row.id,
                },
            )
            if limit_to_group:
                q.append(
                    orm.Group,
                    filters={"id": group_structure.id},
                    with_node=orm.StructureData,
                )
            if q.count() > 0:
                print(f"Structure {row.unique_id} {row.name} has been deposited into the database already: {q.first()[0]}")
                continue

            atoms = sort(row.toatoms())  # We have to sort the atoms so it is easier for the subsequent calculations
            atoms.set_tags(None)
            struct = orm.StructureData(ase=atoms)
            struct.base.attributes.set("ce_uuid", row.unique_id)
            struct.base.attributes.set("ce_uid", row.id)
            struct.base.attributes.set("gen", row.gen)
            struct.label = row.name
            struct.description = f"Initial structure generated in {db_path}.db."
            if not dryrun:
                struct.store()
                struct.set_extra_many({"clease_keys": row.key_value_pairs})
                group_structure.add_nodes(struct)

                reduced = reduction(struct)
                group_reduced_structure.add_nodes(reduced)
            print(row.formula, i)


def aiidatodb(
    db_path: Union[Path, str],
    group: orm.Group,
    calc_finder: Callable = None,
    reset_converge_tag=False,
):
    """
    Exact calculation from AiiDA and save them into the CLEASE database.

    Each initial structure in the CLEASE database is checked against a group of workflows.
    Any machine calculations with the initial structure in the CLEASE database will be deposited.

    Args:
        db_path (Path, str): Path to the database file
        group (orm.Group): The AiiDA group containing the workflows
        calc_finder (callable): A callable object for finding the workflow for a given group and UUID of the initial structure
        reset_converge_tag (bool): If true, will reset the converge flag if NO calculation is found in AiiDA for that (ase.db) row.
    """

    if calc_finder is None:
        calc_finder = get_calc_finder()

    conn = connect(str(db_path))
    # Select the initial structures
    rows = list(conn.select(struct_type="initial"))
    # Process the rows
    for row in rows:
        # Find the calculation
        try:
            node, init_it = calc_finder(group, row.unique_id)

        except NotExistent:
            # conn.update(row.id, converged=False)
            print(f"No data for row {row.id} {row.unique_id}")

            ## If not found - set converged to be False
            if reset_converge_tag and not hasattr(row, "final_struct_id"):
                conn.update(row.id, converged=False)
                print(f"No data for row {row.id} {row.unique_id}")
            continue

        ra = RelaxationAnalyser(node)

        # Include metadata in the database
        custom_kvp_init = {
            "aiida_structure_uuid": init_it,
            "aiida_relax_uuid": node.uuid,
        }  # For the initial structure row
        custom_kvp_final = {
            "aiida_structure_uuid": ra.output_structure.uuid,
            "aiida_relax_uuid": node.uuid,
        }  # For the final structure row
        custom_kvp_final_origin = {
            "aiida_structure_uuid": ra.input_structure.uuid,
            "aiida_relax_uuid": node.uuid,
        }  # For input structure for the final structure

        final_atoms = ra.output_structure.get_ase()
        if ra.is_converged:
            edict = ra.node.outputs.misc["total_energies"]
        else:
            edict = ra.last_relax_calc.outputs.misc["total_energies"]

        energy = edict.get("energy_no_entropy", edict.get("energy_extrapolated"))
        assert energy is not None
        calc = SinglePointCalculator(final_atoms, energy=energy)
        final_atoms.set_calculator(calc)

        print(f"Processing {node.label}")

        # Delete the existing structure
        if hasattr(row, "final_struct_id"):
            conn.delete([row.final_struct_id])
        if hasattr(row, "final_origin_struct_id"):
            conn.delete([row.final_origin_struct_id])

        # Inserting the input structure for the geometry optimisation
        final_atoms_origin = ra.input_structure.get_ase()
        final_origin_id = conn.write(final_atoms_origin, key_value_pairs=custom_kvp_final_origin)
        custom_kvp_init["final_origin_struct_id"] = final_origin_id

        update_db(
            uid_initial=row.id,
            final_struct=final_atoms,
            custom_kvp_init=custom_kvp_init,
            custom_kvp_final=custom_kvp_final,
            db_name=db_path,
        )


def get_calc_finder(allowed_exit_status=0):
    """
    Following the link to find a converged calculation

    Returns a tuple of (WorkChainNode, ce_uuid)
    """
    if isinstance(allowed_exit_status, int):
        allowed_exit_status = [allowed_exit_status]

    def _inner(group_workflows, ce_initial_uuid) -> Tuple[orm.WorkChainNode, str]:
        q = orm.QueryBuilder()
        q.append(orm.Group, filters={"id": {"in": [group_workflows.id]}})
        q.append(
            orm.Node,
            with_group=orm.Group,
            filters={"attributes.exit_status": {"in": allowed_exit_status}},
            project=["*"],
        )
        q.append(orm.StructureData, with_outgoing=orm.Node, tag="reduced")
        q.append(
            orm.CalcFunctionNode,
            with_outgoing="reduced",
            filters={"attributes.function_name": "niggli_reduce"},
        )
        q.append(
            orm.StructureData,
            with_outgoing=orm.CalcFunctionNode,
            filters={"attributes.ce_uuid": ce_initial_uuid},
            project=["attributes.ce_uuid"],
        )
        return q.one()

    return _inner
