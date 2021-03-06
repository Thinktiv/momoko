#!/usr/bin/env python

import tornado.httpserver
import tornado.ioloop
import tornado.options
import tornado.web

import momoko

import settings


class BaseHandler(tornado.web.RequestHandler):
    @property
    def db(self):
        return self.application.db


class OverviewHandler(BaseHandler):
    def get(self):
        self.write('''
<ul>
    <li><a href="/query">A single query</a></li>
    <li><a href="/batch">A batch of queries</a></li>
    <li><a href="/chain">A chain of queries</a></li>
</ul>
        ''')
        self.finish()


class SingleQueryHandler(BaseHandler):
    @tornado.web.asynchronous
    def get(self):
        # One simple query
        self.db.execute('SELECT 42, 12, 40, 11;', callback=self._on_response)

    def _on_response(self, cursor):
        self.write('Query results: %s' % cursor.fetchall())
        self.finish()


class BatchQueryHandler(BaseHandler):
    @tornado.web.asynchronous
    def get(self):
        # These queries are executed all at once and therefore they need to be
        # stored in an dictionary so you know where the resulting cursors
        # come from, because they won't arrive in the same order.
        self.db.batch({
            'query1': ['SELECT 42, 12, %s, %s;', (23, 56)],
            'query2': 'SELECT 1, 2, 3, 4, 5;',
            'query3': 'SELECT 465767, 4567, 3454;'
        }, self._on_response)

    def _on_response(self, cursors):
        for key, cursor in cursors.items():
            self.write('Query results: %s = %s<br>' % (key, cursor.fetchall()))
        self.finish()


class QueryChainHandler(BaseHandler):
    @tornado.web.asynchronous
    def get(self):
        # Execute a list of queries in the order you specified
        self.db.chain((
            ['SELECT 42, 12, %s, 11;', (23,)],
            'SELECT 1, 2, 3, 4, 5;'
        ), self._on_response)

    def _on_response(self, cursors):
        for cursor in cursors:
            self.write('Query results: %s<br>' % cursor.fetchall())
        self.finish()


def main():
    try:
        tornado.options.parse_command_line()
        application = tornado.web.Application([
            (r'/', OverviewHandler),
            (r'/query', SingleQueryHandler),
            (r'/batch', BatchQueryHandler),
            (r'/chain', QueryChainHandler),
        ], debug=True)

        application.db = momoko.AsyncClient({
            'host': settings.host,
            'port': settings.port,
            'database': settings.database,
            'user': settings.user,
            'password': settings.password,
            'min_conn': settings.min_conn,
            'max_conn': settings.max_conn,
            'cleanup_timeout': settings.cleanup_timeout
        })

        http_server = tornado.httpserver.HTTPServer(application)
        http_server.listen(8888)
        tornado.ioloop.IOLoop.instance().start()
    except KeyboardInterrupt:
        print('Exit')


if __name__ == '__main__':
    main()
