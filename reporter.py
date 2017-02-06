import json
import os

import datetime
import pandas
import glob

from sqlalchemy import Boolean
from sqlalchemy import Date, DateTime, Float, BigInteger, String
from sqlalchemy import Table, Column
from sqlalchemy import MetaData
from sqlalchemy.exc import StatementError
from sqlalchemy.sql.ddl import CreateSchema

import settings
import resources
from run_config import hpo_ids, use_multi_schemas, engine, datetime_tpe

LOG_TABLE_NAME = 'pmi_sprint_reporter_log'
SCHEMA_EXISTS_QUERY = "SELECT 1 FROM information_schema.schemata WHERE schema_name = '%s'"


def create_schema(schema):
    """
    Create schema if it doesn't exist
    :param schema: name of schema
    :return:
    """
    result = engine.execute(SCHEMA_EXISTS_QUERY % schema)
    if result.rowcount == 0:
        engine.execute(CreateSchema(schema))


def drop_tables(schema):
    """
    Drop any existing CDM tables (only PMI-related and logging tables)
    :param schema: Database schema
    :return:
    """
    metadata = MetaData(bind=engine, reflect=True, schema=schema)
    pmi_tables = pandas.read_csv(resources.pmi_tables_csv_path).table_name.unique()
    tables_to_drop = filter(lambda t: t.name in pmi_tables or t.name == LOG_TABLE_NAME, metadata.sorted_tables)
    metadata.drop_all(tables=tables_to_drop)


def create_tables(schema):
    """
    Create CDM tables within the specified database schema.
    :param schema: Database schema to create the tables in
    :return:
    """
    metadata = MetaData()
    cdm_df = pandas.read_csv(resources.cdm_csv_path)
    tables = cdm_df.groupby(['table_name'])

    for table_name, table_df in tables:
        columns = []
        for index, (_, column_name, is_nullable, data_type, _) in table_df.iterrows():
            if data_type in ('character varying', 'text'):
                tpe = String(500)
            elif data_type == 'integer':
                tpe = BigInteger()
            elif data_type == 'numeric':
                tpe = Float()
            elif data_type == 'date':
                tpe = Date()
            elif data_type == 'datetime':
                tpe = datetime_tpe
            else:
                raise NotImplementedError('Unexpected data_type `%s` in cdm.csv' % data_type)
            nullable = is_nullable == 'yes'
            columns.append(Column(column_name, tpe, nullable=nullable))
        Table(table_name, metadata, *columns, schema=schema)

    Table(LOG_TABLE_NAME,
          metadata,
          Column('log_id', DateTime, nullable=False),
          Column('table_name', String(100), nullable=False),
          Column('phase', String(200), nullable=False),
          Column('success', Boolean(), nullable=False),
          Column('message', String(500), nullable=True),
          Column('params', String(800), nullable=True),
          schema=schema)

    metadata.create_all(engine)


def process(hpo_id, schema):
    """
    Find sprint files for the specified HPO and load CDM tables in the schema
    :param hpo_id:
    :param schema:
    :return:
    """
    # determine the log table
    metadata = MetaData(bind=engine, reflect=True, schema=schema)
    log_table = None

    table_map = dict()

    for table in metadata.sorted_tables:
        unqualified_table_name = table.name.split('.')[-1]
        table_map[unqualified_table_name] = table

    # may or may not contain schema prefix
    log_table = table_map[LOG_TABLE_NAME]

    sprint_num = settings.sprint_num
    cdm_df = pandas.read_csv(resources.cdm_csv_path)
    included_tables = pandas.read_csv(resources.pmi_tables_csv_path).table_name.unique()
    tables = cdm_df[cdm_df['table_name'].isin(included_tables)].groupby(['table_name'])

    # allow files to be found regardless of CaSe
    def path_to_file_map_item(p):
        file_path_parts = p.split(os.sep)
        filename = file_path_parts[-1]
        return filename.lower(), p

    file_map_items = map(path_to_file_map_item, glob.glob(os.path.join(settings.csv_dir, '*.csv')))
    file_map = dict(file_map_items)

    # used to insert records from data frame
    conn = engine.connect()

    for table_name, table_df in tables:
        cdm_table = table_map[table_name]

        csv_filename = '%(hpo_id)s_%(table_name)s_datasprint_%(sprint_num)s.csv' % locals()
        csv_path = os.path.join(settings.csv_dir, csv_filename)
        phase = 'Received CSV file "%s"' % csv_filename

        # not sure if phase eval dynamic
        def success():
            engine.execute(log_table.insert(),
                           log_id=datetime.datetime.utcnow(),
                           table_name=table_name,
                           phase=phase,
                           success=True)

        def fail(message, params=None):
            engine.execute(log_table.insert(),
                           log_id=datetime.datetime.utcnow(),
                           table_name=table_name,
                           phase=phase,
                           success=False,
                           message=message,
                           params=params or None)

        try:
            if csv_filename not in file_map:
                raise Exception('File not found')
            success()

            csv_path = file_map[csv_filename]

            # get column names for this table
            column_names = table_df.column_name.unique()

            with open(csv_path) as f:
                phase = 'Parsing CSV file'
                df = pandas.read_csv(f, na_values=['', ' ', '.'])
                success()

                # lowercase field names
                df = df.rename(columns=str.lower)

                # add missing columns (with NaN values)
                df = df.reindex(columns=column_names)

                # fill in blank concept_id columns with 0
                concept_columns = filter(lambda x: x.endswith('concept_id') and 'source' not in x, column_names)
                df[concept_columns] = df[concept_columns].fillna(value=0)

                # insert one at a time for more informative logs e.g. bulk insert via df.to_sql may obscure error
                phase = 'Loading file into table'
                df.to_sql(name=table_name, con=conn, if_exists='append', index=False, schema=schema, chunksize=1)
                success()
        except StatementError, e:
            fail(e.message, str(e.params))
        except Exception, e:
            fail(e.message)


def export_log():
    """
    Dumps all logs for all HPOs to `_data/log.json`

    Note: params column is excluded for the unlikely case it may contain sensitive data
    """
    all_log_items = []

    for hpo_id in hpo_ids:
        schema = hpo_id if use_multi_schemas else None
        metadata = MetaData(bind=engine, reflect=True, schema=schema)
        log_table = Table(LOG_TABLE_NAME, metadata, autoload=True)
        for row in engine.execute(log_table.select()):
            row_dict = dict(zip(row.keys(), row))
            del row_dict['params']
            row_dict['hpo_id'] = hpo_id
            row_dict['log_id'] = str(row_dict['log_id'])  # for json serialize
            all_log_items.append(row_dict)

    log_path = os.path.join(resources.data_path, 'log.json')
    with open(log_path, 'w') as log_file:
        log_file.write(json.dumps(all_log_items))


def main():
    for hpo_id in hpo_ids:
        print 'Processing %s...' % hpo_id
        schema = hpo_id if use_multi_schemas else None
        if use_multi_schemas:
            create_schema(schema)
        drop_tables(schema=schema)
        create_tables(schema=schema)
        process(hpo_id, schema)


if __name__ == '__main__':
    main()
