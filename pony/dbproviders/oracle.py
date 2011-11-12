import os
os.environ["NLS_LANG"] = "AMERICAN_AMERICA.UTF8"

from types import NoneType
from datetime import date, datetime
from decimal import Decimal

import cx_Oracle

from pony import orm, dbschema, sqlbuilding, dbapiprovider, sqltranslation
from pony.dbapiprovider import DBAPIProvider, wrap_dbapi_exceptions, LongStr, LongUnicode
from pony.utils import is_utf8

def get_provider(*args, **keyargs):
    return OraProvider(*args, **keyargs)

trigger_template = """
create trigger %s
  before insert on %s  
  for each row
begin
  if :new.%s is null then
    select %s.nextval into :new.%s from dual;
  end if;
end;"""

class OraTable(dbschema.Table):
    def create(table, provider, connection, created_tables=None):
        commands = table.get_create_commands(created_tables)
        for i, sql in enumerate(commands):
            if orm.debug:
                print sql
                print
            cursor = connection.cursor()
            try: provider.execute(cursor, sql)
            except orm.DatabaseError, e:
                if e.exceptions[0].args[0].code == 955:
                    if orm.debug: print 'ALREADY EXISTS:', e.args[0].message
                    if not i:
                        if len(commands) > 1: print 'SKIP FURTHER DDL COMMANDS FOR TABLE %s\n' % table.name
                        return
                else: raise
    def get_create_commands(table, created_tables=None):
        result = dbschema.Table.get_create_commands(table, created_tables, False)
        for column in table.column_list:
            if column.is_pk == 'auto':
                quote_name = table.schema.provider.quote_name
                case = table.schema.case
                seq_name = quote_name(table.name + '_SEQ')
                result.append(case('create sequence %s nocache') % seq_name)
                table_name = quote_name(table.name)
                trigger_name = quote_name(table.name + '_BI')  # Before Insert
                column_name = quote_name(column.name)
                result.append(case(trigger_template) % (trigger_name, table_name, column_name, seq_name, column_name))
                break
        return result

class OraColumn(dbschema.Column):
    auto_template = None
    
class OraSchema(dbschema.DBSchema):
    table_class = OraTable
    column_class = OraColumn

class OraNoneMonad(sqltranslation.NoneMonad):
    def __init__(monad, translator, value=None):
        assert value in (None, '')
        sqltranslation.ConstMonad.__init__(monad, translator, None)

class OraTranslator(sqltranslation.SQLTranslator):
    NoneMonad = OraNoneMonad
    
    @classmethod
    def get_normalized_type_of(translator, value):
        if value == '': return NoneType
        return sqltranslation.SQLTranslator.get_normalized_type_of(value)
        
class OraBuilder(sqlbuilding.SQLBuilder):
    def INSERT(builder, table_name, columns, values, returning=None):
        result = sqlbuilding.SQLBuilder.INSERT(builder, table_name, columns, values)
        if returning is not None:
            result.extend([ ' RETURNING ', builder.quote_name(returning), ' INTO :new_id' ])
        return result

class OraBoolConverter(dbapiprovider.BoolConverter):
    def sql2py(converter, val):
        return bool(val)  # TODO: True/False, T/F, Y/N, Yes/No, etc.
    def sql_type(converter):
        return "NUMBER(1)"

def _string_sql_type(converter):
    if converter.max_len:
        return 'VARCHAR2(%d CHAR)' % converter.max_len
    return 'CLOB'

class OraUnicodeConverter(dbapiprovider.UnicodeConverter):
    def validate(converter, val):
        if val == '': return None
        return dbapiprovider.UnicodeConverter.validate(converter, val)
    def sql2py(converter, val):
        if isinstance(val, cx_Oracle.LOB):
            val = val.read()
            val = val.decode('utf8')
        return val
    sql_type = _string_sql_type  # TODO: Add support for NVARCHAR2 and NCLOB datatypes

class OraStrConverter(dbapiprovider.StrConverter):
    def validate(converter, val):
        if val == '': return None
        return dbapiprovider.StrConverter.validate(converter, val)
    def sql2py(converter, val):
        if isinstance(val, cx_Oracle.LOB):
            val = val.read()
            if is_utf8(converter.encoding): return val
            val = val.decode('utf8')
        assert isinstance(val, unicode)
        val = val.encode(converter.encoding, 'replace')
        return val
    sql_type = _string_sql_type

class OraIntConverter(dbapiprovider.IntConverter):
    def sql_type(converter):
        return 'NUMBER(38)'

class OraRealConverter(dbapiprovider.RealConverter):
    default_tolerance = 1e-14
    def sql_type(converter):
        return 'NUMBER'

class OraDecimalConverter(dbapiprovider.DecimalConverter):
    def sql_type(converter):
        return 'NUMBER(%d, %d)' % (converter.precision, converter.scale)

class OraBlobConverter(dbapiprovider.BlobConverter):
    def sql2py(converter, val):
        return buffer(val.read())

class OraDateConverter(dbapiprovider.DateConverter):
    def sql2py(converter, val):
        if isinstance(val, datetime): return val.date()
        if not isinstance(val, date): raise ValueError(
            'Value of unexpected type received from database: instead of date got %s', type(val))
        return val

class OraDatetimeConverter(dbapiprovider.DatetimeConverter):
    def sql_type(converter):
        return 'TIMESTAMP(6)'

class OraProvider(DBAPIProvider):
    paramstyle = 'named'
    row_value_syntax = True

    dbschema_cls = OraSchema
    translator_cls = OraTranslator
    sqlbuilder_cls = OraBuilder

    def __init__(provider, *args, **keyargs):
        DBAPIProvider.__init__(provider, cx_Oracle)
        provider.pool = _get_pool(*args, **keyargs)

    def get_default_entity_table_name(provider, entity):
        return DBAPIProvider.get_default_entity_table_name(provider, entity).upper()

    def get_default_m2m_table_name(provider, attr, reverse):
        return DBAPIProvider.get_default_m2m_table_name(provider, attr, reverse).upper()

    def get_default_column_names(provider, attr, reverse_pk_columns):
        return [ column.upper() for column in DBAPIProvider.get_default_column_names(provider, attr, reverse_pk_columns) ]

    def get_default_m2m_column_names(provider, entity):
        return [ column.upper() for column in DBAPIProvider.get_default_m2m_column_names(provider, entity) ]

    @wrap_dbapi_exceptions
    def execute(provider, cursor, sql, arguments=None):
        if arguments is not None:
            set_input_sizes(cursor, arguments)
            cursor.execute(sql, arguments)
        else: cursor.execute(sql)

    @wrap_dbapi_exceptions
    def executemany(provider, cursor, sql, arguments_list):
        set_input_sizes(cursor, arguments_list[0])
        cursor.executemany(sql, arguments_list)

    @wrap_dbapi_exceptions
    def execute_returning_id(provider, cursor, sql, arguments, result_type):
        if result_type is not int: raise NotImplementedError
        set_input_sizes(cursor, arguments)
        var = cursor.var(cx_Oracle.NUMBER)
        arguments['new_id'] = var
        cursor.execute(sql, arguments)
        new_id = var.getvalue()
        return result_type(new_id)

    converter_classes = [
        (bool, OraBoolConverter),
        (unicode, OraUnicodeConverter),
        (str, OraStrConverter),
        ((int, long), OraIntConverter),
        (float, OraRealConverter),
        (Decimal, OraDecimalConverter),
        (buffer, OraBlobConverter),
        (datetime, OraDatetimeConverter),
        (date, OraDateConverter)
    ]

def _get_pool(*args, **keyargs):
    user = password = dsn = None
    if len(args) == 1:
        conn_str = args[0]
        if '/' in conn_str:
            user, tail = conn_str.split('/', 1)
            if '@' in tail: password, dsn = tail.split('@', 1)
        if None in (user, password, dsn): raise ValueError(
            "Incorrect connection string (must be in form of 'user/password@dsn')")
    elif len(args) == 2: user, password = args
    elif len(args) == 3: user, password, dsn = args
    elif args: raise ValueError('Invalid number of positional arguments')
    if user != keyargs.setdefault('user', user):
        raise ValueError('Ambiguous value for user')
    if password != keyargs.setdefault('password', password):
        raise ValueError('Ambiguous value for password')
    if dsn != keyargs.setdefault('dsn', dsn):
        raise ValueError('Ambiguous value for dsn')
    keyargs.setdefault('threaded', True)
    keyargs.setdefault('min', 1)
    keyargs.setdefault('max', 10)
    keyargs.setdefault('increment', 1)
    return Pool(**keyargs)

def to_int_or_decimal(val):
    val = val.replace(',', '.')
    if '.' in val: return Decimal(val)
    return int(val)

def to_decimal(val):
    return Decimal(val.replace(',', '.'))

def output_type_handler(cursor, name, defaultType, size, precision, scale):
    if defaultType == cx_Oracle.NUMBER:
        if scale == 0:
            if precision: return cursor.var(cx_Oracle.STRING, 40, cursor.arraysize, outconverter=int)
            return cursor.var(cx_Oracle.STRING, 40, cursor.arraysize, outconverter=to_int_or_decimal)
        if scale != -127:
            return cursor.var(cx_Oracle.STRING, 100, cursor.arraysize, outconverter=to_decimal)
    elif defaultType in (cx_Oracle.STRING, cx_Oracle.FIXED_CHAR):
        return cursor.var(unicode, size, cursor.arraysize)  # from cx_Oracle example
    return None

class Pool(object):
    def __init__(pool, **keyargs):
        pool._pool = cx_Oracle.SessionPool(**keyargs)
    def connect(pool):
        con = pool._pool.acquire()
        con.outputtypehandler = output_type_handler
        return con
    def release(pool, con):
        pool._pool.release(con)
    def drop(pool, con):
        pool._pool.drop(con)

def get_inputsize(arg):
    if isinstance(arg, datetime):
        return cx_Oracle.TIMESTAMP
    return None

def set_input_sizes(cursor, arguments):
    if type(arguments) is dict:
        input_sizes = {}
        for name, arg in arguments.iteritems():
            size = get_inputsize(arg)
            if size is not None: input_sizes[name] = size
        cursor.setinputsizes(**input_sizes)
    elif type(arguments) is tuple:
        input_sizes = map(get_inputsize, arguments)
        cursor.setinputsizes(*input_sizes)
    else: assert False
