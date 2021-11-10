import os
import click

from .click_utils import (
    command,
    die,
    )


_index_sql = None
            

fragdb_merge_sql = """
-- This expects the database to import to be attached as 'old'
-- using something like:
--   attach database "subset.000.fragdb" as old

-- Step 1: Copy over the error_record table


INSERT INTO error_record (title, input_smiles, errmsg)
 SELECT title, input_smiles, errmsg
   FROM old.error_record
        ;

-- Step 2: Copy over the record table


INSERT INTO record (title, input_smiles, num_normalized_heavies, normalized_smiles)
 SELECT title, input_smiles, num_normalized_heavies, normalized_smiles
   FROM old.record
        ;


-- Step 3: Copy over the fragmentation table

-- The fragmentation is self-contained. All we need to do is get the
-- correct fragment id, which we can do with a simple title lookup
-- because the title was added in step 2.

INSERT INTO fragmentation (
	record_id,
	num_cuts,
	enumeration_label,
	variable_num_heavies,
	variable_symmetry_class,
	variable_smiles,
	attachment_order,
	constant_num_heavies,
	constant_symmetry_class,
	constant_smiles,
	constant_with_H_smiles)
 SELECT new_record.id,
	old_fragmentation.num_cuts,
	old_fragmentation.enumeration_label,
	old_fragmentation.variable_num_heavies,
	old_fragmentation.variable_symmetry_class,
	old_fragmentation.variable_smiles,
	old_fragmentation.attachment_order,
	old_fragmentation.constant_num_heavies,
	old_fragmentation.constant_symmetry_class,
	old_fragmentation.constant_smiles,
	old_fragmentation.constant_with_H_smiles
   FROM record as new_record,
        old.record as old_record,
        old.fragmentation as old_fragmentation
  WHERE old_record.title = new_record.title AND
        old_fragmentation.record_id = old_record.id
        ;
"""

def open_output_fragdb(filename, options):
    import sqlite3
    from .. import fragment_db
    from .. import schema
    
    # Remove any existing file.
    try:
        os.unlink(filename)
    except FileNotFoundError:
        pass
    db = sqlite3.connect(filename)
    c = db.cursor()
    fragment_db.init_fragdb(c, options)
    schema._execute_sql(c, fragment_db.get_fragment_create_index_sql())
    return db, c

def check_options_mismatch(filename, options, first_filename, first_options):
    d = options.to_dict()
    first_d = first_options.to_dict()
    if d == first_d:
        return

    # Figure out which values are different
    lines = [f"Cannot merge. The options in {filename!r} differ from {first_filename!r}."]
    for k in d:
        if d[k] != first_d[k]:
            lines.append(f"  {k}: {d[k]!r} != {first_d[k]!r}")
    die(*lines)

@command(name="fragdb_merge")

@click.option(
    "--output",
    "-o",
    "output_filename",
    default = None,
    )

@click.argument(
    "filenames",
    metavar="FILENAME",
    nargs=-1,
    required=True,
    )
@click.pass_obj
def fragdb_merge(
        reporter,
        filenames,
        output_filename,
        ):
    assert filenames, "should have been handled by click"
    from .. import fragment_db, schema
    import sqlite3

    if output_filename is None:
        output_filename = "merged.fragdb"
        reporter.report(f"No --output file name specified. Using {output_filename!r}.")
    
    first_filename = None
    first_options = None
    output_db = None
    output_c = None

    num_records = num_error_records = None
    try:
        for filename in filenames:
            # Ensure it's a valid SQLite database
            try:
                old_db = fragment_db.open_fragdb(filename)
            except ValueError as err:
                die(str(err))
            old_options = old_db.options
            old_db.close()

            if first_options is None:
                first_options = old_options
                first_filename = filename
                try:
                    output_db, output_c = open_output_fragdb(
                        output_filename,
                        first_options,
                        )
                except sqlite3.OperationalError as err:
                    die(f"Error trying to open {output_filename!r} for writing: {err}")
            else:
                check_options_mismatch(filename, old_options, first_filename, first_options)

            try:
                output_c.execute("ATTACH DATABASE ? AS old", (filename,))
            except sqlite3.OperationalError as err:
                die(f"Cannot attach {filename!r} using sqlite3: {err}")

            try:
                # Check for any duplicate record ids
                output_c.execute("""
SELECT old_record.title
  FROM old.record as old_record, record as new_record
 WHERE old_record.title = new_record.title
""")
                for (title,) in output_c:
                    die(
                        f"Cannot merge {filename!r}: Duplicate record id {title!r}.",
                         "  (Use 'fragdb_merge' to merge fragdb files from fragmenting different SMILES files,",
                         "   not to merge the fragdb files generated by 'fragdb_split'.)"
                        )

                # We're free to merge!
                schema._execute_sql(output_c, fragdb_merge_sql)
                
            finally:
                output_c.execute("COMMIT")
                output_c.execute("DETACH DATABASE old")
                output_c.execute("BEGIN TRANSACTION")
            
    finally:
        if output_c is not None:
            output_c.execute("COMMIT")
            num_records, = next(output_c.execute("SELECT count(*) from record"))
            num_error_records, = next(output_c.execute("SELECT count(*) from error_record"))
            output_c.close()
            output_db.close()

    if num_records is not None and num_error_records is not None:
        reporter.report(
            "Merge complete. "
            f"#files: {len(filenames)} "
            f"#records: {num_records} "
            f"#error records: {num_error_records}"
            )
