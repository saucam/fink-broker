# Copyright 2019 AstroLab Software
# Author: Julien Peloton
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from pyspark.sql import DataFrame
from pyspark.sql.functions import concat_ws, col, lit
from pyspark.mllib.common import _java2py
from pyspark.sql import SparkSession

import numpy as np

import os

from fink_broker.sparkUtils import get_spark_context
from fink_broker.tester import spark_unit_tests

def load_science_portal_column_names():
    """ Load names of the alert fields to use in the science portal.

    These column names should match DataFrame column names. Careful when
    you update it, as it will change the structure of the HBase table.

    The column names are sorted by column family names:
        - i: for column that identify the alert (original alert)
        - d: for column that further describe the alert (Fink added value)
        - b: for binary blob (FITS image)

    Returns
    --------
    cols_*: list of string
        List of DataFrame column names to use for the science portal

    Examples
    --------
    >>> cols_i, cols_d, cols_b = load_science_portal_column_names()
    >>> print(len(cols_d))
    2
    """
    # Column family i
    cols_i = [
        'objectId',
        'schemavsn',
        'publisher',
        'candidate.*'
    ]

    # Column family d
    cols_d = [
        'cdsxmatch',
        'rfscore'
    ]

    # Column family b
    cols_b = [
        col('cutoutScience.stampData').alias('cutoutScience'),
        col('cutoutTemplate.stampData').alias('cutoutTemplate'),
        col('cutoutDifference.stampData').alias('cutoutDifference')
    ]

    return cols_i, cols_d, cols_b

def assign_column_family_names(df, cols_i, cols_d, cols_b):
    """ Assign a column family name to each column qualifier.

    There are currently 3 column families:
        - i: for column that identify the alert (original alert)
        - d: for column that further describe the alert (Fink added value)
        - b: for binary blob (FITS image)

    The split is done in `load_science_portal_column_names`.

    Parameters
    ----------
    df: DataFrame
        Input DataFrame containing alert data from the raw science DB (parquet).
        See `load_parquet_files` for more information.
    cols_*: list of string
        List of DataFrame column names to use for the science portal.

    Returns
    ---------
    cf: dict
        Dictionary with keys being column names (also called
        column qualifiers), and the corresponding column family.

    """
    cf = {i: 'i' for i in df.select(cols_i).columns}
    cf.update({i: 'd' for i in df.select(cols_d).columns})
    cf.update({i: 'b' for i in df.select(cols_b).columns})

    return cf

def retrieve_row_key_cols():
    """ Retrieve the list of columns to be used to create the row key.

    The column names are defined here. Be careful in not changing it frequently
    as you can replace (remove and add) columns for existing table,
    but you cannot change keys, you must copy the table into new table
    when changing keys design.

    Returns
    --------
    row_key_cols: list of string
    """
    # build the row key: objectId_jd_ra_dec
    row_key_cols = [
        'objectId',
        'jd',
        'ra',
        'dec'
    ]
    return row_key_cols

def attach_rowkey(df, sep='_'):
    """ Create and attach the row key to an existing DataFrame.

    The column used to define the row key are declared in
    `retrieve_row_key_cols`. the row key is made of a string concatenation
    of those column data, with a separator: str(col1_col2_col3_etc)

    Parameters
    ----------
    df: DataFrame
        Input DataFrame containing alert data from the raw science DB (parquet),
        and already flattened with a select (i.e. candidate.jd must be jd).

    Returns
    ----------
    df: DataFrame
        Input DataFrame with a new column with the row key. The type of the
        row key value is string.
    row_key_name: string
        Name of the rowkey, made of the columns that were used.

    Examples
    ----------
    # Read alert from the raw database
    >>> df_raw = spark.read.format("parquet").load(ztf_alert_sample_rawdatabase)

    # Select alert data
    >>> df = df_raw.select("decoded.*")

    >>> df = df.select(['objectId', 'candidate.*'])

    >>> df_rk, row_key_name = attach_rowkey(df)

    >>> 'objectId_jd_ra_dec' in df_rk.columns
    True
    """
    row_key_cols = retrieve_row_key_cols()
    row_key_name = '_'.join(row_key_cols)

    to_concat = [col(i).astype('string') for i in row_key_cols]

    df = df.withColumn(
        row_key_name,
        concat_ws(sep, *to_concat)
    )
    return df, row_key_name

def construct_hbase_catalog_from_flatten_schema(
        schema: dict, catalogname: str, rowkeyname: str, cf: dict) -> str:
    """ Convert a flatten DataFrame schema into a HBase catalog.

    From
    {'name': 'schemavsn', 'type': 'string', 'nullable': True, 'metadata': {}}

    To
    'schemavsn': {'cf': 'i', 'col': 'schemavsn', 'type': 'string'},

    Parameters
    ----------
    schema : dict
        Schema of the flatten DataFrame.
    catalogname : str
        Name of the HBase catalog.
    rowkeyname : str
        Name of the rowkey in the HBase catalog.
    cf: dict
        Dictionary with keys being column names (also called
        column qualifiers), and the corresponding column family.
        See `assign_column_family_names`.

    Returns
    ----------
    catalog : str
        Catalog for HBase.

    Examples
    --------
    # Read alert from the raw database
    >>> df_raw = spark.read.format("parquet").load(ztf_alert_sample_rawdatabase)

    # Select alert data and Kafka publication timestamp
    >>> df_ok = df_raw.select("decoded.*", "timestamp")

    >>> cols_i, cols_d, cols_b = load_science_portal_column_names()

    >>> cf = assign_column_family_names(df_ok, cols_i, [], [])

    # Flatten the DataFrame
    >>> df_flat = df_ok.select(cols_i)

    Attach the row key
    >>> df_rk, row_key_name = attach_rowkey(df_flat)

    >>> catalog = construct_hbase_catalog_from_flatten_schema(
    ...     df_rk.schema, "mycatalogname", row_key_name, cf)
    """
    schema_columns = schema.jsonValue()["fields"]

    catalog = ''.join("""
    {{
        'table': {{
            'namespace': 'default',
            'name': '{}'
        }},
        'rowkey': '{}',
        'columns': {{
    """).format(catalogname, rowkeyname)

    for column in schema_columns:
        # Last entry should not have comma (malformed json)
        if schema_columns.index(column) != len(schema_columns) - 1:
            sep = ","
        else:
            sep = ""

        # Deal with array
        if type(column["type"]) == dict:
            column["type"] = "string" # column["type"]["type"]

        if type(column["type"]) == 'timestamp':
            column["type"] = "string" # column["type"]["type"]

        if column["name"] == rowkeyname:
            catalog += """
            '{}': {{'cf': 'rowkey', 'col': '{}', 'type': '{}'}}{}
            """.format(column["name"], column["name"], column["type"], sep)
        else:
            catalog += """
            '{}': {{'cf': '{}', 'col': '{}', 'type': '{}'}}{}
            """.format(
                column["name"],
                cf[column["name"]],
                column["name"],
                column["type"],
                sep
            )
    catalog += """
        }
    }
    """

    return catalog.replace("\'", "\"")

def construct_schema_row(df, rowkeyname, version):
    """ Construct a DataFrame whose columns are those of the
    original ones, and one row containing schema types

    Parameters
    ----------
    df: Spark DataFrame
        Input Spark DataFrame. Need to be flattened.
    rowkeyname: string
        Name of the HBase row key (column name)
    version: string
        Version of the HBase table (row value for the rowkey column).

    Returns
    ---------
    df_schema: Spark DataFrame
        Spark DataFrame with one row (the types of its column). Only the row
        key is the version of the HBase table.

    Examples
    --------
    # Read alert from the raw database
    >>> df_raw = spark.read.format("parquet").load(ztf_alert_sample_rawdatabase)

    # Select alert data and Kafka publication timestamp
    >>> df = df_raw.select("decoded.*", "timestamp")

    # inplace replacement
    >>> df = df.select(['objectId', 'candidate.jd', 'candidate.candid'])
    >>> df = df.withColumn('schema_version', lit(''))
    >>> df = construct_schema_row(df, rowkeyname='schema_version', version='schema_v0')
    >>> df.show()
    +--------+------+------+--------------+
    |objectId|    jd|candid|schema_version|
    +--------+------+------+--------------+
    |  string|double|  long|     schema_v0|
    +--------+------+------+--------------+
    <BLANKLINE>
    """
    # Grab the running Spark Session,
    # otherwise create it.
    spark = SparkSession \
        .builder \
        .getOrCreate()

    # Original df columns, but values are types.
    data = [(c.jsonValue()['type']) for c in df.schema]

    index = np.where(np.array(df.columns) == rowkeyname)[0][0]
    data[index] = version

    # Create the DataFrame
    df_schema = spark.createDataFrame([data], df.columns)

    return df_schema

def flattenstruct(df: DataFrame, columnname: str) -> DataFrame:
    """ From a nested column (struct of primitives),
    create one column per struct element.

    The routine accesses the JVM under the hood, and calls the
    Scala routine flattenStruct. Make sure you have the fink_broker jar
    in your classpath.

    Example:
    |-- candidate: struct (nullable = true)
    |    |-- jd: double (nullable = true)
    |    |-- fid: integer (nullable = true)

    Would become:
    |-- candidate_jd: double (nullable = true)
    |-- candidate_fid: integer (nullable = true)

    Parameters
    ----------
    df : DataFrame
        Nested Spark DataFrame
    columnname : str
        The name of the column to flatten.

    Returns
    -------
    DataFrame
        Spark DataFrame with new columns from the input column.

    Examples
    -------
    >>> df = spark.read.format("avro").load(ztf_alert_sample)

    # Candidate is nested
    >>> s = df.schema
    >>> typeOf = {i.name: i.dataType.typeName() for  i in s.fields}
    >>> typeOf['candidate'] == 'struct'
    True

    # Flatten it
    >>> df_flat = flattenstruct(df, "candidate")
    >>> "candidate_ra" in df_flat.schema.fieldNames()
    True

    # Each new column contains array element
    >>> s_flat = df_flat.schema
    >>> typeOf = {i.name: i.dataType.typeName() for  i in s_flat.fields}
    >>> typeOf['candidate_ra'] == 'double'
    True
    """
    sc = get_spark_context()
    obj = sc._jvm.com.astrolabsoftware.fink_broker.catalogUtils
    _df = obj.flattenStruct(df._jdf, columnname)
    df_flatten = _java2py(sc, _df)
    return df_flatten

def explodearrayofstruct(df: DataFrame, columnname: str) -> DataFrame:
    """From a nested column (array of struct),
    create one column per array element.

    The routine accesses the JVM under the hood, and calls the
    Scala routine explodeArrayOfStruct. Make sure you have the fink_broker jar
    in your classpath.

    Example:
    |    |-- prv_candidates: array (nullable = true)
    |    |    |-- element: struct (containsNull = true)
    |    |    |    |-- jd: double (nullable = true)
    |    |    |    |-- fid: integer (nullable = true)

    Would become:
    |-- prv_candidates_jd: array (nullable = true)
    |    |-- element: double (containsNull = true)
    |-- prv_candidates_fid: array (nullable = true)
    |    |-- element: integer (containsNull = true)

    Parameters
    ----------
    df : DataFrame
        Input nested Spark DataFrame
    columnname : str
        The name of the column to explode

    Returns
    -------
    DataFrame
        Spark DataFrame with new columns from the input column.

    Examples
    -------
    >>> df = spark.read.format("avro").load(ztf_alert_sample)

    # Candidate is nested
    >>> s = df.schema
    >>> typeOf = {i.name: i.dataType.typeName() for  i in s.fields}
    >>> typeOf['prv_candidates'] == 'array'
    True

    # Flatten it
    >>> df_flat = explodearrayofstruct(df, "prv_candidates")
    >>> "prv_candidates_ra" in df_flat.schema.fieldNames()
    True

    # Each new column contains array element cast to string
    >>> s_flat = df_flat.schema
    >>> typeOf = {i.name: i.dataType.typeName() for  i in s_flat.fields}
    >>> typeOf['prv_candidates_ra'] == 'string'
    True
    """
    sc = get_spark_context()
    obj = sc._jvm.com.astrolabsoftware.fink_broker.catalogUtils
    _df = obj.explodeArrayOfStruct(df._jdf, columnname)
    df_flatten = _java2py(sc, _df)
    return df_flatten


if __name__ == "__main__":
    """ Execute the test suite with SparkSession initialised """

    globs = globals()
    root = os.environ['FINK_HOME']
    globs["ztf_alert_sample"] = os.path.join(
        root, "schemas/template_schema_ZTF_3p3.avro")

    globs["ztf_alert_sample_rawdatabase"] = os.path.join(
        root, "schemas/template_schema_ZTF_rawdatabase.parquet")

    # Run the Spark test suite
    spark_unit_tests(globs, withstreaming=False)
