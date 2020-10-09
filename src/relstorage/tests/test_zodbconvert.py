##############################################################################
#
# Copyright (c) 2009 Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
from __future__ import absolute_import
from __future__ import print_function

import functools
import gc
import os
import tempfile
import unittest
from contextlib import contextmanager
from textwrap import dedent

import transaction

from zope.testing.setupstack import rmtree
from ZODB.DB import DB
from ZODB.FileStorage import FileStorage
from ZODB.blob import Blob
from ZODB.tests.util import TestCase

from relstorage.zodbconvert import main


def skipIfZapNotSupportedByDest(func):
    @functools.wraps(func)
    def test(self):
        if not self.zap_supported_by_dest:
            raise unittest.SkipTest("zap_all not supported")
        func(self)
    return test

class AbstractZODBConvertBase(TestCase):
    cfgfile = None

    # Set to True in a subclass if the destination can be zapped
    zap_supported_by_dest = False

    def setUp(self):
        super(AbstractZODBConvertBase, self).setUp()
        self._to_close = []
        self.__src_db = None
        self.__dest_db = None
        # zodb.tests.util.TestCase takes us to a temporary directory
        # as our working directory, and also sets it to tempfile.tempdir;
        # but don't require subclasses to know that.
        self.parent_temp_directory = tempfile.tempdir

    def tearDown(self):
        self.flush_changes_before_zodbconvert()
        for i in self._to_close:
            i.close()
        self._to_close = []
        # XXX: On PyPy with psycopg2cffi, running these two tests will
        # result in a hang:
        # HPPostgreSQLDestZODBConvertTests.test_clear_empty_dest
        # HPPostgreSQLDestZODBConvertTests.test_clear_full_dest
        # test_clear_full_dest will hang in the zodbconvert call to
        # zap_all(), in the C code of the PG driver. Presumably some
        # connection with some lock got left open and was preventing
        # the TRUNCATE statements from taking out a lock. The same
        # tests do not hang with psycopg2cffi on C Python. Manually
        # running the gc (twice!) here fixes the issue. Note that this
        # only started when we wrapped the destination storage in
        # ZlibStorage (which copies methods into its own dict) so
        # there's something weird going on with the GC. Seen in PyPy
        # 2.5.0 and 5.3.
        gc.collect()
        gc.collect()
        super(AbstractZODBConvertBase, self).tearDown()

    src_db_needs_closed_before_zodbconvert = True
    dest_db_needs_closed_before_zodbconvert = True

    def flush_changes_before_zodbconvert(self):
        for db_name in 'src_db', 'dest_db':
            db_attr = '_' + AbstractZODBConvertBase.__name__ + '__' + db_name
            db = getattr(self, db_attr)
            needs_closed = getattr(self, db_name + '_needs_closed_before_zodbconvert')

            if needs_closed and db is not None:
                db.close()
                setattr(self, db_attr, None)
                if db in self._to_close:
                    self._to_close.remove(db)


    def _closing(self, thing):
        self._to_close.append(thing)
        return thing

    def _create_src_storage(self):
        raise NotImplementedError()

    def _create_dest_storage(self):
        raise NotImplementedError()

    def _create_src_db(self):
        if self.__src_db is None:
            self.__src_db = self._closing(DB(self._create_src_storage()))
        return self.__src_db

    def _create_dest_db(self):
        if self.__dest_db is None:
            self.__dest_db = self._closing(DB(self._create_dest_storage()))
        return self.__dest_db

    @contextmanager
    def __conn(self, name):
        db = getattr(self, '_create_' + name + '_db')()
        conn = db.open()
        try:
            yield conn
        finally:
            conn.close()

    def _src_conn(self):
        return self.__conn('src')

    def _dest_conn(self):
        return self.__conn('dest')

    def __write_value_for_key_in_db(self, val, key, db_conn_func):
        with db_conn_func() as conn:
            conn.root()[key] = val
            transaction.commit()

    def __check_value_of_key_in_db(self, val, key, db_conn_func):
        with db_conn_func() as conn2:
            db_val = conn2.root().get(key)
            if isinstance(val, Blob):
                self.assertIsInstance(db_val, Blob)
                with val.open('r') as f:
                    val = f.read()
                with db_val.open('r') as f:
                    db_val = f.read()

            self.assertEqual(db_val, val)

    def _write_value_for_key_in_src(self, x, key='x'):
        self.__write_value_for_key_in_db(x, key, self._src_conn)

    def _write_value_for_key_in_dest(self, x, key='x'):
        self.__write_value_for_key_in_db(x, key, self._dest_conn)

    def _check_value_of_key_in_dest(self, x, key='x'):
        self.__check_value_of_key_in_db(x, key, self._dest_conn)

    def _check_value_of_key_in_src(self, x, key='x'):
        self.__check_value_of_key_in_db(x, key, self._src_conn)

    def run_zodbconvert(self, *args):
        self.flush_changes_before_zodbconvert()
        return main(*args)

    def __flatten_db_iternext(self, conn):
        storage = conn._storage
        if not hasattr(storage, 'record_iternext'):
            # MVCCAdapter
            storage = storage._storage

        # [(oid, tid, state)]
        result = []
        cookie = None
        while 1:
            oid, tid, state, cookie = storage.record_iternext(cookie)
            result.append((oid, tid, state))
            if cookie is None:
                break
        # Iteration order is not guaranteed, although at this writing
        # both FileStorage and RelStorage iterate by OID
        result.sort()
        return result


    def deep_compare_current_states(self):
        with self._src_conn() as c:
            src_state = self.__flatten_db_iternext(c)
        with self._dest_conn() as c:
            dest_state = self.__flatten_db_iternext(c)
        # The should both have something
        self.assertTrue(src_state)
        self.assertTrue(dest_state)
        # And they should all be equal
        self.assertEqual(src_state, dest_state)


    def test_convert(self):
        self._write_value_for_key_in_src(10)
        self.run_zodbconvert(['', self.cfgfile])
        self._check_value_of_key_in_dest(10)
        self.deep_compare_current_states()

    def test_dry_run(self):
        self._write_value_for_key_in_src(10)
        self.run_zodbconvert(['', '--dry-run', self.cfgfile])
        self._check_value_of_key_in_dest(None)

    def test_incremental_after_full_copy(self, first_convert_args=('',)):
        from persistent.mapping import PersistentMapping
        # Make sure to write new OIDs as well as new TIDs.
        for i in range(9):
            # Make sure to write new OIDs as well as new TIDs.
            self._write_value_for_key_in_src(PersistentMapping(i=i), key=i)
        self._write_value_for_key_in_src(Blob(b'abc'), 'the_blob')
        self._write_value_for_key_in_src(Blob(b'def'), 'the_blob')
        self._write_value_for_key_in_src(10)
        self._check_value_of_key_in_src(PersistentMapping(i=2), 2)

        self.run_zodbconvert(first_convert_args + (self.cfgfile,))

        self._check_value_of_key_in_dest(10)
        self._check_value_of_key_in_dest(PersistentMapping(i=2), 2)
        self._check_value_of_key_in_dest(Blob(b'def'), 'the_blob')
        self.deep_compare_current_states()

        for i in reversed(range(9)):
            # Make sure to write new OIDs as well as new TIDs,
            # But this time in *reverse* of the order we wrote them before.
            self._write_value_for_key_in_src(PersistentMapping(i=i**2), key=i)
        self._write_value_for_key_in_src(Blob(b'123'), 'the_blob')
        self._check_value_of_key_in_src(PersistentMapping(i=4), 2)
        self._write_value_for_key_in_src("hi")

        self.run_zodbconvert(['', '--incremental', self.cfgfile])

        self._check_value_of_key_in_dest("hi")
        self._check_value_of_key_in_dest(PersistentMapping(i=4), 2)
        self._check_value_of_key_in_dest(Blob(b'123'), 'the_blob')
        self.deep_compare_current_states()

    def test_incremental_after_incremental(self):
        # In case they do the conversion in different orders
        # depending on whether --incremental is specified or not.
        self.test_incremental_after_full_copy(first_convert_args=('', '--incremental'))

    def test_incremental_empty_src_dest(self):
        # Should work and not raise a POSKeyError
        self.run_zodbconvert(['', '--incremental', self.cfgfile])
        self._check_value_of_key_in_dest(None)

    @skipIfZapNotSupportedByDest
    def test_clear_empty_dest(self):
        x = 10
        self._write_value_for_key_in_src(x)
        self.run_zodbconvert(['', '--clear', self.cfgfile])
        self._check_value_of_key_in_dest(x)

    @skipIfZapNotSupportedByDest
    def test_clear_full_dest(self):
        self._write_value_for_key_in_dest(999)
        self._write_value_for_key_in_dest(666, key='y')
        self._write_value_for_key_in_dest(8675309, key='z')

        self._write_value_for_key_in_src(1, key='x')
        self._write_value_for_key_in_src(2, key='y')
        # omit z

        self.run_zodbconvert(['', '--clear', self.cfgfile])

        self._check_value_of_key_in_dest(1, key='x')
        self._check_value_of_key_in_dest(2, key='y')
        self._check_value_of_key_in_dest(None, key='z')

    def test_no_overwrite(self):
        self._create_src_db() # create the root object
        self._create_dest_db() # create the root object

        with self.assertRaises(SystemExit) as exc:
            self.run_zodbconvert(['', self.cfgfile])

        self.assertIn('Try --clear', str(exc.exception))

    # Also:
    # - when the destination keeps history: Verifying iteration
    #   of all the records, including transaction metadata
    # - verifying multiple states are saved when both keeps history
    # - If destination doesn't keep history, verifying only the most recent state# is saved.
    #
    # Some of that is probably handled in the History[Free|Preserving][From|To]FileStorageTest,
    # but I think that directly uses copyTransactionsFrom and doesn't have the other handling
    # zodbconvert does (e.g., incremental)


class FSZODBConvertTests(AbstractZODBConvertBase):
    keep_history = True

    def setUp(self):
        super(FSZODBConvertTests, self).setUp()

        fd, self.srcfile = tempfile.mkstemp('.fssource')
        os.close(fd)
        os.remove(self.srcfile)
        self.src_blobs = tempfile.mkdtemp('.fssource')

        fd, self.destfile = tempfile.mkstemp('.fsdest')
        os.close(fd)
        os.remove(self.destfile)
        self.dest_blobs = tempfile.mkdtemp('.fsdest')

        cfg = self._cfg_header() + self._cfg_source() + self._cfg_dest()
        self.cfgfile = self._write_cfg(cfg)

    def _cfg_header(self):
        return ""

    def _cfg_filestorage(self, name, path, blob_dir):
        return dedent("""
        <filestorage %s>
            path %s
            blob-dir %s
        </filestorage>
        """ % (name, path, blob_dir))

    def _cfg_one(self, name, path, blob_dir):
        return self._cfg_filestorage(name, path, blob_dir)

    def _cfg_source(self):
        return self._cfg_one('source', self.srcfile, self.src_blobs)

    def _cfg_dest(self):
        return self._cfg_one('destination', self.destfile, self.dest_blobs)

    def _write_cfg(self, cfg):
        fd, cfgfile = tempfile.mkstemp('.conf', 'zodbconvert-')
        os.write(fd, cfg.encode('ascii'))
        os.close(fd)
        return cfgfile

    def tearDown(self):
        for fname in self.destfile, self.srcfile, self.cfgfile:
            if os.path.exists(fname):
                os.remove(fname)
        self.destfile = self.srcfile = self.cfgfile = None
        for dname in self.src_blobs, self.dest_blobs:
            if os.path.exists(dname):
                rmtree(dname)

        super(FSZODBConvertTests, self).tearDown()

    def _load_zconfig(self):
        from relstorage.zodbconvert import schema_xml
        from relstorage.zodbconvert import StringIO
        import ZConfig

        schema = ZConfig.loadSchemaFile(StringIO(schema_xml))
        conf, _ = ZConfig.loadConfig(schema, self.cfgfile)
        return conf

    def _create_src_storage(self):
        conf = self._load_zconfig()
        return conf.source.open()

    def _create_dest_storage(self):
        conf = self._load_zconfig()
        return conf.destination.open()

    def test_storage_has_data(self):
        from relstorage.zodbconvert import storage_has_data
        src = FileStorage(self.srcfile, create=True)
        self.assertFalse(storage_has_data(src))
        db = DB(src)  # add the root object
        db.close()
        self.assertTrue(storage_has_data(src))

class ZlibWrappedFSZODBConvertTests(FSZODBConvertTests):

    # XXX: Add tests for:
    # - verifying that a state becomes compressed or uncompressed.
    #
    def _cfg_header(self):
        return "%import zc.zlibstorage\n"

    def _cfg_source(self):
        return ("\n<zlibstorage source>"
                + super(ZlibWrappedFSZODBConvertTests, self)._cfg_source()
                + "</zlibstorage>")

    def _cfg_dest(self):
        return ("\n<zlibstorage destination>"
                + super(ZlibWrappedFSZODBConvertTests, self)._cfg_dest()
                + "</zlibstorage>")

def test_suite():
    suite = unittest.TestSuite()
    suite.addTest(unittest.makeSuite(FSZODBConvertTests))
    suite.addTest(unittest.makeSuite(ZlibWrappedFSZODBConvertTests))
    return suite

if __name__ == '__main__':
    unittest.main(defaultTest='test_suite')
