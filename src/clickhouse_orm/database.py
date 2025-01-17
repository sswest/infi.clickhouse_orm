from __future__ import unicode_literals, annotations
import re
import logging
import datetime
from io import BytesIO
from math import ceil
from string import Template
from collections import namedtuple
from typing import Optional, Generator, Union, Any

import pytz
import httpx

from .engines import DatabaseEngine, Atomic
from .models import ModelBase, MODEL
from .utils import parse_tsv, import_submodules
from .session import ctx_session_id, ctx_session_timeout


logger = logging.getLogger("clickhouse_orm")
Page = namedtuple("Page", "objects number_of_objects pages_total number page_size")


class DatabaseException(Exception):
    """
    Raised when a database operation fails.
    """


class ServerError(DatabaseException):
    """
    Raised when a server returns an error.
    """

    def __init__(self, message):
        self.code = None
        processed = self.get_error_code_msg(message)
        if processed:
            self.code, self.message = processed
        else:
            # just skip custom init
            # if non-standard message format
            self.message = message
            super().__init__(message)

    ERROR_PATTERNS = (
        # ClickHouse prior to v19.3.3
        re.compile(
            r"""
            Code:\ (?P<code>\d+),
            \ e\.displayText\(\)\ =\ (?P<type1>[^ \n]+):\ (?P<msg>.+?),
            \ e.what\(\)\ =\ (?P<type2>[^ \n]+)
        """,
            re.VERBOSE | re.DOTALL,
        ),
        # ClickHouse v19.3.3+
        re.compile(
            r"""
            Code:\ (?P<code>\d+),
            \ e\.displayText\(\)\ =\ (?P<type1>[^ \n]+):\ (?P<msg>.+)
        """,
            re.VERBOSE | re.DOTALL,
        ),
        # ClickHouse v21+
        re.compile(
            r"""
            Code:\ (?P<code>\d+).
            \ (?P<type1>[^ \n]+):\ (?P<msg>.+)
        """,
            re.VERBOSE | re.DOTALL,
        ),
    )

    @classmethod
    def get_error_code_msg(cls, full_error_message):
        """
        Extract the code and message of the exception that clickhouse-server generated.

        See the list of error codes here:
        https://github.com/yandex/ClickHouse/blob/master/dbms/src/Common/ErrorCodes.cpp
        """
        for pattern in cls.ERROR_PATTERNS:
            match = pattern.match(full_error_message)
            if match:
                # assert match.group('type1') == match.group('type2')
                return int(match.group("code")), match.group("msg").strip()

        return 0, full_error_message

    def __str__(self):
        if self.code is not None:
            return "{} ({})".format(self.message, self.code)


class Database:
    """
    Database instances connect to a specific ClickHouse database for running queries,
    inserting data and other operations.
    """

    _client_class = httpx.Client

    def __init__(
        self,
        db_name,
        db_url="http://localhost:8123/",
        username=None,
        password=None,
        readonly=False,
        auto_create=True,
        timeout=60,
        verify_ssl_cert=True,
        log_statements=False,
        engine: DatabaseEngine = Atomic(),
    ):
        """
        Initializes a database instance. Unless it's readonly, the database will be
        created on the ClickHouse server if it does not already exist.

        - `db_name`: name of the database to connect to.
        - `db_url`: URL of the ClickHouse server.
        - `username`: optional connection credentials.
        - `password`: optional connection credentials.
        - `readonly`: use a read-only connection.
        - `auto_create`: automatically create the database
                        if it does not exist (unless in readonly mode).
        - `timeout`: the connection timeout in seconds.
        - `verify_ssl_cert`: whether to verify the server's certificate when connecting via HTTPS.
        - `log_statements`: when True, all database statements are logged.
        - `engine`: By default, ClickHouse uses the Atomic database engine.
        """
        self.db_name = db_name
        self.db_url = db_url
        self.readonly = False
        self._readonly = readonly
        self.auto_create = auto_create
        self.timeout = timeout
        self.engine = engine
        self.request_session = self._client_class(verify=verify_ssl_cert, timeout=timeout)
        if username:
            self.request_session.auth = (username, password or "")
        self.log_statements = log_statements
        self.settings = {}
        self.db_exists = False  # this is required before running _is_existing_database
        self.connection_readonly = False
        self.server_version = None
        self.server_timezone = None
        self.has_codec_support = None
        self.has_low_cardinality_support = None
        self._init = False
        if self._client_class is httpx.Client:
            self.init()

    def init(self):
        if self._init:
            return
        self.db_exists = self._is_existing_database()
        if self._readonly:
            if not self.db_exists:
                raise DatabaseException(
                    "Database does not exist, and cannot be created under readonly connection"
                )
            self.connection_readonly = self._is_connection_readonly()
            self.readonly = True
        elif self.auto_create and not self.db_exists:
            self.create_database()
        self.server_version = self._get_server_version()
        # Versions 1.1.53981 and below don't have timezone function
        if self.server_version > (1, 1, 53981):
            self.server_timezone = self._get_server_timezone()
        else:
            self.server_timezone = pytz.utc
        # Versions 19.1.16 and above support codec compression
        self.has_codec_support = self.server_version >= (19, 1, 16)
        # Version 19.0 and above support LowCardinality
        self.has_low_cardinality_support = self.server_version >= (19, 0)

    def create_database(self):
        """
        Creates the database on the ClickHouse server if it does not already exist.
        """
        self._send(
            "CREATE DATABASE IF NOT EXISTS `%s` ENGINE = %s"
            % (self.db_name, self.engine.create_database_sql())
        )
        self.db_exists = True

    def drop_database(self):
        """
        Deletes the database on the ClickHouse server.
        """
        self._send("DROP DATABASE `%s`" % self.db_name)
        self.db_exists = False

    def create_table(self, model_class: type[MODEL]) -> None:
        """
        Creates a table for the given model class, if it does not exist already.
        """
        if model_class.is_system_model():
            raise DatabaseException("You can't create system table")
        if getattr(model_class, "engine") is None:
            raise DatabaseException("%s class must define an engine" % model_class.__name__)
        self._send(model_class.create_table_sql(self))

    def drop_table(self, model_class: type[MODEL]) -> None:
        """
        Drops the database table of the given model class, if it exists.
        """
        if model_class.is_system_model():
            raise DatabaseException("You can't drop system table")
        self._send(model_class.drop_table_sql(self))

    def does_table_exist(self, model_class: type[MODEL]) -> bool:
        """
        Checks whether a table for the given model class already exists.
        Note that this only checks for existence of a table with the expected name.
        """
        sql = "SELECT count() FROM system.tables WHERE database = '%s' AND name = '%s'"
        r = self._send(sql % (self.db_name, model_class.table_name()))
        return r.text.strip() == "1"

    def get_model_for_table(self, table_name: str, system_table: bool = False):
        """
        Generates a model class from an existing table in the database.
        This can be used for querying tables which don't have a corresponding model class,
        for example system tables.

        - `table_name`: the table to create a model for
        - `system_table`: whether the table is a system table, or belongs to the current database
        """
        db_name = "system" if system_table else self.db_name
        sql = "DESCRIBE `%s`.`%s` FORMAT TSV" % (db_name, table_name)
        lines = self._send(sql).iter_lines()
        fields = [parse_tsv(line)[:2] for line in lines]
        model = ModelBase.create_ad_hoc_model(fields, table_name)
        if system_table:
            model._system = model._readonly = True
        return model

    def add_setting(self, name: str, value: Any):
        """
        Adds a database setting that will be sent with every request.
        For example, `db.add_setting("max_execution_time", 10)` will
        limit query execution time to 10 seconds.
        The name must be string, and the value is converted to string in case
        it isn't. To remove a setting, pass `None` as the value.
        """
        assert isinstance(name, str), "Setting name must be a string"
        if value is None:
            self.settings.pop(name, None)
        else:
            self.settings[name] = str(value)

    def insert(self, model_instances, batch_size=1000):
        """
        Insert records into the database.

        - `model_instances`: any iterable containing instances of a single model class.
        - `batch_size`: number of records to send per chunk
                        (use a lower number if your records are very large).
        """
        i = iter(model_instances)
        try:
            first_instance = next(i)
        except StopIteration:
            return  # model_instances is empty
        model_class = first_instance.__class__

        if first_instance.is_read_only() or first_instance.is_system_model():
            raise DatabaseException("You can't insert into read only and system tables")

        fields_list = ",".join(["`%s`" % name for name in first_instance.fields(writable=True)])
        fmt = "TSKV" if model_class.has_funcs_as_defaults() else "TabSeparated"
        query = "INSERT INTO $table (%s) FORMAT %s\n" % (fields_list, fmt)

        def gen():
            buf = BytesIO()
            buf.write(self._substitute(query, model_class).encode("utf-8"))
            first_instance.set_database(self)
            buf.write(first_instance.to_db_string())
            # Collect lines in batches of batch_size
            lines = 2
            for instance in i:
                instance.set_database(self)
                buf.write(instance.to_db_string())
                lines += 1
                if lines >= batch_size:
                    # Return the current batch of lines
                    yield buf.getvalue()
                    # Start a new batch
                    buf = BytesIO()
                    lines = 0
            # Return any remaining lines in partial batch
            if lines:
                yield buf.getvalue()

        self._send(gen())

    def count(
        self, model_class: Optional[type[MODEL]], conditions: Optional[Union[str, "Q"]] = None
    ) -> int:
        """
        Counts the number of records in the model's table.

        - `model_class`: the model to count.
        - `conditions`: optional SQL conditions (contents of the WHERE clause).
        """
        from clickhouse_orm.query import Q

        query = "SELECT count() FROM $table"
        if conditions:
            if isinstance(conditions, Q):
                conditions = conditions.to_sql(model_class)
            query += " WHERE " + str(conditions)
        query = self._substitute(query, model_class)
        r = self._send(query)
        return int(r.text) if r.text else 0

    def select(
        self, query: str, model_class: Optional[type[MODEL]] = None, settings: Optional[dict] = None
    ) -> Generator[MODEL, None, None]:
        """
        Performs a query and returns a generator of model instances.

        - `query`: the SQL query to execute.
        - `model_class`: the model class matching the query's table,
          or `None` for getting back instances of an ad-hoc model.
        - `settings`: query settings to send as HTTP GET parameters
        """
        query += " FORMAT TabSeparatedWithNamesAndTypes"
        query = self._substitute(query, model_class)
        r = self._send(query, settings, True)
        try:
            lines = r.iter_lines()
            field_names = parse_tsv(next(lines))
            field_types = parse_tsv(next(lines))
            if not model_class:
                model_class = ModelBase.create_ad_hoc_model(zip(field_names, field_types))
            for line in lines:
                # skip blank line left by WITH TOTALS modifier
                if line.strip():
                    yield model_class.from_tsv(line, field_names, self.server_timezone, self)
        except StopIteration:
            return
        finally:
            r.close()

    def raw(self, query: str, settings: Optional[dict] = None, stream: bool = False) -> str:
        """
        Performs a query and returns its output as text.

        - `query`: the SQL query to execute.
        - `settings`: query settings to send as HTTP GET parameters
        - `stream`: if true, the HTTP response from ClickHouse will be streamed.
        """
        query = self._substitute(query, None)
        return self._send(query, settings=settings, stream=stream).text

    def paginate(
        self,
        model_class: type[MODEL],
        order_by: str,
        page_num: int = 1,
        page_size: int = 100,
        conditions=None,
        settings: Optional[dict] = None,
    ):
        """
        Selects records and returns a single page of model instances.

        - `model_class`: the model class matching the query's table,
          or `None` for getting back instances of an ad-hoc model.
        - `order_by`: columns to use for sorting the query (contents of the ORDER BY clause).
        - `page_num`: the page number (1-based), or -1 to get the last page.
        - `page_size`: number of records to return per page.
        - `conditions`: optional SQL conditions (contents of the WHERE clause).
        - `settings`: query settings to send as HTTP GET parameters

        The result is a namedtuple containing `objects` (list), `number_of_objects`,
        `pages_total`, `number` (of the current page), and `page_size`.
        """
        from clickhouse_orm.query import Q

        count = self.count(model_class, conditions)
        pages_total = int(ceil(count / float(page_size)))
        if page_num == -1:
            page_num = max(pages_total, 1)
        elif page_num < 1:
            raise ValueError("Invalid page number: %d" % page_num)
        offset = (page_num - 1) * page_size
        query = "SELECT * FROM $table"
        if conditions:
            if isinstance(conditions, Q):
                conditions = conditions.to_sql(model_class)
            query += " WHERE " + str(conditions)
        query += " ORDER BY %s" % order_by
        query += " LIMIT %d, %d" % (offset, page_size)
        query = self._substitute(query, model_class)
        return Page(
            objects=list(self.select(query, model_class, settings)) if count else [],
            number_of_objects=count,
            pages_total=pages_total,
            number=page_num,
            page_size=page_size,
        )

    def migrate(self, migrations_package_name, up_to=9999):
        """
        Executes schema migrations.

        - `migrations_package_name` - fully qualified name of the Python package
          containing the migrations.
        - `up_to` - number of the last migration to apply.
        """
        from .migrations import MigrationHistory  # pylint: disable=C0415

        logger = logging.getLogger("migrations")
        applied_migrations = self._get_applied_migrations(migrations_package_name)
        modules = import_submodules(migrations_package_name)
        unapplied_migrations = set(modules.keys()) - applied_migrations
        for name in sorted(unapplied_migrations):
            logger.info("Applying migration %s...", name)
            for operation in modules[name].operations:
                operation.apply(self)
            self.insert(
                [
                    MigrationHistory(
                        package_name=migrations_package_name,
                        module_name=name,
                        applied=datetime.date.today(),
                    )
                ]
            )
            if int(name[:4]) >= up_to:
                break

    @property
    def session_id(self):
        """return current client session_id"""
        return ctx_session_id.get(None)

    @property
    def _context_params(self):
        """return context params"""
        params = {}
        if ctx_session_id.get(None):
            params.update(session_id=self.session_id, session_timeout=ctx_session_timeout.get(60))
        return params

    def _get_applied_migrations(self, migrations_package_name):
        from .migrations import MigrationHistory  # pylint: disable=C0415

        self.create_table(MigrationHistory)
        query = "SELECT module_name from $table WHERE package_name = '%s'" % migrations_package_name
        query = self._substitute(query, MigrationHistory)
        return set(obj.module_name for obj in self.select(query))

    def _send(self, data: str | bytes | Generator, settings: dict = None, stream: bool = False):
        if isinstance(data, str):
            data = data.encode("utf-8")
            if self.log_statements:
                logger.info(data)
        params = self._build_params(settings)
        request = self.request_session.build_request(
            method="POST", url=self.db_url, content=data, params=params
        )
        r = self.request_session.send(request, stream=stream)
        if isinstance(r, httpx.Response) and r.status_code != 200:
            r.read()
            raise ServerError(r.text)
        return r

    def _build_params(self, settings):
        params = dict(settings or {})
        params.update(self.settings)
        params.update(self._context_params)
        if self.db_exists:
            params["database"] = self.db_name
        # Send the readonly flag, unless the connection is already readonly (to prevent db error)
        if self.readonly and not self.connection_readonly:
            params["readonly"] = "1"
        return params

    def _substitute(self, query, model_class=None):
        """
        Replaces $db and $table placeholders in the query.
        """
        if "$" in query:
            mapping = dict(db="`%s`" % self.db_name)
            if model_class:
                if model_class.is_system_model():
                    mapping["table"] = "`system`.`%s`" % model_class.table_name()
                elif model_class.is_temporary_model():
                    mapping["table"] = "`%s`" % model_class.table_name()
                else:
                    mapping["table"] = "`%s`.`%s`" % (self.db_name, model_class.table_name())
            query = Template(query).safe_substitute(mapping)
        return query

    def _get_server_timezone(self):
        try:
            r = self._send("SELECT timezone()")
            return pytz.timezone(r.text.strip())
        except ServerError as err:
            logger.exception("Cannot determine server timezone (%s), assuming UTC", err)
            return pytz.utc

    def _get_server_version(self, as_tuple=True):
        try:
            r = self._send("SELECT version();")
            ver = r.text
        except ServerError as err:
            logger.exception("Cannot determine server version (%s), assuming 1.1.0", err)
            ver = "1.1.0"
        return tuple(int(n) for n in ver.split(".") if n.isdigit()) if as_tuple else ver

    def _is_existing_database(self):
        r = self._send("SELECT count() FROM system.databases WHERE name = '%s'" % self.db_name)
        return r.text.strip() == "1"

    def _is_connection_readonly(self):
        r = self._send("SELECT value FROM system.settings WHERE name = 'readonly'")
        return r.text.strip() != "0"


# Expose only relevant classes in import *
__all__ = [c.__name__ for c in [Page, DatabaseException, ServerError, Database]]
