#!/usr/bin/env python3

import argparse
import os
import subprocess
import psycopg2
import psycopg2.extras
import time
import logging
import threading
from queue import Queue
from multiprocessing import cpu_count
args = None
workers = []
queue = Queue()
err_count = 0


def execute_on_db(sql, params=None, dbname='postgres'):
    conn = psycopg2.connect(host=args.host, port=args.port, dbname=dbname, user=args.username)
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    if cur.statusmessage.startswith('SELECT') or cur.description:
        return cur.fetchall()
    else:
        return [{'rows_affected': str(cur.rowcount)}]


def shell_exec_with_output(commands, ok_code=0):
    process = subprocess.Popen(commands, stdout=subprocess.PIPE, shell=True, stderr=subprocess.STDOUT)
    exitcode = process.wait()
    output = process.stdout.read().strip()
    if exitcode != ok_code:
        logging.error('Error executing: %s', commands)
        logging.error(output)
    return exitcode, output


def worker_process(id):
    """Takes tables from the queue if any, and pg_dumps the table to /dev/null"""
    logging.info('Starting worker process %s ...', id)
    while True:
        dbname, tbl = queue.get()
        cmd = '{pg_dump} -h {host} -p {port} -U "{user}" -t {tbl} "{dbname}" >/dev/null'.format(
                pg_dump=os.path.join(args.bindir, 'pg_dump'), host=args.host, port=args.port, user=args.username, tbl=tbl, dbname=dbname)
        logging.info('Executing %s', cmd)
        retcode, output = shell_exec_with_output(cmd)
        if retcode != 0:
            if output.find("No matching tables were found") >= 0:
                logging.warning("Table %s could not be found [%s]", tbl, dbname)
                continue
            logging.error("Failed to dump contents of table %s [%s]", tbl, dbname)
            global err_count
            err_count += 1
            return


def launch_workers():
    logging.info('Launching %s worker processes', args.jobs)

    for i in range(args.jobs):
        t = threading.Thread(target=worker_process, args=(i,))
        t.daemon = True
        t.start()
        workers.append(t)


def verify_globals():
    logging.info('Dumping globals with pg_dumpall...')
    cmd = '{pg_dumpall} -g -h {host} -p {port} -U "{user}" >/dev/null'.format(
            pg_dumpall=os.path.join(args.bindir, 'pg_dumpall'), host=args.host, port=args.port, user=args.username)
    logging.info('Executing %s', cmd)
    retcode, output = shell_exec_with_output(cmd)
    if retcode != 0:
        raise Exception('Failed to verify globals')


def verify_schema(dbname):
    logging.info('Dumping schema with pg_dump --schema-only for %s', dbname)
    cmd = '{pg_dump} -h {host} -p {port} -U "{user}" --schema-only "{dbname}" >/dev/null'.format(
            pg_dump=os.path.join(args.bindir, 'pg_dump'), host=args.host, port=args.port, user=args.username, dbname=dbname)
    logging.info('Executing %s', cmd)
    retcode, output = shell_exec_with_output(cmd)
    if retcode != 0:
        raise Exception('Failed to verify schema for DB ' + dbname)


def add_tables_from_db(dbname):
    logging.info('Processing DB: %s', dbname)
    sql = """select quote_ident(nspname)||'.'||quote_ident(relname) as tbl
                from pg_class c
                join pg_namespace n on n.oid = c.relnamespace 
                where relkind = 'r'
                and relpersistence = 'p'
                and not nspname like any(array['information_schema', E'pg\\_%'])
                order by relpages desc"""
    tables = execute_on_db(sql, dbname=dbname)
    tables = [x['tbl'] for x in tables]
    if tables:
        for t in tables:
            queue.put((dbname, t))
        logging.info('Added %s tables to the queue', len(tables))
    else:
        logging.info('No tables found')
    return len(tables)


def all_workers_alive():
    for w in workers:
        if not w.is_alive():
            return False
    return True


def main():
    argp = argparse.ArgumentParser(description='A helper to dump all dbs/tables to /dev/null in parallel.'
                                               'Meant to be used on replicas where parallel pg_dump cannot be used (pre PG 10) to validate data files integrity.'
                                               'NB! Integrity is still not fully guaranteed with this approach (no snapshot, no constraint/index validation)', add_help=False)

    argp.add_argument('--help', help='Show help', action='help')
    argp.add_argument('-b', '--bindir', help='Postgres binaries folder', required=True)
    argp.add_argument('-h', '--host', help='PG host. IP or unix socket', default='/var/run/postgresql/')
    argp.add_argument('-p', '--port', help='PG port', default=5432, type=int)
    argp.add_argument('-U', '--username', help='PG user', default=os.getenv('USER'))    # password is assumed to be in .pgpass

    argp.add_argument('-j', '--jobs', help='Max parallel processes to use. Default is count of CPUs/4',
                      default=int(max(cpu_count()/4, 1)), type=int, metavar=max(cpu_count()/4, 1))
    argp.add_argument('-d', '--dbname', help='Test only a single DB')
    argp.add_argument('-q', '--quiet', help='Only errors', action='store_true')

    global args
    args, unknown_args = argp.parse_known_args()

    logging.basicConfig(level=(logging.ERROR if args.quiet else logging.INFO), format='%(asctime)s (%(levelname)s) PID=%(process)d: %(message)s')
    logging.info('Args: %s, unknown_args: %s', args, unknown_args)

    if not os.path.exists(os.path.join(args.bindir, 'pg_dumpall')):
        raise Exception('Invalid BINDIR! Could not find pg_dumpall')

    dbs = execute_on_db('select datname from pg_database where not datistemplate and datallowconn order by datname', dbname='template1')
    dbs = [x['datname'] for x in dbs]
    logging.info('dbs found: %s', dbs)

    if args.dbname:
        if args.dbname not in dbs:
            raise Exception('DB not found: ' + args.dbname)
        dbs = [args.dbname]

    if not args.dbname:
        verify_globals()

    launch_workers()

    tables_added = 0

    for db in dbs:
        verify_schema(db)
        table_count = add_tables_from_db(db)
        tables_added += table_count

    if tables_added == 0:
        raise Exception('No tables found to be dumped!')

    logging.info('Waiting for %s tables to be pg_dumped...', tables_added)
    i = 0
    while not queue.empty():
        if not all_workers_alive():
            logging.error('Not all worker processes are alive. Exiting')
            exit(1)
        time.sleep(5)
        i += 5
        if i % 60 == 0:     # progress reporting
            logging.info("%s tables in the queue...", queue.qsize())

    if err_count == 0:
        logging.info("Done. No errors encountered")
    else:
        logging.info("Errors encountered")
        exit(1)


if __name__ == '__main__':
    main()
