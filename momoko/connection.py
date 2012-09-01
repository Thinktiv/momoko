# -*- coding: utf-8 -*-
"""
    momoko.connection
    ~~~~~~~~~~~~~~~~~

    Connection handling.

    :copyright: (c) 2011-2012 by Frank Smit.
    :license: MIT, see LICENSE for more details.
"""

# Information
#
# - http://initd.org/psycopg/articles/2010/12/01/postgresql-notifications-psycopg2-eventlet/
# - http://initd.org/psycopg/docs/advanced.html#asynchronous-notifications
# - http://wiki.postgresql.org/wiki/PgBouncer
# - https://github.com/wulczer/txpostgres
# - http://www.postgresql.org/docs/9.1/static/tutorial-transactions.html
# - https://bitbucket.org/descent/psytornet/overview
# - An ORM? Look here: https://github.com/coleifer/peewee
# - http://initd.org/psycopg/docs/advanced.html#asynchronous-support

# Schedule
#
# 1. Working state (similar functionality as stable Momoko)
#    * execute
#    * Exception handling!
#    * callproc
#    * mogrify
#    - batch (can be done with gen)
#    - chain (can be done with gen)
#    * clean pool
#    * close pool
#    * create new connection if none is free
#    * recover from broken connection
# 2. Implement Psycopg2 functionality
#    - Everything that works in asynchronous mode.
#    - Replacements for things that do not work in asynchronous mode
# 3. Transactions (included in point 2, but this is a more advanced feature)
# 4. Aynchronous notifications (included in point 2, but this is a more advanced feature)
#    Can be done manually with BEGIN and COMMIT. Maybe introduce a convenience function?
#    Does anyone use this?
# 5. Investigate and test PgBouncer
# 6. Investigate txpostgres and psytornet
# 7. Investigate and try making an asynchronous ORM suitable for callback based database drivers

from functools import partial

from tornado import gen
from tornado.ioloop import IOLoop, PeriodicCallback

from .utils import Op, psycopg2
from .exceptions import PoolError


# The dummy callback is used to keep the asynchronous cursor alive in case no
# callback has been specified. This will prevent the cursor from being garbage
# collected once, for example, ``ConnectionPool.execute`` has finished.
def _dummy_callback(cursor, error):
    pass


class BConnectionPool(object):
    """Blocking connection pool acting as a single connection.
    """
    pass


class ConnectionPool(object):
    """Asynchronous connection pool acting as a single connection.

    ``dsn`` and ``connection_factory`` are passed to :py:class:`momoko.connection.Connection`
    when a new connection is created. :py:class:`~momoko.connection.Connection`
    also contains the documentation about these two parameters.

    :param minconn: Amount of connection created upon initialization.
    :param maxconn: Maximum amount of connections supported by the pool.
    :param cleanup_timeout:
        Time in seconds between pool cleanups. Unused connections are closed and
        removed from the pool until only ``minconn`` are left. When an integer
        below ``1`` is used the pool cleaner will be disabled.
    :param ioloop: An instance of Tornado's IOLoop.
    """
    def __init__(self, dsn, connection_factory=None, minconn=1, maxconn=5,
                 cleanup_timeout=10, ioloop=None):
        self._dsn = dsn
        self._minconn = minconn
        self._maxconn = maxconn
        self._connection_factory = connection_factory
        self._ioloop = ioloop or IOLoop.instance()
        self.closed = False
        self._pool = []

        # Create connections
        for i in range(self._minconn):
            self._new_connection()

        # Create a periodic callback that tries to close inactive connections
        if cleanup_timeout > 0:
            self._cleaner = PeriodicCallback(self._clean_pool,
                cleanup_timeout * 1000)
            self._cleaner.start()

    def _new_connection(self, callback=None):
        if len(self._pool) > self._maxconn:
            raise PoolError('connection pool exausted')

        connection = Connection(self._ioloop)
        if callback is not None:
            callbacks = [
                partial(callback, connection),
                lambda error: self._pool.append(connection)]
        else:
            callbacks = [lambda error: self._pool.append(connection)]

        connection.open(self._dsn, self._connection_factory, callbacks)

    @gen.engine
    def _get_connection(self, callback):
        """Look for a free connection and create a new connection when
        no free connection is available.
        """
        if self.closed:
            raise PoolError('connection pool is closed')

        free_connection = None
        for connection in self._pool:
            if not connection.isexecuting():
                free_connection = connection
                break

        if free_connection is None:
            free_connection = yield Op(self._new_connection)

        callback(free_connection)

    def _clean_pool(self):
        """Close a number of inactive connections when the number of connections
        in the pool exceeds the number in `min_conn`.
        """
        if self.closed:
            raise PoolError('connection pool is closed')

        pool_len = len(self._pool)
        if pool_len > self._minconn:
            overflow = pool_len - self._minconn
            for i in self._pool[:]:
                if not i.isexecuting():
                    i.close()
                    overflow -= 1
                    self._pool.remove(i)
                    if overflow == 0:
                        break

    @gen.engine
    def _do_op(self, name, operation, parameters=(), cursor_factory=None,
               callback=_dummy_callback, retries=5):

        connection = yield gen.Task(self._get_connection)
        cursor = connection.cursor(cursor_factory)
        op = getattr(cursor, name)

        try:
            op(operation, parameters)
        except (psycopg2.Warning, psycopg2.Error) as e:
            if retries == 0:
                raise e
            if connection in self._pool:
                self._pool.remove(connection)
            self._ioloop.add_callback(partial(self._do_op, name, operation,
                parameters, cursor_factory, callback, retries-1))
            return

        connection.set_callbacks(partial(callback, cursor))

    def execute(self, operation, parameters=(), cursor_factory=None,
                callback=_dummy_callback, retries=5):
        self._do_op('execute', operation, parameters, cursor_factory,
            callback, retries)

    def callproc(self, procname, parameters=(), cursor_factory=None,
                callback=_dummy_callback, retries=5):
        self._do_op('callproc', procname, parameters, cursor_factory,
            callback, retries)

    @gen.engine
    def mogrify(self, operation, parameters=(), callback=_dummy_callback):
        connection = yield gen.Task(self._get_connection)
        cursor = connection.cursor()
        result = cursor.mogrify(operation, parameters)
        self._ioloop.add_callback(partial(callback, result, None))

    def close(self):
        if self.closed:
            raise PoolError('connection pool is already closed')

        for conn in self._pool:
            if not conn.closed:
                conn.close()

        self._cleaner.stop()
        self._pool = []
        self.closed = True


class Connection(object):
    """Asynchronous connection class that wraps a Psycopg2 connection.

    :param ioloop: An instance of Tornado's IOLoop.
    """
    def __init__(self, ioloop=None):
        self._connection = None
        self._fileno = None
        self._ioloop = ioloop
        self._callbacks = []

    def open(self, dsn, connection_factory=None, callbacks=()):
        """Open an asynchronous connection.

        :param dsn:
            A `Data Source Name`_ string containing one of the collowing values:

            * **dbname** - the database name
            * **user** - user name used to authenticate
            * **password** - password used to authenticate
            * **host** - database host address (defaults to UNIX socket if not provided)
            * **port** - connection port number (defaults to 5432 if not provided)

            Or any other parameter supported by PostgreSQL. See the PostgreSQL
            documentation for a complete list of supported parameters_.

        :param connection_factory:
            The ``connection_factory`` argument can be used to create non-standard
            connections. The class returned should be a subclass of
            `psycopg2.extensions.connection`_.
        :param callbacks:
            Sequence of callables. These are executed after the connection
            has been established.

        .. _Data Source Name: http://en.wikipedia.org/wiki/Data_Source_Name
        .. _parameters: http://www.postgresql.org/docs/current/static/libpq-connect.html#LIBPQ-PQCONNECTDBPARAMS
        .. _psycopg2.extensions.connection: http://initd.org/psycopg/docs/connection.html#connection
        """
        self._connection = psycopg2.connect(dsn, connection_factory, async=1)
        self._fileno = self._connection.fileno()
        self._callbacks = callbacks

        # Connection state should be 2 (write)
        self._ioloop.add_handler(self._fileno, self._io_callback, IOLoop.WRITE)

    def _io_callback(self, fd, events):
        try:
            error = None
            state = self._connection.poll()
        except (psycopg2.Warning, psycopg2.Error) as e:
            error = e
            state = psycopg2.extensions.POLL_OK

        if state == psycopg2.extensions.POLL_OK:
            for callback in self._callbacks:
                callback(error)
        elif state == psycopg2.extensions.POLL_READ:
            self._ioloop.update_handler(self._fileno, IOLoop.READ)
        elif state == psycopg2.extensions.POLL_WRITE:
            self._ioloop.update_handler(self._fileno, IOLoop.WRITE)

    def close(self):
        """Close asynchronous connection.
        """
        self._ioloop.remove_handler(self._fileno)
        self._connection.close()

    def cursor(self, cursor_factory=None):
        """Return a new Psycopg2 cursor object.

        :param cursor_factory:
            The ``cursor_factory`` argument can be used to create non-standard cursors.
            The class returned should be a subclass of `psycopg2.extensions.cursor`_.
        :return: A Psycopg2 cursor object.

        .. _psycopg2.extensions.cursor: http://initd.org/psycopg/docs/extensions.html#psycopg2.extensions.cursor
        """
        cursor_factory = cursor_factory or psycopg2.extensions.cursor
        return self._connection.cursor(cursor_factory=cursor_factory)

    def set_callbacks(self, *callbacks):
        """Set callbacks that need to be executed after the execution of a database
        operation using previously aquired cursor.
        """
        self._callbacks = callbacks

        # Connection state should be 1 (read)
        self._ioloop.update_handler(self._fileno, IOLoop.READ)

    @property
    def closed(self):
        """Read-only attribute reporting whether the database connection is
        open (0) or closed (1).
        """
        return self._connection.closed

    def isexecuting(self):
        """Return True if the connection is executing an asynchronous operation.
        """
        return self._connection.isexecuting()