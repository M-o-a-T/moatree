# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function, division, unicode_literals
##
##  This file is part of etcTree, a dynamic and Pythonic view of
##  whatever information you tend to store in etcd.
##
##  etcTree is Copyright © 2015 by Matthias Urlichs <matthias@urlichs.de>,
##  it is licensed under the GPLv3. See the file `README.rst` for details,
##  including optimistic statements by the author.
##
##  This program is free software: you can redistribute it and/or modify
##  it under the terms of the GNU General Public License as published by
##  the Free Software Foundation, either version 3 of the License, or
##  (at your option) any later version.
##
##  This program is distributed in the hope that it will be useful,
##  but WITHOUT ANY WARRANTY; without even the implied warranty of
##  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
##  GNU General Public License (included; see the file LICENSE)
##  for more details.
##
##  This header is auto-generated and may self-destruct at any time,
##  courtesy of "make update". The original is in ‘scripts/_boilerplate.py’.
##  Thus, do not remove the next line, or insert any blank lines above.
##
import logging
logger = logging.getLogger(__name__)
##BP

"""\
This is the etcd interface.
"""

import aioetcd as etcd
from aioetcd.client import Client
import asyncio
import weakref
import inspect

from .node import mtRoot

class _NOTGIVEN: pass

class EtcClient(object):
	last_mod = None
	def __init__(self, root="", loop=None, **args):
		assert (root == '' or root[0] == '/')
		self.root = root
		self.args = args
		self._loop = loop if loop is not None else asyncio.get_event_loop()
		self.client = Client(loop=loop, **args)
		self.watched = weakref.WeakValueDictionary()
	
	@asyncio.coroutine
	def _init(self):
		if self.last_mod is not None: # pragma: no cover
			return
		try:
			self.last_mod = (yield from self.client.read(self.root)).etcd_index
		except etcd.EtcdKeyNotFound:
			self.last_mod = (yield from self.client.write(self.root, value=None, dir=True)).etcd_index

	def __del__(self):
		self._kill()

	def _kill(self):
		try: del self.client
		except AttributeError: pass
		
	def close(self):
		self.client.close()
		self._kill()

	def _extkey(self, key):
		key = str(key)
		assert (key == '' or key[0] == '/')
		return self.root+key

	def get(self, key, **kw):
		return self.client.get(self._extkey(key), **kw)
	get._is_coroutine = True

	def read(self, key, **kw):
		return self.client.read(self._extkey(key), **kw)
	read._is_coroutine = True
	
	@asyncio.coroutine
	def delete(self, key, prev=_NOTGIVEN, index=None, **kw):
		"""\
			Delete a value.

			@recursive: delete a whole tree.

			@index: current mod stamp

			@prev: current value
			"""
		if prev is not _NOTGIVEN:
			kw['prevValue'] = prev
		if index is not None:
			kw['prevIndex'] = index
		res = yield from self.client.delete(self._extkey(key), **kw)
		self.last_mod = res.modifiedIndex
		return res
	
	@asyncio.coroutine
	def set(self, key, value, prev=_NOTGIVEN, index=None, **kw):
		"""\
			Either create or update a value.

			@key: the object path.

			@ttl: time-to-live in seconds.

			@append=True: generate a new guaranteed-unique and sequential entry.

			@dir=True: generate a directory entry

			"""
		key = self._extkey(key)
		logger.debug("Write %s to %s prev=%s index=%s %s",value,key, prev,index, repr(kw))
		if prev is _NOTGIVEN and index is None:
			kw['prevExist'] = False
		elif not kw.get('append',False):
			kw['prevExist'] = True
			if index is not None:
				kw['prevIndex'] = index
			if prev not in (None,_NOTGIVEN):
				kw['prevValue'] = prev

		res = yield from self.client.write(key, value=value, **kw)
		self.last_mod = res.modifiedIndex
		logger.debug("WROTE: %s",repr(res.__dict__))
		return res

	@asyncio.coroutine
	def tree(self, key, types=None, immediate=True, static=False, create=None):
		"""\
			Generate an object tree, populate it, and update it.
			if @create is True, create the directory node.

			If @immediate is set, run a recursive query and grab everything now.
			Otherwise fill the tree in the background.
			@static=True turns off the tree's auto-update.

			*Warning*: If you update the tree by direct assignment, you
			*must* call its `_wait()` coroutine in order to process them.
			The tree may or may not contain your updates before you do
			that.
			"""

		assert key[0] == '/'

		if not static:
			res = self.watched.get(key,None)
			if res is not None:
				return res
			
		try:
			res = yield from self.client.read(self._extkey(key), recursive=immediate)
		except etcd.EtcdKeyNotFound:
			if create is False:
				raise
			res = yield from self.client.write(self._extkey(key), prevExist=False, dir=True, value=None)
		else:
			if create is True:
				raise etcd.EtcdAlreadyExist(self._extkey(key))
		w = None if static else EtcWatcher(self,key,res.etcd_index)
		cls = None
		if types:
			cls = types.type[True]
		if cls is None:
			cls = mtRoot
		else:
			assert issubclass(cls,mtRoot)
		root = cls(conn=self, watcher=w, name=None, seq=res.modifiedIndex, cseq=res.createdIndex, types=types,
			ttl=res.ttl if hasattr(res,'ttl') else None)

		if immediate:
			def d_add(tree, node):
				for t in tree:
					n = t['key']
					n = n[n.rindex('/')+1:]
					if t.get('dir',False):
						sd = node._ext_lookup(n, dir=True, cseq=t['createdIndex'], seq=t['modifiedIndex'],
							ttl=res.ttl if hasattr(res,'ttl') else None)
						d_add(t.get('nodes',()),sd)
					else:
						node._ext_lookup(n, dir=False, value=t['value'], cseq=t['createdIndex'], seq=t['modifiedIndex'],
							ttl=t['ttl'] if 'ttl' in t else None)
				node._updated('populate')
			d_add(res._children,root)
		else:
			@asyncio.coroutine
			def d_get(node, res):
				for c in res.children:
					if c is res:
						continue # pragma: no cover
					n = c.key
					n = n[n.rindex('/')+1:]
					if c.dir:
						sd = node._ext_lookup(n,dir=True, cseq=res.createdIndex, seq=res.modifiedIndex,
							ttl=res.ttl if hasattr(res,'ttl') else None)
						data = yield from self.client.read(c.key)
						yield from d_get(sd, data)
					else:
						node._ext_lookup(n,dir=False, value=c.value, cseq=res.createdIndex, seq=res.modifiedIndex,
							ttl=res.ttl if hasattr(res,'ttl') else None)
				node._updated('populate')
			yield from d_get(root, res)

		if w is not None:
			w._set_root(root)
			self.watched[key] = root
		return root
		
class EtcWatcher(object):
	"""\
		Runs a watcher on a (sub)tree.

		@conn: the EtcClient to monitor.
		@key: the path to monitor, relative to conn.
		@seq: etcd_index to start monitoring from.
		"""
	_reader = None
	def __init__(self, conn,key,seq, types=None):
		self.conn = conn
		self.key = key
		self.extkey = self.conn._extkey(key)
		self.last_read = seq
		self.last_seen = seq

		self.uptodate = asyncio.Condition(loop=conn._loop)
		self._reader = asyncio.ensure_future(self._watch_read(), loop=conn._loop)

	def __del__(self): # pragma: no cover
		self._kill()

	def _kill(self): # pragma: no cover
		"""Tear down everything"""
		#logger.warning("_KILL")
		r,self._reader = self._reader,None
		if r is not None:
			r.cancel()
			r = None
		
	@asyncio.coroutine
	def close(self):
		r,self._reader = self._reader,None
		if r is not None:
			r.cancel()
			try:
				yield from r
			except asyncio.CancelledError:
				pass
		self._kill()
		
	def _set_root(self, root):
		self.root = weakref.ref(root)
		
	@asyncio.coroutine
	def sync(self, mod=None):
		"""Wait for pending updates"""
		if mod is None or mod < self.conn.last_mod:
			mod = self.conn.last_mod
		logger.debug("Syncing, wait for %d",mod)
		try:
			yield from self.uptodate.acquire()
			while self._reader is not None and self.last_seen < mod:
				yield from self.uptodate.wait() # pragma: no cover
				                                # processing got done during .acquire()
		finally:
			self.uptodate.release()
		logger.debug("Syncing, done, at %d",self.last_seen)

	@asyncio.coroutine
	def _watch_read(self): # pragma: no cover
		"""\
			Task which reads from etcd and processes the events received.
			"""
		logger.info("READER started")
		conn = Client(loop=self.conn._loop, **self.conn.args)
		key = self.extkey
		try:
			while True:
				@asyncio.coroutine
				def cb(x):
					logger.debug("IN: %s",repr(x.__dict__))
					try:
						yield from self._watch_write(x)
					except Exception as e:
						import traceback
						traceback.print_exc()
						logger.fatal("Error in write watcher")
						# XXX TODO trigger a major error
						self.conn._kill()
						raise
					self.last_read = x.modifiedIndex

				yield from conn.eternal_watch(key, index=self.last_read+1, recursive=True, callback=cb)

		except GeneratorExit:
			raise
		except asyncio.CancelledError:
			logger.info("READER cancelled")
		except BaseException as e:
			logger.exception("READER died")
			raise
		else:
			logger.info("READER ended")

	async def _watch_write(self, x):
		"""\
			Callback which processes incoming events
			"""
		# Drop references so that termination works
		r = self.root()
		if r is None: # pragma: no cover
			raise etcd.StopWatching

		logger.debug("RUN: %s",repr(x.__dict__))
		assert x.key.startswith(self.extkey), (x.key,self.key, x.modifiedIndex)
		key = x.key[len(self.extkey):]
		key = tuple(k for k in key.split('/') if k != '')
		if x.action in {'compareAndDelete','delete','expire'}:
			try:
				for n,k in enumerate(key):
					r = r._ext_lookup(k)
					if r is None: # pragma: no cover
						break
				else:
					r._ext_delete()
			except (KeyError,AttributeError): # pragma: no cover
				pass
		else:
			for n,k in enumerate(key):
				r = r._ext_lookup(k, dir= True if x.dir else n<len(key)-1, value= None if x.dir or n<len(key)-1 else x.value)
				if r is None:
					break # pragma: no cover
			else:
				kw = {}
				if hasattr(x,'ttl'): # pragma: no branch
					kw['ttl'] = x.ttl
				pn = getattr(x,'_prev_node',None)
				cseq=pn.createdIndex if pn is not None else x.createdIndex
				r._ext_update(x.value, cseq=cseq, seq=x.modifiedIndex, **kw)
				r._cseq = x.createdIndex

		await self.uptodate.acquire()
		try:
			self.last_seen = x.modifiedIndex
			self.uptodate.notify_all()
			logger.debug("DONE %d",x.modifiedIndex)
		finally:
			self.uptodate.release()

class EtcTypes(object):

	def __init__(self):
		self.type = [None,None]
		self.nodes = {}
	
	def __repr__(self): # pragma: no cover
		return "<%s:%s>" % (self.__class__.__name__,repr(self.type))

	def step(self,key):
		"""Lookup with auto-generation of new nodes"""
		res = self.nodes.get(key,None)
		if res is None:
			self.nodes[key] = res = EtcTypes()
		return res
	
	def items(self,key):
		"""\
			Enumerate sub-entries matching this key.
			Yields (name,sub-entry) tuples.
			Note that a name of "**" is supposed to match a whole subtree,
			so the matching algorithm in .lookup() carries it over.
			"""
		res = self.nodes.get(key,None)
		if res is not None:
			yield key,res
		res = self.nodes.get('*',None)
		if res is not None:
			yield '*',res
		res = self.nodes.get('**',None)
		if res is not None:
			yield '**',res
		
	def __getitem__(self,path):
		"""Shortcut to directly lookup a non-directory node"""
		if path[0] == "/":
			path = tuple(p for p in path.split('/') if p != '')
		else:
			path = path.split(".")
		for p in path:
			self = self.step(p)
		return self.type[0]

	def __setitem__(self,path,value):
		"""Shortcut to register a non-directory node"""
		if path[0] == "/":
			path = tuple(p for p in path.split('/') if p != '')
		else:
			path = path.split(".")
		for p in path:
			self = self.step(p)
		self._register(False)(value)

	def register(self, *path, cls=None, dir=None):
		"""\
			Teach this node that a sub-node named @name is to be of type @sub.
			"""
		if len(path) == 1:
			path = path[0]
			if path[0] == "/":
				path = tuple(p for p in path.split('/') if p != '')
			else:
				path = path.split(".")
		for p in path:
			self = self.step(p)
		if cls is None:
			return self._register(dir)
		else:
			return self._register(dir)(cls)

	def _register(self, dir):
		def reg(cls):
			"""Register a callback on this node"""
			if dir is not True:
				assert self.type[0] is None
				self.type[0] = cls
			if dir is not False:
				assert self.type[1] is None
				self.type[1] = cls
			return cls
		return reg
	
	def lookup(self, path, dir):
		"""\
			Find the node type that's to be associated with a path below me.

			This is called on the root node.
			"""
		nodes = [(".",self)]
		for p in path:
			cn = []
			for k,n in nodes:
				for nk,nn in n.items(p):
					cn.append((nk,nn))
				if k == '**':
					cn.append((k,n))
			if not cn:
				return None
			nodes = cn
		for p,n in nodes:
			t = n.type[dir]
			if t is not None:
				return t
		return None

